"""Database and state persistence layer for long-form YouTube episodes.
Uses SQLite for atomic querying and syncs to JSON for Git tracking.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "longform.db"
JSON_FILE = DATA_DIR / "longform_episodes.json"


def _get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize database tables for episodes, assets, and analytics."""
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                topic_brand TEXT NOT NULL,
                topic_model TEXT NOT NULL,
                topic_colour TEXT,
                format_id TEXT NOT NULL,
                hook_style_id TEXT NOT NULL,
                title TEXT,
                script_hash TEXT,
                script_word_count INTEGER,
                duration_seconds REAL,
                thumbnail_path TEXT,
                video_path TEXT,
                srt_path TEXT,
                youtube_id TEXT,
                status TEXT NOT NULL DEFAULT 'initialized',
                created_at TEXT NOT NULL,
                published_at TEXT,
                qa_report TEXT,
                assets_used TEXT,
                attributions TEXT,
                retention_pct REAL,
                avg_view_duration_sec REAL,
                error_message TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at);
        """)
        conn.commit()
    _sync_from_json()
    _sync_to_json()


def _sync_from_json() -> None:
    """Import git-tracked JSON episodes into SQLite if not already present."""
    if not JSON_FILE.exists():
        return
    try:
        with open(JSON_FILE, encoding="utf-8") as f:
            data = json.load(f)
        episodes = data.get("episodes", [])
        with _get_connection() as conn:
            for ep in episodes:
                eid = ep.get("episode_id")
                if not eid:
                    continue
                exists = conn.execute("SELECT 1 FROM episodes WHERE episode_id = ?", (eid,)).fetchone()
                if not exists:
                    conn.execute("""
                        INSERT INTO episodes (
                            episode_id, topic_brand, topic_model, topic_colour, format_id, hook_style_id,
                            title, script_hash, script_word_count, duration_seconds, thumbnail_path, video_path,
                            srt_path, youtube_id, status, created_at, published_at, qa_report, assets_used,
                            attributions, retention_pct, avg_view_duration_sec, error_message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        eid,
                        ep.get("topic_brand", "Unknown"),
                        ep.get("topic_model", "Unknown"),
                        ep.get("topic_colour"),
                        ep.get("format_id", "model_deep_dive"),
                        ep.get("hook_style_id", "provocative_claim"),
                        ep.get("title"),
                        ep.get("script_hash"),
                        ep.get("script_word_count"),
                        ep.get("duration_seconds"),
                        ep.get("thumbnail_path"),
                        ep.get("video_path"),
                        ep.get("srt_path"),
                        ep.get("youtube_id"),
                        ep.get("status", "initialized"),
                        ep.get("created_at", datetime.now(timezone.utc).isoformat()),
                        ep.get("published_at"),
                        json.dumps(ep.get("qa_report")) if ep.get("qa_report") else None,
                        json.dumps(ep.get("assets_used")) if ep.get("assets_used") else None,
                        json.dumps(ep.get("attributions")) if ep.get("attributions") else None,
                        ep.get("retention_pct"),
                        ep.get("avg_view_duration_sec"),
                        ep.get("error_message")
                    ))
            conn.commit()
    except Exception as e:
        print(f"  [Longform DB] Notice syncing from JSON: {e}")


def create_episode(
    topic_brand: str,
    topic_model: str,
    topic_colour: str | None,
    format_id: str,
    hook_style_id: str,
    episode_id: str | None = None
) -> str:
    """Create a new episode record and return its episode_id."""
    init_db()
    now_utc = datetime.now(timezone.utc).isoformat()
    if not episode_id:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        episode_id = f"ep_{ts}"

    with _get_connection() as conn:
        conn.execute("""
            INSERT INTO episodes (
                episode_id, topic_brand, topic_model, topic_colour,
                format_id, hook_style_id, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'initialized', ?)
        """, (episode_id, topic_brand, topic_model, topic_colour, format_id, hook_style_id, now_utc))
        conn.commit()

    _sync_to_json()
    return episode_id


def get_episode(episode_id: str) -> dict[str, Any] | None:
    """Retrieve an episode by ID."""
    init_db()
    with _get_connection() as conn:
        row = conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for json_col in ("qa_report", "assets_used", "attributions"):
            if d.get(json_col):
                try:
                    d[json_col] = json.loads(d[json_col])
                except Exception:
                    pass
        return d


def update_episode(episode_id: str, **kwargs: Any) -> None:
    """Update fields for a given episode."""
    init_db()
    if not kwargs:
        return

    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        if isinstance(v, (dict, list)):
            values.append(json.dumps(v, ensure_ascii=False))
        else:
            values.append(v)
    values.append(episode_id)

    query = f"UPDATE episodes SET {', '.join(fields)} WHERE episode_id = ?"
    with _get_connection() as conn:
        conn.execute(query, tuple(values))
        conn.commit()

    _sync_to_json()


def get_recent_formats(limit: int = 5) -> list[str]:
    """Return the formats used in the last `limit` completed or published episodes."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute("""
            SELECT format_id FROM episodes
            WHERE status IN ('published', 'rendered', 'qa_passed', 'initialized')
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [r["format_id"] for r in rows if r["format_id"]]


def get_recent_hook_styles(limit: int = 5) -> list[str]:
    """Return the hook styles used in the last `limit` completed or published episodes."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute("""
            SELECT hook_style_id FROM episodes
            WHERE status IN ('published', 'rendered', 'qa_passed', 'initialized')
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [r["hook_style_id"] for r in rows if r["hook_style_id"]]


def get_recent_scripts(limit: int = 20) -> list[dict[str, Any]]:
    """Return the last `limit` scripts with their text and hashes for TF-IDF similarity checking."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute("""
            SELECT episode_id, title, script_hash FROM episodes
            WHERE status IN ('published', 'rendered', 'qa_passed')
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_format_retention_weights() -> dict[str, float]:
    """Return average retention score for each format, normalized for probabilistic weighting."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute("""
            SELECT format_id, AVG(retention_pct) as avg_retention, COUNT(*) as cnt
            FROM episodes
            WHERE retention_pct IS NOT NULL AND status = 'published'
            GROUP BY format_id
        """).fetchall()
        weights = {}
        for r in rows:
            if r["avg_retention"] and r["avg_retention"] > 0:
                weights[r["format_id"]] = float(r["avg_retention"])
        return weights


def get_hook_retention_weights() -> dict[str, float]:
    """Return average retention score for each hook style for probabilistic weighting."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute("""
            SELECT hook_style_id, AVG(retention_pct) as avg_retention, COUNT(*) as cnt
            FROM episodes
            WHERE retention_pct IS NOT NULL AND status = 'published'
            GROUP BY hook_style_id
        """).fetchall()
        weights = {}
        for r in rows:
            if r["avg_retention"] and r["avg_retention"] > 0:
                weights[r["hook_style_id"]] = float(r["avg_retention"])
        return weights


def get_all_episodes() -> list[dict[str, Any]]:
    """Return all episodes from database."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute("SELECT * FROM episodes ORDER BY created_at ASC").fetchall()
        episodes = []
        for r in rows:
            d = dict(r)
            for json_col in ("qa_report", "assets_used", "attributions"):
                if d.get(json_col):
                    try:
                        d[json_col] = json.loads(d[json_col])
                    except Exception:
                        pass
            episodes.append(d)
        return episodes


def _sync_to_json() -> None:
    """Mirror SQLite records to JSON for version control tracking."""
    try:
        with _get_connection() as conn:
            rows = conn.execute("SELECT * FROM episodes ORDER BY created_at ASC").fetchall()
            episodes = []
            for r in rows:
                d = dict(r)
                for json_col in ("qa_report", "assets_used", "attributions"):
                    if d.get(json_col):
                        try:
                            d[json_col] = json.loads(d[json_col])
                        except Exception:
                            pass
                episodes.append(d)

        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump({"episodes": episodes, "synced_at": datetime.now(timezone.utc).isoformat()}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  [Longform DB] Notice syncing to JSON: {e}")
