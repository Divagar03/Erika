"""
Daily YouTube Shorts Queue & Scheduler for Erika
Enforces:
- 10 Daily YouTube Shorts (selected from yesterday's 12 Instagram Reels)
- Maximum 10 uploads per calendar day to stay strictly within Google YouTube Data API quota
- 1-day delay: Today's Instagram Reels become tomorrow's YouTube Shorts
- Idempotent and crash-resilient queue persistence in data/youtube_queue.json
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent.resolve()
DATA_DIR = ROOT / "data"
QUEUE_FILE = DATA_DIR / "youtube_queue.json"
REGISTRY_FILE = DATA_DIR / "registry.json"
LOG_FILE = DATA_DIR / "log.json"
MEDIA_DIR = ROOT / "media"

MAX_DAILY_YOUTUBE_SHORTS = 10


def load_youtube_queue() -> dict:
    if not QUEUE_FILE.exists():
        return {}
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_youtube_queue(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _clean_vehicle_title(raw_str: str) -> str:
    """Extract a clean, aesthetic vehicle title for the YouTube Short."""
    if not raw_str:
        return "Zenith Motors Supercar Reel"
    
    # If detail string like 'set=porsche_911_gt3rs, format=...'
    m = re.search(r'set=([a-z0-9_]+)', raw_str)
    if m:
        name = m.group(1).replace("_", " ").title()
        return f"{name} • Supercar Reel"
    
    # If raw set name like 'ducati_panigale_v4s'
    if "_" in raw_str and "," not in raw_str:
        return f"{raw_str.replace('_', ' ').title()} • Supercar Reel"
    
    # Remove technical metadata prefixes
    cleaned = re.sub(r'(set|format|media_id|yt_id)=[^,]+,?\s*', '', raw_str).strip()
    if cleaned:
        return f"{cleaned[:50]} • Supercar Reel"
    return "Zenith Motors Supercar Reel"


def get_or_create_daily_youtube_queue() -> dict:
    """Return the YouTube queue for today (UTC). If none exists or date rolled over,
    builds a fresh 10-item queue from yesterday's Instagram reels.
    """
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    yesterday_str = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")

    current_queue = load_youtube_queue()
    if current_queue.get("date") == today_str and "queue" in current_queue:
        return current_queue

    print(f"\n[YouTube Queue] Initializing fresh daily queue for {today_str} (Source: {yesterday_str} Reels)...")

    candidates: list[dict] = []
    seen_media: set[str] = set()

    if REGISTRY_FILE.exists():
        try:
            with open(REGISTRY_FILE, encoding="utf-8") as f:
                reg = json.load(f)
            for p in reg.get("posts", []):
                if p.get("status") == "published":
                    m_file = p.get("media_file", "")
                    if m_file and m_file not in seen_media:
                        seen_media.add(m_file)
                        candidates.append({
                            "id": Path(m_file).stem,
                            "media_file": m_file,
                            "publish_timestamp": p.get("publish_timestamp", ""),
                            "set_name": p.get("set_name", ""),
                            "format": p.get("format", "reel"),
                            "caption": p.get("caption", ""),
                            "yt_id": p.get("yt_id"),
                            "metrics": p.get("metrics", {})
                        })
        except Exception as e:
            print(f"  [YouTube Queue] Notice reading registry: {e}")

    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, encoding="utf-8") as f:
                logs = json.load(f)
            for l in logs:
                if l.get("status") == "ok" and l.get("media", "").endswith(".mp4"):
                    m_file = l.get("media")
                    if m_file and m_file not in seen_media:
                        seen_media.add(m_file)
                        candidates.append({
                            "id": Path(m_file).stem,
                            "media_file": m_file,
                            "publish_timestamp": l.get("time", ""),
                            "set_name": l.get("detail", ""),
                            "format": "reel",
                            "caption": l.get("caption", ""),
                            "yt_id": None,
                            "metrics": {}
                        })
        except Exception as e:
            print(f"  [YouTube Queue] Notice reading log: {e}")

    # Filter strictly for yesterday's reels
    yesterday_reels = [
        c for c in candidates
        if c.get("publish_timestamp", "").startswith(yesterday_str)
    ]

    # Rank by metrics score: 0.4*views + 0.3*shares + 0.2*saves + 0.1*likes
    def score_item(c):
        m = c.get("metrics", {})
        return (
            0.4 * m.get("views", 0) +
            0.3 * m.get("shares", 0) +
            0.2 * m.get("saves", 0) +
            0.1 * m.get("likes", 0)
        )

    yesterday_ranked = sorted(yesterday_reels, key=score_item, reverse=True)

    # Pick up to 10 from yesterday
    selected = yesterday_ranked[:MAX_DAILY_YOUTUBE_SHORTS]

    # If fewer than 10 existed yesterday, fill from recent unposted to reach 10
    if len(selected) < MAX_DAILY_YOUTUBE_SHORTS:
        remaining_needed = MAX_DAILY_YOUTUBE_SHORTS - len(selected)
        other_candidates = [
            c for c in reversed(candidates)
            if c not in selected and not c.get("yt_id")
        ]
        selected += other_candidates[:remaining_needed]

    # Build queue entries with clean metadata
    queue_items = []
    for item in selected:
        clean_title = _clean_vehicle_title(item.get("set_name", ""))
        queue_items.append({
            "id": item.get("id"),
            "media_file": item.get("media_file"),
            "title": f"{clean_title} #Shorts",
            "caption": item.get("caption", ""),
            "set_name": item.get("set_name", ""),
            "status": "pending",
            "yt_id": None,
            "published_at": None,
            "error": None
        })

    new_queue = {
        "date": today_str,
        "source_date": yesterday_str,
        "target_count": MAX_DAILY_YOUTUBE_SHORTS,
        "published_today": 0,
        "queue": queue_items
    }
    save_youtube_queue(new_queue)
    print(f"  [YouTube Queue] Successfully queued {len(queue_items)} Shorts for {today_str}.")
    return new_queue


def get_next_pending_youtube_short() -> tuple[dict | None, dict]:
    """Return (item, queue_data) for the next pending short, or (None, queue_data) if finished."""
    queue_data = get_or_create_daily_youtube_queue()
    if queue_data.get("published_today", 0) >= MAX_DAILY_YOUTUBE_SHORTS:
        return None, queue_data

    for item in queue_data.get("queue", []):
        if item.get("status") == "pending":
            return item, queue_data

    return None, queue_data


def record_youtube_short_publish(item_id: str, yt_id: Optional[str], error: Optional[str] = None) -> None:
    """Record upload success or failure in the queue file."""
    queue_data = load_youtube_queue()
    for item in queue_data.get("queue", []):
        if item.get("id") == item_id:
            if yt_id:
                item["status"] = "published"
                item["yt_id"] = yt_id
                item["published_at"] = datetime.now(timezone.utc).isoformat()
                item["error"] = None
                queue_data["published_today"] = queue_data.get("published_today", 0) + 1
            else:
                item["status"] = "failed"
                item["error"] = error
            break
    save_youtube_queue(queue_data)
