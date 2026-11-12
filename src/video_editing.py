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
):
    """Retourne une copie reglee puis filtree de l'image PIL (RGB). Ne modifie pas l'original."""
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
):
    """Cree des COPIES reglees/filtrees dans un dossier temporaire et renvoie leurs chemins."""
    if (
        filter_name == FILTER_NONE
        and not brightness and not contrast
        and not temperature and not tint
    ):
        return image_paths
    out_dir = mkdtemp()
    filtered = []
    for i, path in enumerate(image_paths):
        with Image.open(path) as im:
            res = apply_filter(
                im, filter_name, keep_colors, sat_min, brightness, contrast, temperature, tint
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
):
    center_crop_to_square(image_paths)
    # Reglages et filtre appliques sur des copies (les photos d'origine restent intactes)
    image_paths = apply_filter_to_files(
        image_paths, filter_name, keep_colors, sat_min, brightness, contrast, temperature, tint
    )
    # Load audio
    audio = AudioFileClip(audio_path).with_duration(duration)
    fps = bpm / 60
    # Loop or trim image sequence to match duration
    nb_frames = max(int(fps * duration), 1)
    paths = [image_paths[i % len(image_paths)] for i in range(nb_frames)]
    # Build the video
    clip = ImageSequenceClip(paths, fps=fps)
    # clip = crop_clip_to_square(clip)
    clip = clips_array([
        [clip, clip.with_effects([Rotate(270)])],
        [clip.with_effects([Rotate(90)]), clip.with_effects([Rotate(180)])],
    ])
    # Attacher l'audio sauf en mode muet (rythme et duree restent calcules sur l'audio)
    if not mute:
        clip = clip.with_audio(audio)
    # Write file
    if mute:
        clip.write_videofile(
            output_path,
            fps=fps,
            codec="libx264",
            audio=False,
            write_logfile=False,
        )
    else:
        clip.write_videofile(
            output_path,
            fps=fps,
            codec="libx264",
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
