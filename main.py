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
import re
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
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

IG_API_BASE = "https://graph.facebook.com/v21.0"
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
        "gh_token": os.environ.get("GITHUB_TOKEN", "").strip(),
    }


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

def load_persona() -> dict:
    if not PERSONA_FILE.exists():
        print(f"ERROR: persona.json not found at {PERSONA_FILE}")
        sys.exit(1)
    with open(PERSONA_FILE, encoding="utf-8-sig") as f:
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

def sanitize_log_str(s: str) -> str:
    """Redact any keys or tokens from log strings."""
    if not isinstance(s, str):
        return s
    s = re.sub(r'key=[a-zA-Z0-9_\-\.]+', 'key=[REDACTED]', s)
    s = re.sub(r'AQ\.[a-zA-Z0-9_\-\.]+', '[REDACTED]', s)
    s = re.sub(r'AIzaSy[a-zA-Z0-9_\-\.]+', '[REDACTED]', s)
    s = re.sub(r'access_token=[a-zA-Z0-9_\-\.]+', 'access_token=[REDACTED]', s)
    return s


def write_log(entry: dict) -> None:
    """Append entry to data/log.json, keep last LOG_MAX_ENTRIES."""
    # Sanitize every field to prevent credential leakage
    entry = {k: (sanitize_log_str(v) if isinstance(v, str) else v) for k, v in entry.items()}
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

def call_pollinations_text(prompt: str, json_mode: bool = False) -> str:
    """Free zero-cost text generation (OpenAI model, no key, no rate limits)."""
    url = "https://text.pollinations.ai/"
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "model": "openai",
        "jsonMode": json_mode,
        "seed": random.randint(1000, 999999),
    }
    print("  Generating text via Pollinations (free tier, no quota limits)...")
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.text


def call_gemini(prompt: str, env: dict, json_mode: bool = False) -> str:
    """Call Gemini API with automatic instant fallback to Pollinations Text on timeouts/spikes."""
    model_name = env.get('gemini_model', 'gemini-3.8-flash')
    gemini_key = env.get('gemini_key', '').strip()

    # Try Gemini if key is provided
    if gemini_key:
        url = f"{GEMINI_API_BASE}/models/{model_name}:generateContent"
        headers = {
            "x-goog-api-key": gemini_key,
            "Content-Type": "application/json"
        }
        gen_config = {
            "temperature": 0.8,
            "maxOutputTokens": 2048,
        }
        if json_mode:
            gen_config["responseMimeType"] = "application/json"

        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }

        for attempt in range(2):
            try:
                print(f"  Gemini attempt {attempt + 1} calling '{model_name}'...")
                r = requests.post(url, headers=headers, json=body, timeout=25)
                if not r.ok:
                    err_text = r.text[:200]
                    print(f"  Gemini HTTP {r.status_code} ({model_name}): {err_text}")
                r.raise_for_status()
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as exc:
                print(f"  Gemini attempt {attempt + 1} failed: {exc}")
                if attempt == 0:
                    time.sleep(3)

    # Automatic fallback to free Pollinations Text!
    try:
        return call_pollinations_text(prompt, json_mode=json_mode)
    except Exception as exc:
        raise RuntimeError(f"Text generation failed across all providers: {exc}")


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


def generate_fallback_post(persona: dict) -> dict:
    """Generate high-engagement impossible luxury architecture post directly from persona."""
    theme = random.choice(persona["themes"])
    templates = [
        (
            f"Breathtaking cantilevered cliffside glass villa suspended above the ocean in {theme}, Architectural Digest style, infinity pool, warm golden hour twilight, photorealistic.",
            "The Obsidian Sanctuary. Perched 300 meters above the open ocean, where raw concrete meets endless horizons. Could you live here completely off the grid? Rate this design 1–10 below 👇",
            ["architecturelovers", "luxuryvilla", "modernarchitecture", "architecturaldigest", "dreamhome", "interiorarchitecture", "luxurylifestyle", "surrealarchitecture"]
        ),
        (
            f"Minimalist modern Japanese onsen pavilion in {theme}, charred cedar timber, steaming natural stone pool, serene misty morning light, award-winning architecture.",
            "Komorebi Pavilion. Deep in the misty bamboo forest, designed around the sound of flowing water and quiet contemplation. Tag someone who needs this level of peace ✨",
            ["japanesearchitecture", "zenliving", "modernretreat", "minimalistdesign", "luxuryarchitecture", "architecturedose", "aestheticvibes", "designinspiration"]
        ),
        (
            f"Subterranean desert luxury sanctuary in {theme}, organic curved travertine walls, skylight reflecting pool, dramatic architectural shadows, dusk lighting.",
            "The Dunes Villa. Carved seamlessly into the desert landscape to stay naturally cool under the scorching sun. Would you spend a weekend here? Drop your thoughts 👇",
            ["desertarchitecture", "organicmodernism", "brutalisminnature", "luxuryrealestate", "modernluxury", "interiorlovers", "architecturalphotography", "dreamresidence"]
        ),
        (
            f"Nordic glass and blackened timber villa in {theme}, panoramic floor-to-ceiling glass, warm interior fireplace glow, dramatic snow-capped peaks and fjord.",
            "Fjord House. Floating between the icy waters and the northern sky. Designed for warmth against the wild. Who would you bring to watch the aurora from here? ❄️",
            ["nordicdesign", "scandinavianhome", "cozyretreat", "luxurychalet", "contemporaryarchitecture", "mountainarchitecture", "beautifuldestinations", "cabinlife"]
        ),
    ]
    scene, caption, tags = random.choice(templates)
    return {"scene": scene, "caption": caption, "hashtags": tags}


def generate_post_content(persona: dict, env: dict) -> dict:
    """Generate high-engagement Architectural Digest style post content via Gemini."""
    theme = random.choice(persona["themes"])
    prompt = f"""You are the creative director of a world-famous luxury architecture and design magazine named "{persona['name']}".
Theme: "{theme}"

Generate a JSON object (no markdown, no extra text) for an insanely viral, captivating Instagram post about this architectural masterpiece.
The JSON must have exactly these fields:
{{
  "scene": "<hyper-detailed architectural photography description, Architectural Digest style, award-winning luxury villa or sanctuary, materials like travertine, charred timber, floor-to-ceiling glass, infinity pools, volumetric golden hour or blue hour lighting, 8k photorealistic>",
  "caption": "<2-3 sentence evocative, captivating caption. Give the project a fictional name (e.g. 'The Obsidian Villa', 'Villa Solaria'), describe the location and architectural highlight, and end with an interactive question to spark comments (e.g. 'Could you live here off-grid? Rate this design from 1-10 👇')>",
  "hashtags": ["architecturelovers", "modernarchitecture", "luxuryvilla", "architecturaldigest", "dreamhome", "interiorarchitecture", "surrealarchitecture", "contemporarydesign"]
}}
Rules:
- NEVER mention AI, virtual, render, or algorithm
- caption must sound like a prestigious architectural magazine
- hashtags must be 8 relevant high-traffic architectural tags, lowercase, no # prefix
"""
    try:
        text = call_gemini(prompt, env, json_mode=True)
        data = parse_json_response(text)
    except Exception as exc:
        print(f"  External LLM API unavailable ({exc}). Using persona-grounded generator...")
        data = generate_fallback_post(persona)

    # Validate
    if "scene" not in data or "caption" not in data or "hashtags" not in data:
        data = generate_fallback_post(persona)
    if isinstance(data["hashtags"], list):
        data["hashtags"] = [h.lstrip("#").lower() for h in data["hashtags"]][:8]
    return data


def generate_fallback_reel(persona: dict) -> dict:
    """Cohesive 5-scene virtual walkthrough of a single luxury sanctuary (cinematic tour edit)."""
    tours = [
        (
            "Inside Villa Obsidian: Suspended 300m Above The Amalfi Sea",
            "A seamless tour through Villa Obsidian. Where raw volcanic stone, cantilevered glass, and endless horizons merge into pure serenity. Would you spend a week here completely off the grid? Rate this home 1–10 👇",
            [
                {"scene": "Dramatic wide aerial shot of Villa Obsidian, a cantilevered glass and blackened timber villa hovering 300m above the turquoise Amalfi sea at golden hour", "text_overlay": "01 // THE ARRIVAL"},
                {"scene": "Grand entrance of Villa Obsidian with floating travertine stepping stones over shallow reflecting pool leading to a massive pivoting glass door", "text_overlay": "02 // THE ENTRANCE"},
                {"scene": "Interior living pavilion of Villa Obsidian with sunken minimalist seating, raw concrete walls, and panoramic floor-to-ceiling glass looking over the ocean", "text_overlay": "03 // LIVING HORIZON"},
                {"scene": "Cantilevered infinity glass-bottom pool of Villa Obsidian suspended out over the cliff edge with water spilling toward the sea", "text_overlay": "04 // THE INFINITY EDGE"},
                {"scene": "Twilight blue hour view of Villa Obsidian with illuminated warm interior, sunken outdoor fire pit burning softly under twilight sky", "text_overlay": "05 // TWILIGHT SANCTUARY"},
            ],
            ["architecturelovers", "luxuryvilla", "architecturaldigest", "dreamhome", "villatour", "modernarchitecture", "surrealarchitecture", "reelsviral", "luxurydesign", "designinspiration"]
        ),
        (
            "24 Hours Inside Komorebi Sanctuary: The Hidden Kyoto Forest Haven",
            "Experience peace at Komorebi Sanctuary, tucked deep within the mist of Kyoto. Built around the flow of natural water and ancient cedar. Who would you bring here for total tranquility? Drop a comment 👇",
            [
                {"scene": "Misty bamboo forest clearing revealing the charred cedar timber architecture of Komorebi Sanctuary in Kyoto at dawn", "text_overlay": "01 // THE FOREST APPROACH"},
                {"scene": "Minimalist Japanese entry veranda with paper shoji screens, smooth river stone path, and morning dew", "text_overlay": "02 // MINIMALIST ARRIVAL"},
                {"scene": "Open-air tatami tea lounge with low wooden table framing misty bamboo groves through floor-to-ceiling frameless glass", "text_overlay": "03 // THE TEA PAVILION"},
                {"scene": "Steaming natural thermal onsen pool carved from volcanic black granite surrounded by soft moss and bamboo", "text_overlay": "04 // THERMAL ONSEN"},
                {"scene": "Evening dusk view of Komorebi Pavilion with soft paper lanterns casting warm amber glow across steaming water", "text_overlay": "05 // DUSK TRANQUILITY"},
            ],
            ["japanesearchitecture", "zenliving", "forestsanctuary", "luxuryretreat", "architecturedose", "aestheticvibes", "designinspiration", "modernzen", "architecturaldigest", "reelsinsta"]
        ),
        (
            "The Mirage Glasshouse: Utah's Subterranean Desert Oasis",
            "Hidden beneath the red rock canyons of Utah lies The Mirage. A subterranean architectural marvel that stays naturally cool under the desert sun. Could you live here off the grid? Drop your thoughts 👇",
            [
                {"scene": "Dramatic desert aerial reveal of The Mirage Glasshouse carved organically into soaring red sandstone rock formations", "text_overlay": "01 // DESERT CANYON REVEAL"},
                {"scene": "Curved raw concrete entrance ramp descending beneath a monumental rock archway into sun-dappled courtyard", "text_overlay": "02 // THE SUBTERRANEAN ENTRY"},
                {"scene": "Sunken desert living lounge with natural rock walls, skylight streaming daylight into oasis reflecting pool", "text_overlay": "03 // SUNKEN ROCK LOUNGE"},
                {"scene": "Private subterranean lap pool illuminated by hidden linear cove lighting reflecting off raw desert stone", "text_overlay": "04 // OASIS POOL"},
                {"scene": "Starlit night view looking up through the open glass atrium at the Milky Way above towering red canyon walls", "text_overlay": "05 // DESERT STARRY NIGHT"},
            ],
            ["desertarchitecture", "organicmodernism", "luxuryrealestate", "modernluxury", "brutalisminnature", "dreamresidence", "architecturalphotography", "canyonretreat", "reelsviral", "designlovers"]
        )
    ]
    title, caption, scenes, tags = random.choice(tours)
    return {
        "format": title,
        "caption": caption,
        "hashtags": tags,
        "scenes": scenes
    }


def generate_reel_content(persona: dict, env: dict) -> dict:
    """Generate a cohesive 5-shot architectural tour edit of a single luxury villa via Gemini."""
    prompt = f"""You are the creative director of a prestigious architectural design publication named "{persona['name']}".

Create a JSON object for an ultra-viral, cinematic Instagram Reel that presents a COHESIVE 5-SHOT VIRTUAL TOUR of ONE SINGLE dream sanctuary (NOT 5 random different houses).
Every shot must depict the SAME property from progressive angles (Arrival -> Entrance -> Interior Lounge -> Signature Pool/Feature -> Evening Twilight).

Return exactly this JSON structure:
{{
  "format": "<Punchy title e.g. 'Inside Villa Solaria: Suspended Above the Sea'>",
  "caption": "<Engaging 2-3 sentence reel caption describing this single property walkthrough, ending with a question to drive comments, max one emoji, NEVER mention AI>",
  "hashtags": ["architecturelovers", "luxuryvilla", "architecturaldigest", "dreamhome", "villatour", "modernarchitecture", "surrealarchitecture", "reelsviral", "luxurydesign", "designinspiration"],
  "scenes": [
    {{"scene": "<Shot 1: Dramatic exterior wide reveal of the villa, materials, setting, Architectural Digest style>", "text_overlay": "01 // THE ARRIVAL"}},
    {{"scene": "<Shot 2: Stepping into the grand entrance/approach of the same villa>", "text_overlay": "02 // THE ENTRANCE"}},
    {{"scene": "<Shot 3: Main living lounge/interior of the same villa with panoramic views>", "text_overlay": "03 // LIVING HORIZON"}},
    {{"scene": "<Shot 4: Signature infinity pool/outdoor terrace of the same villa>", "text_overlay": "04 // INFINITY EDGE"}},
    {{"scene": "<Shot 5: Evening blue-hour view of the same villa with warm ambient lighting and firepit>", "text_overlay": "05 // TWILIGHT SANCTUARY"}}
  ]
}}
Rules:
- All 5 scenes MUST feature the SAME property and architecture style so the reel forms a cohesive video tour edit
- NEVER mention AI, virtual, render, or algorithm
- hashtags lowercase, no # prefix, 8-10 tags
"""
    try:
        text = call_gemini(prompt, env, json_mode=True)
        data = parse_json_response(text)
        assert "scenes" in data and len(data["scenes"]) == 5
        data["hashtags"] = [h.lstrip("#").lower() for h in data.get("hashtags", [])][:10]
        return data
    except Exception as exc:
        print(f"  External LLM API unavailable ({exc}). Using curated cohesive tour edit generator...")
        return generate_fallback_reel(persona)

def generate_upload_caption(video_path: Path, persona: dict, env: dict) -> str:
    """Use Gemini to write a caption for a user-uploaded reel."""
    prompt = f"""You are the creative director of a prestigious architecture publication named "{persona['name']}".
Write an Instagram Reel caption (2-3 sentences, max one emoji) for an architectural tour video reel.
The filename is: {video_path.name}
NEVER mention AI, algorithm, or code. End with an interactive question to spark comments. Return ONLY the caption text.
"""
    try:
        return call_gemini(prompt, env).strip()
    except Exception:
        return "An architectural masterpiece where visionary design meets pure tranquility. Which detail stands out most to you? Drop your thoughts below 👇"


# ---------------------------------------------------------------------------
# Image generation (Pollinations.ai)
# ---------------------------------------------------------------------------

ARCH_ASSETS_DIR = Path(__file__).parent / "assets" / "architecture"
ARCH_INDEX_FILE = Path(__file__).parent / "data" / "arch_index.json"
ERIKA_INDEX_FILE = ARCH_INDEX_FILE  # backwards compatibility


def get_next_curated_architecture_image() -> Path:
    """Cycle sequentially through the verified luxury architecture gallery."""
    ARCH_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    images = sorted(list(ARCH_ASSETS_DIR.glob("*.jpg")) + list(ARCH_ASSETS_DIR.glob("*.png")))
    if not images:
        # Fallback to erika dir if architecture is empty
        fallback_dir = Path(__file__).parent / "assets" / "erika"
        images = sorted(list(fallback_dir.glob("*.jpg")) + list(fallback_dir.glob("*.png")))
    if not images:
        raise FileNotFoundError(f"No curated images found in {ARCH_ASSETS_DIR}")

    idx = 0
    if ARCH_INDEX_FILE.exists():
        try:
            with open(ARCH_INDEX_FILE, encoding="utf-8") as f:
                data = json.load(f)
                idx = data.get("index", 0)
        except Exception:
            idx = 0

    chosen = images[idx % len(images)]
    next_idx = (idx + 1) % len(images)
    try:
        ARCH_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ARCH_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({"index": next_idx, "last_image": chosen.name}, f, indent=2)
    except Exception:
        pass
    return chosen


# Photorealistic models to cycle through (all produce natural-looking results)
REALISTIC_MODELS = [
    "Realistic Vision",
    "AbsoluteReality",
    "ICBINP - I Can't Believe It's Not Photography",
    "Photon",
    "Edge Of Realism",
    "majicMIX realistic",
]


def generate_image_aihorde(persona: dict, scene: str, output_path: Path, width: int = 1080, height: int = 1350) -> bool:
    """Generate hyper-realistic architectural photography via AI Horde."""
    headers = {
        "apikey": "0000000000",
        "Client-Agent": "AuraSanctuaries:1.0:architecture@example.com"
    }

    # High-end Architectural Digest photography prompt
    arch_prefix = (
        "RAW architectural photograph, Architectural Digest feature, award-winning contemporary luxury sanctuary, "
        "shot on Hasselblad H6D-100c, 24mm tilt-shift architectural lens, volumetric atmospheric lighting, "
        "photorealistic textures of travertine stone, raw concrete, floor-to-ceiling glass, warm timber, "
    )
    style_suffix = (
        "clean geometric lines, golden hour sunlight, hyperrealistic reflections in water, "
        "high-end interior design, cinematic luxury atmosphere, 8k resolution, crisp architectural detail"
    )
    negative = (
        "people, humans, crowd, man, woman, face, "
        "ugly, distorted architecture, crooked walls, melting geometry, "
        "cartoon, anime, painting, illustration, 3d render, cgi, video game graphics, "
        "blurry, low resolution, watermark, text, signature, oversaturated"
    )

    prompt = f"{arch_prefix} {scene[:170]}, {style_suffix} ### {negative}"

    # Unique random seed per generation so AI images are never repeated
    seed = str(random.randint(10000, 9999999))

    # Use any free photorealistic model on the network
    REALISTIC_MODELS = [
        "Realistic Vision",
        "AbsoluteReality",
        "ICBINP - I Can't Believe It's Not Photography",
        "Photon",
        "Edge Of Realism",
        "majicMIX realistic",
    ]

    payload = {
        "prompt": prompt,
        "params": {
            "steps": 28,
            "width": 512,
            "height": 640,
            "sampler_name": "k_dpmpp_2m",
            "cfg_scale": 6.5,
            "seed": seed,
        },
        "models": REALISTIC_MODELS,  # Allow any of these workers to pick up instantly
    }
    try:
        print(f"  Generating architectural sanctuary image via AI Horde (seed: {seed})...")
        r = requests.post("https://aihorde.net/api/v2/generate/async", headers=headers, json=payload, timeout=20)
        job_id = r.json().get("id")
        if not job_id:
            print(f"  AI Horde job submission failed: {r.text[:200]}")
            return False

        # Wait up to ~60s
        for _ in range(20):
            time.sleep(3)
            check = requests.get(f"https://aihorde.net/api/v2/generate/check/{job_id}", headers=headers, timeout=10).json()
            if check.get("done"):
                break

        status = requests.get(f"https://aihorde.net/api/v2/generate/status/{job_id}", headers=headers, timeout=15).json()
        gen = status.get("generations", [{}])[0]
        img_url = gen.get("img")
        model_used = gen.get("model", "AI Horde")
        if not img_url:
            print(f"  AI Horde returned no image URL. Status: {status}")
            return False

        img_bytes = requests.get(img_url, timeout=25).content
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img = img.resize((width, height), Image.Resampling.LANCZOS)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, quality=95)
        print(f"  Realistic image saved: {output_path} ({output_path.stat().st_size // 1024} KB) [{model_used}]")
        return True
    except Exception as exc:
        print(f"  AI Horde generation error: {exc}")
        return False


def download_image(
    persona: dict,
    scene: str,
    output_path: Path,
    width: int = 1080,
    height: int = 1350,
) -> None:
    """Generate consistent Erika image or choose from verified photoshoot library."""
    print(f"  Preparing consistent image for Erika: {output_path.name}")

    # 1. Try AI Horde with fixed seed and facial anchor
    try:
        if generate_image_aihorde(persona, scene, output_path, width=width, height=height):
            return
    except Exception as exc:
        print(f"  Live AI generation failed: {exc}")

    # 2. Guaranteed fallback: Curated architectural photography gallery
    print("  Using verified luxury architecture photography library...")
    chosen = get_next_curated_architecture_image()
    from PIL import Image
    img = Image.open(chosen).convert("RGB")
    if img.size != (width, height):
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=95)
    print(f"  Consistent Erika photo saved from curated library: {chosen.name} ({output_path.stat().st_size // 1024} KB)")


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
    """Stage given files, pull --rebase, then push (authenticated via GITHUB_TOKEN)."""
    # Configure authenticated remote URL so git push works from Python subprocess
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if gh_token and gh_repo:
        auth_url = f"https://x-access-token:{gh_token}@github.com/{gh_repo}.git"
        git_run("remote", "set-url", "origin", auth_url)

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

    # Pull remote changes with merge strategy favoring recent commits to avoid rebase deadlocks
    branch = _get_branch()
    r = git_run("pull", "--rebase", "--autostash", "origin", branch)
    if r.returncode != 0:
        print(f"  git rebase warning: {r.stderr.strip()[:200]}. Aborting rebase and merging...")
        git_run("rebase", "--abort")
        git_run("pull", "origin", branch, "--no-rebase", "-X", "theirs")

    # Push with up to 3 retries
    for attempt in range(3):
        r = git_run("push", "origin", branch)
        if r.returncode == 0:
            print(f"  Pushed: {message}")
            return
        print(f"  Push attempt {attempt + 1} rejected, syncing remote...")
        time.sleep(2)
        git_run("pull", "origin", branch, "--no-rebase", "-X", "theirs")

    raise RuntimeError(f"git push failed after 3 attempts (returncode={r.returncode}): {r.stderr}")


def _get_branch() -> str:
    branch = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not branch:
        r = git_run("rev-parse", "--abbrev-ref", "HEAD")
        branch = r.stdout.strip() or "main"
    return branch


def wait_for_raw_url(gh_repo: str, branch: str, file_path: Path) -> str:
    """Wait until the raw.githubusercontent.com URL is accessible (HTTP 200)."""
    rel = file_path.relative_to(ROOT).as_posix()
    # Use the exact Git commit SHA so GitHub CDN does not serve stale branch 404s
    r_sha = git_run("rev-parse", "HEAD")
    ref = r_sha.stdout.strip() if r_sha.returncode == 0 and r_sha.stdout.strip() else branch
    url = f"{RAW_GITHUB_BASE}/{gh_repo}/{ref}/{rel}"
    print(f"  Waiting for raw URL: {url}")

    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            # Query with a timestamp param so Fastly CDN never serves negative cached 404
            test_url = f"{url}?t={int(time.time())}"
            r = requests.get(test_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, stream=True)
            if r.status_code == 200:
                print(f"  URL ready (HTTP 200)")
                return url
            print(f"  Not ready yet (HTTP {r.status_code}), retrying...")
        except Exception as exc:
            print(f"  URL check error: {exc}, retrying...")
        time.sleep(MEDIA_WAIT_INTERVAL)

    print("  CDN probe timeout on runner, but git push succeeded. Proceeding to publish...")
    return url


# ---------------------------------------------------------------------------
# Instagram Graph API
# ---------------------------------------------------------------------------

def ig_post(endpoint: str, env: dict, payload: dict) -> dict:
    """POST to Instagram Graph API using form-encoded payload."""
    url = f"{IG_API_BASE}/{endpoint}"
    params = {"access_token": env["ig_token"]}
    for attempt in range(3):
        try:
            # Meta Graph API expects form data (data=payload)
            r = requests.post(url, params=params, data=payload, timeout=60)
            if not r.ok:
                print(f"  Meta API response ({r.status_code}): {r.text}")
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"  IG API error (attempt {attempt + 1}): {exc}")
            if attempt < 2:
                time.sleep(5)
    raise RuntimeError(f"Instagram API POST /{endpoint} failed after 3 attempts")


def ig_get(endpoint: str, env: dict, params: dict | None = None) -> dict:
    """GET from Instagram Graph API with response debugging."""
    url = f"{IG_API_BASE}/{endpoint}" if not endpoint.startswith("http") else endpoint
    p = {"access_token": env["ig_token"]}
    if params:
        p.update(params)
    r = requests.get(url, params=p, timeout=30)
    if not r.ok:
        print(f"  Meta API GET error ({r.status_code}) on {endpoint}: {r.text[:300]}")
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


def resolve_ig_user_id(env: dict) -> str:
    """Validate or auto-discover the Instagram Business Account ID from the token."""
    configured_id = env.get("ig_user_id", "").strip()

    # 1. Try querying /me on graph.facebook.com
    try:
        data = ig_get("me", env, {"fields": "id,name,instagram_business_account"})
        print(f"  Token identity: {data.get('name')} (id: {data.get('id')})")
        ig_biz = data.get("instagram_business_account")
        if isinstance(ig_biz, dict) and ig_biz.get("id"):
            found_id = ig_biz["id"]
            print(f"  Auto-resolved Instagram Business ID: {found_id}")
            return found_id
    except Exception as exc:
        print(f"  Meta /me check failed: {exc}")

    # 1b. Try querying graph.instagram.com/me in case it is an Instagram User Token
    try:
        data = ig_get("https://graph.instagram.com/v21.0/me", env, {"fields": "id,username,user_id"})
        print(f"  Instagram Token identity: {data.get('username')} (id: {data.get('id')}, user_id: {data.get('user_id')})")
        found_id = data.get("user_id") or data.get("id")
        if found_id:
            print(f"  Auto-resolved via graph.instagram.com: {found_id}")
            return found_id
    except Exception as exc:
        print(f"  Instagram /me check failed: {exc}")

    # 2. Try querying /me/accounts if token has user page access
    try:
        pages_data = ig_get("me/accounts", env, {"fields": "name,instagram_business_account"})
        for page in pages_data.get("data", []):
            ig_biz = page.get("instagram_business_account")
            if isinstance(ig_biz, dict) and ig_biz.get("id"):
                found_id = ig_biz["id"]
                print(f"  Found Instagram Business ID under page '{page.get('name')}': {found_id}")
                return found_id
    except Exception as exc:
        print(f"  Meta /me/accounts check: {exc}")

    return configured_id


def publish_photo(image_url: str, caption: str, env: dict) -> str:
    """Create a photo container and publish it. Returns media ID."""
    ig_user = resolve_ig_user_id(env)
    full_caption = caption  # hashtags already embedded in caption
    print(f"  Creating photo container for IG user: {ig_user}...")
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
    ig_user = resolve_ig_user_id(env)
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
    """Build a ~20 second 9:16 vertical video from 5 images using fast cinematic crossfade transitions."""
    width, height = 1080, 1920
    clip_duration = 3.2  # 3.2s per cut (16s total edit - peak viral retention)
    fade_duration = 0.35 # quick cinematic dissolve

    inputs: list[str] = []
    for img in image_paths:
        inputs += ["-loop", "1", "-t", str(clip_duration), "-i", str(img)]

    filter_parts: list[str] = []
    for i in range(len(image_paths)):
        filter_parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,format=yuv420p[v{i}]"
        )

    # Chain xfade transitions
    n = len(image_paths)
    if n == 1:
        full_filter = f"{filter_parts[0]};[v0]copy[vout]"
    else:
        last_out = "v0"
        for i in range(1, n):
            offset = i * (clip_duration - fade_duration)
            out_label = f"x{i}" if i < n - 1 else "vout"
            filter_parts.append(f"[{last_out}][v{i}]xfade=transition=fade:duration={fade_duration}:offset={offset:.1f}[{out_label}]")
            last_out = f"x{i}"
        full_filter = ";".join(filter_parts)

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
        "-crf", "22",
        "-preset", "veryfast",
        "-threads", "0",
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
    print(f"  Video built: {output_path} ({output_path.stat().st_size // 1024} KB)")


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

    print("Committing log and index...")
    log_ok("post", full_caption, str(image_path.name), f"media_id={media_id}")
    files_to_log = [LOG_FILE]
    if ERIKA_INDEX_FILE.exists():
        files_to_log.append(ERIKA_INDEX_FILE)
    git_commit_and_push(files_to_log, f"chore: log post {ts}")


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
    files_to_log = [LOG_FILE]
    if ARCH_INDEX_FILE.exists():
        files_to_log.append(ARCH_INDEX_FILE)
    git_commit_and_push(files_to_log, f"chore: log weekly_reel {ts}")


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
