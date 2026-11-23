import os
import re
import json
import PIL
import unicodedata
from PIL import Image
import base64
import zipfile
import tempfile
from io import BytesIO
from zoneinfo import ZoneInfo
from datetime import datetime
from collections import defaultdict
from os.path import join, splitext, split

import numpy as np
import qrcode
import streamlit as st
from streamlit import session_state
from streamlit.components.v1 import html

import video_editing
from config import (
    DEFAULT_BPM,
    DEFAULT_DURATION,
    VIDEO_NAME_FORMAT,
    NB_KEYS_PER_AUDIO_TRACK,
    DUPLICATE_VIDEO_DOWNLOAD_KEY,
)
from s3_utils import upload_file_to_bucket, create_s3_key_url

# Valeurs par defaut de la musique (remplacent celles de config.py)
DEFAULT_BPM = 300.0       # rythme : 300 BPM = 5 images par seconde
DEFAULT_DURATION = 20.0   # duree de la video en secondes


# Position du gros bouton de prise de photo (fixe a l'ecran, pour pouvoir
# appuyer depuis le cote sans etre dans le champ de la camera).
BUTTON_POSITIONS = {
    "Bas gauche": "bottom: 24px; left: 24px;",
    "Haut gauche": "top: 72px; left: 24px;",
    "Bas droite": "bottom: 24px; right: 24px;",
    "Haut droite": "top: 72px; right: 24px;",
}
DEFAULT_BUTTON_POSITION = "Bas gauche"


def main():
    try:
        st.set_page_config(page_title="Kaléidoscope", layout="wide")
    except Exception:
        # Deja fait pendant cette session : sans importance, la page reste en pleine largeur
        pass
    if "tempdir" not in session_state:
        setup_session_state()
        restore_state()
    ensure_state()
    camera_big = session_state.get("fullscreen_cam", True)
    if not camera_big:
        st.title("Video Clip Creator")
    settings_section()
    # Une seule page : camera, photos, video, historique
    photo_section()
    video_section()
    display_history()
    new_session_section()
    library_section()
    save_state()
    st.caption("Version du 23/11/2026 — 22h15")
    # Espace en bas de page : le gros bouton fixe ne masque pas le contenu
    st.markdown('<div style="height: 180px"></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Memoire de la soiree : survit a un rechargement de la page / a une sortie par erreur
# ---------------------------------------------------------------------------
STATE_DIR = join(tempfile.gettempdir(), "kaleido_etat")
STATE_FILE = join(STATE_DIR, "etat.json")
STATE_S3_KEY = "etat/courante.json"


class PersistentDir:
    """Dossier des photos : contrairement a un dossier temporaire de session, il survit au rechargement de la page."""

    def __init__(self):
        self.name = join(STATE_DIR, "photos")
        os.makedirs(self.name, exist_ok=True)
        try:  # menage : fichiers de plus de 3 jours
            limit = datetime.now().timestamp() - 3 * 86400
            for fname in os.listdir(self.name):
                path = join(self.name, fname)
                if os.path.isfile(path) and os.path.getmtime(path) < limit:
                    os.remove(path)
        except OSError:
            pass


def state_snapshot() -> dict:
    return {
        "event_name": session_state.get("event_name", ""),
        "history": session_state.get("history", []),
        "pending_entry": session_state.get("pending_entry"),
        "video_counter": session_state.get("video_counter", 0),
        "photo_counter": session_state.get("photo_counter", 0),
        "series_key": session_state.get("series_key", 0),
        "image_paths": [p for p in session_state.get("image_paths", []) if os.path.exists(p)],
        "unchecked": sorted(session_state.get("unchecked", set())),
        "clips": [c for c in session_state.get("clips", []) if os.path.exists(c.get("path", ""))],
        "zipped_clips_s3_url": session_state.get("zipped_clips_s3_url"),
        "save_photos": bool(session_state.get("save_photos_cb", False)),
    }


def load_remote_state():
    """(donnees, ok) : etat de la soiree sauvegarde sur Scaleway (historique seulement). ok=False si injoignable."""
    try:
        body = music_client().get_object(Bucket=st.secrets["BUCKET_NAME"], Key=STATE_S3_KEY)["Body"].read()
        return json.loads(body.decode("utf-8")), True
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        return None, code in ("NoSuchKey", "404")


def restore_state():
    """Au demarrage d'une session navigateur : retrouve l'etat enregistre (disque du serveur, sinon Scaleway)."""
    data, remote_ok = None, False
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        remote_ok = True
    except Exception:
        data = None
    if data is None:
        data, remote_ok = load_remote_state()
        for entry in (data or {}).get("history", []):
            thumb_b64 = entry.pop("thumb_b64", None)
            entry["thumb"] = None
            if thumb_b64:
                try:
                    path = join(session_state.tempdir.name, f"history_restaure_{entry.get('number', 0)}.jpg")
                    with open(path, "wb") as fh:
                        fh.write(base64.b64decode(thumb_b64))
                    entry["thumb"] = path
                except Exception:
                    pass
    session_state["_remote_ok"] = remote_ok
    if not data:
        return
    session_state.history = data.get("history", [])
    session_state.pending_entry = data.get("pending_entry")
    session_state.video_counter = int(data.get("video_counter", 0))
    session_state.photo_counter = int(data.get("photo_counter", 0))
    session_state.series_key = int(data.get("series_key", 0))
    session_state.image_paths = [p for p in data.get("image_paths", []) if os.path.exists(p)]
    session_state.unchecked = set(data.get("unchecked", []))
    session_state.clips = [c for c in data.get("clips", []) if os.path.exists(c.get("path", ""))]
    session_state.zipped_clips_s3_url = data.get("zipped_clips_s3_url")
    if data.get("save_photos") and "save_photos_cb" not in session_state:
        session_state.save_photos_cb = True
    name = data.get("event_name", "")
    try:
        from_url = st.query_params.get("evenement", "")
    except Exception:
        from_url = ""
    if name and not from_url and "event_name_input" not in session_state:
        session_state.event_name_input = name


def push_remote_state(snap: dict):
    """Copie l'historique sur Scaleway (en arriere-plan) : il survit meme a un redemarrage du serveur."""
    import threading

    try:
        history = []
        for entry in snap["history"]:
            copy = dict(entry)
            thumb = copy.pop("thumb", None)
            if thumb and os.path.exists(thumb):
                with open(thumb, "rb") as fh:
                    copy["thumb_b64"] = base64.b64encode(fh.read()).decode("ascii")
            history.append(copy)
        payload = json.dumps(
            {"event_name": snap["event_name"], "history": history, "video_counter": snap["video_counter"],
             "photo_counter": snap["photo_counter"], "save_photos": snap["save_photos"]},
            ensure_ascii=False,
        ).encode("utf-8")
        client, bucket = music_client(), st.secrets["BUCKET_NAME"]
    except Exception:
        return

    def worker():
        try:
            client.put_object(Bucket=bucket, Key=STATE_S3_KEY, Body=payload, ContentType="application/json")
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def save_state():
    """Fin de chaque affichage : enregistre l'etat sur le disque du serveur (+ l'historique sur Scaleway si change)."""
    try:
        snap = state_snapshot()
        text = json.dumps(snap, ensure_ascii=False)
        if text != session_state.get("_saved_text"):
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, STATE_FILE)
            session_state["_saved_text"] = text
        shared = json.dumps(
            [snap["event_name"], snap["history"], snap["video_counter"]], sort_keys=True, ensure_ascii=False
        )
        # Jamais d'ecrasement de la copie Scaleway tant qu'on n'a pas pu la lire (reseau coupe au demarrage)
        if session_state.get("_remote_ok") and shared != session_state.get("_saved_remote"):
            session_state["_saved_remote"] = shared
            push_remote_state(snap)
    except Exception:
        pass


def start_new_session():
    """Repart de zero : historique, photos et video affichees sont vides (les videos restent sur Scaleway)."""
    for clip in session_state.get("clips", []):
        try:
            os.remove(clip["path"])
        except OSError:
            pass
    for path in session_state.get("image_paths", []):
        try:
            os.remove(path)
        except OSError:
            pass
    for entry in session_state.get("history", []) + ([session_state.pending_entry] if session_state.get("pending_entry") else []):
        try:
            if entry.get("thumb"):
                os.remove(entry["thumb"])
        except OSError:
            pass
    session_state.history = []
    session_state.pending_entry = None
    session_state.clips = []
    session_state.zipped_clips_s3_url = None
    session_state.image_paths = []
    session_state.selected_image_paths = []
    session_state.unchecked = set()
    session_state.series_key += 1
    session_state.video_counter = 0
    session_state.photos_zip = None
    session_state.new_session_cb = False


def new_session_section():
    """Case a cocher (puis confirmation) pour demarrer une nouvelle session sans rien garder a l'ecran."""
    if not (session_state.history or session_state.image_paths or session_state.clips or session_state.get("pending_entry")):
        return
    st.divider()
    if st.checkbox("🆕 Commencer une nouvelle session (repartir sans aucune création)", key="new_session_cb"):
        st.warning(
            "Ceci vide l'historique de la soirée, les photos et la vidéo affichés ici. "
            "Rien n'est supprimé sur Scaleway : les liens et QR codes déjà donnés restent valables, "
            "et les vidéos se retrouvent dans « Retrouver mes séances »."
        )
        st.button("Oui, repartir de zéro", type="primary", on_click=start_new_session, key="new_session_go")


# ---------------------------------------------------------------------------
# Etat de la session
# ---------------------------------------------------------------------------
def setup_session_state():
    session_state.tempdir = PersistentDir()
    session_state.image_paths = []
    session_state.selected_image_paths = []
    session_state.mute_video = False
    session_state.auto_enhance_on = True
    session_state.extras = None
    session_state.save_photos = False
    session_state.filter_name = video_editing.FILTER_NONE
    session_state.keep_colors = []
    session_state.sat_min = video_editing.DEFAULT_SAT_MIN
    session_state.brightness = video_editing.DEFAULT_BRIGHTNESS
    session_state.contrast = video_editing.DEFAULT_CONTRAST
    session_state.temperature = video_editing.DEFAULT_TEMPERATURE
    session_state.tint = video_editing.DEFAULT_TINT
    session_state.camera_key = 0
    session_state.photo_added = False
    session_state.session_key = 0
    # list of dicts containing "file", "bpm" and "duration" keys/values
    session_state.audio_tracks = [mk_track_dict()]
    # List of dicts with "path" and "s3_url" keys/values
    session_state.clips = []
    session_state.zipped_clips_s3_url = None


def ensure_state():
    """Valeurs par defaut de l'etat ajoute par la version avec historique (sessions deja ouvertes comprises)."""
    defaults = {
        "event_name": lambda: "",
        "history": lambda: [],
        "video_counter": lambda: 0,
        "photo_counter": lambda: 0,
        "series_key": lambda: 0,
        "unchecked": lambda: set(),
        "camera_key": lambda: 0,
        "photo_added": lambda: False,
        "pending_entry": lambda: None,
    }
    for key, factory in defaults.items():
        if key not in session_state:
            session_state[key] = factory()


def mk_track_dict() -> defaultdict:
    return defaultdict(
            file=None,
            bpm=DEFAULT_BPM,
            duration=DEFAULT_DURATION
        )


def is_valid_image(data: bytes) -> bool:
    """Vrai si les octets forment une image lisible (evite les photos vides ou tronquees)."""
    if not data:
        return False
    try:
        with Image.open(BytesIO(data)) as im:
            im.load()
        return True
    except Exception:
        return False


def selected_paths() -> list:
    """Photos cochees (toutes par defaut). La selection est gardee dans l'etat de la session."""
    return [
        p for p in session_state.image_paths
        if p not in session_state.unchecked and os.path.exists(p) and os.path.getsize(p) > 0
    ]


# ---------------------------------------------------------------------------
# Reglages de la soiree (a faire une fois, puis on replie)
# ---------------------------------------------------------------------------
def settings_section():
    with st.expander("⚙️ Réglages de la soirée", expanded=True):
        # Le nom est garde dans l'adresse de la page (?evenement=...) : si la page se recharge ou plante,
        # le meme nom revient tout seul et les nouvelles videos retombent dans le meme dossier.
        if "event_name_input" not in session_state:
            try:
                session_state.event_name_input = st.query_params.get("evenement", "")
            except Exception:
                session_state.event_name_input = ""
        session_state.event_name = st.text_input(
            "Nom de l'événement (sert à nommer les vidéos)",
            key="event_name_input",
            placeholder="ex : Soirée EDF",
        )
        try:
            wanted = session_state.event_name.strip()
            if wanted and st.query_params.get("evenement", "") != wanted:
                st.query_params["evenement"] = wanted
            elif not wanted and "evenement" in st.query_params:
                del st.query_params["evenement"]
        except Exception:
            pass

        st.markdown("**Son**")
        # Une seule musique par evenement
        create_audio_track_inputs(0, session_state.audio_tracks[0])

        st.markdown("**Image**")
        session_state.auto_enhance_on = st.checkbox(
            "✨ Amélioration automatique (recommandé) : exposition, netteté, bruit, couleurs",
            value=True,
            key="auto_enhance_cb",
        )
        bright_col, contrast_col = st.columns(2)
        with bright_col:
            session_state.brightness = st.slider(
                "Luminosité", min_value=-50, max_value=50, value=video_editing.DEFAULT_BRIGHTNESS,
                step=5, key="brightness_slider",
            )
        with contrast_col:
            session_state.contrast = st.slider(
                "Contraste", min_value=-50, max_value=50, value=video_editing.DEFAULT_CONTRAST,
                step=5, key="contrast_slider",
            )
        temp_col, tint_col = st.columns(2)
        with temp_col:
            session_state.temperature = st.slider(
                "Température (− froid / + chaud)", min_value=-50, max_value=50,
                value=video_editing.DEFAULT_TEMPERATURE, step=5, key="temperature_slider",
            )
        with tint_col:
            session_state.tint = st.slider(
                "Teinte (− vert / + magenta)", min_value=-50, max_value=50,
                value=video_editing.DEFAULT_TINT, step=5, key="tint_slider",
            )
        looks_section()
        session_state.filter_name = st.selectbox("Filtre", video_editing.FILTER_NAMES, key="filter_select")
        if session_state.filter_name == video_editing.FILTER_BW_COLORS:
            st.caption("Couleurs à conserver (le reste passe en noir et blanc) :")
            color_names = list(video_editing.COLOR_HUES.keys())
            color_cols = st.columns(3)
            kept = []
            for i, color_name in enumerate(color_names):
                with color_cols[i % 3]:
                    if st.checkbox(
                        color_name,
                        value=color_name in video_editing.DEFAULT_KEEP_COLORS,
                        key=f"keep_color_{color_name}",
                    ):
                        kept.append(color_name)
            session_state.keep_colors = kept
            session_state.sat_min = st.slider(
                "Seuil de saturation (plus bas = garde davantage de couleurs)",
                min_value=0.10, max_value=0.90, value=video_editing.DEFAULT_SAT_MIN, step=0.05,
                key="sat_min_slider",
            )
        else:
            session_state.keep_colors = []
            session_state.sat_min = video_editing.DEFAULT_SAT_MIN

        overlay_section()

        # Poste de reglage : on choisit une photo de reference, on regle, et c'est applique a toute la serie
        st.markdown("**Aperçu des réglages** (appliqués à toutes les photos cochées)")
        paths = selected_paths()
        if paths:
            ref_idx = 0
            if len(paths) > 1:
                ref_idx = st.selectbox(
                    "Photo de référence",
                    list(range(len(paths))),
                    format_func=lambda i: f"Photo {i + 1} sur {len(paths)}",
                    key="ref_photo_idx",
                )
            ref_idx = min(ref_idx, len(paths) - 1)
            params = video_editing.enhance_params(paths) if session_state.auto_enhance_on else None
            before_col, after_col = st.columns(2)
            with before_col:
                st.image(square_thumb(paths[ref_idx], 700), caption="Avant", use_container_width=True)
            with after_col:
                try:
                    after = preview_image(paths[ref_idx], width=700, enhance=params, extras=session_state.extras)
                    st.image(after, caption="Après", use_container_width=True)
                except Exception:
                    st.caption("Aperçu indisponible pour cette photo.")
        else:
            st.caption("Prenez une photo pour voir l'aperçu des réglages.")

        # Option : couper le son de la video (le rythme et la duree restent cales sur l'audio)
        session_state.mute_video = st.checkbox("Couper le son de la vidéo", value=False, key="mute_checkbox")
        session_state.save_photos = st.checkbox(
            "💾 Sauvegarder aussi les photos sur Scaleway (en privé : un zip vidéos + photos par vidéo)",
            value=False,
            key="save_photos_cb",
        )

        st.markdown("**Caméra et bouton photo**")
        st.checkbox(
            "Caméra en grand (carré plein écran, sans bandes grises)",
            value=True,
            key="fullscreen_cam",
        )
        st.radio(
            "Caméra utilisée",
            ["Selfie (avant)", "Arrière"],
            horizontal=True,
            key="cam_facing",
            on_change=restart_camera,
        )
        zoom_off = not session_state.get("fullscreen_cam", True)
        st.slider(
            "Zoom caméra avant (selfie) — ×1 = image complète. Réduit le fond et la déformation "
            "des bords, mais la photo est moins nette.",
            min_value=1.0, max_value=2.0, value=1.0, step=0.1,
            key="cam_zoom_front", disabled=zoom_off,
        )
        st.slider(
            "Zoom caméra arrière",
            min_value=1.0, max_value=2.0, value=1.0, step=0.1,
            key="cam_zoom_rear", disabled=zoom_off,
        )
        st.selectbox(
            "Position du gros bouton de prise de photo",
            list(BUTTON_POSITIONS),
            index=list(BUTTON_POSITIONS).index(DEFAULT_BUTTON_POSITION),
            key="btn_pos",
        )



# ---------------------------------------------------------------------------
# Calque : logo / phrase du client (PNG transparent) pose sur le carre final de la video
# ---------------------------------------------------------------------------
OVERLAY_PREFIX = "calques/"
MAX_OVERLAY_MB = 8


def fetch_overlay_list():
    """{nom: cle} des calques PNG du bucket, ou None si Scaleway est injoignable."""
    try:
        client, bucket = music_client(), st.secrets["BUCKET_NAME"]
        found = {}
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=OVERLAY_PREFIX):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(OVERLAY_PREFIX):]
                if name.lower().endswith(".png"):
                    found[splitext(name)[0]] = obj["Key"]
        session_state.overlay_error = ""
        return dict(sorted(found.items()))
    except Exception as exc:
        session_state.overlay_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return None


@st.cache_data(show_spinner=False, max_entries=10)
def fetch_overlay_bytes(key: str) -> bytes:
    return music_client().get_object(Bucket=st.secrets["BUCKET_NAME"], Key=key)["Body"].read()


def overlay_preview(overlay_bytes: bytes):
    """Apercu du carre final : derniere photo en kaleidoscope (ou fond gris) + calque."""
    side = 300
    paths = selected_paths()
    if paths:
        with Image.open(paths[-1]) as im:
            im = im.convert("RGB")
            w, h = im.size
            m = min(w, h)
            im = im.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m)).resize((side, side))
            base = video_editing.make_tile(np.array(im))
    else:
        base = np.full((side * 2, side * 2, 3), 128, dtype=np.uint8)
    ov = video_editing.load_overlay(overlay_bytes, base.shape[0])
    return video_editing.blend_overlay(base, ov)


def overlay_section():
    """Choix du calque, ajout/suppression de calques PNG. Renseigne session_state.overlay_bytes."""
    st.markdown("**Calque : logo ou phrase du client**")
    if "overlay_list" not in session_state:
        session_state.overlay_list = fetch_overlay_list()
    saved = session_state.overlay_list or {}
    mine = {f"📁 {name}": key for name, key in saved.items()}
    options = ["Aucun"] + list(mine)
    choice = st.selectbox("Calque posé sur la vidéo (PNG transparent)", options, key="overlay_select")
    one_off = st.file_uploader(
        "…ou envoyer un PNG pour cette soirée seulement",
        type=["png"], key=f"overlay_oneoff_{session_state.get('overlay_oneoff_key', 0)}",
    )
    data = None
    if one_off is not None:
        data = one_off.getvalue()
    elif choice in mine:
        try:
            data = fetch_overlay_bytes(mine[choice])
        except Exception as exc:
            st.warning(f"Ce calque ne peut pas être lu : {type(exc).__name__}")
    if data is not None:
        try:
            with Image.open(BytesIO(data)) as im:
                im.load()
            st.image(overlay_preview(data), caption="Aperçu du carré final (la photo est celle de la dernière prise)", width=300)
        except Exception:
            st.warning("Ce fichier n'est pas une image PNG lisible : il sera ignoré.")
            data = None
    session_state.overlay_bytes = data
    st.caption(
        "Le calque est posé sur le carré final entier (pas sur chaque petit carré). "
        "Préparez un PNG carré à fond transparent ; un calque non carré est centré sans déformation."
    )

    if st.checkbox("➕ Ajouter ou supprimer mes calques", key="overlay_manage_toggle"):
        if session_state.overlay_list is None:
            st.caption(f"Liste de vos calques indisponible (connexion ou droits Scaleway ?) {session_state.get('overlay_error', '')}")
        files = st.file_uploader(
            "Fichiers PNG à enregistrer", type=["png"], accept_multiple_files=True,
            key=f"overlay_add_{session_state.get('overlay_add_key', 0)}",
        )
        if files and st.button("Enregistrer ces calques", key="overlay_save"):
            done, problems = 0, []
            for f in files:
                try:
                    if len(f.getvalue()) > MAX_OVERLAY_MB * 1024 * 1024:
                        raise ValueError(f"fichier trop lourd (plus de {MAX_OVERLAY_MB} Mo)")
                    with Image.open(BytesIO(f.getvalue())) as im:
                        im.load()
                    music_client().put_object(
                        Bucket=st.secrets["BUCKET_NAME"],
                        Key=f"{OVERLAY_PREFIX}{clean_file_name(splitext(f.name)[0])}.png",
                        Body=f.getvalue(),
                        ContentType="image/png",
                    )
                    done += 1
                except ValueError as exc:
                    problems.append(f"{f.name} : {exc}")
                except Exception as exc:
                    problems.append(f"{f.name} : enregistrement impossible ({type(exc).__name__})")
            fetch_overlay_bytes.clear()
            session_state.overlay_add_key = session_state.get("overlay_add_key", 0) + 1
            session_state.overlay_list = fetch_overlay_list()
            if done:
                st.toast(f"{done} calque(s) enregistré(s) ✓")
            if problems:
                st.error("Non enregistrés : " + " ; ".join(problems))
            else:
                st.rerun()
        if saved:
            victim = st.selectbox("Calque à supprimer", list(saved), key="overlay_del_choice")
            if st.button("Supprimer ce calque définitivement", key="overlay_delete"):
                try:
                    music_client().delete_object(Bucket=st.secrets["BUCKET_NAME"], Key=saved[victim])
                    fetch_overlay_bytes.clear()
                    session_state.overlay_list = fetch_overlay_list()
                    st.toast(f"« {victim} » supprimé ✓")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Suppression impossible : {type(exc).__name__}: {str(exc)[:160]}")


# ---------------------------------------------------------------------------
# Looks : ambiances de couleur (integres + fichiers .cube enregistres sur Scaleway)
# ---------------------------------------------------------------------------
LOOK_PREFIX = "looks/"


def fetch_look_list():
    """{nom: cle} des looks .cube du bucket, ou None si Scaleway est injoignable."""
    try:
        client, bucket = music_client(), st.secrets["BUCKET_NAME"]
        found = {}
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=LOOK_PREFIX):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(LOOK_PREFIX):]
                if name.lower().endswith(".cube"):
                    found[splitext(name)[0]] = obj["Key"]
        session_state.look_error = ""
        return dict(sorted(found.items()))
    except Exception as exc:
        session_state.look_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return None


@st.cache_resource(show_spinner=False, max_entries=20)
def load_custom_look(key: str):
    data = music_client().get_object(Bucket=st.secrets["BUCKET_NAME"], Key=key)["Body"].read()
    return video_editing.parse_cube(data)


def looks_section():
    """Choix du look, intensite, ombres, ajout/suppression de looks .cube. Renseigne session_state.extras."""
    if "look_list" not in session_state:
        session_state.look_list = fetch_look_list()
    custom = session_state.look_list or {}
    mine = {f"📁 {name}": key for name, key in custom.items()}
    options = [video_editing.LOOK_NONE] + video_editing.BUILTIN_LOOK_NAMES + list(mine)
    choice = st.selectbox("Look (ambiance de couleur)", options, key="look_select")
    strength = 100
    lut = None
    if choice != video_editing.LOOK_NONE:
        strength = st.slider("Intensité du look", min_value=0, max_value=100, value=100, step=5, key="look_strength")
        try:
            lut = load_custom_look(mine[choice]) if choice in mine else video_editing.builtin_look(choice)
        except Exception as exc:
            lut = None
            st.warning(f"Ce look ne peut pas être lu : {str(exc)[:120]}")
    shadows = st.slider(
        "Déboucher les ombres (utile si la lumière vient d'un seul côté)",
        min_value=0, max_value=100, value=0, step=5, key="shadows_slider",
    )
    skin = st.slider(
        "Lissage léger de la peau",
        min_value=0, max_value=100, value=0, step=5, key="skin_slider",
        help="Atténue les petits défauts dans les zones de teinte peau, en gardant yeux, lèvres et cheveux nets. "
             "Repéré par la couleur : des objets de teinte chair peuvent être légèrement lissés aussi.",
    )
    session_state.extras = {
        "look": lut, "look_strength": strength / 100.0, "shadows": float(shadows), "skin": float(skin),
    }
    session_state.extras_sig = (choice, strength, shadows, skin)

    if st.checkbox("➕ Ajouter ou supprimer mes looks (.cube)", key="look_manage_toggle"):
        st.caption(
            "Créez un look dans Photoshop, Lightroom ou DaVinci et exportez-le en LUT 3D (fichier .cube). "
            "Il s'appliquera à toutes les photos."
        )
        if session_state.look_list is None:
            st.caption(f"Liste de vos looks indisponible (connexion ou droits Scaleway ?) {session_state.get('look_error', '')}")
        files = st.file_uploader(
            "Fichiers .cube", type=["cube"], accept_multiple_files=True,
            key=f"look_add_{session_state.get('look_add_key', 0)}",
        )
        if files and st.button("Enregistrer ces looks", key="look_save"):
            saved, problems = 0, []
            for f in files:
                try:
                    video_editing.parse_cube(f.getvalue())  # on verifie avant d'enregistrer
                    music_client().put_object(
                        Bucket=st.secrets["BUCKET_NAME"],
                        Key=f"{LOOK_PREFIX}{clean_file_name(splitext(f.name)[0])}.cube",
                        Body=f.getvalue(),
                    )
                    saved += 1
                except ValueError as exc:
                    problems.append(f"{f.name} : {exc}")
                except Exception as exc:
                    problems.append(f"{f.name} : enregistrement impossible ({type(exc).__name__})")
            load_custom_look.clear()
            session_state.look_add_key = session_state.get("look_add_key", 0) + 1
            session_state.look_list = fetch_look_list()
            if saved:
                st.toast(f"{saved} look(s) enregistré(s) ✓")
            if problems:
                st.error("Non enregistrés : " + " ; ".join(problems))
            else:
                st.rerun()
        if custom:
            victim = st.selectbox("Look à supprimer", list(custom), key="look_del_choice")
            if st.button("Supprimer ce look définitivement", key="look_delete"):
                try:
                    music_client().delete_object(Bucket=st.secrets["BUCKET_NAME"], Key=custom[victim])
                    load_custom_look.clear()
                    session_state.look_list = fetch_look_list()
                    st.toast(f"« {victim} » supprimé ✓")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Suppression impossible : {type(exc).__name__}: {str(exc)[:160]}")


def preview_image(path: str, width: int = 600, enhance=None, extras=None):
    """Retourne une petite version de la photo avec les reglages et le filtre courants."""
    with Image.open(path) as im:
        small = im.convert("RGB")
        w, h = small.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        small = small.crop((left, top, left + side, top + side))  # carre : comme dans la video
        small.thumbnail((width, width))
    return video_editing.apply_filter(
        small,
        session_state.filter_name,
        session_state.keep_colors,
        session_state.sat_min,
        session_state.brightness,
        session_state.contrast,
        session_state.temperature,
        session_state.tint,
        enhance=enhance,
        extras=extras,
    )


# Musiques de l'evenement : rangees dans le bucket Scaleway (dossier "musiques/"), donc conservees
# d'une soiree a l'autre. Si Scaleway est injoignable ou la liste vide : envoi d'un fichier comme avant.
MUSIC_PREFIX = "musiques/"
MUSIC_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
}


@st.cache_resource
def music_client():
    """Client Scaleway a delais courts (la liste des musiques ne doit jamais bloquer l'application)."""
    import boto3
    from botocore.config import Config

    region = st.secrets["REGION"]
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=os.environ.get("KALEIDO_S3_ENDPOINT") or f"https://s3.{region}.scw.cloud",
        aws_access_key_id=st.secrets["SCW_ACCESS_KEY"],
        aws_secret_access_key=st.secrets["SCW_SECRET_KEY"],
        config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}),
    )


def backup_photos_async(paths: list, key_dir: str):
    """
    Envoie les photos d'origine sur Scaleway (en prive), dans le dossier de la video, SANS bloquer
    la creation de la video. Les photos deja envoyees ne sont pas renvoyees. Echec = silencieux.
    """
    import threading

    try:
        done = session_state.setdefault("backed_up_photos", set())
        todo = [p for p in paths if p not in done and os.path.exists(p)]
        if not todo:
            return
        client, bucket = music_client(), st.secrets["BUCKET_NAME"]
        done.update(todo)
    except Exception:
        return

    def worker():
        for path in todo:
            try:
                with open(path, "rb") as fh:
                    client.put_object(
                        Bucket=bucket,
                        Key=f"{key_dir}/photos/{os.path.basename(path)}",
                        Body=fh.read(),
                        ContentType="image/jpeg",
                    )
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()


def complete_zip_async(zip_name: str, key_dir: str, videos: list, photos: list):
    """
    Fabrique EN ARRIERE-PLAN un seul zip (videos + photos d'origine), le depose en prive sur Scaleway
    a cote de la seance, sans bloquer la creation de la video. Les photos ne partent qu'une fois (dans le zip).
    videos : [(nom, octets)], photos : [chemins]. Echec = silencieux.
    """
    import threading

    try:
        client, bucket = music_client(), st.secrets["BUCKET_NAME"]
    except Exception:
        return

    def worker():
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
                for name, data in videos:
                    zf.writestr(f"{zip_name}/{name}", data)
                for path in photos:
                    if os.path.exists(path):
                        zf.write(path, arcname=f"{zip_name}/photos/{os.path.basename(path)}")
            client.upload_file(tmp_path, bucket, f"{key_dir}/{zip_name}.zip")
        except Exception:
            pass
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    threading.Thread(target=worker, daemon=True).start()


def fetch_music_list():
    """{nom affiche: cle} des musiques du bucket (triees), ou None si Scaleway est injoignable."""
    try:
        client, bucket = music_client(), st.secrets["BUCKET_NAME"]
        found = {}
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=MUSIC_PREFIX):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(MUSIC_PREFIX):]
                if name and name.lower().endswith(MUSIC_EXTENSIONS):
                    found[splitext(name)[0]] = obj["Key"]
        session_state.music_error = ""
        return dict(sorted(found.items()))
    except Exception as exc:
        session_state.music_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        return None


def ensure_music_list(force: bool = False):
    if force or "music_list" not in session_state:
        session_state.music_list = fetch_music_list()


@st.cache_data(show_spinner=False, max_entries=30)
def download_music(key: str) -> bytes:
    return music_client().get_object(Bucket=st.secrets["BUCKET_NAME"], Key=key)["Body"].read()


def save_music_files(files) -> int:
    """Envoie les fichiers audio dans le bucket. Retourne le nombre de fichiers enregistres."""
    client, bucket = music_client(), st.secrets["BUCKET_NAME"]
    saved = 0
    for f in files:
        base, ext = splitext(f.name)
        ext = ext.lower()
        if ext not in MUSIC_EXTENSIONS:
            continue
        client.put_object(
            Bucket=bucket,
            Key=f"{MUSIC_PREFIX}{clean_file_name(base)}{ext}",
            Body=f.getvalue(),
            ContentType=AUDIO_MIME.get(ext, "application/octet-stream"),
        )
        saved += 1
    download_music.clear()
    return saved


def delete_music(key: str):
    """Supprime definitivement une musique du bucket."""
    music_client().delete_object(Bucket=st.secrets["BUCKET_NAME"], Key=key)
    download_music.clear()


class S3Audio:
    """Musique du bucket, utilisable comme un fichier envoye (memes .name et .getbuffer())."""

    def __init__(self, key: str):
        self.key = key
        self.name = os.path.basename(key)

    def getbuffer(self) -> bytes:
        return download_music(self.key)


def music_chooser(track: defaultdict, track_idx: int):
    """Liste de musiques a cocher + ajout de musiques. Renseigne track['file']."""
    ensure_music_list()
    music = session_state.music_list
    audio_types = [e.lstrip(".") for e in MUSIC_EXTENSIONS]
    if music:
        choice = st.radio("Musique de l'événement", list(music), key="music_choice")
        key = music[choice]
        try:
            st.audio(download_music(key), format=AUDIO_MIME.get(splitext(key)[1].lower(), "audio/mpeg"))
            track["file"] = S3Audio(key)
        except Exception:
            track["file"] = None
            st.warning("Impossible de charger cette musique (connexion ?). Réessayez.")
        st.caption(f"{len(music)} musique(s) dans la liste.")
    else:
        if music is None:
            st.caption("Liste des musiques indisponible (connexion ou droits Scaleway ?). Vous pouvez envoyer un fichier.")
            if session_state.get("music_error"):
                st.caption(f"Détail technique : {session_state.music_error}")
        else:
            st.caption("Aucune musique enregistrée. Ajoutez-en ci-dessous, ou envoyez un fichier pour cette fois.")
        track["file"] = st.file_uploader(
            "Fichier audio (la musique de l'événement)",
            type=audio_types,
            key=track_idx * NB_KEYS_PER_AUDIO_TRACK,
        )
    refresh_col, add_col = st.columns(2)
    with refresh_col:
        if st.button("↻ Actualiser la liste", key="music_refresh"):
            ensure_music_list(force=True)
            st.rerun()
    with add_col:
        add = st.checkbox("➕ Ajouter des musiques", key="music_add_toggle")
    if add:
        new_files = st.file_uploader(
            "Choisissez un ou plusieurs fichiers audio",
            type=audio_types,
            accept_multiple_files=True,
            key=f"music_add_{session_state.get('music_add_key', 0)}",
        )
        if new_files and st.button("Enregistrer dans la liste", key="music_save"):
            try:
                n = save_music_files(new_files)
                session_state.music_add_key = session_state.get("music_add_key", 0) + 1
                ensure_music_list(force=True)
                st.toast(f"{n} musique(s) enregistrée(s) ✓")
                st.rerun()
            except Exception as exc:
                st.error(f"Enregistrement impossible : {type(exc).__name__}: {str(exc)[:200]}")
    music_extras(track, track_idx, music)


def music_extras(track: defaultdict, track_idx: int, music):
    """Supprimer une musique de la liste, ou utiliser un fichier une seule fois sans l'enregistrer."""
    if not music:
        return
    audio_types = [e.lstrip(".") for e in MUSIC_EXTENSIONS]
    if st.checkbox("📎 Utiliser un fichier une seule fois (sans l'enregistrer)", key="music_oneoff_toggle"):
        once = st.file_uploader("Fichier audio", type=audio_types, key=track_idx * NB_KEYS_PER_AUDIO_TRACK)
        if once is not None:
            track["file"] = once
    if st.checkbox("🗑 Supprimer une musique de la liste", key="music_del_toggle"):
        victim = st.selectbox("Musique à supprimer", list(music), key="music_del_choice")
        st.caption("La musique sera supprimée définitivement de Scaleway.")
        if st.button("Supprimer définitivement", key="music_delete"):
            try:
                delete_music(music[victim])
                ensure_music_list(force=True)
                st.toast(f"« {victim} » supprimée ✓")
                st.rerun()
            except Exception as exc:
                st.error(f"Suppression impossible : {type(exc).__name__}: {str(exc)[:200]}")


MUSIC_SETTINGS_KEY = MUSIC_PREFIX + "_reglages.json"
BAR_CHOICES = [8, 16, 24, 32]


@st.cache_data(show_spinner=False, max_entries=40)
def detect_song_bpm(name: str, size: int, _data: bytes):
    """Tempo propose pour une musique (None si non detecte). Calcule une seule fois par musique."""
    with tempfile.NamedTemporaryFile(suffix=splitext(name)[1] or ".mp3") as fh:
        fh.write(_data)
        fh.flush()
        return video_editing.propose_song_bpm(video_editing.estimate_song_bpm(fh.name))


def load_music_settings(force: bool = False) -> dict:
    """{nom de fichier: {"bpm": ..., "mesures": ...}} memorise sur Scaleway (vide si indisponible)."""
    if force or "music_settings" not in session_state:
        try:
            body = music_client().get_object(Bucket=st.secrets["BUCKET_NAME"], Key=MUSIC_SETTINGS_KEY)["Body"].read()
            data = json.loads(body.decode("utf-8"))
            session_state.music_settings = data if isinstance(data, dict) else {}
        except Exception:
            session_state.music_settings = {}
    return session_state.music_settings


def save_music_setting(name: str, bpm: float, bars: int):
    settings = dict(load_music_settings(force=True))
    settings[name] = {"bpm": bpm, "mesures": bars}
    music_client().put_object(
        Bucket=st.secrets["BUCKET_NAME"],
        Key=MUSIC_SETTINGS_KEY,
        Body=json.dumps(settings, ensure_ascii=False, indent=1).encode("utf-8"),
        ContentType="application/json",
    )
    session_state.music_settings = settings


def tempo_keys(track_idx: int):
    base = track_idx * NB_KEYS_PER_AUDIO_TRACK
    return base + 1, base + 2, f"bars_{track_idx}"  # bpm, duree, mesures


def recompute_duration(track_idx: int):
    """Duree = nombre de mesures x duree d'une mesure. Le BPM saisi est le DOUBLE du tempo du morceau :
    une mesure (4 temps du morceau) = 8 images = 480 / BPM secondes."""
    bpm_key, dur_key, bars_key = tempo_keys(track_idx)
    try:
        session_state[dur_key] = round(session_state[bars_key] * 480.0 / session_state[bpm_key], 1)
    except Exception:
        pass


def propose_tempo(track: defaultdict, track_idx: int):
    """A chaque nouvelle musique : tempo memorise, sinon tempo detecte (x2). Les champs restent modifiables."""
    f = track.get("file")
    music_id = getattr(f, "name", None)
    bpm_key, dur_key, bars_key = tempo_keys(track_idx)
    last_key = f"last_music_{track_idx}"
    if not music_id or session_state.get(last_key) == music_id:
        return
    session_state[last_key] = music_id
    saved = load_music_settings().get(music_id)
    hint = ""
    if saved and saved.get("bpm"):
        bpm, bars = float(saved["bpm"]), int(saved.get("mesures", 16))
        hint = f"Réglages mémorisés pour cette musique : {bpm:g} BPM, {bars} mesures."
    else:
        bars, bpm = 16, None
        try:
            data = bytes(f.getbuffer())
            with st.spinner("Recherche du tempo de la musique…"):
                song = detect_song_bpm(music_id, len(data), data)
        except Exception:
            song = None
        if song:
            bpm = float(2 * song)
            hint = (f"Tempo détecté ≈ {song} BPM, proposé : {bpm:g} (le double). "
                    "Un tempo à moitié ou double est possible : vérifiez à l'oreille et corrigez si besoin.")
        else:
            hint = "Tempo non détecté : réglez le BPM à la main."
    if bars not in BAR_CHOICES:
        bars = 16
    session_state[bars_key] = bars
    if bpm:
        session_state[bpm_key] = bpm
        session_state[dur_key] = round(bars * 480.0 / bpm, 1)
    session_state[f"tempo_hint_{track_idx}"] = hint


def create_audio_track_inputs(track_idx: int, track: defaultdict):
    spec = [3, 1, 1, 1, 1] if track_idx else [3, 1, 1, 1]
    cols = st.columns(spec)
    with cols[0]:
        music_chooser(track, track_idx)
    bpm_key, dur_key, bars_key = tempo_keys(track_idx)
    session_state.setdefault(bars_key, 16)
    propose_tempo(track, track_idx)
    with cols[1]:
        track["bpm"] = st.number_input(
            "BPM (rythme)",
            min_value=1.0,
            key=bpm_key,
            on_change=recompute_duration,
            args=(track_idx,),
            **({} if bpm_key in session_state else {"value": DEFAULT_BPM}),
        )
    with cols[2]:
        st.selectbox(
            "Mesures",
            BAR_CHOICES,
            key=bars_key,
            on_change=recompute_duration,
            args=(track_idx,),
            help="Longueur de la vidéo en mesures (4 temps du morceau). 16 ou 24 : les changements de phase "
                 "tombent alors pile sur le début d'une mesure.",
        )
    with cols[3]:
        track["duration"] = st.number_input(
            "Durée (s)",
            min_value=1.0,
            key=dur_key,
            **({} if dur_key in session_state else {"value": DEFAULT_DURATION}),
        )
    hint = session_state.get(f"tempo_hint_{track_idx}")
    if hint:
        st.caption(hint)
    f = track.get("file")
    if f is not None and getattr(f, "name", None):
        if st.button("💾 Mémoriser ce tempo pour cette musique", key=f"save_tempo_{track_idx}"):
            try:
                save_music_setting(f.name, float(track["bpm"]), int(session_state[bars_key]))
                st.toast("Tempo mémorisé pour cette musique ✓")
            except Exception as exc:
                st.error(f"Impossible de mémoriser (droits Scaleway ?) : {type(exc).__name__}")
    if track_idx:
        with cols[4]:
            if st.button("Remove track", key=track_idx * NB_KEYS_PER_AUDIO_TRACK + 3):
                del session_state.audio_tracks[track_idx]
                st.rerun()


# ---------------------------------------------------------------------------
# Mode Photos : camera, photos de la serie en cours
# ---------------------------------------------------------------------------
def photo_section():
    picture_from_camera()
    nb_all = len(session_state.image_paths)
    info_col, reset_col = st.columns([3, 1])
    with info_col:
        counter = st.empty()
    with reset_col:
        st.button("Réinitialiser les photos", on_click=reset_photos, key="reset_top")
    if nb_all:
        st.caption("Décochez les photos à écarter de la vidéo.")
        display_image_selector(session_state.image_paths)
    # Le compteur est rempli apres les cases a cocher : il est toujours a jour
    counter.write(f"{nb_all} photo(s) prise(s) — {len(selected_paths())} cochée(s) pour la vidéo.")
    if nb_all:
        try:
            with Image.open(session_state.image_paths[-1]) as last:
                st.caption(f"Taille de la dernière photo : {last.size[0]} × {last.size[1]} pixels ({last.size[0] * last.size[1] / 1e6:.1f} Mpx)")
        except Exception:
            pass
    if nb_all:
        photos_download_block()
    with st.expander("Envoyer des photos depuis cet appareil"):
        images_uploader()


def photos_to_export(only_checked: bool) -> list:
    """Photos lisibles a exporter : toutes, ou seulement les cochees."""
    if only_checked:
        return selected_paths()
    out = []
    for path in session_state.image_paths:
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                out.append(path)
        except OSError:
            pass
    return out


def build_photos_zip(paths: list, with_retouched: bool) -> bytes:
    """Zip : dossier 'originales' (photos telles que prises) + 'retouchees' (reglages actuels)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i, path in enumerate(paths, 1):
            zf.write(path, arcname=f"originales/photo_{i:02d}{splitext(path)[1] or '.jpg'}")
        if with_retouched:
            retouched = video_editing.apply_filter_to_files(
                paths,
                session_state.filter_name,
                session_state.keep_colors,
                session_state.sat_min,
                session_state.brightness,
                session_state.contrast,
                session_state.temperature,
                session_state.tint,
                auto_enhance_on=session_state.auto_enhance_on,
                extras=session_state.extras,
            )
            if retouched != paths:  # sinon rien n'a ete modifie : inutile de doubler les fichiers
                for i, path in enumerate(retouched, 1):
                    zf.write(path, arcname=f"retouchees/photo_{i:02d}.jpg")
    return buf.getvalue()


def photos_download_block():
    """Recuperer les photos de la serie en cours (a faire avant « Réinitialiser les photos »)."""
    with st.expander("📥 Récupérer les photos"):
        only_checked = st.checkbox("Seulement les photos cochées", value=False, key="zip_only_checked")
        with_retouched = st.checkbox(
            "Ajouter aussi les versions retouchées (réglages actuels)", value=True, key="zip_with_retouched"
        )
        paths = photos_to_export(only_checked)
        signature = (
            tuple(paths), with_retouched, session_state.auto_enhance_on, session_state.filter_name,
            session_state.get("extras_sig"),
            session_state.brightness, session_state.contrast, session_state.temperature, session_state.tint,
        )
        if st.button("Préparer le fichier", key="prepare_photos_zip", disabled=not paths):
            with st.spinner("Préparation des photos…"):
                try:
                    session_state.photos_zip = {
                        "data": build_photos_zip(paths, with_retouched),
                        "sig": signature,
                        "count": len(paths),
                    }
                except Exception as exc:
                    session_state.photos_zip = None
                    st.error(f"Impossible de préparer les photos : {exc}")
        ready = session_state.get("photos_zip")
        if ready and ready["sig"] == signature:
            name = clean_file_name(session_state.event_name, max_length=40) if session_state.event_name.strip() else "photos"
            stamp = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y%m%d_%H%M")
            st.download_button(
                f"⬇️ Télécharger {ready['count']} photo(s) (zip)",
                data=ready["data"],
                file_name=f"{name}_{stamp}.zip",
                mime="application/zip",
                key="download_photos_zip",
            )
        elif ready:
            st.caption("Les photos ou les réglages ont changé : appuyez de nouveau sur « Préparer le fichier ».")
        st.caption("Les photos ne sont pas conservées après « Réinitialiser » ni à la fermeture de la page : récupérez-les avant.")


def current_zoom() -> float:
    """Zoom de la camera actuellement choisie (avant ou arriere)."""
    rear = session_state.get("cam_facing") == "Arrière"
    return float(session_state.get("cam_zoom_rear" if rear else "cam_zoom_front", 1.0))


def crop_center_zoom(data: bytes, zoom: float) -> bytes:
    """Garde le centre de l'image (1/zoom de chaque cote) : meme zone que l'apercu zoome."""
    with Image.open(BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        nw, nh = max(2, int(w / zoom)), max(2, int(h / zoom))
        left, top = (w - nw) // 2, (h - nh) // 2
        out = BytesIO()
        im.crop((left, top, left + nw, top + nh)).save(out, format="JPEG", quality=95)
        return out.getvalue()


def restart_camera():
    """Relance la camera (nouvelle cle) : le choix avant/arriere est pris en compte tout de suite."""
    session_state.camera_key += 1


# Apres chaque photo la camera est relancee (retour rapide au direct) et repart sur la camera avant.
# Si « Arrière » est choisi, ce petit script appuie une fois sur le bouton « changer de camera ».
REAR_CAMERA_JS = """
<script>
(function () {
  var W = window.parent, doc = W.document;
  W.__kaleidoRear = __REAR__;
  if (doc.getElementById('kaleido-switch')) return;
  var s = doc.createElement('script');
  s.id = 'kaleido-switch';
  s.textContent = '(' + function () {
    setInterval(function () {
      if (!window.__kaleidoRear) return;
      document.querySelectorAll('div[data-testid="stCameraInput"]').forEach(function (w) {
        if (w.dataset.kaleidoSwitched) return;
        var btn = w.querySelector('[data-testid="stCameraInputSwitchButton"] button');
        var v = w.querySelector('video');
        if (btn && v && v.readyState >= 2) {
          w.dataset.kaleidoSwitched = '1'; btn.click();
          setTimeout(function () { w.dataset.kaleidoReady = '1'; }, 1500);
        }
      });
    }, 400);
  } + ')();';
  doc.head.appendChild(s);
})();
</script>
"""


def reset_photos():
    """
    Valide la video en cours (elle part dans l'historique) et repart a zero :
    plus de photos, plus de video affichee, selection vide.
    """
    entry = session_state.get("pending_entry")
    if entry:
        session_state.history.insert(0, entry)
        session_state.pending_entry = None
    for clip in session_state.clips:
        try:
            os.remove(clip["path"])
        except OSError:
            pass
    session_state.clips = []
    session_state.zipped_clips_s3_url = None
    session_state.image_paths = []
    session_state.unchecked = set()
    session_state.series_key += 1
    session_state.photos_zip = None


def next_photo_path(prefix: str) -> str:
    session_state.photo_counter += 1
    return os.path.join(session_state.tempdir.name, f"{prefix}_{session_state.photo_counter}.jpg")


BIG_CAMERA_CSS = """
<style>
:root { --cam-side: min(calc(100vw - 2.5rem), calc(100vh - 250px));
        --cam-side: min(calc(100vw - 2.5rem), calc(100dvh - 250px)); }
div[data-testid="stMainBlockContainer"] { padding-top: 3.2rem; }
div[data-testid="stCameraInput"] { width: var(--cam-side); margin: 0 auto; }
div[data-testid="stCameraInputWebcamComponent"] { width: 100%; }
div[data-testid="stCameraInputWebcamStyledBox"] {
    width: 100% !important; height: var(--cam-side) !important; aspect-ratio: 1 / 1;
}
div[data-testid="stCameraInputWebcamStyledBox"] { overflow: hidden; }
div[data-testid="stCameraInputWebcamStyledBox"] video {
    width: 100% !important; height: 100% !important; object-fit: cover;
    scale: __ZOOM__; transform-origin: center center;
}
</style>
"""


def picture_from_camera():
    big = session_state.get("fullscreen_cam", True)
    if big:
        # Seul le carre qui sera filme est affiche (centre du flux), aussi grand que possible.
        st.markdown(BIG_CAMERA_CSS.replace("__ZOOM__", f"{current_zoom():.2f}"), unsafe_allow_html=True)
    # Ancien affichage : repere carre par bandeaux gris haut/bas (cale sur iPad portrait).
    if not big:
        st.markdown(
            """
            <style>
            div[data-testid="stCameraInput"] > div { position: relative; }
            div[data-testid="stCameraInput"] > div::after {
                content: "";
                position: absolute;
                top: 0; left: 0; right: 0; bottom: 0;
                pointer-events: none;
                z-index: 10;
                background:
                    linear-gradient(to bottom,
                        rgba(0,0,0,0.45) 0%,
                        rgba(0,0,0,0.45) 24%,
                        rgba(0,0,0,0) 24%,
                        rgba(0,0,0,0) 76%,
                        rgba(0,0,0,0.45) 76%,
                        rgba(0,0,0,0.45) 100%);
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
    # Gros bouton rond de prise de photo, fixe a l'ecran, a l'endroit choisi dans les reglages.
    position_css = BUTTON_POSITIONS.get(session_state.get("btn_pos"), BUTTON_POSITIONS[DEFAULT_BUTTON_POSITION])
    st.markdown(
        f"""
        <style>
        div[data-testid="stCameraInput"] button[data-testid="stCameraInputButton"] {{
            position: fixed;
            {position_css}
            z-index: 1000;
            width: 130px;
            height: 130px;
            border-radius: 50%;
            font-size: 1.05rem;
            font-weight: 700;
            box-shadow: 0 4px 14px rgba(0,0,0,0.35);
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    # Retour rapide a la video : apres chaque photo, on change la cle du widget camera,
    # ce qui l'affiche de nouveau en direct (plus besoin de "Clear photo").
    # Les photos prises restent en dessous, decochables si elles ne conviennent pas.
    if session_state.get("photo_failed"):
        session_state.photo_failed = False
        st.toast("⚠️ Photo non enregistrée (caméra pas encore prête). Refaites-la.")
    if session_state.get("photo_added"):
        session_state.photo_added = False
        st.toast(f"Photo {len(session_state.image_paths)} enregistrée ✓")
    picture = st.camera_input(
        "Take a photo",
        key=f"camera_{session_state.camera_key}",
        label_visibility="collapsed" if big else "visible",
    )
    rear = session_state.get("cam_facing") == "Arrière"
    html(REAR_CAMERA_JS.replace("__REAR__", "true" if rear else "false"), height=0)
    if rear:
        # Bouton grise pendant la bascule vers la camera arriere (evite une photo de la camera avant)
        st.markdown(
            """
            <style>
            div[data-testid="stCameraInput"]:not([data-kaleido-ready]) button[data-testid="stCameraInputButton"] {
                opacity: 0.3; pointer-events: none;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
    if picture is not None:
        data = picture.getvalue()
        if is_valid_image(data):
            zoom = current_zoom()
            if big and zoom > 1.0:
                data = crop_center_zoom(data, zoom)
            image_path = next_photo_path("photo")
            with open(image_path, "wb") as f:
                f.write(data)
            session_state.image_paths.append(image_path)
            session_state.photo_added = True
        else:
            # Image vide ou illisible : on l'ignore, sans bloquer l'application
            session_state.photo_failed = True
        session_state.camera_key += 1
        st.rerun()


def images_uploader():
    # Add image uploader to upload multiple images
    uploaded_images = st.file_uploader(
        "Upload image(s)",
        accept_multiple_files=True,
        key=f"uploaded_images_{session_state.session_key}",
        label_visibility="visible",
    )
    if uploaded_images:
        refused = 0
        for uploaded_img in uploaded_images:
            data = uploaded_img.getvalue()
            if not is_valid_image(data):
                refused += 1
                continue
            image_path = next_photo_path("uploaded")
            with open(image_path, "wb") as f:
                f.write(data)
            session_state.image_paths.append(image_path)
        if refused:
            session_state.photo_failed = True
        session_state.session_key += 1
        st.rerun()


def display_image_carousel(image_paths):
    # Read and encode all images to base64
    base64_images = []
    for path in image_paths:
        with open(path, "rb") as img_file:
            b64 = base64.b64encode(img_file.read()).decode("utf-8")
            base64_images.append(f"data:image/jpeg;base64,{b64}")
    # Build the HTML carousel
    html_code = f"""
    <div style="display: flex; overflow-x: auto; gap: 10px; padding: 10px; border: 1px solid #ddd; border-radius: 10px;">
        {''.join([f'<img src="{src}" style="height: 150px; border-radius: 8px;" />' for src in base64_images])}
    </div>
    """
    html(html_code, height=180)


def square_thumb(path: str, size: int = 400):
    """Vignette carree (centre de la photo) : c'est ce qui sera dans la video."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        im.thumbnail((size, size))
        return im


def display_image_selector(image_paths):
    """
    Affiche chaque photo (en carre) avec une case a cocher.
    La selection est memorisee dans session_state.unchecked (les photos decochees),
    pour rester correcte apres chaque nouvelle photo et quand on demarre une nouvelle serie.
    """
    cols = st.columns(3)
    for idx, path in enumerate(image_paths):
        with cols[idx % 3]:
            readable = True
            try:
                st.image(square_thumb(path), use_container_width=True)
            except Exception:
                readable = False
                st.warning("Photo illisible (ignorée)")
                session_state.unchecked.add(path)
            checked = st.checkbox(
                "Inclure",
                value=readable and path not in session_state.unchecked,
                disabled=not readable,
                key=f"select_img_{session_state.series_key}_{idx}",
            )
            if not readable:
                continue
            if checked:
                session_state.unchecked.discard(path)
            else:
                session_state.unchecked.add(path)


# ---------------------------------------------------------------------------
# Mode Video : creation, derniere video, historique
# ---------------------------------------------------------------------------
def video_section():
    paths = selected_paths()
    nb_all = len(session_state.image_paths)
    track_has_file = lambda track: track["file"] is not None
    has_audio = all(map(track_has_file, session_state.audio_tracks))
    st.divider()
    if nb_all == 0:
        st.info("Prenez quelques photos avec le gros bouton, puis créez la vidéo.")
    elif not paths:
        st.warning("Toutes les photos sont décochées : cochez-en au moins une.")
    if not has_audio:
        st.warning("Ajoutez la musique dans les réglages de la soirée (Fichier audio).")
    can_create = bool(paths) and has_audio

    session_state.reverse_video = st.checkbox(
        "⏪ Montage à l'envers (la dernière photo en premier)",
        value=False,
        key="reverse_cb",
        help="Exemple : photo du groupe complet d'abord, puis on retire une personne à chaque photo. "
             "La vidéo commence sur la dernière photo et se termine sur la première : le groupe se reconstitue.",
    )
    session_state.multiply_video = st.checkbox(
        "🔳 Démultiplier les carrés au fil de la vidéo",
        value=False,
        key="multiply_cb",
        help="Les photos s'enchaînent d'abord en rythme (comme d'habitude), puis la photo de chaque petit carré "
             "tourne d'un quart de tour à chaque temps, puis les carrés se multiplient : ×16, puis ×64 si vous le "
             "choisissez, avant de revenir à 4. "
             "Avec 32 s : 4 s de photos, 4 s qui tournent, puis les multiplications. "
             "La taille de la vidéo ne change pas.",
    )
    if session_state.multiply_video:
        session_state.multiply_max = st.radio(
            "Aller jusqu'à",
            [16, 64],
            format_func=lambda v: "×16 (4 → 16 → 4 carrés)" if v == 16 else "×64 (4 → 16 → 64 → 16 → 4 carrés)",
            horizontal=True,
            key="multiply_max_radio",
        )
    session_state.spin_back = ""
    if session_state.multiply_video:
        if session_state.get("multiply_max", 16) == 64:
            spin_options = ["Aucune", "Pendant la redescente ×16", "Pendant le pic ×64"]
        else:
            spin_options = ["Aucune", "Pendant le ×16"]
        spin_choice = st.radio(
            "La rotation revient",
            spin_options,
            horizontal=True,
            key=f"spin_back_radio_{len(spin_options)}",
            help="Les petits carrés refont un quart de tour sur place à chaque temps pendant cette phase.",
        )
        if spin_choice.endswith("×64"):
            session_state.spin_back = "peak"
        elif spin_choice != "Aucune":
            session_state.spin_back = "descent"
    create = st.button("🎬 Créer la vidéo", type="primary", disabled=not can_create)
    if create:
        with st.spinner("Création de la vidéo…"):
            create_new_clips()
        st.rerun()

    if session_state.clips:
        st.subheader("Vidéo")
        for clip in session_state.clips:
            try:
                display_video(clip)
            except st.errors.StreamlitDuplicateElementKey:
                st.text(DUPLICATE_VIDEO_DOWNLOAD_KEY)
        if len(session_state.clips) > 1 and session_state.zipped_clips_s3_url:
            link_col, qrcode_col = st.columns(2)
            with link_col:
                st.markdown(f"[link to all videos]({session_state.zipped_clips_s3_url})")
            with qrcode_col:
                st.image(generate_qr_code(session_state.zipped_clips_s3_url), caption="Scannez pour tout récupérer (zip)")
        st.info(
            "Satisfait ? Appuyez sur « Réinitialiser les photos » : la vidéo part dans l'historique "
            "et vous repartez de zéro.  \nPas satisfait ? Cochez/décochez ou refaites des photos, "
            "puis « Créer la vidéo » : la nouvelle version remplace celle-ci."
        )
        st.button("Réinitialiser les photos (valider la vidéo)", on_click=reset_photos, key="reset_bottom")


# ---------------------------------------------------------------------------
# Bibliotheque : retrouver les seances passees (videos + photos) depuis l'application
# ---------------------------------------------------------------------------
SESSION_DIR_RE = re.compile(r"^\d{2}-\d{2}-\d{4}:\d{2}-\d{2}-\d{2}$")
NO_EVENT_LABEL = "(sans nom d'événement)"


def scan_library():
    """
    Parcourt le bucket et regroupe par evenement puis par seance :
    {evenement: {seance: {"videos": [...], "photos": [...], "zip": cle|None}}}
    """
    client, bucket = music_client(), st.secrets["BUCKET_NAME"]
    library = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if parts[0] in ("musiques", "looks") or len(parts) < 2 and not key.endswith(".zip"):
                continue
            # le zip des videos d'une seance s'appelle "<seance>.zip" a cote du dossier
            if parts[-1].lower().endswith(".zip") and SESSION_DIR_RE.match(parts[-1][:-4]):
                event = "/".join(parts[:-1]) or NO_EVENT_LABEL
                library.setdefault(event, {}).setdefault(parts[-1][:-4], {"videos": [], "photos": [], "zip": None, "sizes": {}})["zip"] = key
                continue
            idx = next((i for i, part in enumerate(parts) if SESSION_DIR_RE.match(part)), None)
            if idx is None:
                continue
            event = "/".join(parts[:idx]) or NO_EVENT_LABEL
            entry = library.setdefault(event, {}).setdefault(parts[idx], {"videos": [], "photos": [], "zip": None, "sizes": {}})
            entry["sizes"][key] = obj.get("Size", 0)
            rest = parts[idx + 1:]
            if rest and rest[0] == "photos" and len(rest) > 1:
                entry["photos"].append(key)
            elif rest and rest[-1].lower().endswith(".mp4"):
                entry["videos"].append(key)
            elif len(rest) == 1 and rest[0].lower().endswith(".zip"):
                entry["complete"] = key
    return library


def session_datetime(session_name: str):
    try:
        return datetime.strptime(session_name, "%d-%m-%Y:%H-%M-%S")
    except ValueError:
        return datetime.min


def session_label(session_name: str, entry: dict) -> str:
    when = session_datetime(session_name)
    day = when.strftime("%d/%m/%Y à %H:%M") if when != datetime.min else session_name
    return f"{day} — {len(entry['videos'])} vidéo(s), {len(entry['photos'])} photo(s)"


@st.cache_data(max_entries=80, show_spinner=False)
def fetch_object_bytes(key: str) -> bytes:
    return music_client().get_object(Bucket=st.secrets["BUCKET_NAME"], Key=key)["Body"].read()


def zip_objects(items: list) -> bytes:
    """items : liste de (cle Scaleway, nom dans le zip). Telecharge et assemble en memoire."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for key, arcname in items:
            zf.writestr(arcname, music_client().get_object(Bucket=st.secrets["BUCKET_NAME"], Key=key)["Body"].read())
    return buf.getvalue()


MAX_ZIP_MB = 250   # au-dela, l'hebergement gratuit risque de manquer de memoire : on propose seance par seance


def library_download_all(library: dict, event: str, session_name: str):
    """Boutons « tout telecharger » : la seance (videos + photos) ou tout l'evenement."""
    entry = library[event][session_name]
    session_items = [(k, f"videos/{k.split('/')[-1]}") for k in sorted(entry["videos"])]
    session_items += [(k, f"photos/{k.split('/')[-1]}") for k in sorted(entry["photos"])]
    session_mb = sum(entry["sizes"].get(k, 0) for k, _ in session_items) / 1e6
    event_items = []
    for name in sorted(library[event], key=session_datetime):
        e = library[event][name]
        folder = name.replace(":", "-")
        event_items += [(k, f"{folder}/videos/{k.split('/')[-1]}") for k in sorted(e["videos"])]
        event_items += [(k, f"{folder}/photos/{k.split('/')[-1]}") for k in sorted(e["photos"])]
    event_mb = sum(
        library[event][n]["sizes"].get(k, 0) for n in library[event]
        for k in library[event][n]["videos"] + library[event][n]["photos"]
    ) / 1e6
    safe_event = clean_file_name(event, max_length=40)
    for label, items, size_mb, ident, file_name in (
        ("cette séance", session_items, session_mb, ("session", event, session_name),
         f"{safe_event}_{session_name.replace(':', '-')}_tout.zip"),
        (f"tout l'événement « {event} »", event_items, event_mb, ("event", event, ""),
         f"{safe_event}_evenement_complet.zip"),
    ):
        if not items:
            continue
        if size_mb > MAX_ZIP_MB:
            st.caption(f"Télécharger {label} d'un seul coup ({size_mb:.0f} Mo) est trop lourd ici : faites-le séance par séance.")
            continue
        if st.button(f"📦 Préparer tout télécharger : {label} ({size_mb:.0f} Mo)", key=f"all_prepare_{ident[0]}"):
            with st.spinner("Préparation du zip, cela peut prendre un moment…"):
                try:
                    session_state.library_all = {"id": ident, "data": zip_objects(items)}
                except Exception as exc:
                    st.error(f"Préparation impossible : {type(exc).__name__}: {str(exc)[:160]}")
        ready = session_state.get("library_all")
        if ready and ready["id"] == ident:
            st.download_button(f"⬇️ Télécharger {label} (zip)", data=ready["data"], file_name=file_name,
                               mime="application/zip", key=f"all_download_{ident[0]}")


def library_section():
    """Retrouver ses seances passees : videos (lecteur + lien), photos (miniatures + zip)."""
    with st.expander("📚 Retrouver mes séances (vidéos et photos)"):
        if st.button("🔄 Charger / actualiser la liste", key="library_refresh"):
            with st.spinner("Lecture de Scaleway…"):
                try:
                    session_state.library = scan_library()
                    session_state.library_error = ""
                except Exception as exc:
                    session_state.library = None
                    session_state.library_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        if session_state.get("library_error"):
            st.error(f"Lecture impossible (connexion ou droits Scaleway ?) {session_state.library_error}")
        library = session_state.get("library")
        if not library:
            st.caption("Appuyez sur « Charger » pour voir vos événements, classés par date.")
            return
        events = sorted(
            library,
            key=lambda ev: max(session_datetime(name) for name in library[ev]),
            reverse=True,
        )
        event = st.selectbox("Événement", events, key="library_event")
        sessions = sorted(library[event], key=session_datetime, reverse=True)
        session_name = st.selectbox(
            "Séance",
            sessions,
            format_func=lambda name: session_label(name, library[event][name]),
            key="library_session",
        )
        entry = library[event][session_name]
        for key in sorted(entry["videos"]):
            url = create_s3_key_url(key)
            st.video(url)
            st.code(url, language=None)
        if entry["zip"]:
            st.markdown(f"[⬇️ Zip des vidéos de cette séance]({create_s3_key_url(entry['zip'])})")
        complete = entry.get("complete")
        if complete:
            try:
                url = music_client().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": st.secrets["BUCKET_NAME"], "Key": complete},
                    ExpiresIn=3600,
                )
                mb = entry["sizes"].get(complete, 0) / 1e6
                st.link_button(f"⬇️ Dossier complet : vidéos + photos (zip, {mb:.0f} Mo)", url, type="primary")
                st.caption(f"Fichier : {complete.split('/')[-1]} — téléchargé directement depuis Scaleway, sans passer par l'application. Le lien reste valable 1 h : actualisez la liste pour en obtenir un nouveau.")
            except Exception:
                st.caption("Lien du zip complet indisponible pour le moment.")
        heavy = st.checkbox(
            "Autres téléchargements (lourds : à faire depuis l'ordinateur, pas la tablette)", key="library_heavy"
        )
        if heavy:
            library_download_all(library, event, session_name)
        if not entry["photos"]:
            if not complete:
                st.caption("Aucune photo sauvegardée pour cette séance (la case « Sauvegarder aussi les photos » n'était pas cochée).")
            return
        st.markdown(f"**{len(entry['photos'])} photo(s) sauvegardée(s)**")
        photos = sorted(entry["photos"])
        shown = photos[:12]
        if st.checkbox("Afficher les miniatures des photos (plus lent)", key="library_thumbs"):
            cols = st.columns(3)
            for i, key in enumerate(shown):
                try:
                    with Image.open(BytesIO(fetch_object_bytes(key))) as im:
                        thumb = im.convert("RGB")
                        thumb.thumbnail((400, 400))
                    cols[i % 3].image(thumb, use_container_width=True)
                except Exception:
                    cols[i % 3].caption("(photo illisible)")
            if len(photos) > len(shown):
                st.caption(f"+ {len(photos) - len(shown)} autres photos dans le zip.")
        if not heavy:
            return
        if st.button("Préparer le zip des photos", key="library_zip_prepare"):
            with st.spinner("Préparation…"):
                buf = BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                    for key in photos:
                        zf.writestr(key.split("/")[-1], fetch_object_bytes(key))
                session_state.library_zip = {"id": (event, session_name), "data": buf.getvalue()}
        zip_ready = session_state.get("library_zip")
        if zip_ready and zip_ready["id"] == (event, session_name):
            st.download_button(
                f"⬇️ Télécharger les {len(photos)} photos (zip)",
                data=zip_ready["data"],
                file_name=f"photos_{clean_file_name(event, max_length=40)}_{session_name.replace(':', '-')}.zip",
                mime="application/zip",
                key="library_zip_download",
            )


def display_history():
    """Videos precedentes de la soiree (la plus recente en haut), avec vignette, lien et QR code."""
    if not session_state.history:
        return
    st.subheader("Historique de la soirée")
    for entry in session_state.history:
        thumb_col, text_col, qr_col = st.columns([1, 2, 1])
        with thumb_col:
            if entry.get("thumb") and os.path.exists(entry["thumb"]):
                st.image(entry["thumb"], width=120)
        with text_col:
            st.markdown(f"**{entry['label']}**  \n{entry.get('date', '')} {entry['time']}")
            for clip in entry["clips"]:
                st.markdown(f"[lien]({clip['s3_url']})")
        with qr_col:
            for clip in entry["clips"]:
                st.image(generate_qr_code(clip["s3_url"]), width=120)


def make_video_stem(event: str, serie_number: int, audio_name: str, multiple_tracks: bool) -> str:
    """Nom du fichier video (sans extension) : nom de l'evenement (ou du son) + numero de video."""
    base = event or audio_name
    stem = f"{base}_{serie_number:03d}"
    if event and multiple_tracks:
        stem += f"_{audio_name}"
    return stem


def create_new_clips():
    # On ne garde pas les anciens fichiers video sur le disque : l'historique garde les liens.
    for clip in session_state.clips:
        try:
            os.remove(clip["path"])
        except OSError:
            pass
    session_state.clips = []
    now = datetime.now(ZoneInfo("Europe/Paris"))
    datetime_str = now.strftime("%d-%m-%Y:%H-%M-%S")
    session_state.video_counter += 1
    n = session_state.video_counter
    event = clean_file_name(session_state.event_name, max_length=40) if session_state.event_name.strip() else ""
    key_dir = join(event, datetime_str) if event else datetime_str
    selected = selected_paths()
    session_state.selected_image_paths = selected
    multiple_tracks = len(session_state.audio_tracks) > 1
    for track in session_state.audio_tracks:
        audio_name = clean_file_name(splitext(track["file"].name)[0])
        stem = make_video_stem(event, n, audio_name, multiple_tracks)
        clip_path, duration = create_clip(
            track,
            selected,
            file_stem=stem,
            mute=session_state.mute_video,
            auto_enhance_on=session_state.auto_enhance_on,
            extras=session_state.extras,
            reverse=session_state.get("reverse_video", False),
            multiply=(session_state.get("multiply_max", 16) if session_state.get("multiply_video", False) else 0),
            overlay_bytes=session_state.get("overlay_bytes"),
            spin_back=session_state.get("spin_back", ""),
            filter_name=session_state.filter_name,
            keep_colors=session_state.keep_colors,
            sat_min=session_state.sat_min,
            brightness=session_state.brightness,
            contrast=session_state.contrast,
            temperature=session_state.temperature,
            tint=session_state.tint,
        )
        s3_url = upload_file_to_bucket(clip_path, join(key_dir, clip_path))
        session_state.clips.append({
            "path": clip_path,
            "s3_url": s3_url,
            "duration": duration
        })
    if session_state.get("save_photos", False):
        # Un zip complet (videos + photos d'origine) fabrique en arriere-plan, depose en prive sur Scaleway
        zip_label = clean_file_name(
            f"{event or clean_file_name(splitext(session_state.audio_tracks[0]['file'].name)[0])}_"
            f"{now.strftime('%d-%m-%Y_%Hh%M')}", max_length=80)
        try:
            videos_data = []
            for c in session_state.clips:
                with open(c["path"], "rb") as fh:
                    videos_data.append((os.path.basename(c["path"]), fh.read()))
            complete_zip_async(zip_label, key_dir, videos_data, photos_to_export(True))
        except Exception:
            pass
    zipped_clips_path = zip_all_clips(datetime_str)
    zipped_clips_s3_url = upload_file_to_bucket(zipped_clips_path, key_dir + ".zip")
    session_state.zipped_clips_s3_url = zipped_clips_s3_url
    # Historique : vignette (1re photo cochee, avec reglages et filtre) + liens
    thumb_path = os.path.join(session_state.tempdir.name, f"history_{n}.jpg")
    try:
        preview_image(selected[0], width=240).save(thumb_path, quality=85)
    except Exception:
        thumb_path = None
    session_state.pending_entry = {
        "label": event or clean_file_name(splitext(session_state.audio_tracks[0]["file"].name)[0]),
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%d/%m/%Y"),
        "thumb": thumb_path,
        "clips": [dict(c) for c in session_state.clips],
        "number": n,
    }


def clean_file_name(name: str, max_length: int = 60) -> str:
    """
    Rend un nom de fichier sur : sans accents, guillemets, espaces ni symboles.
    Garde seulement lettres, chiffres, tiret et underscore (evite les liens/QR codes casses).
    """
    name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_-")
    return (name[:max_length].strip("_-")) or "video"


def create_clip(
    track: defaultdict,
    image_paths: list,
    file_stem: str | None = None,
    mute: bool = False,
    filter_name: str = video_editing.FILTER_NONE,
    keep_colors=None,
    sat_min: float = video_editing.DEFAULT_SAT_MIN,
    brightness: int = video_editing.DEFAULT_BRIGHTNESS,
    contrast: int = video_editing.DEFAULT_CONTRAST,
    temperature: int = video_editing.DEFAULT_TEMPERATURE,
    tint: int = video_editing.DEFAULT_TINT,
    auto_enhance_on: bool = False,
    reverse: bool = False,
    extras: dict = None,
    multiply: int = 0,
    overlay_bytes: bytes = None,
    spin_back: str = "",
) -> tuple[str, float]:
    """
    ### Description:
    Wrapper around video_editing.create_clip to prepare its inputs.
    ### Returns:
    Returns the path to the clip file and its duration.
    """
    audio_name, audio_ext = splitext(track["file"].name)
    audio_name = clean_file_name(audio_name)
    if file_stem:
        video_filename = f"{file_stem}.mp4"
    else:
        # to str in case splitext returns None
        video_filename = VIDEO_NAME_FORMAT.format(
            audio_name=audio_name,
            bpm=track["bpm"],
        )
    # For some reason, I couldn't access the file provided by the fileuploader.
    # So create a temp file as aid band fix (yet another one).
    work_dir = tempfile.mkdtemp()
    work_paths = []
    for i, path in enumerate(image_paths):
        copy_path = os.path.join(work_dir, f"w_{i:03d}.jpg")
        with Image.open(path) as src:
            src.convert("RGB").save(copy_path, quality=97)
        work_paths.append(copy_path)
    overlay_path = None
    if overlay_bytes:
        overlay_path = os.path.join(work_dir, "calque.png")
        with open(overlay_path, "wb") as fh:
            fh.write(overlay_bytes)
    with tempfile.NamedTemporaryFile(suffix=audio_ext) as audio_file:
        audio_file.write(track["file"].getbuffer())
        video_editing.create_clip(
            image_paths=work_paths,
            audio_path=audio_file.name,
            bpm=track["bpm"],
            duration=track["duration"],
            output_path=video_filename,
            mute=mute,
            filter_name=filter_name,
            keep_colors=keep_colors,
            sat_min=sat_min,
            brightness=brightness,
            contrast=contrast,
            temperature=temperature,
            tint=tint,
            auto_enhance_on=auto_enhance_on,
            reverse=reverse,
            extras=extras,
            multiply=multiply,
            overlay_path=overlay_path,
            spin_back=spin_back,
        )
        return video_filename, track["duration"]


def zip_all_clips(zip_filename: str) -> str:
    zip_path = os.path.join(session_state.tempdir.name, zip_filename) + ".zip"
    with zipfile.ZipFile(zip_path, "w") as zipf:
        for clip in session_state.clips:
            arcname = os.path.basename(clip["path"])  # Name inside zip
            zipf.write(clip["path"], arcname=arcname)
    return zip_path


def display_video(clip: dict):
    with open(clip["path"], "rb") as video_file:
        clip_filename = split(clip["path"])[1]
        st.subheader(clip_filename)
        st.video(video_file.read())
        button_col, url_col, qrcode_col = st.columns(3)
        with button_col:
            st.download_button(
                "Download Video",
                video_file,
                file_name=clip_filename,
                key=clip["path"] + str(clip["duration"])
            )
        with url_col:
            st.markdown(f"[link]({clip['s3_url']})")
        with qrcode_col:
            qr_img = generate_qr_code(clip["s3_url"])
            st.image(qr_img, caption="Scannez pour récupérer la vidéo")


def generate_qr_code(url: str) -> BytesIO:
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


if __name__ == "__main__":
    main()
