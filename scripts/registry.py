"""
Content Registry & Deduplication Engine for Erika
Enforces:
- 7-day model cooldown
- 3-day music track cooldown
- 2-hour slot idempotency
- Caption deduplication
- Reserved-to-published state tracking
- Instagram metrics tracking & Top-10 scoring
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent.resolve()
REGISTRY_FILE = ROOT / "data" / "registry.json"


def _load() -> dict:
    if not REGISTRY_FILE.exists():
        data = {"schema_version": 1, "posts": [], "reserved": []}
        _save(data)
        return data
    try:
        with open(REGISTRY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"schema_version": 1, "posts": [], "reserved": []}


def _save(data: dict) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def caption_hash(caption: str) -> str:
    """Return a 16-character SHA-256 fingerprint of the caption."""
    cleaned = caption.strip().lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


SET_BRAND_MAP = {
    "aston_martin_valour": "aston_martin",
    "audi_r8_v10": "audi",
    "bmw_m4_black": "bmw",
    "bmw_s1000rr": "bmw",
    "ducati_panigale_v4s": "ducati",
    "ferrari_f80": "ferrari",
    "ferrari_sf90": "ferrari",
    "lamborghini_diablo": "lamborghini",
    "lamborghini_revuelto": "lamborghini",
    "nissan_gtr_r35": "nissan",
    "porsche_911_gt3rs": "porsche",
    "red_sportscar": "roadster",
    "white_mustang_sunset": "ford",
}

SET_MODEL_MAP = {
    "aston_martin_valour": "valour",
    "audi_r8_v10": "r8",
    "bmw_m4_black": "m4",
    "bmw_s1000rr": "s1000rr",
    "ducati_panigale_v4s": "panigale_v4s",
    "ferrari_f80": "f80",
    "ferrari_sf90": "sf90",
    "lamborghini_diablo": "diablo",
    "lamborghini_revuelto": "revuelto",
    "nissan_gtr_r35": "gtr_r35",
    "porsche_911_gt3rs": "911_gt3rs",
    "red_sportscar": "crimson_roadster",
    "white_mustang_sunset": "mustang_gt",
}

def get_set_brand(set_name: str) -> str:
    s = set_name.lower().strip()
    if s in SET_BRAND_MAP:
        return SET_BRAND_MAP[s]
    for b in ["bmw", "ferrari", "lamborghini", "porsche", "audi", "nissan", "ducati", "aston", "mustang", "ford"]:
        if b in s:
            return "aston_martin" if b == "aston" else ("ford" if b == "mustang" else b)
    return s.split("_")[0]

def get_set_model(set_name: str) -> str:
    s = set_name.lower().strip()
    if s in SET_MODEL_MAP:
        return SET_MODEL_MAP[s]
    for m in ["m4", "m3", "m2", "m5", "s1000rr", "f80", "sf90", "diablo", "revuelto", "gt3rs", "r8", "gtr", "panigale", "valour", "mustang"]:
        if m in s:
            return m
    return s

def is_brand_on_cooldown(brand_or_set: str, cooldown_hours: int = 24) -> bool:
    """Return True if any post from this brand was published within the last cooldown_hours
    OR on the same UTC calendar day.
    Guarantees: If BMW is posted today, NO BMW can be posted for the rest of today!
    """
    if not brand_or_set:
        return False
    target_brand = get_set_brand(brand_or_set)
    data = _load()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cooldown_hours)
    for post in data.get("posts", []):
        if post.get("status") != "published":
            continue
        p_brand = post.get("brand") or get_set_brand(post.get("set_name", ""))
        if p_brand == target_brand:
            try:
                ts = datetime.fromisoformat(post["publish_timestamp"])
                # Block if published on same UTC calendar day OR within 24 hours
                if ts > cutoff or ts.date() == now.date():
                    return True
            except Exception:
                pass
    return False

def is_model_on_cooldown(model_or_set: str, cooldown_days: int = 7) -> bool:
    """Return True if this exact model (e.g. BMW M4) was published within the last cooldown_days days.
    Guarantees: Even when the brand (BMW) is eligible again tomorrow, the same model (M4)
    cannot repeat; it must pick a different model (e.g. BMW S1000RR)!
    """
    if not model_or_set:
        return False
    target_model = get_set_model(model_or_set)
    target_set = model_or_set.lower().strip()
    data = _load()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cooldown_days)
    for post in data.get("posts", []):
        if post.get("status") != "published":
            continue
        p_set = post.get("set_name", "").lower().strip()
        p_model = post.get("model") or get_set_model(p_set)
        if p_set == target_set or (target_model and p_model == target_model):
            try:
                ts = datetime.fromisoformat(post["publish_timestamp"])
                if ts > cutoff:
                    return True
            except Exception:
                pass
    return False

def is_set_on_cooldown(set_name: str, cooldown_days: int = 7) -> bool:
    """Return True if set_name or its exact model was published within cooldown_days."""
    return is_model_on_cooldown(set_name, cooldown_days=cooldown_days)


def is_music_on_cooldown(track_name: str, cooldown_days: int = 3) -> bool:
    """Return True if track_name was used within the last cooldown_days days."""
    if not track_name:
        return False
    data = _load()
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    for post in data.get("posts", []):
        if post.get("music_track") == track_name and post.get("status") == "published":
            try:
                ts = datetime.fromisoformat(post["publish_timestamp"])
                if ts > cutoff:
                    return True
            except Exception:
                pass
    return False


def is_slot_occupied(slot_hour_utc: int) -> bool:
    """Return True if a post was already confirmed in this 2-hour UTC slot today."""
    data = _load()
    now = datetime.now(timezone.utc)
    slot_start = now.replace(hour=slot_hour_utc, minute=0, second=0, microsecond=0)
    slot_end = slot_start + timedelta(hours=2)
    for post in data.get("posts", []):
        if post.get("status") != "published":
            continue
        try:
            ts = datetime.fromisoformat(post["publish_timestamp"])
            if slot_start <= ts < slot_end and ts.date() == now.date():
                print(f"  [Registry] Slot {slot_hour_utc:02d}:00–{(slot_hour_utc+2)%24:02d}:00 UTC already posted at {ts.isoformat()}. Skipping.")
                return True
        except Exception:
            pass
    return False


def is_caption_duplicate(cap_hash: str) -> bool:
    """Return True if the most recent published post had an identical caption hash."""
    data = _load()
    published = [p for p in data.get("posts", []) if p.get("status") == "published"]
    if not published:
        return False
    last = max(published, key=lambda p: p.get("publish_timestamp", ""))
    return last.get("caption_hash") == cap_hash


def reserve_slot(set_name: str, format_name: str, caption: str, music_track: str = "") -> str:
    """Mark a clip as reserved the moment it is picked (prevents race conditions)."""
    data = _load()
    res_id = f"{set_name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    data["reserved"].append({
        "id": res_id,
        "set_name": set_name,
        "format": format_name,
        "caption_hash": caption_hash(caption),
        "music_track": music_track,
        "reserved_at": datetime.now(timezone.utc).isoformat(),
    })
    # Keep reserved list bounded
    data["reserved"] = data["reserved"][-20:]
    _save(data)
    return res_id


def confirm_publish(
    set_name: str,
    format_name: str,
    caption: str,
    media_id: str,
    yt_id: Optional[str] = None,
    media_file: Optional[str] = None,
    music_track: Optional[str] = None,
    reservation_id: Optional[str] = None,
    brand: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Record a verified publish in the persistent registry."""
    data = _load()
    if reservation_id:
        data["reserved"] = [r for r in data.get("reserved", []) if r.get("id") != reservation_id]

    data["posts"].append({
        "set_name": set_name,
        "brand": brand or get_set_brand(set_name),
        "model": model or get_set_model(set_name),
        "format": format_name,
        "caption_hash": caption_hash(caption),
        "media_id": media_id,
        "yt_id": yt_id,
        "media_file": media_file or "",
        "music_track": music_track or "",
        "publish_timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "published",
        "metrics": {
            "views": 0,
            "likes": 0,
            "shares": 0,
            "saves": 0,
            "collected_at": None,
        },
    })
    # Keep last 500 posts
    data["posts"] = data["posts"][-500:]
    _save(data)


def update_metrics(media_id: str, views: int, likes: int, shares: int, saves: int) -> None:
    """Update metrics for a published reel."""
    data = _load()
    for post in data.get("posts", []):
        if post.get("media_id") == media_id:
            post["metrics"] = {
                "views": views,
                "likes": likes,
                "shares": shares,
                "saves": saves,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
            break
    _save(data)


def get_top_posts(day_offset: int = 1, limit: int = 10, min_posts: int = 5) -> list[dict]:
    """Return top posts from yesterday ranked by weighted score:
    Score = 0.4*views + 0.3*shares + 0.2*saves + 0.1*likes
    """
    data = _load()
    target_date = (datetime.now(timezone.utc) - timedelta(days=day_offset)).date()
    day_posts = [
        p for p in data.get("posts", [])
        if p.get("status") == "published"
        and p.get("media_id")
    ]
    # Filter to posts from target date if available, otherwise latest available
    filtered = [
        p for p in day_posts
        if datetime.fromisoformat(p["publish_timestamp"]).date() == target_date
    ]
    posts_to_rank = filtered if len(filtered) >= min_posts else day_posts[-limit:]

    def score(p):
        m = p.get("metrics", {})
        return (
            0.4 * m.get("views", 0) +
            0.3 * m.get("shares", 0) +
            0.2 * m.get("saves", 0) +
            0.1 * m.get("likes", 0)
        )

    ranked = sorted(posts_to_rank, key=score, reverse=True)
    if len(ranked) < min_posts:
        return []
    return ranked[:limit]
