#!/usr/bin/env python3
"""
ig-influencer  –  Automated AI Instagram account
Modes: post | weekly_reel | upload_reel
Flag:  --dry-run  (generate content but do NOT commit or post)

Required environment variables (set as GitHub Secrets):
  IG_USER_ID        – Instagram Business/Creator account ID
  IG_ACCESS_TOKEN   – Long-lived Page access token
  GEMINI_API_KEY    – Google AI Studio API key
  GITHUB_REPOSITORY – owner/repo  (auto-set by Actions)
  GITHUB_REF_NAME   – branch name (auto-set by Actions)
"""

from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.resolve()
PERSONA_FILE = ROOT / "persona.json"
CONFIG_FILE = ROOT / "data" / "config.json"
LOG_FILE = ROOT / "data" / "log.json"
MEDIA_DIR = ROOT / "media"
UPLOADS_DIR = ROOT / "uploads"
UPLOADS_DONE_DIR = ROOT / "uploads" / "done"
MUSIC_DIR = ROOT / "assets" / "music"

IG_API_BASE = "https://graph.instagram.com/v21.0"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
RAW_GITHUB_BASE = "https://raw.githubusercontent.com"

LOG_MAX_ENTRIES = 500
MEDIA_WAIT_TIMEOUT = 300   # seconds to wait for raw.githubusercontent.com
MEDIA_WAIT_INTERVAL = 5    # seconds between checks
MEDIA_WAIT_INITIAL  = 15   # seconds to wait before first check (CDN propagation)
IG_PUBLISH_TIMEOUT = 180   # seconds to poll container status
IG_PUBLISH_INTERVAL = 10

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _env(key: str, required: bool = True) -> str:
    val = os.environ.get(key, "").strip()
    if required and not val:
        print(f"ERROR: Environment variable '{key}' is not set.")
        sys.exit(1)
    return val


def load_env() -> dict:
    return {
        "ig_user_id": _env("IG_USER_ID"),
        "ig_token": _env("IG_ACCESS_TOKEN"),
        "gemini_key": _env("GEMINI_API_KEY"),
        "gh_repo": _env("GITHUB_REPOSITORY"),   # owner/repo
        "gh_branch": _env("GITHUB_REF_NAME", required=False) or "main",
        "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip(),
    }


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

def load_persona() -> dict:
    if not PERSONA_FILE.exists():
        print(f"ERROR: persona.json not found at {PERSONA_FILE}")
        sys.exit(1)
    with open(PERSONA_FILE, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Config / pause check
# ---------------------------------------------------------------------------

def is_paused() -> bool:
    if not CONFIG_FILE.exists():
        return False
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("paused", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def write_log(entry: dict) -> None:
    """Append entry to data/log.json, keep last LOG_MAX_ENTRIES."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entries: list = []
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            entries = []
    entries.append(entry)
    entries = entries[-LOG_MAX_ENTRIES:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def log_ok(kind: str, caption: str, media: str, detail: str = "") -> None:
    write_log({
        "time": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "status": "ok",
        "caption": caption[:300],
        "media": media,
        "detail": detail,
    })


def log_fail(kind: str, detail: str, caption: str = "", media: str = "") -> None:
    write_log({
        "time": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "status": "failed",
        "caption": caption[:300],
        "media": media,
        "detail": detail,
    })


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

def call_gemini(prompt: str, env: dict) -> str:
    """Call Gemini API and return the text response."""
    url = (
        f"{GEMINI_API_BASE}/models/{env['gemini_model']}:generateContent"
        f"?key={env['gemini_key']}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 1.0,
            "topP": 0.95,
            "maxOutputTokens": 1024,
        },
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=60)
            r.raise_for_status()
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            print(f"  Gemini attempt {attempt + 1} failed: {exc}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError("Gemini API failed after 3 attempts")


def parse_json_response(text: str) -> dict:
    """Extract JSON object from a Gemini text response (may include markdown fences)."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    # Find first { and last }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON object found in Gemini response: {text[:200]}")
    return json.loads(text[start:end])


def generate_post_content(persona: dict, env: dict) -> dict:
    """Ask Gemini for scene + caption + hashtags for a photo post."""
    theme = random.choice(persona["themes"])
    prompt = f"""You are writing content for an AI virtual influencer named {persona['name']}.
Her voice: {persona['voice']}
Her visual style: {persona['style']}
Her typical themes include: {', '.join(persona['themes'])}

Generate a JSON object (no markdown, no extra text) for an Instagram photo post about this theme: "{theme}".
The JSON must have exactly these fields:
{{
  "scene": "<short image generation scene description, 1-2 sentences, vivid and visual>",
  "caption": "<2-3 sentence Instagram caption in her voice, max one emoji, not more>",
  "hashtags": ["hashtag1", "hashtag2", ..., "hashtag8"]
}}
Rules:
- caption must feel authentic, NOT generic or corporate
- hashtags must be lowercase, no # prefix, relevant and specific
- scene must be very descriptive for image generation
- she is an AI character – she can acknowledge this lightly if it feels natural
"""
    text = call_gemini(prompt, env)
    data = parse_json_response(text)
    # Validate
    assert "scene" in data and "caption" in data and "hashtags" in data, \
        f"Missing fields in Gemini response: {data}"
    if isinstance(data["hashtags"], list):
        data["hashtags"] = [h.lstrip("#").lower() for h in data["hashtags"]][:8]
    return data


def generate_reel_content(persona: dict, env: dict) -> dict:
    """Ask Gemini for 5 scenes for a trending-style weekly reel."""
    formats = [
        "outfit changes through the day",
        "day in my life as an AI character",
        "get ready with me (morning routine)",
        "my favorites this week montage",
        "places I wandered this week",
    ]
    reel_format = random.choice(formats)
    prompt = f"""You are writing content for an AI virtual influencer named {persona['name']}.
Her voice: {persona['voice']}
Her visual style: {persona['style']}

Generate a JSON object for a trending-style Instagram Reel in the format: "{reel_format}".
Return exactly this structure:
{{
  "format": "{reel_format}",
  "caption": "<engaging 2-3 sentence Reel caption in her voice, max one emoji>",
  "hashtags": ["tag1", ..., "tag10"],
  "scenes": [
    {{"scene": "<vivid image-generation prompt for scene 1>", "text_overlay": "<short overlay text or empty string>"}},
    {{"scene": "<scene 2>", "text_overlay": "<overlay 2>"}},
    {{"scene": "<scene 3>", "text_overlay": "<overlay 3>"}},
    {{"scene": "<scene 4>", "text_overlay": "<overlay 4>"}},
    {{"scene": "<scene 5>", "text_overlay": "<overlay 5>"}}
  ]
}}
Rules:
- exactly 5 scenes, each visually distinct yet cohesive
- hashtags lowercase, no # prefix, 8-10 tags
- caption feels authentic, Reel-friendly (energetic yet her voice)
"""
    text = call_gemini(prompt, env)
    data = parse_json_response(text)
    assert "scenes" in data and len(data["scenes"]) == 5, \
        f"Expected 5 scenes, got: {data}"
    data["hashtags"] = [h.lstrip("#").lower() for h in data.get("hashtags", [])][:10]
    return data


def generate_upload_caption(video_path: Path, persona: dict, env: dict) -> str:
    """Use Gemini to write a caption for a user-uploaded reel."""
    prompt = f"""You are writing content for an AI virtual influencer named {persona['name']}.
Her voice: {persona['voice']}

Write an Instagram Reel caption (2-3 sentences, max one emoji) for a video reel she is posting.
The filename is: {video_path.name}
Return ONLY the caption text – no JSON, no extra commentary.
"""
    return call_gemini(prompt, env).strip()


# ---------------------------------------------------------------------------
# Image generation (Pollinations.ai)
# ---------------------------------------------------------------------------

def build_image_prompt(persona: dict, scene: str) -> str:
    look = persona["look"]
    style = persona["style"]
    return f"{look}, {scene}, {style}, highly detailed, 8k"


def download_image(
    persona: dict,
    scene: str,
    output_path: Path,
    width: int = 1080,
    height: int = 1350,
) -> None:
    """Download an image from Pollinations.ai with the fixed persona seed."""
    prompt_text = build_image_prompt(persona, scene)
    encoded = urllib.parse.quote(prompt_text)
    seed = persona.get("seed", 42817)
    url = f"{POLLINATIONS_BASE}/{encoded}?width={width}&height={height}&seed={seed}&model=flux&nologo=true"

    print(f"  Generating image: {output_path.name}")
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=120, stream=True)
            r.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            size = output_path.stat().st_size
            if size < 10_000:
                raise ValueError(f"Image too small ({size} bytes), likely an error page")
            print(f"  Image saved: {output_path} ({size // 1024} KB)")
            return
        except Exception as exc:
            print(f"  Image attempt {attempt + 1} failed: {exc}")
            if output_path.exists():
                output_path.unlink()
            if attempt < 4:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Pollinations.ai failed after 5 attempts for scene: {scene[:80]}")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_run(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result


def git_commit_and_push(files: list[Path], message: str) -> None:
    """Stage given files, pull --rebase, then push."""
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        r = git_run("add", rel)
        if r.returncode != 0:
            raise RuntimeError(f"git add failed: {r.stderr}")

    r = git_run("diff", "--cached", "--quiet")
    if r.returncode == 0:
        print("  Nothing to commit.")
        return

    r = git_run("commit", "-m", message)
    if r.returncode != 0:
        raise RuntimeError(f"git commit failed: {r.stderr}")

    # Pull rebase first to avoid conflicts
    r = git_run("pull", "--rebase", "origin", _get_branch())
    if r.returncode != 0:
        print(f"  git pull --rebase warning (continuing): {r.stderr}")

    r = git_run("push", "origin", _get_branch())
    if r.returncode != 0:
        raise RuntimeError(f"git push failed: {r.stderr}")
    print(f"  Pushed: {message}")


def _get_branch() -> str:
    branch = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not branch:
        r = git_run("rev-parse", "--abbrev-ref", "HEAD")
        branch = r.stdout.strip() or "main"
    return branch


def wait_for_raw_url(gh_repo: str, branch: str, file_path: Path) -> str:
    """Wait until the raw.githubusercontent.com URL is accessible (HTTP 200)."""
    rel = file_path.relative_to(ROOT).as_posix()
    url = f"{RAW_GITHUB_BASE}/{gh_repo}/{branch}/{rel}"
    print(f"  Waiting for raw URL: {url}")
    print(f"  Giving GitHub CDN {MEDIA_WAIT_INITIAL}s to propagate...")
    time.sleep(MEDIA_WAIT_INITIAL)
    deadline = time.time() + MEDIA_WAIT_TIMEOUT
    while time.time() < deadline:
        try:
            r = requests.head(url, timeout=15)
            if r.status_code == 200:
                print(f"  URL ready (HTTP 200)")
                return url
            print(f"  Not ready yet (HTTP {r.status_code}), retrying...")
        except Exception as exc:
            print(f"  URL check error: {exc}, retrying...")
        time.sleep(MEDIA_WAIT_INTERVAL)
    raise TimeoutError(f"raw.githubusercontent.com URL not ready after {MEDIA_WAIT_TIMEOUT}s: {url}")


# ---------------------------------------------------------------------------
# Instagram Graph API
# ---------------------------------------------------------------------------

def ig_post(endpoint: str, env: dict, payload: dict) -> dict:
    """POST to Instagram Graph API."""
    url = f"{IG_API_BASE}/{endpoint}"
    params = {"access_token": env["ig_token"]}
    for attempt in range(3):
        try:
            r = requests.post(url, params=params, json=payload, timeout=60)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            body = exc.response.text if exc.response else ""
            print(f"  IG API error (attempt {attempt + 1}): {exc} – {body[:300]}")
            if attempt < 2:
                time.sleep(10)
        except Exception as exc:
            print(f"  IG API error (attempt {attempt + 1}): {exc}")
            if attempt < 2:
                time.sleep(10)
    raise RuntimeError(f"Instagram API POST /{endpoint} failed after 3 attempts")


def ig_get(endpoint: str, env: dict, params: dict | None = None) -> dict:
    """GET from Instagram Graph API."""
    url = f"{IG_API_BASE}/{endpoint}"
    p = {"access_token": env["ig_token"]}
    if params:
        p.update(params)
    r = requests.get(url, params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def poll_container_status(container_id: str, env: dict) -> None:
    """Poll until container status is FINISHED (or raise on ERROR)."""
    print(f"  Polling container {container_id}...")
    deadline = time.time() + IG_PUBLISH_TIMEOUT
    while time.time() < deadline:
        data = ig_get(container_id, env, {"fields": "status_code"})
        status = data.get("status_code", "")
        print(f"    status_code: {status}")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Instagram container processing failed: {data}")
        time.sleep(IG_PUBLISH_INTERVAL)
    raise TimeoutError(f"Container {container_id} not FINISHED after {IG_PUBLISH_TIMEOUT}s")


def publish_photo(image_url: str, caption: str, env: dict) -> str:
    """Create a photo container and publish it. Returns media ID."""
    ig_user = env["ig_user_id"]
    full_caption = caption  # hashtags already embedded in caption
    print(f"  Creating photo container...")
    data = ig_post(
        f"{ig_user}/media",
        env,
        {"image_url": image_url, "caption": full_caption},
    )
    container_id = data["id"]
    poll_container_status(container_id, env)
    print(f"  Publishing photo...")
    result = ig_post(
        f"{ig_user}/media_publish",
        env,
        {"creation_id": container_id},
    )
    return result["id"]


def publish_reel(video_url: str, caption: str, env: dict) -> str:
    """Create a Reel container and publish it. Returns media ID."""
    ig_user = env["ig_user_id"]
    print(f"  Creating reel container...")
    data = ig_post(
        f"{ig_user}/media",
        env,
        {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": True,
        },
    )
    container_id = data["id"]
    poll_container_status(container_id, env)
    print(f"  Publishing reel...")
    result = ig_post(
        f"{ig_user}/media_publish",
        env,
        {"creation_id": container_id},
    )
    return result["id"]


# ---------------------------------------------------------------------------
# Caption builder
# ---------------------------------------------------------------------------

def build_full_caption(caption: str, hashtags: list[str]) -> str:
    tags = " ".join(f"#{h}" for h in hashtags)
    return f"{caption}\n\n{tags}"


# ---------------------------------------------------------------------------
# Video builder (FFmpeg)
# ---------------------------------------------------------------------------

def build_reel_video(image_paths: list[Path], output_path: Path) -> None:
    """Build a ~20 second 9:16 video from 5 images using FFmpeg zoompan."""
    duration_per_image = 4  # seconds each → total ~20s
    width, height = 1080, 1920

    filter_parts: list[str] = []
    inputs: list[str] = []

    for i, img in enumerate(image_paths):
        inputs += ["-loop", "1", "-t", str(duration_per_image), "-i", str(img)]

    # Build zoompan filtergraph
    streams: list[str] = []
    for i in range(len(image_paths)):
        # Alternate zoom direction for variety
        zoom_dir = "in" if i % 2 == 0 else "out"
        if zoom_dir == "in":
            zoom_expr = "min(zoom+0.0015,1.5)"
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "ih/2-(ih/zoom/2)"
        else:
            zoom_expr = "if(lte(zoom,1.0),1.5,max(zoom-0.0015,1.0))"
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "ih/2-(ih/zoom/2)"

        fps = 25
        n_frames = duration_per_image * fps
        filter_parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
            f"d={n_frames}:s={width}x{height}:fps={fps}[v{i}]"
        )
        streams.append(f"[v{i}]")

    concat_filter = "".join(f"{s}" for s in streams)
    concat_filter += f"concat=n={len(image_paths)}:v=1:a=0[vout]"
    full_filter = ";".join(filter_parts) + ";" + concat_filter

    # Check for music
    music_files = list(MUSIC_DIR.glob("*.mp3"))
    has_music = bool(music_files)
    music_path = random.choice(music_files) if has_music else None

    cmd = [*inputs]
    if has_music:
        cmd += ["-i", str(music_path)]

    cmd += [
        "-filter_complex", full_filter,
        "-map", "[vout]",
    ]

    if has_music:
        music_idx = len(image_paths)
        cmd += [
            "-map", f"{music_idx}:a",
            "-shortest",
            "-c:a", "aac",
            "-b:a", "128k",
        ]

    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "23",
        "-preset", "fast",
        "-movflags", "+faststart",
        str(output_path),
        "-y",
    ]

    print(f"  Running FFmpeg to build reel video...")
    result = subprocess.run(
        ["ffmpeg", *cmd],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"FFmpeg stderr: {result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg failed with code {result.returncode}")
    print(f"  Video built: {output_path}")


# ---------------------------------------------------------------------------
# Mode: post
# ---------------------------------------------------------------------------

def mode_post(persona: dict, env: dict, dry_run: bool) -> None:
    """Generate and publish a single photo post."""
    print("\n=== MODE: post ===")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print("Generating post content with Gemini...")
    content = generate_post_content(persona, env)
    scene = content["scene"]
    caption_text = content["caption"]
    hashtags = content["hashtags"]
    full_caption = build_full_caption(caption_text, hashtags)

    print(f"\nScene:   {scene}")
    print(f"Caption: {caption_text}")
    print(f"Tags:    {' '.join('#' + h for h in hashtags)}")

    image_path = MEDIA_DIR / f"post_{ts}.jpg"

    if dry_run:
        print(f"\n[DRY RUN] Would download image → {image_path}")
        print("[DRY RUN] Would commit and push image")
        print("[DRY RUN] Would publish to Instagram")
        return

    print("\nDownloading image...")
    download_image(persona, scene, image_path, width=1080, height=1350)

    print("Committing image to repo...")
    git_commit_and_push([image_path], f"chore: add post image {ts}")

    print("Waiting for raw URL to be available...")
    raw_url = wait_for_raw_url(env["gh_repo"], env["gh_branch"], image_path)

    print("Publishing to Instagram...")
    media_id = publish_photo(raw_url, full_caption, env)
    print(f"Published! media_id={media_id}")

    print("Committing log...")
    log_ok("post", full_caption, str(image_path.name), f"media_id={media_id}")
    git_commit_and_push([LOG_FILE], f"chore: log post {ts}")


# ---------------------------------------------------------------------------
# Mode: weekly_reel
# ---------------------------------------------------------------------------

def mode_weekly_reel(persona: dict, env: dict, dry_run: bool) -> None:
    """Generate and publish a weekly reel from 5 AI images."""
    print("\n=== MODE: weekly_reel ===")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print("Generating reel content with Gemini...")
    content = generate_reel_content(persona, env)
    scenes = content["scenes"]
    caption_text = content["caption"]
    hashtags = content["hashtags"]
    full_caption = build_full_caption(caption_text, hashtags)

    print(f"\nFormat:  {content.get('format', 'N/A')}")
    print(f"Caption: {caption_text}")
    for i, sc in enumerate(scenes):
        print(f"Scene {i + 1}: {sc['scene'][:80]}...")

    image_paths: list[Path] = []
    video_path = MEDIA_DIR / f"reel_{ts}.mp4"

    if dry_run:
        print(f"\n[DRY RUN] Would generate 5 images and build video → {video_path}")
        print("[DRY RUN] Would commit and publish reel")
        return

    print("\nGenerating 5 images...")
    for i, sc in enumerate(scenes):
        img_path = MEDIA_DIR / f"reel_{ts}_scene{i + 1}.jpg"
        download_image(persona, sc["scene"], img_path, width=1080, height=1920)
        image_paths.append(img_path)

    print("Building reel video with FFmpeg...")
    build_reel_video(image_paths, video_path)

    print("Committing video to repo...")
    files_to_commit = image_paths + [video_path]
    git_commit_and_push(files_to_commit, f"chore: add weekly reel {ts}")

    print("Waiting for raw URL...")
    raw_url = wait_for_raw_url(env["gh_repo"], env["gh_branch"], video_path)

    print("Publishing reel to Instagram...")
    media_id = publish_reel(raw_url, full_caption, env)
    print(f"Published! media_id={media_id}")

    log_ok("weekly_reel", full_caption, video_path.name, f"media_id={media_id}")
    git_commit_and_push([LOG_FILE], f"chore: log weekly_reel {ts}")


# ---------------------------------------------------------------------------
# Mode: upload_reel
# ---------------------------------------------------------------------------

def mode_upload_reel(persona: dict, env: dict, dry_run: bool) -> None:
    """Publish any .mp4 files waiting in uploads/."""
    print("\n=== MODE: upload_reel ===")

    mp4_files = sorted(UPLOADS_DIR.glob("*.mp4"))
    if not mp4_files:
        print("No .mp4 files found in uploads/. Nothing to do.")
        return

    any_committed = False

    for video_path in mp4_files:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        print(f"\nProcessing: {video_path.name}")

        # Caption: prefer a same-name .txt file; otherwise ask Gemini
        txt_path = video_path.with_suffix(".txt")
        if txt_path.exists():
            raw_caption = txt_path.read_text(encoding="utf-8").strip()
            print(f"  Using caption from {txt_path.name}")
        else:
            print("  No caption file found; generating caption with Gemini...")
            raw_caption = generate_upload_caption(video_path, persona, env)

        # Build hashtags via Gemini
        hashtag_prompt = f"""Generate 8 relevant Instagram hashtags for a reel.
The reel caption is: {raw_caption[:200]}
Return ONLY a JSON array of lowercase strings without # prefix.
"""
        try:
            ht_text = call_gemini(hashtag_prompt, env).strip()
            # Extract JSON array
            start = ht_text.find("[")
            end = ht_text.rfind("]") + 1
            hashtags = json.loads(ht_text[start:end]) if start != -1 else []
            hashtags = [h.lstrip("#").lower() for h in hashtags][:8]
        except Exception:
            hashtags = []

        full_caption = build_full_caption(raw_caption, hashtags)
        print(f"  Caption: {raw_caption[:100]}...")

        # Target path in media/
        dest_video = MEDIA_DIR / f"upload_{ts}_{video_path.name}"
        dest_done = UPLOADS_DONE_DIR / video_path.name

        if dry_run:
            print(f"  [DRY RUN] Would move {video_path.name} → media/ and publish reel")
            continue

        # Move to media/
        import shutil
        shutil.copy2(str(video_path), str(dest_video))

        # Commit
        files_to_commit = [dest_video]
        if txt_path.exists():
            files_to_commit.append(txt_path)
        git_commit_and_push(files_to_commit, f"chore: add upload reel {ts}")

        # Wait for raw URL
        try:
            raw_url = wait_for_raw_url(env["gh_repo"], env["gh_branch"], dest_video)
        except TimeoutError as exc:
            log_fail("reel_upload", str(exc), full_caption, dest_video.name)
            git_commit_and_push([LOG_FILE], f"chore: log upload_reel failure {ts}")
            # Don't block other uploads
            print(f"  ERROR: {exc}")
            continue

        # Publish
        try:
            media_id = publish_reel(raw_url, full_caption, env)
            print(f"  Published! media_id={media_id}")
        except Exception as exc:
            log_fail("reel_upload", str(exc), full_caption, dest_video.name)
            git_commit_and_push([LOG_FILE], f"chore: log upload_reel failure {ts}")
            print(f"  ERROR publishing reel: {exc}")
            continue

        # Move original to done/
        UPLOADS_DONE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(video_path), str(dest_done))
        if txt_path.exists():
            shutil.move(str(txt_path), str(UPLOADS_DONE_DIR / txt_path.name))

        log_ok("reel_upload", full_caption, dest_video.name, f"media_id={media_id}")

        # Stage the moved files
        git_run("add", "uploads/")
        any_committed = True

    if any_committed and not dry_run:
        # Commit done moves + log
        r = git_run("diff", "--cached", "--quiet")
        if r.returncode != 0:
            git_run("commit", "-m", f"chore: move uploaded reels to done")
            git_run("pull", "--rebase", "origin", _get_branch())
            git_run("push", "origin", _get_branch())

        git_commit_and_push([LOG_FILE], f"chore: log upload_reel batch")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

MODES = {"post", "weekly_reel", "upload_reel"}


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if not args or args[0] not in MODES:
        print(f"Usage: python main.py <mode> [--dry-run]")
        print(f"Modes: {', '.join(sorted(MODES))}")
        sys.exit(1)

    mode = args[0]

    if dry_run:
        print("=== DRY RUN MODE (no commits, no posts) ===")

    # Check pause
    if is_paused():
        print("Automation is paused (data/config.json paused=true). Exiting.")
        sys.exit(0)

    persona = load_persona()
    print(f"Persona: {persona['name']}")

    if dry_run:
        # In dry-run we don't need real IG credentials
        env = {
            "ig_user_id": "DRY_RUN",
            "ig_token": "DRY_RUN",
            "gemini_key": _env("GEMINI_API_KEY"),
            "gh_repo": os.environ.get("GITHUB_REPOSITORY", "owner/repo"),
            "gh_branch": os.environ.get("GITHUB_REF_NAME", "main"),
            "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
        }
    else:
        env = load_env()

    try:
        if mode == "post":
            mode_post(persona, env, dry_run)
        elif mode == "weekly_reel":
            mode_weekly_reel(persona, env, dry_run)
        elif mode == "upload_reel":
            mode_upload_reel(persona, env, dry_run)
    except Exception as exc:
        print(f"\nFATAL ERROR in mode '{mode}': {exc}")
        if not dry_run:
            try:
                log_fail(mode, str(exc))
                git_commit_and_push([LOG_FILE], f"chore: log {mode} failure")
            except Exception as log_exc:
                print(f"  Also failed to write failure log: {log_exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
