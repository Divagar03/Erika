"""
scripts/rotation_engine.py
==========================
Master brand/model/colour rotation engine per the MASTER PROMPT spec.

Public API
----------
  get_next_selection()          -> (brand, model, colour)
  commit_state(brand, model, colour, clip_hashes, source_urls, post_id, num_clips)
  skip_model(brand, model, reason)
  skip_brand(brand, reason)
  get_state()                   -> raw state dict (read-only snapshot)

All state is persisted to data/rotation_state.json.
State is ONLY written after a successful post (commit_state).
"""

import json
import os
import random
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(__file__)
STATE_PATH = os.path.join(_HERE, "..", "data", "rotation_state.json")

# ---------------------------------------------------------------------------
# Fixed colour vocabulary (grey ≠ silver)
# ---------------------------------------------------------------------------
COLOUR_VOCAB = [
    "white", "black", "grey", "silver", "red", "blue",
    "green", "yellow", "orange", "purple", "brown", "gold", "pink", "beige"
]

# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------
def _load_state():
    if not os.path.exists(STATE_PATH):
        raise FileNotFoundError(
            f"rotation_state.json not found at {STATE_PATH}. "
            "Run scripts/build_rotation_state.py first."
        )
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)
    logger.debug("rotation_state.json saved.")


# ---------------------------------------------------------------------------
# Brand rotation (outer circle)  — Rule 1
# ---------------------------------------------------------------------------
def _get_next_brand(state):
    """
    Advances the brand pointer and returns the next brand, enforcing:
    - Never the same brand twice in a row.
    - On completing a full round, shuffle the queue (first brand must differ
      from last brand of the previous round).
    """
    queue = state["brand_queue"]
    pointer = state["brand_pointer"]
    last_brand = state["last_brand"]

    total = len(queue)
    if total == 0:
        raise RuntimeError("brand_queue is empty — no brands available.")

    # Try up to total*2 positions to find a valid next brand
    for _ in range(total * 2):
        # Check if we've completed a full round (pointer wrapped around)
        if pointer >= total:
            pointer = 0
            # Shuffle for the new round; ensure first != last_brand
            random.shuffle(queue)
            if last_brand and queue[0] == last_brand and total > 1:
                # Swap first element with a random other
                swap_idx = random.randint(1, total - 1)
                queue[0], queue[swap_idx] = queue[swap_idx], queue[0]
            state["brand_queue"] = queue
            logger.info("Brand queue reshuffled for new round.")

        candidate = queue[pointer]
        pointer += 1

        if candidate == last_brand:
            # Skip back-to-back repeat
            continue

        state["brand_pointer"] = pointer
        return candidate

    raise RuntimeError(
        f"Could not find a valid next brand (last_brand={last_brand}, queue={queue})"
    )


# ---------------------------------------------------------------------------
# Model rotation (inner circle)  — Rule 2
# ---------------------------------------------------------------------------
def _get_next_model(state, brand):
    """
    Returns the next model for the brand, enforcing:
    - Each model posts once per cycle before repeating.
    - On new cycle, reshuffle; first model must differ from last.
    """
    b_state = state["brands"].get(brand)
    if b_state is None:
        raise KeyError(f"Brand '{brand}' not found in rotation state.")

    queue = b_state["model_queue"]
    pointer = b_state["model_pointer"]
    last_model = b_state["last_model"]
    models_data = b_state["models"]

    total = len(queue)
    if total == 0:
        raise RuntimeError(f"Brand '{brand}' has no models in its queue.")

    # Collect available models (not unavailable_until_next_cycle)
    available = [m for m in queue if not models_data.get(m, {}).get("unavailable_until_next_cycle", False)]

    if not available:
        # Reset all unavailability flags (new cycle)
        for m in models_data:
            models_data[m]["unavailable_until_next_cycle"] = False
        available = list(queue)
        logger.info(f"Brand '{brand}': all models were unavailable — reset for new cycle.")

    # Walk the queue from current pointer
    for attempt in range(total * 2):
        if pointer >= total:
            pointer = 0
            # Reshuffle model queue
            random.shuffle(queue)
            if last_model and queue[0] == last_model and total > 1:
                swap_idx = random.randint(1, total - 1)
                queue[0], queue[swap_idx] = queue[swap_idx], queue[0]
            b_state["model_queue"] = queue
            logger.info(f"Brand '{brand}': model queue reshuffled for new cycle.")

        candidate = queue[pointer]
        pointer += 1

        if models_data.get(candidate, {}).get("unavailable_until_next_cycle", False):
            continue

        b_state["model_pointer"] = pointer
        return candidate

    raise RuntimeError(
        f"Brand '{brand}': Could not find a valid next model after {total*2} attempts."
    )


# ---------------------------------------------------------------------------
# Colour rotation  — Rule 3
# ---------------------------------------------------------------------------
def _get_next_colour(state, brand, model):
    """
    Returns a colour not in the model's used_colours.
    When all colours have been used, resets the set (new cycle);
    first colour of new cycle must differ from last_colour.
    """
    m_state = state["brands"][brand]["models"].get(model)
    if m_state is None:
        raise KeyError(f"Model '{model}' not found under brand '{brand}' in rotation state.")

    used = m_state.get("used_colours", [])
    last_colour = m_state.get("last_colour")

    remaining = [c for c in COLOUR_VOCAB if c not in used]

    if not remaining:
        # All colours used — reset
        m_state["used_colours"] = []
        used = []
        remaining = list(COLOUR_VOCAB)
        logger.info(f"{brand} {model}: all colours used, resetting colour cycle.")

    # Shuffle remaining for variety; ensure first != last_colour
    random.shuffle(remaining)
    if last_colour and remaining[0] == last_colour and len(remaining) > 1:
        swap_idx = random.randint(1, len(remaining) - 1)
        remaining[0], remaining[swap_idx] = remaining[swap_idx], remaining[0]

    return remaining[0]


# ---------------------------------------------------------------------------
# Public: get_next_selection
# ---------------------------------------------------------------------------
def get_next_selection():
    """
    Returns (brand, model, colour) for the next reel.
    Does NOT commit state — call commit_state() after a successful post.
    """
    state = _load_state()
    brand = _get_next_brand(state)
    model = _get_next_model(state, brand)
    colour = _get_next_colour(state, brand, model)
    logger.info(f"Next selection: {brand} | {model} | {colour}")
    return brand, model, colour


# ---------------------------------------------------------------------------
# Public: skip_model
# ---------------------------------------------------------------------------
def skip_model(brand, model, reason="no_clips"):
    """
    Marks a model as unavailable for the current cycle and returns the next
    model in the SAME brand's queue (never switches brand).
    Returns the new (brand, model, colour) tuple, or raises if brand exhausted.
    """
    state = _load_state()
    m_state = state["brands"][brand]["models"].get(model)
    if m_state:
        m_state["unavailable_until_next_cycle"] = True
        logger.warning(f"SKIP MODEL: {brand} {model} — {reason}")

    # Try to get the next model in the same brand
    try:
        new_model = _get_next_model(state, brand)
    except RuntimeError:
        raise RuntimeError(
            f"Brand '{brand}': All models are unavailable. Apply skip_brand()."
        )

    colour = _get_next_colour(state, brand, new_model)
    logger.info(f"Skip → next selection: {brand} | {new_model} | {colour}")
    return brand, new_model, colour


# ---------------------------------------------------------------------------
# Public: skip_brand
# ---------------------------------------------------------------------------
def skip_brand(brand, reason="all_models_failed"):
    """
    Skips the entire brand for this round (without breaking brand order).
    Advances to the next brand and returns (brand, model, colour).
    """
    state = _load_state()
    logger.warning(f"SKIP BRAND: {brand} — {reason}")

    # Force past this brand by temporarily setting last_brand to something
    # other than the skipped brand, then advance.
    # We do this by directly advancing the pointer past the brand.
    queue = state["brand_queue"]
    pointer = state["brand_pointer"]
    total = len(queue)

    # Find brand in queue and skip it
    for _ in range(total):
        if pointer >= total:
            pointer = 0
        if queue[pointer] == brand:
            pointer += 1
            continue
        # candidate
        candidate = queue[pointer]
        if candidate == state["last_brand"]:
            pointer += 1
            continue
        pointer += 1
        state["brand_pointer"] = pointer
        new_model = _get_next_model(state, candidate)
        colour = _get_next_colour(state, candidate, new_model)
        logger.info(f"Skip brand → next selection: {candidate} | {new_model} | {colour}")
        return candidate, new_model, colour

    raise RuntimeError("skip_brand: could not find any valid next brand.")


# ---------------------------------------------------------------------------
# Public: select_next_available_vehicle
# ---------------------------------------------------------------------------
def select_next_available_vehicle(is_available_fn, max_skips: int = 100):
    """
    Finds the next (brand, model, colour) that satisfies is_available_fn(brand, model, colour).
    Strictly follows Master Spec:
      - RULE 1: Brand Rotation (outer circle)
      - RULE 2: Model Rotation inside each brand (inner circle)
      - RULE 3: Colour Lock (per reel)
      - SKIP RULE: If a model has no clips, mark unavailable and pick next model
        in SAME brand. If all models of brand fail, skip to next brand.
    Returns (brand, model, colour, state_candidate) or None.
    State is NOT saved to disk here — commit_state must be called after post.
    """
    state = _load_state()
    b_queue = state.get("brand_queue", [])
    if not b_queue:
        return None

    skips = 0
    while skips < max_skips:
        try:
            brand = _get_next_brand(state)
        except Exception as exc:
            logger.warning(f"Failed getting next brand: {exc}")
            return None

        b_state = state["brands"].get(brand, {})
        models_in_queue = list(b_state.get("model_queue", []))
        if not models_in_queue:
            skips += 1
            continue

        # Try models in this brand's queue
        for _ in range(len(models_in_queue)):
            try:
                model = _get_next_model(state, brand)
            except Exception as exc:
                logger.warning(f"Model selection failed for {brand}: {exc}")
                break

            colour = _get_next_colour(state, brand, model)

            # Check availability callback
            if is_available_fn(brand, model, colour):
                logger.info(f"Selected: {brand} | {model} | {colour}")
                return brand, model, colour, state
            else:
                # Mark model unavailable for this cycle
                if model in b_state.get("models", {}):
                    b_state["models"][model]["unavailable_until_next_cycle"] = True
                logger.warning(f"SKIP MODEL: {brand} {model} — unavailable clips")
                skips += 1

        # If every model of this brand failed, skip brand for this round
        logger.warning(f"SKIP BRAND: {brand} — all models unavailable for this round")

    return None


# ---------------------------------------------------------------------------
# Public: commit_state
# ---------------------------------------------------------------------------
def commit_state(brand, model, colour, clip_hashes, source_urls, post_id, num_clips, state=None):
    """
    Call ONLY after a successful Instagram post.
    Updates all pointers and appends to used_clip_hashes, post_log.
    """
    import datetime
    if state is None:
        state = _load_state()

    # Update brand state
    state["last_brand"] = brand

    # Update model state
    b_state = state["brands"][brand]
    b_state["last_model"] = model

    # Update colour state
    m_state = b_state["models"][model]
    if colour not in m_state["used_colours"]:
        m_state["used_colours"].append(colour)
    m_state["last_colour"] = colour
    m_state["unavailable_until_next_cycle"] = False

    # Update clip hashes + urls
    state["used_clip_hashes"].extend(clip_hashes)
    state["used_source_urls"].extend(source_urls)

    # Append post log entry
    state["post_log"].append({
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "brand": brand,
        "model": model,
        "colour": colour,
        "num_clips": num_clips,
        "clip_hashes": clip_hashes,
        "post_id": post_id,
    })

    _save_state(state)
    logger.info(
        f"State committed: {brand} | {model} | {colour} | "
        f"{num_clips} clips | post_id={post_id}"
    )


# ---------------------------------------------------------------------------
# Public: get_state
# ---------------------------------------------------------------------------
def get_state():
    """Returns a copy of the current rotation state (read-only)."""
    return _load_state()


# ---------------------------------------------------------------------------
# Public: is_clip_used
# ---------------------------------------------------------------------------
def is_clip_used(clip_hash=None, source_url=None):
    """Check whether a clip has been used before (by hash or URL)."""
    state = _load_state()
    if clip_hash and clip_hash in state.get("used_clip_hashes", []):
        return True
    if source_url and source_url in state.get("used_source_urls", []):
        return True
    return False


# ---------------------------------------------------------------------------
# CLI helper (python -m scripts.rotation_engine preview)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "preview"

    if cmd == "preview":
        brand, model, colour = get_next_selection()
        print(f"\n{'='*50}")
        print(f"  NEXT REEL SELECTION")
        print(f"  Brand : {brand}")
        print(f"  Model : {model}")
        print(f"  Colour: {colour}")
        print(f"{'='*50}")

    elif cmd == "status":
        s = get_state()
        print(f"Brand queue length : {len(s['brand_queue'])}")
        print(f"Brand pointer      : {s['brand_pointer']}")
        print(f"Last brand         : {s['last_brand']}")
        print(f"Used clip hashes   : {len(s['used_clip_hashes'])}")
        print(f"Post log entries   : {len(s['post_log'])}")
        if s["post_log"]:
            last = s["post_log"][-1]
            print(f"Last post          : {last['brand']} {last['model']} {last['colour']} @ {last['timestamp']}")

    elif cmd == "simulate":
        # Simulate N posts without committing
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        print(f"\nSimulating {n} selections (no state commit):\n")
        # We need a local simulation state
        sim_state = _load_state()
        for i in range(1, n + 1):
            brand = _get_next_brand(sim_state)
            model = _get_next_model(sim_state, brand)
            colour = _get_next_colour(sim_state, brand, model)
            print(f"  {i:3d}. {brand:20s} | {model:30s} | {colour}")
            # Fake advance for simulation
            sim_state["last_brand"] = brand
            sim_state["brands"][brand]["last_model"] = model
            m_st = sim_state["brands"][brand]["models"][model]
            if colour not in m_st["used_colours"]:
                m_st["used_colours"].append(colour)
            m_st["last_colour"] = colour

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python scripts/rotation_engine.py [preview|status|simulate [N]]")
