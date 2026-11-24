"""High-retention animated and static graphics generator for long-form episodes.
Produces spec cards, comparison split-screens, timelines, stat counters,
telemetry HUDs, countdown number cards, and chapter title cards in 1080p.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from PIL import Image, ImageDraw, ImageFont


def _get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Safely fetch system font or default bitmap font."""
    font_names = [
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "segui_sb.ttf" if bold else "segoeui.ttf",
        "Helvetica-Bold.ttf" if bold else "Helvetica.ttf",
    ]
    for fn in font_names:
        try:
            return ImageFont.truetype(fn, size)
        except Exception:
            continue
    return ImageFont.load_default()


def create_chapter_title_card(
    chapter_num: int,
    chapter_title: str,
    brand: str,
    model: str,
    output_path: Path
) -> Path:
    """Create a sleek, high-impact 1080p title card for chapter transitions (1.5s)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1920, 1080), color=(8, 11, 16))
    draw = ImageDraw.Draw(img)

    # Dark cinematic vignette & technical grid
    for x in range(0, 1920, 160):
        draw.line([(x, 0), (x, 1080)], fill=(16, 22, 32), width=1)
    for y in range(0, 1080, 160):
        draw.line([(0, y), (1920, y)], fill=(16, 22, 32), width=1)

    # Accent neon bar
    accent_color = (0, 195, 255)
    draw.rectangle([140, 360, 150, 720], fill=accent_color)

    font_label = _get_font(28, bold=True)
    font_num = _get_font(42, bold=True)
    font_title = _get_font(72, bold=True)
    font_sub = _get_font(32, bold=False)

    draw.text((180, 360), f"ZENITH MOTORS DOCUMENTARY // CHAPTER {chapter_num:02d}", fill=accent_color, font=font_label)
    draw.text((180, 420), chapter_title.upper()[:48], fill=(255, 255, 255), font=font_title)
    draw.text((180, 530), f"SUBJECT: {brand.upper()} {model.upper()}", fill=(180, 195, 215), font=font_sub)

    # Bottom technical telemetry line
    draw.line([(180, 680), (1740, 680)], fill=(35, 50, 75), width=2)
    draw.text((180, 700), "TELEMETRIC ANALYSIS • PROPULSION DYNAMICS • STRUCTURAL AERODYNAMICS", fill=(100, 125, 155), font=font_label)

    img.save(output_path, "JPEG", quality=92)
    return output_path


def create_spec_card_graphic(
    brand: str,
    model: str,
    specs: list[tuple[str, str]],
    output_path: Path
) -> Path:
    """Create a high-tech 1080p automotive specification card."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1920, 1080), color=(10, 14, 20))
    draw = ImageDraw.Draw(img)

    # Outer futuristic card boundary
    draw.rectangle([100, 100, 1820, 980], outline=(0, 180, 240), width=3)
    draw.rectangle([120, 120, 1800, 960], outline=(30, 45, 65), width=1)

    font_header = _get_font(52, bold=True)
    font_spec_val = _get_font(60, bold=True)
    font_spec_lbl = _get_font(26, bold=False)

    # Header
    draw.text((160, 150), f"{brand.upper()} {model.upper()}", fill=(255, 255, 255), font=font_header)
    draw.text((160, 220), "OFFICIAL BENCHMARK SPECIFICATIONS & POWERTRAIN ARCHITECTURE", fill=(0, 210, 255), font=font_spec_lbl)
    draw.line([(160, 260), (1760, 260)], fill=(35, 50, 75), width=2)

    # Spec grid (up to 4 items in 2x2 layout)
    card_boxes = [
        (160, 310, 920, 580),
        (1000, 310, 1760, 580),
        (160, 640, 920, 910),
        (1000, 640, 1760, 910),
    ]

    for idx, (label, val) in enumerate(specs[:4]):
        bx1, by1, bx2, by2 = card_boxes[idx]
        draw.rectangle([bx1, by1, bx2, by2], fill=(16, 23, 34), outline=(40, 60, 85), width=2)
        # Accent corner
        draw.rectangle([bx1, by1, bx1 + 10, by2], fill=(255, 190, 0))
        draw.text((bx1 + 35, by1 + 40), label.upper(), fill=(140, 165, 195), font=font_spec_lbl)
        draw.text((bx1 + 35, by1 + 110), str(val), fill=(255, 255, 255), font=font_spec_val)

    img.save(output_path, "JPEG", quality=92)
    return output_path


def create_barchart_graphic(
    metric_title: str,
    brand_a: str,
    model_a: str,
    val_a: float,
    label_a: str,
    brand_b: str,
    model_b: str,
    val_b: float,
    label_b: str,
    output_path: Path
) -> Path:
    """Create a side-by-side bar chart comparison card."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1920, 1080), color=(9, 12, 18))
    draw = ImageDraw.Draw(img)

    font_title = _get_font(48, bold=True)
    font_sub = _get_font(26, bold=False)
    font_name = _get_font(38, bold=True)
    font_num = _get_font(56, bold=True)

    draw.text((140, 120), "TELEMETRIC BENCHMARK COMPARISON", fill=(0, 200, 255), font=font_sub)
    draw.text((140, 170), metric_title.upper(), fill=(255, 255, 255), font=font_title)
    draw.line([(140, 250), (1780, 250)], fill=(35, 50, 75), width=2)

    # Bar comparison
    max_val = max(val_a, val_b, 1.0)
    bar_max_width = 1100

    # Entity A
    draw.text((140, 360), f"{brand_a.upper()} {model_a.upper()}", fill=(240, 245, 255), font=font_name)
    width_a = int((val_a / max_val) * bar_max_width)
    draw.rectangle([140, 430, 140 + width_a, 520], fill=(0, 170, 255))
    draw.text((160 + width_a, 440), label_a, fill=(0, 210, 255), font=font_num)

    # Entity B
    draw.text((140, 630), f"{brand_b.upper()} {model_b.upper()}", fill=(240, 245, 255), font=font_name)
    width_b = int((val_b / max_val) * bar_max_width)
    draw.rectangle([140, 700, 140 + width_b, 790], fill=(255, 70, 70))
    draw.text((160 + width_b, 710), label_b, fill=(255, 100, 100), font=font_num)

    img.save(output_path, "JPEG", quality=92)
    return output_path


def create_split_screen_graphic(
    left_image_path: Path | None,
    right_image_path: Path | None,
    left_label: str,
    right_label: str,
    output_path: Path
) -> Path:
    """Create a modern 50/50 dual split-screen card."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (1920, 1080), color=(10, 14, 20))

    half_w = 955  # with 10px divider
    h = 1080

    for idx, (p, label, offset_x) in enumerate([(left_image_path, left_label, 0), (right_image_path, right_label, 965)]):
        if p and p.exists():
            try:
                src = Image.open(p).convert("RGB")
                # Aspect crop to fit half screen
                src_ratio = src.width / src.height
                tgt_ratio = half_w / h
                if src_ratio > tgt_ratio:
                    new_w = int(h * src_ratio)
                    scaled = src.resize((new_w, h), Image.Resampling.LANCZOS)
                    crop_x = (new_w - half_w) // 2
                    cropped = scaled.crop((crop_x, 0, crop_x + half_w, h))
                else:
                    new_h = int(half_w / src_ratio)
                    scaled = src.resize((half_w, new_h), Image.Resampling.LANCZOS)
                    crop_y = (new_h - h) // 2
                    cropped = scaled.crop((0, crop_y, half_w, crop_y + h))
                canvas.paste(cropped, (offset_x, 0))
            except Exception:
                pass

    draw = ImageDraw.Draw(canvas)
    # Center divider bar
    draw.rectangle([955, 0, 965, 1080], fill=(0, 200, 255))

    font_tag = _get_font(36, bold=True)
    # Overlay label banners
    draw.rectangle([40, 940, 500, 1020], fill=(0, 0, 0, 180))
    draw.rectangle([40, 940, 50, 1020], fill=(0, 200, 255))
    draw.text((70, 955), left_label[:28].upper(), fill=(255, 255, 255), font=font_tag)

    draw.rectangle([1005, 940, 1465, 1020], fill=(0, 0, 0, 180))
    draw.rectangle([1005, 940, 1015, 1020], fill=(255, 80, 80))
    draw.text((1035, 955), right_label[:28].upper(), fill=(255, 255, 255), font=font_tag)

    canvas.save(output_path, "JPEG", quality=92)
    return output_path


def create_countdown_card(
    rank_number: int,
    headline: str,
    subtext: str,
    output_path: Path
) -> Path:
    """Create a dramatic numbered countdown card (#10 to #1) for countdown episodes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1920, 1080), color=(7, 9, 14))
    draw = ImageDraw.Draw(img)

    font_giant = _get_font(260, bold=True)
    font_head = _get_font(64, bold=True)
    font_sub = _get_font(34, bold=False)

    # Giant rank number in the background
    num_str = f"#{rank_number:02d}"
    draw.text((120, 320), num_str, fill=(25, 35, 50), font=font_giant)

    # Foreground content
    accent = (255, 190, 0) if rank_number <= 3 else (0, 210, 255)
    draw.rectangle([140, 300, 150, 680], fill=accent)
    draw.text((180, 360), f"COUNTDOWN POSITION {rank_number}", fill=accent, font=_get_font(28, bold=True))
    draw.text((180, 430), headline.upper()[:45], fill=(255, 255, 255), font=font_head)
    draw.text((180, 550), subtext[:75], fill=(185, 200, 220), font=font_sub)

    # Progress bar along bottom
    total_ranks = 10
    pct = (total_ranks - rank_number + 1) / total_ranks
    draw.rectangle([180, 720, 1740, 735], fill=(25, 35, 50))
    draw.rectangle([180, 720, int(180 + (1740 - 180) * pct), 735], fill=accent)

    img.save(output_path, "JPEG", quality=92)
    return output_path


def create_kinetic_fact_card(
    big_stat: str,
    stat_label: str,
    context_sentence: str,
    output_path: Path
) -> Path:
    """Create a high-impact kinetic stat graphic highlighting key numbers (HP, top speed, price)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1920, 1080), color=(10, 14, 22))
    draw = ImageDraw.Draw(img)

    font_stat = _get_font(130, bold=True)
    font_lbl = _get_font(38, bold=True)
    font_ctx = _get_font(32, bold=False)

    # Center box
    draw.rectangle([160, 240, 1760, 840], fill=(16, 24, 38), outline=(0, 190, 250), width=2)
    draw.rectangle([160, 240, 1760, 255], fill=(0, 200, 255))

    draw.text((220, 320), big_stat, fill=(255, 210, 0), font=font_stat)
    draw.text((220, 510), stat_label.upper(), fill=(255, 255, 255), font=font_lbl)
    draw.text((220, 600), context_sentence[:85], fill=(180, 195, 220), font=font_ctx)

    img.save(output_path, "JPEG", quality=92)
    return output_path
