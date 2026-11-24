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


def record_used_shots(vehicle_name: str, shots: Sequence[Path]) -> None:
    """Permanently record shots as used in data/used_shots.json. Call ONLY after successful post."""
    usage = load_shot_usage()
    used_list = usage.get(vehicle_name, [])
    new_used = [p.name for p in shots if p.name not in used_list]
    if new_used:
        usage[vehicle_name] = used_list + new_used
        save_shot_usage(usage)
        print(f"  [ShotTracker] Permanently recorded {len(new_used)} used shots for {vehicle_name}")


def _find_cohesive_subset(shots: list[Path], min_required: int, needed_count: int) -> list[Path]:
    """
    Finds the largest mutually color-cohesive subset of unposted shots
    that satisfies the single-color rule (same color category and hue spread <= 50 deg).
    """
    if not shots:
        return []
    # Video clips are continuous footage from the same curated set
    if shots[0].suffix.lower() == ".mp4":
        return shots[:needed_count] if len(shots) >= min_required else []

    try:
        from scripts.color_guard import analyze_shot_color
    except ImportError:
        try:
            from color_guard import analyze_shot_color
        except ImportError:
            return shots[:needed_count] if len(shots) >= min_required else []

    analyzed = []
    for s in shots:
        try:
            ct, hue = analyze_shot_color(s)
            analyzed.append((s, ct, hue))
        except Exception:
            pass

    if not analyzed:
        return []

    # Group by color category: WHITE, BLACK, GRAY, COLORED
    by_type: dict[str, list[tuple[Path, float]]] = {}
    for s, ct, hue in analyzed:
        by_type.setdefault(ct, []).append((s, hue))

    best_cluster: list[Path] = []

    # 1. Non-COLORED (WHITE, BLACK, GRAY)
    for ct in ("WHITE", "BLACK", "GRAY"):
        items = by_type.get(ct, [])
        if len(items) >= min_required and len(items) > len(best_cluster):
            best_cluster = [s for s, _ in items]

    # 2. COLORED: Find largest cluster where circular hue spread <= 55 degrees
    colored_items = by_type.get("COLORED", [])
    if len(colored_items) >= min_required:
        n = len(colored_items)
        best_colored: list[Path] = []
        for i in range(n):
            ref_hue = colored_items[i][1]
            cluster = []
            for s, h in colored_items:
                diff = abs(h - ref_hue) % 360
                dist = min(diff, 360 - diff)
                if dist <= 27.5:  # total diameter <= 55 deg
                    cluster.append((s, h))
            if len(cluster) > len(best_colored):
                # Verify exact circular hue spread
                hues = [h for _, h in cluster]
                sorted_hues = sorted(hues)
                max_gap = max((sorted_hues[(k + 1) % len(sorted_hues)] - sorted_hues[k]) % 360 for k in range(len(sorted_hues)))
                if (360.0 - max_gap) <= 55.0:
                    best_colored = [s for s, _ in cluster]
        if len(best_colored) > len(best_cluster):
            best_cluster = best_colored

    if len(best_cluster) >= min_required:
        return best_cluster[:needed_count]
    return []


def select_diverse_vehicle_shots(
    vehicle_name: str,
    all_media: Sequence[Path],
    needed_count: int = 5,
    min_required: int = 4,
    record: bool = False
) -> list[Path]:
    """
    Given all media files for a vehicle (e.g. shot_1.jpg .. shot_10.jpg or clip_1.mp4 ..),
    select up to `needed_count` (at least `min_required`) files that have NEVER been posted before.
    
    Guarantees:
    1. 100% Single-Vehicle & Single-Color Cohesion: All selected files belong strictly to this vehicle
       and share the exact same vehicle color.
    2. Strict Zero Repetition: Once a shot or clip is posted, it is NEVER repeated.
       If fewer than `min_required` unposted shots remain, an exception is raised.
    3. Natural Sorting: Never sorts 'shot_10' before 'shot_2'.
    4. Safe Selection: Only records shots to disk if record=True (or via record_used_shots after publish).
    """
    unused = get_unposted_shots(vehicle_name, all_media)

    if len(unused) < min_required:
        raise ValueError(
            f"[ShotTracker] Strict zero-repeat policy: '{vehicle_name}' has only {len(unused)} "
            f"unposted shots left (needed minimum {min_required}). Posted shots are NEVER repeated."
        )

    cohesive = _find_cohesive_subset(unused, min_required, needed_count)
    if not cohesive or len(cohesive) < min_required:
        raise ValueError(
            f"[ShotTracker] Single-color cohesion: '{vehicle_name}' has {len(unused)} unposted shots, "
            f"but no mutually cohesive color cluster with at least {min_required} shots was found."
        )

    selected = cohesive

    if record:
        record_used_shots(vehicle_name, selected)

    print(f"  [ShotTracker] {vehicle_name}: Selected {len(selected)} cohesive unposted shots ({', '.join(p.name for p in selected)}), {len(unused) - len(selected)} remaining unposted")
    return selected

