"""
Shot & Clip Usage Tracker for Zenith Motors
Tracks which media files (photo shots or video clips) have been used for each vehicle,
ensuring that once a clip or photo of a car is posted, it is not posted again on the
next rotation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
USAGE_FILE = ROOT / "data" / "used_shots.json"


def natural_sort_key(p: Path) -> list:
    """Natural alphanumeric sort key (shot_1, shot_2 ... shot_9, shot_10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', p.name)]


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


def get_unposted_shots(vehicle_name: str, all_media: Sequence[Path]) -> list[Path]:
    """
    Return all media files that have NEVER been posted for this vehicle.
    Strict zero-repeat rule: once a shot is used, it is never eligible again.
    """
    usage = load_shot_usage()
    used_set = set(usage.get(vehicle_name, []))
    sorted_media = sorted(list(all_media), key=natural_sort_key)
    return [p for p in sorted_media if p.name not in used_set]


def select_diverse_vehicle_shots(
    vehicle_name: str,
    all_media: Sequence[Path],
    needed_count: int = 5,
    min_required: int = 4
) -> list[Path]:
    """
    Given all media files for a vehicle (e.g. shot_1.jpg .. shot_10.jpg or clip_1.mp4 ..),
    select up to `needed_count` (at least `min_required`) files that have NEVER been posted before.
    
    Guarantees:
    1. 100% Single-Vehicle & Single-Color Cohesion: All selected files belong strictly to this vehicle.
    2. Strict Zero Repetition: Once a shot or clip is posted, it is NEVER repeated.
       If fewer than `min_required` unposted shots remain, an exception is raised.
    3. Natural Sorting: Never sorts 'shot_10' before 'shot_2'.
    """
    unused = get_unposted_shots(vehicle_name, all_media)

    if len(unused) < min_required:
        raise ValueError(
            f"[ShotTracker] Strict zero-repeat policy: '{vehicle_name}' has only {len(unused)} "
            f"unposted shots left (needed minimum {min_required}). Posted shots are NEVER repeated."
        )

    actual_count = min(needed_count, len(unused))
    selected = unused[:actual_count]

    # Permanently append newly selected shots to usage history (never pruned, never wrapped)
    usage = load_shot_usage()
    used_list = usage.get(vehicle_name, [])
    new_used = [p.name for p in selected if p.name not in used_list]
    usage[vehicle_name] = used_list + new_used
    save_shot_usage(usage)

    print(f"  [ShotTracker] {vehicle_name}: Selected {len(selected)} unposted shots ({', '.join(p.name for p in selected)}), {len(unused) - len(selected)} remaining unposted")
    return selected

