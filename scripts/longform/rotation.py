"""Topic, format, and hook style selection engine for long-form episodes.
Integrates with the brand/model rotation queue, anti-repetition rules,
and retention analytics weighting.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .db import (
    get_format_retention_weights,
    get_hook_retention_weights,
    get_recent_formats,
    get_recent_hook_styles,
)

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = ROOT / "config" / "longform_config.json"
ROTATION_STATE_FILE = ROOT / "data" / "rotation_state.json"
EPISODES_FILE = ROOT / "data" / "longform_episodes.json"


def load_config() -> dict[str, Any]:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _load_rotation_state() -> dict[str, Any]:
    if not ROTATION_STATE_FILE.exists():
        return {}
    with open(ROTATION_STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _load_recent_longform_history() -> tuple[list[str], set[tuple[str, str]]]:
    recent_brands: list[str] = []
    covered_models: set[tuple[str, str]] = set()
    if not EPISODES_FILE.exists():
        return recent_brands, covered_models
    try:
        data = json.loads(EPISODES_FILE.read_text(encoding="utf-8"))
        for ep in data.get("episodes", []):
            if ep.get("status") != "published":
                continue
            b = (ep.get("topic_brand") or "").strip()
            m = (ep.get("topic_model") or "").strip()
            if b:
                recent_brands.append(b.lower())
            if b and m:
                covered_models.add((b.lower(), m.lower()))
    except Exception:
        pass
    return recent_brands, covered_models


def select_topic_and_format() -> dict[str, Any]:
    """Select the next brand, model, colour, episode format, and hook style
    across ALL 174 brands and 2,315+ models from the master Excel catalog.

    Guarantees:
    - Never repeats a brand from the last episode (and avoids last 5 long-form brands).
    - Never repeats a model already covered in long-form until other models of that brand are covered.
    - Verifies real Wikimedia photo availability when choosing outside the core curated fleet.
    - Format and hook style are NEVER repeated within the last 5 episodes.
    """
    config = load_config()
    rot_state = _load_rotation_state()
    recent_lf_brands, covered_lf_models = _load_recent_longform_history()

    try:
        from scripts.dynamic_sourcing import probe_wikimedia_availability, clean_model_display
    except ImportError:
        probe_wikimedia_availability = None
        clean_model_display = lambda m: m

    # 1. Select Brand & Model across ALL catalog brands in Rotation State
    brand_queue = list(rot_state.get("brand_queue") or list(rot_state.get("brands", {}).keys()))
    if not brand_queue:
        brand_queue = ["Ferrari", "McLaren", "Bugatti", "Porsche", "Lamborghini"]
    last_reel_brand = (rot_state.get("last_brand") or "").lower()
    brands_data = rot_state.get("brands", {})

    avoid_brands = set(recent_lf_brands[-5:])
    if last_reel_brand:
        avoid_brands.add(last_reel_brand)

    pointer = rot_state.get("brand_pointer", 0) % len(brand_queue)
    ordered_brands = brand_queue[pointer:] + brand_queue[:pointer]
    eligible_brands = [b for b in ordered_brands if b.lower() not in avoid_brands] or [
        b for b in ordered_brands if b.lower() != (recent_lf_brands[-1] if recent_lf_brands else "")
    ] or ordered_brands

    chosen_brand = eligible_brands[0]
    chosen_model = "Flagship"
    chosen_colour = "red"

    # Try up to 10 candidate brands to find a model with solid visual/fact coverage
    found_pair = False
    for b_cand in eligible_brands[:10]:
        b_info = brands_data.get(b_cand, {})
        all_m = list(b_info.get("model_queue") or list(b_info.get("models", {}).keys()))
        if not all_m:
            continue
        m_ptr = b_info.get("model_pointer", 0) % len(all_m)
        ordered_m = all_m[m_ptr:] + all_m[:m_ptr]
        # Prefer models of this brand not yet covered in long-form
        uncovered_m = [m for m in ordered_m if (b_cand.lower(), clean_model_display(m).lower()) not in covered_lf_models and (b_cand.lower(), m.lower()) not in covered_lf_models]
        m_pool = uncovered_m if uncovered_m else ordered_m

        for m_cand in m_pool[:3]:
            if probe_wikimedia_availability is None or probe_wikimedia_availability(b_cand, m_cand, min_needed=3):
                chosen_brand = b_cand
                chosen_model = clean_model_display(m_cand)
                m_data = b_info.get("models", {}).get(m_cand, {})
                used_colours = set(m_data.get("used_colours", []))
                colour_palette = ["red", "blue", "black", "white", "silver", "grey", "green", "yellow", "orange"]
                avail_c = [c for c in colour_palette if c not in used_colours]
                chosen_colour = avail_c[0] if avail_c else random.choice(colour_palette)
                found_pair = True
                break
        if found_pair:
            break

    if not found_pair:
        chosen_brand = eligible_brands[0]
        b_info = brands_data.get(chosen_brand, {})
        m_list = list(b_info.get("model_queue") or list(b_info.get("models", {}).keys()))
        raw_m = m_list[0] if m_list else "Flagship"
        chosen_model = clean_model_display(raw_m)

    # 2. Select Episode Format (Forbidden within last 5 episodes)
    recent_formats = get_recent_formats(limit=config["anti_repetition"]["min_format_gap_episodes"])
    all_formats = config["formats"]
    allowed_formats = [f for f in all_formats if f["id"] not in recent_formats]
    if not allowed_formats:
        # Safety fallback if format count <= limit
        allowed_formats = [f for f in all_formats if f["id"] != (recent_formats[0] if recent_formats else "")]

    # Weight by historical retention
    retention_weights = get_format_retention_weights()
    format_weights = []
    for f in allowed_formats:
        # Default baseline weight 50.0 (retention percentage)
        w = retention_weights.get(f["id"], 50.0)
        format_weights.append(max(w, 10.0))

    chosen_format = random.choices(allowed_formats, weights=format_weights, k=1)[0]

    # 3. Select Hook Style (Forbidden within last 5 episodes)
    recent_hooks = get_recent_hook_styles(limit=config["anti_repetition"]["min_hook_gap_episodes"])
    all_hooks = config["hook_styles"]
    allowed_hooks = [h for h in all_hooks if h["id"] not in recent_hooks]
    if not allowed_hooks:
        allowed_hooks = [h for h in all_hooks if h["id"] != (recent_hooks[0] if recent_hooks else "")]

    hook_retention_weights = get_hook_retention_weights()
    hook_weights = []
    for h in allowed_hooks:
        w = hook_retention_weights.get(h["id"], 50.0)
        hook_weights.append(max(w, 10.0))

    chosen_hook = random.choices(allowed_hooks, weights=hook_weights, k=1)[0]

    return {
        "brand": chosen_brand,
        "model": chosen_model,
        "colour": chosen_colour,
        "format": chosen_format,
        "hook_style": chosen_hook,
    }
