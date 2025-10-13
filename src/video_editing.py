from sys import argv
from PIL import Image
from os import listdir
from os.path import join
from shutil import copytree

from moviepy.video.fx import Rotate, Crop
from moviepy import Clip, ImageSequenceClip, AudioFileClip, clips_array


def create_clip(
    image_paths: list[str],
    audio_path: str,
    bpm: float,
    duration: float,
    output_path: str
):
    center_crop_to_square(image_paths)
    # Load audio
    audio = AudioFileClip(audio_path).with_duration(duration)
    fps = bpm / 60
    # Loop or trim image sequence to match duration
    nb_frames = max(int(fps * duration), 1)
    paths = [image_paths[i % len(image_paths)] for i in range(nb_frames)]
    # Build the video
    clip = ImageSequenceClip(paths, fps=fps)
    # clip = crop_clip_to_square(clip)
    clip = (
        clips_array([
            [clip, clip.with_effects([Rotate(270)])],
            [clip.with_effects([Rotate(90)]), clip.with_effects([Rotate(180)])],
        ])
        .with_audio(audio)
    )
    # Write file
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
