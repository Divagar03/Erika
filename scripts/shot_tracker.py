"""
Shot & Clip Usage Tracker for Zenith Motors
Tracks which media files (photo shots or video clips) have been used for each vehicle,
ensuring that once a clip or photo of a car is posted, it is not posted again on the
next rotation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
USAGE_FILE = ROOT / "data" / "used_shots.json"


def load_shot_usage() -> dict[str, list[str]]:
    """Return dict of {vehicle_set: [used_filename_1, used_filename_2, ...]}."""
    if not USAGE_FILE.exists():
        return {}
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_shot_usage(usage: dict[str, list[str]]) -> None:
    """Save shot usage dict to data/used_shots.json."""
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(usage, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  [ShotTracker] Error saving shot usage: {e}")


def select_diverse_vehicle_shots(
    vehicle_name: str,
    all_media: Sequence[Path],
    needed_count: int = 5
) -> list[Path]:
    """
    Given all media files for a vehicle (e.g. shot_01.jpg .. shot_15.jpg or clip_1.mp4 ..),
    select `needed_count` files prioritizing those that have NOT been recently used.
    
    If all files have been used, resets the history for this vehicle and starts a fresh cycle,
    ensuring maximum time before any single shot is ever repeated.
    """
    if len(all_media) <= needed_count:
        # If pool has only needed_count (or fewer), return all of them
        return list(all_media)

    usage = load_shot_usage()
    used_list = usage.get(vehicle_name, [])

    # Group media into unused vs used
    unused: list[Path] = [p for p in all_media if p.name not in used_list]
    used: list[Path] = [p for p in all_media if p.name in used_list]

    if len(unused) >= needed_count:
        selected = unused[:needed_count]
    else:
        # Reset cycle: take all remaining unused + oldest used to fill needed_count
        selected = unused + used[:(needed_count - len(unused))]

    # Update usage: append newly selected shots
    new_used = [p.name for p in selected]
    updated_history = [name for name in used_list if name not in new_used] + new_used

    # Keep history bounded to total media count to allow smooth wrapping
    if len(updated_history) >= len(all_media):
        # Keep only the most recent batch so older shots become available again
        updated_history = new_used

    usage[vehicle_name] = updated_history
    save_shot_usage(usage)

    print(f"  [ShotTracker] {vehicle_name}: Selected {len(selected)} shots ({', '.join(p.name for p in selected)})")
    return selected
