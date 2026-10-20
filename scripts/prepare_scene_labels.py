"""Create local contact sheets and a simple CSV for human scene labels."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


def prepare(roots: list[Path], output: Path) -> int:
    photos = sorted(path for root in roots for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"})
    output.mkdir(parents=True, exist_ok=True)
    with (output / "场景标签.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(("编号", "path", "scene"))
        writer.writerows((index, str(photo), "") for index, photo in enumerate(photos, 1))
    columns, cell_width, cell_height, per_page = 5, 260, 230, 25
    for page_start in range(0, len(photos), per_page):
        page_photos = photos[page_start:page_start + per_page]
        rows = (len(page_photos) + columns - 1) // columns
        canvas = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
        draw = ImageDraw.Draw(canvas)
        for offset, photo in enumerate(page_photos):
            number = page_start + offset + 1
            x = (offset % columns) * cell_width
            y = (offset // columns) * cell_height
            with Image.open(photo) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((cell_width - 12, cell_height - 42))
                canvas.paste(image, (x + (cell_width - image.width) // 2, y + 4))
            draw.text((x + 6, y + cell_height - 34), f"{number:02d}  {photo.name[:28]}", fill="black")
        first = page_start + 1
        last = page_start + len(page_photos)
        canvas.save(output / f"场景总览_{first:02d}-{last:02d}.jpg", quality=90)
    return len(photos)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"prepared {prepare(args.input, args.output)} photos in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
