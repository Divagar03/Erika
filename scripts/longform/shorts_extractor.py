"""Post-upload Shorts extractor.
Extracts 2-3 high-retention 30-45 second vertical highlights from the long-form episode,
crops to 9:16, burns in a CTA ('Full video on channel!'), and queues them into youtube_queue.json.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
MEDIA_DIR = ROOT / "media"
YT_QUEUE_FILE = ROOT / "data" / "youtube_queue.json"


def extract_promotional_shorts(
    master_video_path: Path,
    chapter_timings: list[dict[str, Any]],
    brand: str,
    model: str,
    youtube_id: str | None,
    episode_id: str,
    count: int = 2
) -> list[Path]:
    """Cut vertical 9:16 teaser Shorts and append to daily YouTube Shorts queue."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    generated_shorts: list[Path] = []

    # Pick the most intense chapters (e.g. Chapter 1 Hook, Chapter 4 Aero, Chapter 6 Rehook)
    candidate_indices = [0, min(3, len(chapter_timings) - 1), min(5, len(chapter_timings) - 1)]

    for s_idx, ch_idx in enumerate(candidate_indices[:count], 1):
        if ch_idx >= len(chapter_timings):
            continue

        ch = chapter_timings[ch_idx]
        start_sec = ch["start_seconds"] + 5.0  # Skip intro transition
        duration = 38.0  # 38 seconds (within 30-45s requirement)

        short_filename = f"short_{episode_id}_{s_idx}.mp4"
        out_short_path = MEDIA_DIR / short_filename

        # Filter: Crop center 9:16 (607x1080 -> scaled to 1080x1920) + CTA bottom banner
        vf_filter = (
            "crop=ih*(9/16):ih:(iw-ow)/2:0,scale=1080:1920,"
            "drawtext=text='FULL VIDEO ON CHANNEL':fontcolor=yellow:fontsize=52:box=1:boxcolor=black@0.75:boxborderw=12:x=(w-text_w)/2:y=h-220:enable='between(t,28,38)'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.2f}",
            "-i", str(master_video_path),
            "-t", f"{duration:.2f}",
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-c:a", "aac",
            "-b:a", "192k",
            str(out_short_path)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        if out_short_path.exists() and out_short_path.stat().st_size > 10000:
            generated_shorts.append(out_short_path)
            print(f"  [ShortsExtractor] Created teaser Short #{s_idx}: {short_filename}")

            # Enqueue into data/youtube_queue.json
            _enqueue_to_youtube_queue(
                short_filename=short_filename,
                brand=brand,
                model=model,
                youtube_id=youtube_id,
                chapter_title=ch["title"]
            )

    return generated_shorts


def _enqueue_to_youtube_queue(
    short_filename: str,
    brand: str,
    model: str,
    youtube_id: str | None,
    chapter_title: str
) -> None:
    """Safely append new teaser short to data/youtube_queue.json."""
    if not YT_QUEUE_FILE.exists():
        return

    try:
        with open(YT_QUEUE_FILE, encoding="utf-8") as f:
            q_data = json.load(f)

        title = f"{brand} {model}: {chapter_title[:28]} • Supercar Reel #Shorts"
        caption = (
            f"⚡ The {brand} {model} pushed automotive physics to the edge. "
            f"Watch the full 20-minute investigative deep-dive on our channel! 👇\n\n"
            f"#{brand.replace(' ', '')} #{model.replace(' ', '')} #Supercars #Engineering"
        )

        new_item = {
            "id": Path(short_filename).stem,
            "media_file": short_filename,
            "title": title,
            "caption": caption,
            "set_name": f"{brand.lower()}_{model.lower()}",
            "status": "pending",
            "yt_id": None,
            "published_at": None,
            "error": None
        }

        # Avoid duplicates
        existing_files = {item.get("media_file") for item in q_data.get("queue", [])}
        if short_filename not in existing_files:
            q_data.setdefault("queue", []).append(new_item)
            q_data["target_count"] = len(q_data["queue"])
            with open(YT_QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(q_data, f, indent=2, ensure_ascii=False)
            print(f"  [ShortsExtractor] Enqueued {short_filename} into daily YouTube Shorts queue.")
    except Exception as e:
        print(f"  [ShortsExtractor] Notice queueing teaser short: {e}")
