"""High-impact 1280x720 YouTube thumbnail generator using Pillow.
Generates two high-contrast variants (Mystery vs Spec/Speed) and selects
the variant with the highest visual contrast and clickability score.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat

ROOT = Path(__file__).resolve().parent.parent.parent
THUMBNAIL_DIR = ROOT / "data" / "longform_thumbnails"


def _get_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Load bold high-contrast font from Windows or Linux system paths with default fallback."""
    font_candidates = [
        # Windows
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for p in font_candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _calculate_image_contrast(image: Image.Image) -> float:
    """Calculate RMS contrast of image for automated A/B selection."""
    stat = ImageStat.Stat(image.convert("L"))
    return float(stat.stddev[0])


def generate_thumbnail(
    brand: str,
    model: str,
    format_name: str,
    car_image_path: Path | None,
    episode_id: str
) -> Path:
    """Generate two 1280x720 thumbnail variants and return path to the strongest variant."""
    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    out_a = THUMBNAIL_DIR / f"{episode_id}_thumb_varA.jpg"
    out_b = THUMBNAIL_DIR / f"{episode_id}_thumb_varB.jpg"
    final_out = THUMBNAIL_DIR / f"{episode_id}_thumb.jpg"

    font_large = _get_font(96)
    font_badge = _get_font(44)
    font_small = _get_font(52)

    # -------------------------------------------------------------
    # VARIANT A: Mystery / Scandal Hook Style ("THE FORBIDDEN SECRET")
    # -------------------------------------------------------------
    img_a = Image.new("RGB", (1280, 720), color=(10, 14, 22))
    draw_a = ImageDraw.Draw(img_a)

    # Backdrop car
    if car_image_path and car_image_path.exists():
        try:
            car_img = Image.open(car_image_path).convert("RGBA")
            car_img.thumbnail((1100, 650), Image.Resampling.LANCZOS)
            # Position car bottom right
            cw, ch = car_img.size
            img_a.paste(car_img, (1280 - cw + 80, 720 - ch + 40), car_img)
        except Exception:
            pass

    # High-contrast dark vignette overlay on left for typography readability
    vignette = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    v_draw = ImageDraw.Draw(vignette)
    for x in range(850):
        alpha = int(220 * (1.0 - (x / 850.0)))
        v_draw.line([(x, 0), (x, 720)], fill=(0, 0, 0, alpha))
    img_a = Image.alpha_composite(img_a.convert("RGBA"), vignette).convert("RGB")
    draw_a = ImageDraw.Draw(img_a)

    # Badge: High-contrast red/yellow warning tag
    draw_a.rectangle([50, 45, 360, 105], fill=(225, 20, 20), outline=(255, 255, 255), width=2)
    draw_a.text((70, 52), "THE SECRET MANDATE", fill=(255, 255, 255), font=font_badge)

    # Main hook text with black stroke outline
    line1 = f"{brand.upper()} {model.upper()}"
    line2 = "WHY IT WAS BANNED"
    draw_a.text((50, 130), line1[:18], fill=(255, 220, 0), font=font_large, stroke_width=6, stroke_fill=(0, 0, 0))
    draw_a.text((50, 240), line2, fill=(255, 255, 255), font=font_large, stroke_width=6, stroke_fill=(0, 0, 0))

    img_a.save(out_a, "JPEG", quality=95)

    # -------------------------------------------------------------
    # VARIANT B: Telemetry & Extreme Engineering ("SHATTERING PHYSICS")
    # -------------------------------------------------------------
    img_b = Image.new("RGB", (1280, 720), color=(18, 12, 10))
    draw_b = ImageDraw.Draw(img_b)

    # Backdrop car with slight contrast boost
    if car_image_path and car_image_path.exists():
        try:
            car_img_b = Image.open(car_image_path).convert("RGBA")
            enh = ImageEnhance.Contrast(car_img_b.convert("RGB"))
            car_img_b = enh.enhance(1.25).convert("RGBA")
            car_img_b.thumbnail((1150, 680), Image.Resampling.LANCZOS)
            cw, ch = car_img_b.size
            img_b.paste(car_img_b, (1280 - cw + 60, 720 - ch + 20), car_img_b)
        except Exception:
            pass

    # Left-side dark gradient
    vignette_b = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    vb_draw = ImageDraw.Draw(vignette_b)
    for x in range(850):
        alpha = int(230 * (1.0 - (x / 850.0)))
        vb_draw.line([(x, 0), (x, 720)], fill=(10, 5, 5, alpha))
    img_b = Image.alpha_composite(img_b.convert("RGBA"), vignette_b).convert("RGB")
    draw_b = ImageDraw.Draw(img_b)

    # Badge: High-voltage cyan/gold tag
    draw_b.rectangle([50, 45, 380, 105], fill=(240, 140, 10), outline=(255, 255, 255), width=2)
    draw_b.text((70, 52), "ENGINEERING DISASTER", fill=(0, 0, 0), font=font_badge)

    line1_b = f"{model.upper()}"
    line2_b = "THE $6M TRUTH"
    draw_b.text((50, 130), line1_b[:18], fill=(0, 240, 255), font=font_large, stroke_width=6, stroke_fill=(0, 0, 0))
    draw_b.text((50, 240), line2_b, fill=(255, 255, 255), font=font_large, stroke_width=6, stroke_fill=(0, 0, 0))

    img_b.save(out_b, "JPEG", quality=95)

    # Automated A/B Evaluation: pick variant with highest RMS contrast score
    contrast_a = _calculate_image_contrast(img_a)
    contrast_b = _calculate_image_contrast(img_b)

    if contrast_b > contrast_a:
        chosen_variant = out_b
        print(f"  [Thumbnail] Variant B chosen (Contrast Score: {contrast_b:.1f} vs {contrast_a:.1f})")
    else:
        chosen_variant = out_a
        print(f"  [Thumbnail] Variant A chosen (Contrast Score: {contrast_a:.1f} vs {contrast_b:.1f})")

    # Save to final output
    import shutil
    shutil.copyfile(chosen_variant, final_out)
    return final_out
