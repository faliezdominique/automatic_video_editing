from sys import argv
from PIL import Image
from os import listdir
from os.path import join
from shutil import copytree
from tempfile import mkdtemp

import numpy as np
from PIL import ImageFilter, ImageEnhance

from moviepy.video.fx import Rotate, Crop
from moviepy import Clip, ImageSequenceClip, VideoClip, AudioFileClip, clips_array
from moviepy.audio.fx import AudioLoop


# ---------------------------------------------------------------------------
# Filtres
# ---------------------------------------------------------------------------
FILTER_NONE = "Aucun"
FILTER_BW = "Noir et blanc"
FILTER_SEPIA = "Sépia"
FILTER_BW_COLORS = "Noir et blanc + couleurs conservées"
FILTER_NAMES = [FILTER_NONE, FILTER_BW, FILTER_SEPIA, FILTER_BW_COLORS]

# Plages de teinte en degres (debut, fin). Si debut > fin, la plage passe par 0 (rouge).
COLOR_HUES = {
    "Rouge": (342, 18),
    "Orange": (18, 45),
    "Jaune": (45, 70),
    "Vert": (70, 180),
    "Bleu": (180, 260),
    "Rose": (290, 342),
    "Violet": (260, 290),
}
DEFAULT_KEEP_COLORS = ["Rouge", "Jaune", "Bleu"]
DEFAULT_SAT_MIN = 0.40
# Luminosite / contraste en pourcentage : 0 = inchange, -50 = plus sombre / moins contraste, +50 = inverse
DEFAULT_BRIGHTNESS = 0
DEFAULT_CONTRAST = 0
# Balance des couleurs : temperature (- froid/bleute, + chaud/orange) et teinte (- vert, + magenta), 0 = inchange
DEFAULT_TEMPERATURE = 0
DEFAULT_TINT = 0


# ---------------------------------------------------------------------------
# Amelioration automatique ("protocole classe")
# ---------------------------------------------------------------------------
# Etapes, identiques pour toutes les photos d'une serie (pas de scintillement dans la video) :
#   1. niveaux : noirs/blancs recales sur la photo (plafonne pour rester naturel)
#   2. exposition : gamma pour amener le sujet (zone centrale) vers une luminosite agreable
#   3. bruit : lissage de la couleur seulement (le bruit colore est le plus laid en basse lumiere)
#   4. nettete : masque flou doux sur la luminosite
#   5. couleurs : legere hausse de saturation
# Ne rattrape pas une photo floue ou tres sous-exposee : ameliore, ne fait pas de miracle.
ENHANCE_TARGET_MEAN = 0.46      # luminosite moyenne visee (0-1) au centre de l'image
ENHANCE_MAX_SIDE = 2000         # taille max de travail (au-dela : reduit, evite la saturation memoire)
ENHANCE_SATURATION = 1.08


def _luma(a):
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


def _small_rgb(img, side=256):
    s = img.convert("RGB")
    s.thumbnail((side, side))
    return np.asarray(s, dtype=np.float32)


def enhance_params(images):
    """
    Calcule UNE FOIS les reglages d'amelioration pour toute la serie (mediane des photos).
    `images` : liste d'images PIL ou de chemins.
    """
    blacks, whites, lumas = [], [], []
    for im in images:
        if isinstance(im, (str, bytes)) or hasattr(im, "__fspath__"):
            with Image.open(im) as f:
                try:
                    f.draft("RGB", (512, 512))
                except Exception:
                    pass
                a = _small_rgb(f)
        else:
            a = _small_rgb(im)
        y = _luma(a)
        blacks.append(min(float(np.percentile(y, 0.5)), 40.0))
        whites.append(max(float(np.percentile(y, 99.5)), 200.0))
        h, w = y.shape
        lumas.append(y[h // 5: h - h // 5, w // 5: w - w // 5])   # zone centrale = le sujet
    if not lumas:
        return None
    black = float(np.median(blacks))
    white = float(np.median(whites))
    scale = 255.0 / max(white - black, 1.0)
    means = [float((((c - black) * scale).clip(0, 255) / 255.0).mean()) for c in lumas]
    m = min(max(float(np.median(means)), 0.05), 0.95)
    gamma = float(np.clip(np.log(ENHANCE_TARGET_MEAN) / np.log(m), 0.70, 1.40))
    if abs(gamma - 1.0) < 0.05:
        gamma = 1.0
    return {"black": black, "white": white, "gamma": gamma}


def auto_enhance(img, params):
    """Retourne une copie amelioree de l'image PIL. `params` vient de enhance_params()."""
    img = img.convert("RGB")
    if not params:
        return img
    if max(img.size) > ENHANCE_MAX_SIDE:
        img = img.copy()
        img.thumbnail((ENHANCE_MAX_SIDE, ENHANCE_MAX_SIDE), Image.LANCZOS)
    # 1 + 2 : niveaux puis gamma, en une seule table de correspondance
    scale = 255.0 / max(params["white"] - params["black"], 1.0)
    x = ((np.arange(256) - params["black"]) * scale).clip(0, 255) / 255.0
    lut = (np.power(x, params["gamma"]) * 255.0).round().clip(0, 255).astype("uint8").tolist()
    img = img.point(lut * 3)
    # 3 + 4 : bruit de couleur lisse, nettete sur la luminosite
    side = min(img.size)
    y, cb, cr = img.convert("YCbCr").split()
    chroma_r = max(1.5, side / 450.0)
    cb = cb.filter(ImageFilter.GaussianBlur(chroma_r))
    cr = cr.filter(ImageFilter.GaussianBlur(chroma_r))
    # bruit de luminosite : mesure sur le centre, puis lissage proportionnel (sinon la nettete amplifie le grain)
    c = side // 2
    crop = y.crop((c - min(c, 200), c - min(c, 200), c + min(c, 200), c + min(c, 200)))
    ref = np.asarray(crop, dtype=np.float32)
    med = np.asarray(crop.filter(ImageFilter.MedianFilter(3)), dtype=np.float32)
    noise = float(np.median(np.abs(ref - med)) * 1.48)
    if noise > 1.0:
        weight = min(noise / 5.0, 1.0)
        soft = np.asarray(y.filter(ImageFilter.GaussianBlur(min(0.6 + noise / 8.0, 1.4))), dtype=np.float32)
        mixed = np.asarray(y, dtype=np.float32) * (1 - weight) + soft * weight
        y = Image.fromarray(mixed.round().clip(0, 255).astype("uint8"))
    y = y.filter(ImageFilter.UnsharpMask(
        radius=min(max(side / 700.0, 1.0), 2.5), percent=60, threshold=max(3, int(noise * 2))
    ))
    img = Image.merge("YCbCr", (y, cb, cr)).convert("RGB")
    # 5 : couleurs un peu plus vivantes
    return ImageEnhance.Color(img).enhance(ENHANCE_SATURATION)


# ---------------------------------------------------------------------------
# Looks (ambiances de couleur) : tables 3D (LUT), fichiers .cube ou looks integres
# ---------------------------------------------------------------------------
# Un look = une table de correspondance de couleurs appliquee a TOUTES les photos (rapide : calcul en C).
# On peut le creer dans Photoshop/Lightroom/DaVinci..., l'exporter en .cube (LUT 3D) et le charger dans l'appli.
LOOK_NONE = "Aucun"
LOOK_GRID = 33


def parse_cube(data: bytes):
    """Lit un fichier .cube (LUT 3D) et renvoie un ImageFilter.Color3DLUT. Leve ValueError si invalide."""
    size = None
    rows = []
    for raw in data.decode("utf-8", "ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        up = line.upper()
        if up.startswith("LUT_1D_SIZE"):
            raise ValueError("c'est une LUT 1D : exportez une LUT 3D (.cube)")
        if up.startswith("LUT_3D_SIZE"):
            size = int(line.split()[1])
            continue
        parts = line.split()
        if len(parts) == 3:
            try:
                rows.append([float(x) for x in parts])
            except ValueError:
                continue  # lignes d'en-tete (TITLE, DOMAIN_MIN...) ignorees
    if not size or size < 2 or size > 129:
        raise ValueError("taille de LUT 3D introuvable ou invalide")
    if len(rows) != size ** 3:
        raise ValueError(f"nombre de valeurs incorrect ({len(rows)} au lieu de {size ** 3})")
    table = np.clip(np.array(rows, dtype=np.float32), 0.0, 1.0)
    return ImageFilter.Color3DLUT(size, table.flatten().tolist())


def _lut_from_function(fn, size=LOOK_GRID):
    """Construit une LUT 3D a partir d'une fonction vectorisee (N,3) -> (N,3), valeurs 0-1."""
    g = np.linspace(0.0, 1.0, size, dtype=np.float32)
    b_, g_, r_ = np.meshgrid(g, g, g, indexing="ij")          # le rouge varie le plus vite, comme .cube
    rgb = np.stack([r_, g_, b_], axis=-1).reshape(-1, 3)
    out = np.clip(fn(rgb), 0.0, 1.0).astype(np.float32)
    return ImageFilter.Color3DLUT(size, out.flatten().tolist())


def _luma3(c):
    return (c[:, 0] * 0.299 + c[:, 1] * 0.587 + c[:, 2] * 0.114)[:, None]


def _saturate(c, factor):
    y = _luma3(c)
    return y + (c - y) * factor


def _s_curve(c, k):
    """Courbe en S (k>0 : plus de contraste, k<0 : plus doux), neutre aux extremes."""
    smooth = c * c * (3.0 - 2.0 * c)
    return c + k * (smooth - c)


def _look_eclat(c):
    return _saturate(_s_curve(c, 0.45), 1.22)


def _look_cinema(c):
    y = _luma3(c)
    out = _s_curve(c, 0.55)
    out = out + (1.0 - y) * np.array([-0.020, 0.012, 0.035], dtype=np.float32)   # ombres un peu bleutees
    out = out + y * np.array([0.035, 0.012, -0.030], dtype=np.float32)           # hautes lumieres chaudes
    return _saturate(out, 0.96)


def _look_doux(c):
    out = _s_curve(c, -0.25)                      # moins de contraste
    out = 0.04 + out * 0.94                       # noirs legerement releves, blancs retenus
    out = out + _luma3(c) * np.array([0.015, 0.005, -0.010], dtype=np.float32)   # chaleur discrete
    return _saturate(out, 1.05)


def _look_contraste(c):
    return _saturate(_s_curve(c, 0.85), 1.10)


BUILTIN_LOOK_FUNCTIONS = {
    "Éclat (couleurs vives)": _look_eclat,
    "Cinéma (ombres bleutées, lumières chaudes)": _look_cinema,
    "Doux lumineux (peau flatteuse)": _look_doux,
    "Contraste fort": _look_contraste,
}
BUILTIN_LOOK_NAMES = list(BUILTIN_LOOK_FUNCTIONS)
_builtin_cache = {}


def builtin_look(name):
    """LUT integree (calculee une seule fois), ou None."""
    fn = BUILTIN_LOOK_FUNCTIONS.get(name)
    if fn is None:
        return None
    if name not in _builtin_cache:
        _builtin_cache[name] = _lut_from_function(fn)
    return _builtin_cache[name]


def lift_shadows(img, amount):
    """Deboucher les ombres (amount 0-100) : utile quand la lumiere vient d'un seul cote."""
    a = max(0.0, min(float(amount), 100.0)) / 100.0
    if a <= 0:
        return img
    x = np.arange(256, dtype=np.float32) / 255.0
    lut = (x + a * 1.1 * x * (1.0 - x) ** 2).clip(0, 1)
    table = (lut * 255.0).round().astype("uint8").tolist()
    return img.convert("RGB").point(table * 3)


def skin_mask(img):
    """Masque doux (0-1) des zones de teinte 'peau' : repere par la couleur, sans detection de visage."""
    ycc = np.asarray(img.convert("YCbCr"), dtype=np.float32)
    y, cb, cr = ycc[..., 0], ycc[..., 1], ycc[..., 2]
    mask = ((cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 170) & (y > 45)).astype("uint8") * 255
    side = min(img.size)
    soft = Image.fromarray(mask).filter(ImageFilter.GaussianBlur(max(2.0, side / 90.0)))
    return np.asarray(soft, dtype=np.float32) / 255.0


def smooth_skin(img, amount):
    """
    Lissage LEGER de la peau (amount 0-100) : attenue les petits details (grain, imperfections)
    uniquement dans les zones de teinte peau, en gardant les contours nets (yeux, levres, cheveux).
    """
    a = max(0.0, min(float(amount), 100.0)) / 100.0
    if a <= 0:
        return img
    img = img.convert("RGB")
    side = min(img.size)
    base = np.asarray(img, dtype=np.float32)
    low = np.asarray(img.filter(ImageFilter.GaussianBlur(max(1.5, side / 250.0))), dtype=np.float32)
    detail = base - low
    mag = np.abs(detail).mean(axis=2, keepdims=True)
    keep_edges = np.exp(-((mag / 9.0) ** 2))               # 1 = petit detail (a lisser), 0 = contour fort (a garder)
    smoothed = low + detail * (1.0 - 0.75 * a * keep_edges)
    m = skin_mask(img)[..., None]
    out = base * (1.0 - m) + smoothed * m
    return Image.fromarray(out.clip(0, 255).astype("uint8"))


def extras_active(extras):
    if not extras:
        return False
    return bool(
        extras.get("shadows", 0) > 0
        or extras.get("skin", 0) > 0
        or (extras.get("look") is not None and extras.get("look_strength", 1.0) > 0)
    )


def apply_extras(img, extras):
    """Ombres deboucheees puis look, avec intensite reglable. `extras` : dict ou None."""
    if not extras_active(extras):
        return img
    img = img.convert("RGB")
    if extras.get("shadows", 0) > 0:
        img = lift_shadows(img, extras["shadows"])
    if extras.get("skin", 0) > 0:
        img = smooth_skin(img, extras["skin"])
    look = extras.get("look")
    strength = float(extras.get("look_strength", 1.0))
    if look is not None and strength > 0:
        graded = img.filter(look)
        img = graded if strength >= 1.0 else Image.blend(img, graded, strength)
    return img


def _hue_in_range(hue_deg, start, end):
    if start <= end:
        return (hue_deg >= start) & (hue_deg <= end)
    return (hue_deg >= start) | (hue_deg <= end)


def adjust_image(
    img,
    brightness=DEFAULT_BRIGHTNESS,
    contrast=DEFAULT_CONTRAST,
    temperature=DEFAULT_TEMPERATURE,
    tint=DEFAULT_TINT,
):
    """
    Retourne une copie reglee : balance des couleurs, puis luminosite, puis contraste.
    Tous les reglages sont en % (0 = inchange). Ne modifie pas l'original.
    """
    img = img.convert("RGB")
    if temperature or tint:
        # Gains par canal : +/-20 % au maximum (reglage +/-50)
        gains = np.array([
            1 + temperature / 250.0,   # rouge : monte quand on rechauffe
            1 - tint / 250.0,          # vert : baisse quand on va vers le magenta
            1 - temperature / 250.0,   # bleu : baisse quand on rechauffe
        ])
        a = np.array(img).astype(float) * gains
        img = Image.fromarray(a.clip(0, 255).astype("uint8"))
    if brightness:
        img = ImageEnhance.Brightness(img).enhance(max(0.0, 1 + brightness / 100.0))
    if contrast:
        img = ImageEnhance.Contrast(img).enhance(max(0.0, 1 + contrast / 100.0))
    return img


def apply_filter(
    img,
    filter_name=FILTER_NONE,
    keep_colors=None,
    sat_min=DEFAULT_SAT_MIN,
    brightness=DEFAULT_BRIGHTNESS,
    contrast=DEFAULT_CONTRAST,
    temperature=DEFAULT_TEMPERATURE,
    tint=DEFAULT_TINT,
    enhance=None,
    extras=None,
):
    """
    Retourne une copie reglee puis filtree de l'image PIL (RGB). Ne modifie pas l'original.
    `enhance` : reglages d'amelioration automatique (enhance_params) ou None.
    `extras` : ombres deboucheees + look (voir apply_extras) ou None.
    """
    if enhance:
        img = auto_enhance(img, enhance)
    img = adjust_image(img, brightness, contrast, temperature, tint)
    img = apply_extras(img, extras)
    if filter_name == FILTER_BW:
        return img.convert("L").convert("RGB")
    if filter_name == FILTER_SEPIA:
        a = np.array(img).astype(float)
        r = 0.393 * a[..., 0] + 0.769 * a[..., 1] + 0.189 * a[..., 2]
        g = 0.349 * a[..., 0] + 0.686 * a[..., 1] + 0.168 * a[..., 2]
        b = 0.272 * a[..., 0] + 0.534 * a[..., 1] + 0.131 * a[..., 2]
        out = np.stack([r, g, b], axis=-1).clip(0, 255).astype("uint8")
        return Image.fromarray(out)
    if filter_name == FILTER_BW_COLORS:
        keep_colors = keep_colors or []
        gray = img.convert("L").convert("RGB")
        if not keep_colors:
            return gray
        hsv = np.array(img.convert("HSV")).astype(float)
        hue = hsv[..., 0] * 360.0 / 255.0
        sat = hsv[..., 1] / 255.0
        val = hsv[..., 2] / 255.0
        in_hue = np.zeros(hue.shape, dtype=bool)
        for name in keep_colors:
            start, end = COLOR_HUES[name]
            in_hue |= _hue_in_range(hue, start, end)
        mask = (in_hue & (sat > sat_min) & (val > 0.15)).astype("uint8") * 255
        radius = max(1.0, min(img.size) / 400.0)
        mask = np.array(Image.fromarray(mask).filter(ImageFilter.GaussianBlur(radius)))
        m = mask.astype(float)[..., None] / 255.0
        out = np.array(gray).astype(float) * (1 - m) + np.array(img).astype(float) * m
        return Image.fromarray(out.clip(0, 255).astype("uint8"))
    return img


def apply_filter_to_files(
    image_paths,
    filter_name,
    keep_colors,
    sat_min,
    brightness=DEFAULT_BRIGHTNESS,
    contrast=DEFAULT_CONTRAST,
    temperature=DEFAULT_TEMPERATURE,
    tint=DEFAULT_TINT,
    auto_enhance_on=False,
    extras=None,
):
    """Cree des COPIES reglees/filtrees dans un dossier temporaire et renvoie leurs chemins."""
    if (
        not auto_enhance_on
        and not extras_active(extras)
        and filter_name == FILTER_NONE
        and not brightness and not contrast
        and not temperature and not tint
    ):
        return image_paths
    params = enhance_params(image_paths) if auto_enhance_on else None
    out_dir = mkdtemp()
    filtered = []
    for i, path in enumerate(image_paths):
        with Image.open(path) as im:
            res = apply_filter(
                im, filter_name, keep_colors, sat_min, brightness, contrast, temperature, tint,
                enhance=params, extras=extras,
            )
        fp = join(out_dir, f"filtered_{i}.jpg")
        res.save(fp, quality=95)
        filtered.append(fp)
    return filtered


def create_clip(
    image_paths: list[str],
    audio_path: str,
    bpm: float,
    duration: float,
    output_path: str,
    mute: bool = False,
    filter_name: str = FILTER_NONE,
    keep_colors=None,
    sat_min: float = DEFAULT_SAT_MIN,
    brightness: int = DEFAULT_BRIGHTNESS,
    contrast: int = DEFAULT_CONTRAST,
    temperature: int = DEFAULT_TEMPERATURE,
    tint: int = DEFAULT_TINT,
    auto_enhance_on: bool = False,
    reverse: bool = False,
    extras: dict = None,
    multiply: int = 0,
):
    center_crop_to_square(image_paths)
    # Reglages et filtre appliques sur des copies (les photos d'origine restent intactes)
    image_paths = apply_filter_to_files(
        image_paths, filter_name, keep_colors, sat_min, brightness, contrast, temperature, tint,
        auto_enhance_on=auto_enhance_on, extras=extras,
    )
    # Load audio
    audio = AudioFileClip(audio_path)
    # Musique plus courte que la video : on la fait boucler (sinon erreur de lecture)
    if audio.duration is not None and audio.duration < duration:
        audio = audio.with_effects([AudioLoop(duration=duration)])
    audio = audio.with_duration(duration)
    fps = bpm / 60
    # Loop or trim image sequence to match duration
    nb_frames = max(int(fps * duration), 1)
    # Le kaleidoscope 2x2 est calcule UNE fois par photo (et non a chaque image de la video) :
    # beaucoup plus rapide, resultat identique. Meme disposition qu'avant :
    #   [photo, rotation 270]
    #   [rotation 90, rotation 180]
    tiles = []
    for path in image_paths:
        with Image.open(path) as im:
            a = np.array(im.convert("RGB"))
        top = np.hstack([a, np.rot90(a, 3)])
        bottom = np.hstack([np.rot90(a, 1), np.rot90(a, 2)])
        tiles.append(np.ascontiguousarray(np.vstack([top, bottom])))
    # Demultiplication progressive : la video demarre en x4 (la photo 4 fois dans le carre, comme
    # d'habitude) puis le kaleidoscope est lui-meme repete : x16, puis x64 si demande, et on revient en x4.
    # La taille de la video ne change pas : les carres deviennent simplement plus petits.
    levels = [tiles]
    if multiply and multiply >= 16:
        side = (tiles[0].shape[0] // 8) * 8  # multiple de 8 pour que les repetitions tombent juste
        base = [np.array(Image.fromarray(t).resize((side, side), Image.LANCZOS)) for t in tiles]
        levels = [base]
        for grid in ((2, 4) if multiply >= 64 else (2,)):
            small = [
                np.ascontiguousarray(np.tile(
                    np.array(Image.fromarray(t).resize((side // grid, side // grid), Image.LANCZOS)),
                    (grid, grid, 1),
                ))
                for t in base
            ]
            levels.append(small)
    if reverse:
        # Montage a l'envers : derniere photo prise en premier. On garde un nombre ENTIER de
        # passages pour que la video commence sur la derniere photo et se termine sur la premiere.
        levels = [lvl[::-1] for lvl in levels]
        if nb_frames >= len(levels[0]):
            nb_frames = (nb_frames // len(levels[0])) * len(levels[0])
    n = len(levels[0])
    # Aller-retour : x4 -> x16 (-> x64) puis retour x16 -> x4. Sans demultiplication : un seul niveau.
    order = list(range(len(levels))) + list(range(len(levels) - 2, -1, -1))
    nb_stages = len(order)
    # Duree relative de chaque etape, d'apres la video de reference (32 s : x4 8 s, x16 8 s, x64 4 s,
    # x16 4 s, x4 8 s). Pour un aller-retour jusqu'a x16 : 1 / 2 / 1.
    weights = {1: [1], 3: [1, 2, 1], 5: [2, 2, 1, 1, 2]}[nb_stages]
    # Fin de chaque etape (duree exacte, pour tomber sur les temps de la musique)
    ends = []
    for k in range(1, nb_stages + 1):
        ends.append(int(round(nb_frames * sum(weights[:k]) / sum(weights))))
    ends[-1] = nb_frames
    frames = []
    stage = 0
    for i in range(nb_frames):
        while stage < nb_stages - 1 and i >= ends[stage]:
            stage += 1
        frames.append(levels[order[stage]][i % n])
    # Image par numero exact : ImageSequenceClip arrondit parfois vers le bas (2,4 s x 5 = 11,999...)
    # ce qui doublait une photo et en sautait une autre (et pouvait manquer la derniere image).
    nb_total = len(frames)
    clip = VideoClip(lambda t: frames[min(int(round(t * fps)), nb_total - 1)], duration=nb_total / fps)
    if reverse:
        audio = audio.with_duration(clip.duration)  # la musique s'arrete avec la derniere image
    # Attacher l'audio sauf en mode muet (rythme et duree restent calcules sur l'audio)
    if not mute:
        clip = clip.with_audio(audio)
    # Write file
    if mute:
        clip.write_videofile(
            output_path,
            fps=fps,
            codec="libx264",
            preset="veryfast",
            audio=False,
            write_logfile=False,
        )
    else:
        clip.write_videofile(
            output_path,
            fps=fps,
            codec="libx264",
            preset="veryfast",
            audio_codec="aac",
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            write_logfile=False,
        )
    return output_path


def center_crop_to_square(image_paths: list[str]) -> None:
    """
    Loads images from the given paths, center-crops them to square images
    of the largest possible common size (no padding), and overwrites the originals.

    Parameters
    ----------
    image_paths : list[str]
        List of paths to image files.

    Behavior
    --------
    - Finds the minimum side length among all images.
    - Crops all images to that square size, centered.
    - Overwrites the original files.
    """
    if not image_paths:
        raise ValueError("No image paths provided.")
    
    # Step 1: Load all images and determine min dimension
    images = []
    min_side = None

    for path in image_paths:
        img = Image.open(path)
        images.append((path, img))
        width, height = img.size
        smallest_side = min(width, height)
        if min_side is None or smallest_side < min_side:
            min_side = smallest_side
        print(path, "width", width, "height", height, "smallest side", smallest_side)

    # Step 2: Crop and overwrite each image
    for path, img in images:
        width, height = img.size
        left = (width - min_side) // 2
        top = (height - min_side) // 2
        right = left + min_side
        bottom = top + min_side

        cropped_img = img.crop((left, top, right, bottom))
        cropped_img.save(path)
        img.close()

if __name__ == "__main__":
    if len(argv) < 2:
        print("Please provide a path to the directory of images to crop.")
        exit(1)
    folder_path = argv[1]
    print("Duplicating", folder_path, "and cropping images to biggest common square in copy.")
    cropped_folder_path = folder_path + "_corpped"
    copytree(folder_path, cropped_folder_path, dirs_exist_ok=True)
    images_to_crop = [join(cropped_folder_path, path) for path in listdir(cropped_folder_path)]
    # print("images_to_crop:", images_to_crop)
    center_crop_to_square(images_to_crop)
