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


def load_config() -> dict[str, Any]:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _load_rotation_state() -> dict[str, Any]:
    if not ROTATION_STATE_FILE.exists():
        return {}
    with open(ROTATION_STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def select_topic_and_format() -> dict[str, Any]:
    """Select the next brand, model, colour, episode format, and hook style.
    
    Guarantees:
    - Never the same brand as the last episode.
    - Model advances according to the circular rotation queue.
    - Format is NEVER repeated within the last 5 episodes.
    - Hook style is NEVER repeated within the last 5 episodes.
    - Probability weights adapt to favor historically higher-retention formats/hooks.
    """
    config = load_config()
    rot_state = _load_rotation_state()

    # 1. Select Brand & Model from Rotation State
    brand_queue = rot_state.get("brand_queue", ["Ferrari", "McLaren", "Bugatti", "Porsche", "Lamborghini"])
    last_brand = rot_state.get("last_brand", "")
    brands_data = rot_state.get("brands", {})

    # Find next brand that is not last_brand
    pointer = rot_state.get("brand_pointer", 0) % len(brand_queue)
    chosen_brand = brand_queue[pointer]
    if chosen_brand == last_brand and len(brand_queue) > 1:
        pointer = (pointer + 1) % len(brand_queue)
        chosen_brand = brand_queue[pointer]

    # Model selection for that brand
    brand_info = brands_data.get(chosen_brand, {})
    model_queue = brand_info.get("model_queue", [])
    model_pointer = brand_info.get("model_pointer", 0)

    if model_queue:
        chosen_model = model_queue[model_pointer % len(model_queue)]
    else:
        # Fallback if queue empty
        available_models = list(brand_info.get("models", {}).keys())
        chosen_model = available_models[0] if available_models else "Flagship"

    # Colour lock: pick a colour not recently used for this model
    model_data = brand_info.get("models", {}).get(chosen_model, {})
    used_colours = set(model_data.get("used_colours", []))
    colour_palette = ["red", "blue", "black", "white", "silver", "grey", "green", "yellow", "orange"]
    available_colours = [c for c in colour_palette if c not in used_colours]
    chosen_colour = available_colours[0] if available_colours else random.choice(colour_palette)

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
