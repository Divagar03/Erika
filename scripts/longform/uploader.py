"""YouTube Data API v3 publisher for long-form episodes.
Handles resumable video upload, custom thumbnail upload, SRT captions upload,
timestamped description chapters, Creative Commons attribution, and pinned comments.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
import requests


def _sanitize_text(text: str) -> str:
    """Strip angle brackets and LLM artifacts to prevent YouTube 400 rejection."""
    if not text:
        return ""
    text = re.sub(r"<\|.*?\|>", "", text)
    return text.replace("<", "").replace(">", "").strip()


def _format_timestamp(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def get_youtube_access_token(env: dict) -> tuple[str | None, str]:
    """Refresh Google OAuth2 access token using client secrets and refresh token."""
    client_id = env.get("yt_client_id") or os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = env.get("yt_client_secret") or os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    refresh_token = env.get("yt_refresh_token") or os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()

    if not (client_id and client_secret and refresh_token):
        return None, "YouTube credentials not found in env"

    try:
        res = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=25,
        )
        if res.status_code == 200:
            return res.json().get("access_token"), ""
        return None, f"token_refresh_failed_{res.status_code}_{res.text[:120]}"
    except Exception as exc:
        return None, f"token_refresh_exception_{exc}"


def build_description_with_chapters(
    brand: str,
    model: str,
    summary: str,
    chapter_timings: list[dict[str, Any]],
    attributions: list[dict[str, str]]
) -> str:
    """Build full YouTube description with timestamp chapters and CC attributions."""
    lines = [
        f"An in-depth documentary exploring the engineering, telemetry, and hidden history of the {brand} {model}.",
        "",
        "CHAPTER TIMESTAMPS:",
    ]
    for ch in chapter_timings:
        ts_str = _format_timestamp(ch["start_seconds"])
        lines.append(f"{ts_str} - {ch['title']}")

    lines.extend([
        "",
        "---",
        "AUTOMOTIVE ARCHIVES & ATTRIBUTIONS:",
        "Visuals and documentation referenced under Creative Commons and public domain:",
    ])
    for att in attributions[:12]:
        author = att.get("author", "Contributor")
        license_type = att.get("license", "CC-BY-SA")
        source = att.get("source", "Wikimedia Commons")
        lines.append(f"• {author} ({license_type}) via {source}")

    lines.extend([
        "",
        "DISCLOSURE: This documentary utilizes factual historical archives and AI-assisted production tools (synthetic narration and telemetric graphics) in compliance with YouTube Community Guidelines.",
        "",
        f"#{brand.replace(' ', '')} #{model.replace(' ', '')} #Supercars #Documentary #EngineeringExplained #ZenithMotors"
    ])
    return _sanitize_text("\n".join(lines))


def upload_longform_video(
    video_path: Path,
    thumbnail_path: Path,
    srt_path: Path,
    brand: str,
    model: str,
    format_name: str,
    chapter_timings: list[dict[str, Any]],
    attributions: list[dict[str, str]],
    env: dict,
    is_dry_run: bool = False
) -> tuple[str | None, str]:
    """Publish long-form documentary episode to YouTube with chapters, thumbnail, and captions."""
    if is_dry_run:
        print(f"  [Uploader] [DRY RUN] Would upload {video_path.name} to YouTube with thumbnail and chapters.")
        return "DRY_RUN_YT_ID_12345", ""

    token, err = get_youtube_access_token(env)
    if not token:
        return None, f"Auth failed: {err}"

    # Title under 70 chars, curiosity-driven
    raw_title = f"{brand} {model}: The Engineering Masterclass That Defied Physics"
    if len(raw_title) > 70:
        raw_title = f"{brand} {model}: The Untold Engineering Story"
    title = _sanitize_text(raw_title)[:70]

    description = build_description_with_chapters(brand, model, "", chapter_timings, attributions)

    snippet = {
        "title": title,
        "description": description,
        "tags": [brand, model, "Supercar", "Hypercar", "Engineering", "Documentary", "TopGear", "Review"],
        "categoryId": "2",  # Autos & Vehicles
    }
    status = {
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
    }

    file_size = video_path.stat().st_size
    print(f"  [Uploader] Uploading '{title}' ({file_size // (1024 * 1024)} MB)...")

    # Step 1: Resumable upload initialization
    init_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(file_size),
    }
    init_res = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers=init_headers,
        json={"snippet": snippet, "status": status},
        timeout=30,
    )
    if init_res.status_code not in (200, 201):
        return None, f"init_upload_failed_{init_res.status_code}_{init_res.text[:150]}"

    upload_url = init_res.headers.get("Location")
    if not upload_url:
        return None, "missing_upload_location_header"

    # Step 2: Upload binary stream
    with open(video_path, "rb") as f:
        up_res = requests.put(
            upload_url,
            data=f,
            headers={"Content-Type": "video/mp4"},
            timeout=600,
        )

    if up_res.status_code not in (200, 201):
        return None, f"binary_upload_failed_{up_res.status_code}_{up_res.text[:150]}"

    video_id = up_res.json().get("id")
    print(f"  [Uploader] ✓ Successfully published video! ID: {video_id} (https://youtube.com/watch?v={video_id})")

    # Step 3: Upload Custom Thumbnail
    if thumbnail_path and thumbnail_path.exists():
        try:
            print("  [Uploader] Uploading custom high-contrast thumbnail...")
            with open(thumbnail_path, "rb") as tf:
                requests.post(
                    f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                    data=tf,
                    timeout=30,
                )
        except Exception as e:
            print(f"  [Uploader] Notice thumbnail upload: {e}")

    # Step 4: Upload SRT Captions
    if srt_path and srt_path.exists():
        try:
            print("  [Uploader] Attaching timed SRT subtitles...")
            caption_snippet = {
                "snippet": {
                    "videoId": video_id,
                    "language": "en",
                    "name": "English (Original)",
                    "isDraft": False
                }
            }
            with open(srt_path, "rb") as sf:
                requests.post(
                    f"https://www.googleapis.com/upload/youtube/v3/captions?part=snippet",
                    headers={"Authorization": f"Bearer {token}"},
                    files={
                        "snippet": (None, json.dumps(caption_snippet), "application/json"),
                        "media": ("captions.srt", sf, "text/plain")
                    },
                    timeout=30,
                )
        except Exception as e:
            print(f"  [Uploader] Notice captions upload: {e}")

    # Step 5: Pin initial comment
    try:
        pinned_comment_text = f"Which aspect of the {brand} {model} surprised you most? Drop your perspective below! Full documentary playlist linked in bio."
        requests.post(
            "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": pinned_comment_text
                        }
                    }
                }
            },
            timeout=20,
        )
    except Exception as e:
        print(f"  [Uploader] Notice pinning comment: {e}")

    return video_id, ""
