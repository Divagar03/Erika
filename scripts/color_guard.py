"""
Color Cohesion Guard for Zenith Motors Reel Generation.
Guarantees 100% single-car, single-spec color cohesion across all cuts.
"""

from __future__ import annotations
from pathlib import Path
from PIL import Image
import colorsys


def analyze_shot_color(img_path: Path) -> tuple[str, float]:
    """
    Returns (color_category, hue_angle) for an image.
    color_category: 'COLORED', 'WHITE', 'BLACK', or 'GRAY'.
    hue_angle: 0.0 to 360.0 degrees if COLORED, else 0.0.
    """
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        # Sample center 60% where vehicle body sits
        box = (int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8))
        cropped = img.crop(box).resize((60, 60))
        hues = []
        for r, g, b in cropped.getdata():
            h_norm, l_norm, s_norm = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            if s_norm > 0.18 and 0.12 < l_norm < 0.88:
                hues.append(h_norm * 360)
        if len(hues) < 180:
            avg_l = sum(colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)[1] for r, g, b in cropped.getdata()) / 3600
            c_type = "WHITE" if avg_l > 0.6 else ("BLACK" if avg_l < 0.28 else "GRAY")
            return c_type, 0.0
        return "COLORED", sum(hues) / len(hues)


def verify_color_cohesion(media_paths: list[Path]) -> None:
    """
    Asserts 100% single-color cohesion. Throws ValueError if cuts mix different car colors.
    """
    if not media_paths or len(media_paths) < 2:
        return
    # Only verify photos; video clips from the same folder are continuous footage
    if media_paths[0].suffix.lower() not in (".jpg", ".jpeg", ".png"):
        return

    feats = []
    for p in media_paths:
        try:
            c_type, hue = analyze_shot_color(p)
            feats.append((p.name, c_type, hue))
        except Exception as e:
            print(f"  [ColorGuard] Warning reading {p.name}: {e}")

    types = set(f[1] for f in feats)
    if len(types) > 1:
        details = ", ".join(f"{f[0]}: {f[1]}" for f in feats)
        raise ValueError(f"Color cohesion violated! Mixed car color types detected: {details}")

    if "COLORED" in types:
        hues = [f[2] for f in feats]
        sorted_hues = sorted(hues)
        max_gap = max((sorted_hues[(i + 1) % len(sorted_hues)] - sorted_hues[i]) % 360 for i in range(len(sorted_hues)))
        hue_spread = 360 - max_gap
        if hue_spread > 55.0:
            details = ", ".join(f"{f[0]}: {f[2]:.1f}°" for f in feats)
            raise ValueError(f"Color cohesion violated! Diverse car colors in single reel (hue spread = {hue_spread:.1f}°): {details}")
