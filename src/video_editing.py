from sys import argv
from PIL import Image
from os import listdir
from os.path import join
from shutil import copytree
from tempfile import mkdtemp

import numpy as np
from PIL import ImageFilter, ImageEnhance

from moviepy.video.fx import Rotate, Crop
from moviepy import Clip, ImageSequenceClip, AudioFileClip, clips_array
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
):
    """
    Retourne une copie reglee puis filtree de l'image PIL (RGB). Ne modifie pas l'original.
    `enhance` : reglages d'amelioration automatique (enhance_params) ou None.
    """
    if enhance:
        img = auto_enhance(img, enhance)
    img = adjust_image(img, brightness, contrast, temperature, tint)
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
):
    """Cree des COPIES reglees/filtrees dans un dossier temporaire et renvoie leurs chemins."""
    if (
        not auto_enhance_on
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
                enhance=params,
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
):
    center_crop_to_square(image_paths)
    # Reglages et filtre appliques sur des copies (les photos d'origine restent intactes)
    image_paths = apply_filter_to_files(
        image_paths, filter_name, keep_colors, sat_min, brightness, contrast, temperature, tint,
        auto_enhance_on=auto_enhance_on,
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
    frames = [tiles[i % len(tiles)] for i in range(nb_frames)]
    clip = ImageSequenceClip(frames, fps=fps)
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
