from pathlib import Path
from PIL import Image

SRC_DIR = Path("portal/static/portal/img/app-icons")
DST_DIR = Path("portal/static/portal/img/app-icons-normalized")

DST_DIR.mkdir(parents=True, exist_ok=True)

FILES = [
    "library.png",
    "hospital-orders.png",
    "factory-purchase.png",
    "workflow.png",
    "documents.png",
    "finance.png",
    "invoice.png",
]

CANVAS_SIZE = 512
TARGET_SIZE = 400
WHITE_THRESHOLD = 246


def remove_white_background(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    pixels = []

    for r, g, b, a in img.getdata():
        if a < 10:
            pixels.append((r, g, b, 0))
        elif r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
            pixels.append((255, 255, 255, 0))
        else:
            pixels.append((r, g, b, a))

    img.putdata(pixels)
    return img


def normalize_icon(filename: str):
    src = SRC_DIR / filename
    dst = DST_DIR / filename

    if not src.exists():
        print(f"[SKIP] Missing: {src}")
        return

    img = Image.open(src)
    img = remove_white_background(img)

    bbox = img.getbbox()
    if not bbox:
        print(f"[SKIP] Empty image: {src}")
        return

    icon = img.crop(bbox)

    width, height = icon.size
    scale = TARGET_SIZE / max(width, height)
    new_width = int(width * scale)
    new_height = int(height * scale)

    icon = icon.resize((new_width, new_height), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (255, 255, 255, 0))

    x = (CANVAS_SIZE - new_width) // 2
    y = (CANVAS_SIZE - new_height) // 2

    canvas.alpha_composite(icon, (x, y))
    canvas.save(dst)

    print(f"[OK] {filename} -> {dst}")


def main():
    for filename in FILES:
        normalize_icon(filename)


if __name__ == "__main__":
    main()
