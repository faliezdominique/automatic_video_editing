import os
import re
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
from s3_utils import upload_file_to_bucket


def main():
    if "tempdir" not in session_state:
        setup_session_state()
    st.title("Video Clip Creator")
    # Audio tracks
    st.subheader("Audio tracks")
    # Add audio track
    if st.button(r"\+ audio track"):
        session_state.audio_tracks.append(mk_track_dict())
    # audio track inputs
    for track_idx, track in enumerate(session_state.audio_tracks):
        create_audio_track_inputs(track_idx, track)
    track_has_file = lambda track: track["file"] is not None
    if not all(map(track_has_file, session_state.audio_tracks)):
        st.warning("Please provide an audio file for all the audio tracks.")
        return

    # Pictures
    st.subheader("Pictures")
    # Reset pictures
    if st.button("Reset Photos"):
        session_state.image_paths = []
    # Take pictures
    picture_from_camera()
    images_uploader()
    # Display nb of pictures taken
    st.write(f"{len(session_state.image_paths)} photo(s) taken.")
    # Check that at least one picture has been taken
    if len(session_state.image_paths) == 0:
        st.warning("Please take at least one photo and upload an audio file.")
        return
    st.subheader("Images")
    st.caption("Cochez les photos à inclure dans la vidéo (toutes cochées par défaut).")
    selected_paths = display_image_selector(session_state.image_paths)
    session_state.selected_image_paths = selected_paths
    st.write(f"{len(selected_paths)} photo(s) sélectionnée(s) sur {len(session_state.image_paths)}.")
    if len(selected_paths) == 0:
        st.warning("Veuillez cocher au moins une photo.")
        return

    # Reglages et filtre appliques a toutes les photos (sur des copies : les originaux ne sont pas modifies)
    st.subheader("Réglages et filtre")
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
    if (
        session_state.filter_name != video_editing.FILTER_NONE
        or session_state.brightness != 0
        or session_state.contrast != 0
        or session_state.temperature != 0
        or session_state.tint != 0
    ):
        with Image.open(selected_paths[0]) as im:
            small = im.convert("RGB")
            small.thumbnail((600, 600))
        preview = video_editing.apply_filter(
            small, session_state.filter_name, session_state.keep_colors, session_state.sat_min,
            session_state.brightness, session_state.contrast,
            session_state.temperature, session_state.tint,
        )
        st.image(preview, caption="Aperçu des réglages (1re photo sélectionnée)", width=260)

    # Option : couper le son de la video (le rythme et la duree restent cales sur l'audio)
    session_state.mute_video = st.checkbox("Couper le son de la vidéo", value=False)

    # Videos
    if session_state.zipped_clips_s3_url is not None:
        new_videos_col, link_col, qrcode_col = st.columns(3)
        with new_videos_col:
            create = st.button("Create new videos")
        with link_col:
            st.markdown(f"[link to all videos]({session_state.zipped_clips_s3_url})")
        with qrcode_col:
            qr_img = generate_qr_code(session_state.zipped_clips_s3_url)
            st.image(qr_img, caption="Scan to download zip file")
    else:
        create = st.button("Create new videos")
    if create:
        create_new_clips()
        st.rerun()
    for clip in session_state.clips:
        try:
            display_video(clip)
        except st.errors.StreamlitDuplicateElementKey:
            st.text(DUPLICATE_VIDEO_DOWNLOAD_KEY)

def setup_session_state():
    session_state.tempdir = tempfile.TemporaryDirectory()
    session_state.image_paths = []
    session_state.selected_image_paths = []
    session_state.mute_video = False
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

def mk_track_dict() -> defaultdict:
    return defaultdict(
            file=None,
            bpm=DEFAULT_BPM,
            duration=DEFAULT_DURATION
        )

def create_audio_track_inputs(track_idx: int, track: defaultdict):
    spec = [3, 1, 1, 1] if track_idx else [3, 1, 1]
    cols = st.columns(spec)
    with cols[0]:
        track["file"] = st.file_uploader(
            "Audio File",
            key=track_idx * NB_KEYS_PER_AUDIO_TRACK,
        )
    with cols[1]:
        track["bpm"] = st.number_input(
            "BPM",
            min_value=1.0,
            value=DEFAULT_BPM,
            key=track_idx * NB_KEYS_PER_AUDIO_TRACK + 1)
    with cols[2]:
        track["duration"] = st.number_input(
            "Duration (s)",
            min_value=1.0,
            value=DEFAULT_DURATION,
            key=track_idx * NB_KEYS_PER_AUDIO_TRACK + 2
        )
    if track_idx:
        with cols[3]:
            if st.button("Remove track", key=track_idx * NB_KEYS_PER_AUDIO_TRACK + 3):
                del session_state.audio_tracks[track_idx]
                st.rerun()

def picture_from_camera():
    # Repere carre : bandeaux gris haut/bas a 22% (cale au metre sur iPad portrait).
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
    # Retour rapide a la video : apres chaque photo, on change la cle du widget camera,
    # ce qui l'affiche de nouveau en direct (plus besoin de "Clear photo").
    # Les photos prises restent en dessous, decochables si elles ne conviennent pas.
    if "camera_key" not in session_state:
        session_state.camera_key = 0
    if session_state.get("photo_added"):
        session_state.photo_added = False
        st.toast(f"Photo {len(session_state.image_paths)} enregistrée ✓")
    picture = st.camera_input("Take a photo", key=f"camera_{session_state.camera_key}")
    if picture is not None:
        img_idx = len(session_state.image_paths)
        image_path = os.path.join(session_state.tempdir.name, f"photo_{img_idx}.jpg")
        with open(image_path, "wb") as f:
            f.write(picture.getbuffer())
        session_state.image_paths.append(image_path)
        session_state.camera_key += 1
        session_state.photo_added = True
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
        for uploaded_img in uploaded_images:
            img_idx = len(session_state.image_paths)
            image_path = os.path.join(session_state.tempdir.name, f"uploaded_{img_idx}.jpg")
            with open(image_path, "wb") as f:
                f.write(uploaded_img.getbuffer())
            session_state.image_paths.append(image_path)
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

def display_image_selector(image_paths):
    """Affiche chaque photo avec une case a cocher. Retourne la liste des chemins coches."""
    selected = []
    cols = st.columns(4)
    for idx, path in enumerate(image_paths):
        with cols[idx % 4]:
            st.image(path, width=140)
            checked = st.checkbox("Inclure", value=True, key=f"select_img_{idx}")
            if checked:
                selected.append(path)
    return selected

def create_new_clips():
    for clip in session_state.clips:
        os.remove(clip["path"])
    session_state.clips = []
    datetime_str = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d-%m-%Y:%H-%M-%S")
    for track in session_state.audio_tracks:
        clip_path, duration = create_clip(
            track,
            session_state.selected_image_paths,
            mute=session_state.mute_video,
            filter_name=session_state.filter_name,
            keep_colors=session_state.keep_colors,
            sat_min=session_state.sat_min,
            brightness=session_state.brightness,
            contrast=session_state.contrast,
            temperature=session_state.temperature,
            tint=session_state.tint,
        )
        s3_url = upload_file_to_bucket(clip_path, join(datetime_str, clip_path))
        session_state.clips.append({
            "path": clip_path,
            "s3_url": s3_url,
            "duration": duration
        })
    zipped_clips_path = zip_all_clips(datetime_str)
    zipped_clips_s3_url = upload_file_to_bucket(zipped_clips_path, datetime_str + ".zip")
    session_state.zipped_clips_s3_url = zipped_clips_s3_url

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
    mute: bool = False,
    filter_name: str = video_editing.FILTER_NONE,
    keep_colors=None,
    sat_min: float = video_editing.DEFAULT_SAT_MIN,
    brightness: int = video_editing.DEFAULT_BRIGHTNESS,
    contrast: int = video_editing.DEFAULT_CONTRAST,
    temperature: int = video_editing.DEFAULT_TEMPERATURE,
    tint: int = video_editing.DEFAULT_TINT,
) -> tuple[str, float]:
    """
    ### Description:
    Wrapper around video_editing.create_clip to prepare its inputs.
    ### Returns:
    Returns the path to the clip file and its duration. 
    """
    audio_name, audio_ext = splitext(track["file"].name)
    audio_name = clean_file_name(audio_name)
    # to str in case splitext returns None
    video_filename = VIDEO_NAME_FORMAT.format(
        audio_name=audio_name,
        bpm=track["bpm"],
    )
    # For some reason, I couldn't access the file provided by the fileuploader.
    # So create a temp file as aid band fix (yet another one).
    with tempfile.NamedTemporaryFile(suffix=audio_ext) as audio_file:
        audio_file.write(track["file"].getbuffer())
        video_editing.create_clip(
            image_paths=image_paths,
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
            st.image(qr_img, caption="Scan to download zip file")


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
