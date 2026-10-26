# /// script
# requires-python = "==3.14.8"
# dependencies = ["cairosvg>=2.8,<3", "pillow>=12,<13"]
# ///
"""Render TGDrive's checked-in SVG masters: uv run --script scripts/brand-assets.py.

The approved Interlock logo is outlined in the SVG masters; rendering never
depends on installed fonts or the original image-generation output.
"""

from io import BytesIO
from pathlib import Path

import cairosvg
from PIL import Image

BRAND = Path(__file__).resolve().parents[1] / "website/public/static/brand"
RESAMPLE = Image.Resampling.LANCZOS


def render(name: str, width: int, height: int | None = None) -> Image.Image:
    """Supersample curved edges, including the 16px browser-tab icon."""
    options = {"output_width": width * 4}
    if height is not None:
        options["output_height"] = height * 4
    png = cairosvg.svg2png(url=str(BRAND / name), **options)
    with Image.open(BytesIO(png)) as source:
        target_height = height or round(source.height / 4)
        return source.convert("RGBA").resize((width, target_height), RESAMPLE)


def save(image: Image.Image, name: str) -> None:
    image.save(BRAND / name, optimize=True)


def white_matte(image: Image.Image) -> Image.Image:
    background = Image.new("RGBA", image.size, "white")
    background.alpha_composite(image)
    return background.convert("RGB")


def main() -> None:
    icon_source = (BRAND / "tgdrive-icon.svg").read_bytes()
    (BRAND / "favicon.svg").write_bytes(icon_source)

    for name in ("tgdrive-icon", "tgdrive-logo", "tgdrive-logo-dark"):
        width = 512 if name == "tgdrive-icon" else 2010
        save(render(f"{name}.svg", width), f"{name}.png")

    for size in (16, 32, 48):
        save(render("favicon.svg", size, size), f"favicon-{size}.png")
    render("favicon.svg", 256, 256).save(
        BRAND / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)]
    )

    for size in (192, 512):
        icon = white_matte(render("tgdrive-icon.svg", size, size))
        save(icon, f"app-icon-{size}.png")
    save(white_matte(render("tgdrive-icon.svg", 180, 180)), "apple-touch-icon.png")

    preview = Image.new("RGBA", (1200, 630), "white")
    logo = render("tgdrive-logo.svg", 980)
    preview.alpha_composite(logo, ((1200 - logo.width) // 2, (630 - logo.height) // 2))
    save(preview.convert("RGB"), "social-preview.png")
    print(f"Rendered TGDrive brand assets in {BRAND}")


if __name__ == "__main__":
    main()
