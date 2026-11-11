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
import textwrap
from PIL import Image, ImageDraw, ImageFont
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

# ---------------------------------------------------------------------------
# Registry & Deduplication System (AGENT_SPEC §3)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from registry import (
        is_set_on_cooldown, is_music_on_cooldown, is_slot_occupied,
        is_caption_duplicate, reserve_slot, confirm_publish,
        update_metrics, get_top_posts, caption_hash, REGISTRY_FILE
    )
    REGISTRY_AVAILABLE = True
except ImportError:
    REGISTRY_AVAILABLE = False
    REGISTRY_FILE = ROOT / "data" / "registry.json"
    print("  [Registry] Warning: registry module not found. Deduplication disabled.")

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
        "gemini_key": _env("GEMINI_API_KEY", required=False),
        "gh_repo": _env("GITHUB_REPOSITORY"),   # owner/repo
        "gh_branch": _env("GITHUB_REF_NAME", required=False) or "main",
        "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip(),
        "gh_token": os.environ.get("GITHUB_TOKEN", "").strip(),
        "yt_client_id": os.environ.get("YOUTUBE_CLIENT_ID", "").strip(),
        "yt_client_secret": os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip(),
        "yt_refresh_token": os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip(),
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
    """Generate high-engagement supercar or superbike post directly from persona."""
    theme = random.choice(persona["themes"])
    templates = [
        (
            f"Porsche 911 GT3 RS in Shark Blue with Weissach Package, aggressive swan-neck carbon wing, parked on rain-slicked Tokyo street with neon reflections, 4k automotive photography.",
            "Pure precision engineering. The Porsche 911 GT3 RS in Shark Blue — built for the apex, dialed for track dominance. Could you daily drive this weapon? Drop your thoughts below 👇",
            ["porsche", "gt3rs", "porsche911", "supercars", "carsofinstagram", "trackweapon", "speedhunters", "carreels"]
        ),
        (
            f"Ducati Panigale V4 S in iconic Ducati Red, aggressive dual LED projector headlights, parked on coastal cliffside switchback at golden hour, shallow depth of field.",
            "214 horsepower of pure Italian adrenaline. The Ducati Panigale V4 S doesn't ask for attention — it commands it. Would you take the keys for a canyon run? Drop a comment below 👇",
            ["ducati", "panigale", "panigalev4", "superbikes", "bikersofinstagram", "motorcyclelife", "sportbike", "twowheels"]
        ),
        (
            f"Matte black Lamborghini Revuelto V12 hybrid supercar idling under dramatic industrial concrete lights, glowing orange brake calipers and titanium exhaust tips, cinematic.",
            "1,001 horsepower of electrified V12 fury. The Lamborghini Revuelto redefining the hypercar era. Rate this stealth spec from 1–10 below 👇",
            ["lamborghini", "revuelto", "v12", "hypercars", "supercars", "exoticcars", "carspotting", "carlifestyle"]
        ),
        (
            f"BMW S1000RR M Package superbike in Alpine White with M motorsport tri-color livery, parked on mountain touge pass at blue hour twilight with glowing LED headlights.",
            "The ultimate track missile. BMW S1000RR M Package with full carbon aero winglets and race-tuned electronics. Tag a rider who needs this in their garage ⚡",
            ["bmwmotorrad", "s1000rr", "bmws1000rr", "superbikes", "motorcycles", "sportbikelife", "riderich", "trackday"]
        ),
    ]
    scene, caption, tags = random.choice(templates)
    return {"scene": scene, "caption": caption, "hashtags": tags}


def generate_post_content(persona: dict, env: dict, custom_prompt: str = "") -> dict:
    """Generate high-engagement automotive post content via Gemini."""
    theme = custom_prompt.strip() if custom_prompt and custom_prompt.strip() else random.choice(persona["themes"])
    prompt = f"""You are the creative director of a world-famous supercar & superbike media brand named "{persona['name']}".
Theme: "{theme}"

Generate a JSON object (no markdown, no extra text) for an insanely viral, captivating Instagram post about this machine.
The JSON must have exactly these fields:
{{
  "scene": "<hyper-detailed 4K automotive photography description, Speedhunters and Car and Driver magazine cover style, aggressive supercar or superbike, carbon fiber aerodynamic parts, wet asphalt reflections or sunset lighting, photorealistic>",
  "caption": "<2-3 sentence bold, adrenaline-fueled caption describing performance and visual highlights, ending with a compelling debate question to spark comments (e.g. 'Which keys are you taking? 🔑 Rate this spec 1–10 below 👇')>",
  "hashtags": ["supercars", "hypercars", "superbikes", "carsofinstagram", "bikersofinstagram", "speedhunters", "carlifestyle", "carspotting"]
}}
Rules:
- NEVER mention AI, virtual, render, or algorithm
- caption must sound like an authentic automotive enthusiast publication
- hashtags must be 8 relevant high-traffic automotive tags, lowercase, no # prefix
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



def get_bold_font(size: int = 44) -> ImageFont.ImageFont:
    """Find a crisp, bold sans-serif font across Linux/Ubuntu (Actions runner) and Windows."""
    font_candidates = [
        # Linux / Ubuntu runners
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        # Windows
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for p in font_candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def apply_text_overlay(img_path: Path, text: str) -> None:
    """Overlay clean, viral-style aesthetic text onto a 1080x1920 image with dark pill background."""
    if not text or not text.strip():
        return
    text = text.strip()
    try:
        with Image.open(img_path).convert("RGBA") as base:
            txt_layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(txt_layer)

            raw_len = len(text)
            if raw_len > 90:
                font_size = 38
                max_chars = 32
            elif raw_len > 45:
                font_size = 42
                max_chars = 28
            else:
                font_size = 48
                max_chars = 24

            font = get_bold_font(font_size)

            lines: list[str] = []
            for part in text.split("\n"):
                sub = textwrap.wrap(part.strip(), width=max_chars)
                if sub:
                    lines.extend(sub)
                else:
                    lines.append("")

            lines = [l for l in lines if l]
            if not lines:
                return

            line_height = int(font_size * 1.38)
            total_h = len(lines) * line_height

            img_w, img_h = base.size
            y_start = int((img_h - total_h) * 0.40)

            pad_x = 36
            pad_y = 28
            max_w = 0
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                max_w = max(max_w, bbox[2] - bbox[0])

            box = [
                int((img_w - max_w) / 2) - pad_x,
                y_start - pad_y,
                int((img_w + max_w) / 2) + pad_x,
                y_start + total_h + pad_y,
            ]

            # Sleek translucent pill background
            draw.rounded_rectangle(box, radius=24, fill=(0, 0, 0, 185))

            for i, line in enumerate(lines):
                bbox = draw.textbbox((0, 0), line, font=font)
                w = bbox[2] - bbox[0]
                x = int((img_w - w) / 2)
                y = y_start + i * line_height
                draw.text((x + 2, y + 2), line, fill=(0, 0, 0, 220), font=font)
                draw.text((x, y), line, fill=(255, 255, 255, 255), font=font)

            out = Image.alpha_composite(base, txt_layer).convert("RGB")
            out.save(img_path, "JPEG", quality=95)
            print(f"  Applied viral text overlay to {img_path.name}: '{text[:35]}...'")
    except Exception as exc:
        print(f"  Warning: failed to overlay text on {img_path.name}: {exc}")


def extract_overlay_and_theme(cp: str) -> tuple[str, str]:
    """Extract optional text overlay (e.g. text in quotes or after indicators) and supercar/superbike automotive theme from custom prompt."""
    if not cp:
        return "", ""

    raw = cp.strip()

    # 1. Closed quotes: "..." or '...'
    m = re.search(r'["“]([^"”]{5,})["”]', raw)
    if not m:
        m = re.search(r"(?:^|\s)['‘]([^'’]{5,})['’](?:$|\s)", raw)

    # 2. Unclosed quote starting with " or ' up to end of string (e.g. user typed opening quote without closing it)
    if not m:
        m = re.search(r'["“]([^"”]{5,})$', raw)
    if not m:
        m = re.search(r"(?:^|\s)['‘]([^'’]{5,})$", raw)

    # 3. Keyword indicators without quotes (e.g. 'over those text Day 1...', 'with text Day 1...', 'text: Day 1...')
    if not m:
        m = re.search(r"""(?i)\b(?:over those text|over that text|over this text|with text|text overlay|put text|having text|text is|saying|text:)\s+["“']?(.*)""", raw)

    if m:
        overlay = m.group(1).strip(" \"'“”‘’")
        overlay = re.sub(r"""["”'’]+$""", "", overlay).strip()

        # The visual theme is the rest of the prompt
        theme_part = raw[:m.start()] + raw[m.end():]
        theme_part = re.sub(
            r'(?i)\b(with text overlay|text overlay|with text|over those text|over that text|over this text|text:?|with title|a normal reel|reel)\b',
            '',
            theme_part
        ).strip(' ,:;-')
        if not theme_part or len(theme_part) < 6 or any(k in theme_part.lower() for k in ['villa', 'reel', 'overlay', 'edit', 'photo']):
            theme_part = 'Porsche 911 GT3 RS in Shark Blue and Ducati Panigale V4 S superbike on rain-slicked asphalt, 4k automotive cinematography'
        return overlay, theme_part

    return "", cp

def generate_fallback_reel(persona: dict, custom_prompt: str = "") -> dict:
    """Curated, high-octane 5-shot single machine edits (emblem, wheels, cockpit, exhaust, rolling) for supercars and superbikes."""
    overlay, theme = extract_overlay_and_theme(custom_prompt)
    if overlay:
        if "studies" in overlay.lower() or "study" in overlay.lower():
            title = f"Zenith Motors • Study Journey 📚"
            caption = f"{overlay}\n\nAny mentorship, study guidance, or advice in the comments means the world to me 👇\n\n#collegestudent #studymotivation #studentlife #education #engineering #studentjourney #supercars #superbikes"
            tags = ["ugstudies", "collegestudent", "studymotivation", "undergraduate", "studentlife", "education", "engineering", "supercars", "superbikes", "studentjourney"]
        else:
            title = f"Zenith Motors • {overlay[:35]}... 🏁"
            caption = f"{overlay}\n\nBehind the wheel of pure mechanical perfection. Drop a comment below 👇"
            tags = ["supercars", "hypercars", "superbikes", "carsofinstagram", "bikersofinstagram", "viralreels", "speedhunters", "carlifestyle", "carreels", "drive"]
        scenes = [
            {"scene": f"Extreme macro close-up of {theme} front emblem and piercing LED headlights waking up in dark moody studio, 4k automotive cinema", "text_overlay": overlay},
            {"scene": f"Macro low-angle shot of forged wheels, performance tyre, and brake calipers of {theme}, water droplets, Speedhunters style", "text_overlay": overlay},
            {"scene": f"Driver cockpit of {theme} with Alcantara steering wheel, glowing engine start ignition button, and digital tachometer", "text_overlay": overlay},
            {"scene": f"Rear low-angle angle of {theme} showing aggressive aero carbon wing and quad titanium exhaust tips", "text_overlay": overlay},
            {"scene": f"Dynamic wide cinematic rolling tracking shot of {theme} on rain-slicked dark asphalt at night with reflections", "text_overlay": overlay},
        ]
        return {"format": title, "caption": caption, "hashtags": tags, "scenes": scenes}

    if custom_prompt and custom_prompt.strip():
        cp = custom_prompt.strip()
        title = f"{cp[:45]} • Pure Machine 🏁"
        caption = f"Unleashing the beast: {cp}. Masterclass engineering and heart-pounding adrenaline. Would you take the keys? Drop your thoughts below 👇"
        scenes = [
            {"scene": f"Extreme macro close-up of {cp} front badge and piercing LED headlights waking up in dark moody studio, 4k automotive cinema", "text_overlay": ""},
            {"scene": f"Macro low-angle shot of forged wheels, performance tyre, and brake calipers of {cp}, water droplets, Speedhunters style", "text_overlay": ""},
            {"scene": f"Driver cockpit of {cp} with Alcantara steering wheel, glowing engine start ignition button, and digital tachometer", "text_overlay": ""},
            {"scene": f"Rear low-angle angle of {cp} showing aggressive aero carbon wing and quad titanium exhaust tips", "text_overlay": ""},
            {"scene": f"Dynamic wide cinematic rolling tracking shot of {cp} on rain-slicked dark asphalt at night with reflections", "text_overlay": ""},
        ]
        tags = ["supercars", "hypercars", "superbikes", "carsofinstagram", "bikersofinstagram", "carreels", "speedhunters", "carlifestyle", "trackweapon", "automotive"]
        return {"format": title, "caption": caption, "hashtags": tags, "scenes": scenes}

    tours = [
        (
            "BMW M4 Competition • Stealth Black Track Weapon 🏁",
            "503 horsepower of pure twin-turbo inline-six rage wrapped in Frozen Matte Black. The BMW M4 Competition G82 demands respect on both street and apex. Sound up for that cold start roar 🔊 Drop your dream spec below 👇",
            [
                {"scene": "Extreme macro close-up of BMW M carbon badge on gloss black kidney grille with piercing twin LED laser headlights firing up in dark moody studio, 4k automotive cinema", "text_overlay": ""},
                {"scene": "Macro low-angle shot of 20-inch M forged black wheel, sticky Michelin Pilot Sport tyre, and gold carbon ceramic brake caliper, water droplets, automotive photography", "text_overlay": ""},
                {"scene": "Driver cockpit view of BMW M4 focusing on Alcantara steering wheel with carbon paddle shifters, red start stop engine button, digital M cluster", "text_overlay": ""},
                {"scene": "Rear close-up of carbon rear diffuser, quad titanium exhaust tips with blue heat burn, and carbon ducktail trunk spoiler on Matte Black BMW M4", "text_overlay": ""},
                {"scene": "Wide cinematic rolling tracking shot of Matte Black BMW M4 Competition gliding on rain-slicked dark asphalt at night with streetlights reflecting", "text_overlay": ""}
            ],
            ["bmwm4", "bmwm", "mperformance", "bimmerpost", "supercars", "trackweapon", "carreels", "speedhunters", "carlifestyle", "m4competition"]
        ),
        (
            "Porsche 911 GT3 RS • Shark Blue Track Weapon 🏁",
            "The Porsche 911 GT3 RS in Shark Blue — 518 naturally aspirated horsepower and a swan-neck wing generating 1,895 lbs of downforce. Built purely for the apex. Sound on 🔊 Could you daily drive this weapon? Drop your thoughts below 👇",
            [
                {"scene": "Macro close-up of Porsche crest emblem on carbon hood with aggressive aero nostrils and LED Matrix headlights waking up in dark studio", "text_overlay": ""},
                {"scene": "Low-angle macro shot of lightweight forged magnesium wheel, Michelin Cup 2R tyre, and yellow carbon ceramic brake caliper of Porsche GT3 RS", "text_overlay": ""},
                {"scene": "Cockpit of Porsche GT3 RS focusing on Alcantara steering wheel, drive mode rotary dials, red ignition switch, and 9000 RPM tachometer", "text_overlay": ""},
                {"scene": "Rear carbon fiber diffuser, swan-neck aero wing, and dual center titanium exhaust pipes of Shark Blue Porsche GT3 RS", "text_overlay": ""},
                {"scene": "Cinematic tracking rolling shot of Shark Blue Porsche 911 GT3 RS attacking track curb on wet asphalt with motion blur", "text_overlay": ""}
            ],
            ["porsche", "gt3rs", "porsche911", "supercars", "trackweapon", "carsofinstagram", "speedhunters", "carreels", "carlifestyle", "exoticcars"]
        ),
        (
            "Ducati Panigale V4 S • Pure Italian Adrenaline 🔴",
            "214 HP at 13,000 RPM wrapped in iconic Ducati Red. The Panigale V4 S is poetry in motion and brute force on two wheels. Sound on for that Desmosedici roar 🔊 Would you take the keys for a mountain canyon run? Drop a comment below 👇",
            [
                {"scene": "Extreme macro close-up of Ducati Corse emblem on front red aerodynamic fairing with piercing twin LED projector lights in moody studio", "text_overlay": ""},
                {"scene": "Macro low-angle shot of Pirelli Diablo Supercorsa SP tyre, bronze forged Marchesini wheel, and Brembo Stylema calipers", "text_overlay": ""},
                {"scene": "Rider cockpit view of Ducati Panigale V4 S focusing on clip-on handlebars, electronic TFT race display, and red ignition switch", "text_overlay": ""},
                {"scene": "Close-up of single-sided swingarm, titanium Akrapovic underslung exhaust, and exposed rear wheel of Ducati Panigale V4 S", "text_overlay": ""},
                {"scene": "Cinematic rolling shot of Ducati Panigale V4 S carving coastal mountain hairpin switchback at golden hour sunset", "text_overlay": ""}
            ],
            ["ducati", "panigalev4", "superbikes", "bikersofinstagram", "motorcycles", "sportbike", "twowheels", "riderich", "trackday", "ducatistagram"]
        ),
        (
            "Lamborghini Revuelto • 1000HP V12 Hybrid Beast ⚡",
            "A roaring 6.5L naturally aspirated V12 paired with three electric motors to unleash 1,001 horsepower. The Lamborghini Revuelto is the dawn of a new hypercar era. Which color spec would you order? Drop a comment below 👇",
            [
                {"scene": "Macro close-up of Lamborghini raging bull crest on front nose with sharp Y-shaped LED daytime lights waking up", "text_overlay": ""},
                {"scene": "Low-angle macro shot of forged Bridgestone Potenza sport wheel and giant carbon-ceramic orange brake caliper", "text_overlay": ""},
                {"scene": "Fighter-jet inspired cockpit with carbon steering wheel, digital cluster, and flip-up red fighter-jet ignition start button", "text_overlay": ""},
                {"scene": "High-mounted exposed hexagonal dual exhaust tips with active carbon fiber rear diffuser and wing", "text_overlay": ""},
                {"scene": "Rainy night city neon reflections with glowing Y-shaped taillights reflecting on wet road", "text_overlay": ""}
            ],
            ["lamborghini", "revuelto", "hypercars", "supercars", "v12", "exoticcars", "carspotting", "carlifestyle", "carreels", "speedhunters"]
        ),
        (
            "BMW S1000RR M Package • The Precision Missile ⚪",
            "Engineered to dominate the track. The BMW S1000RR M Package in Alpine White with carbon fiber wheels and M winglets. Pure mechanical precision. Tag someone who loves two wheels 🔥",
            [
                {"scene": "Macro close-up of BMW roundel badge and aggressive front fairing with M carbon winglets and piercing dual LED headlights", "text_overlay": ""},
                {"scene": "Low-angle shot of 17-inch M carbon fiber front wheel, Michelin Power Cup tyre, and blue M brake caliper", "text_overlay": ""},
                {"scene": "Clip-on handlebars with carbon tank cover, digital race display, and M start button", "text_overlay": ""},
                {"scene": "Akrapovic titanium exhaust canister and carbon fiber rear wheel hugger with wide rear tire", "text_overlay": ""},
                {"scene": "Blue-hour twilight silhouette with glowing LED taillight line on winding asphalt", "text_overlay": ""}
            ],
            ["bmwmotorrad", "s1000rr", "superbikes", "bikersofinstagram", "sportbikelife", "motorcycles", "bmws1000rr", "twowheels", "bikelife", "carreels"]
        )
    ]
    title, caption, scenes, tags = random.choice(tours)
    return {
        "format": title,
        "caption": caption,
        "hashtags": tags,
        "scenes": scenes
    }

def generate_reel_content(persona: dict, env: dict, custom_prompt: str = "") -> dict:
    """Generate an aggressive, 5-shot cinematic edit focused strictly on ONE supercar or superbike."""
    destinations = [
        "Porsche 911 GT3 RS in Shark Blue with Weissach Package and swan-neck carbon wing, rain-slicked Tokyo street",
        "Ducati Panigale V4 S in iconic Ducati Red with carbon aerodynamic winglets, coastal switchback at golden hour",
        "matte black Lamborghini Revuelto V12 hybrid supercar idling under moody neon lighting with orange brake calipers",
        "BMW S1000RR M Package superbike in Alpine White with M motorsport tri-color livery, mountain touge pass at sunrise",
        "Ferrari SF90 Stradale in Rosso Corsa with Assetto Fiorano carbon package, rolling through mountain tunnel",
        "Kawasaki Ninja H2 Carbon supercharged superbike with dark mirror chrome paint under midnight city lights",
        "McLaren 765LT Spider in Papaya Spark orange with exposed carbon aero louvers and quad titanium exhaust",
        "Yamaha YZF-R1M superbike with exposed carbon fairings and blue wheels under moody studio spotlights",
        "Koenigsegg Jesko Absolut hypercar on runway tarmac at dramatic dawn sunrise",
        "Nissan GT-R Nismo in stealth grey with red aero accents under rainy night city neon"
    ]
    overlay, theme = extract_overlay_and_theme(custom_prompt)
    if custom_prompt and custom_prompt.strip():
        chosen_dest = theme if theme else custom_prompt.strip()
        msg = f"  Building reel for theme: '{chosen_dest}'"
        if overlay:
            msg += f" with on-screen text overlay: '{overlay}'"
        print(msg)
    else:
        chosen_dest = random.choice(destinations)
        overlay = ""

    prompt = f"""You are the creative director of a prestigious supercar & superbike media brand named "{persona['name']}".
Theme for this reel: {chosen_dest}

Create a JSON object for an aggressive, eye-catching, viral Instagram Reel edit of this supercar or superbike.
The reel must show a progressive 5-shot cinematic edit of ONE machine:
Shot 1: The dramatic hero rolling shot or wide front-quarter angle on road/track
Shot 2: The aggressive front fascia, LED headlight signature, and carbon aero splitter
Shot 3: The driver/rider cockpit, tachometer, Alcantara steering wheel, and mechanical details
Shot 4: The rear diffuser, active aerodynamic carbon wing, and exhaust setup
Shot 5: The golden hour or rainy night neon reflections with glowing LED taillights

Return exactly this JSON structure:
{{
  "format": "<Punchy title e.g. 'Porsche 911 GT3 RS • Shark Blue Track Weapon 🏁'>",
  "caption": "<Adrenaline-fueled, engaging 2-3 sentence caption describing the performance, sound, and presence of this machine. End with a debate question to drive comments (e.g. 'Which keys are you taking for the weekend? 🔑 Drop your dream spec below 👇')>",
  "hashtags": ["supercars", "hypercars", "superbikes", "carsofinstagram", "bikersofinstagram", "carreels", "speedhunters", "carlifestyle", "trackweapon", "automotive"],
  "scenes": [
    {{"scene": "<Shot 1: 4K cinematic rolling hero shot on road, Speedhunters style, photorealistic>", "text_overlay": ""}},
    {{"scene": "<Shot 2: Aggressive front fascia with glowing LED headlights and carbon splitter>", "text_overlay": ""}},
    {{"scene": "<Shot 3: Driver cockpit with digital cluster, Alcantara details, and race controls>", "text_overlay": ""}},
    {{"scene": "<Shot 4: Rear aero wing, carbon diffuser, and titanium exhaust setup>", "text_overlay": ""}},
    {{"scene": "<Shot 5: Sunset or rainy night city reflections with glowing taillights>", "text_overlay": ""}}
  ]
}}
Rules:
- 4K cinematic automotive cinematography (Speedhunters / Top Gear cover style, photorealistic)
- All 5 shots must depict the SAME machine to form a cohesive, satisfying edit
- NEVER mention AI, virtual, render, or code
- hashtags lowercase, no # prefix, 8-10 tags
"""
    try:
        text = call_gemini(prompt, env, json_mode=True)
        data = parse_json_response(text)
        assert "scenes" in data and len(data["scenes"]) == 5
        data["hashtags"] = [h.lstrip("#").lower() for h in data.get("hashtags", [])][:10]
        if overlay:
            for sc in data["scenes"]:
                sc["text_overlay"] = overlay
            if "studies" in overlay.lower() or "study" in overlay.lower():
                data["format"] = f"Zenith Motors • Study Journey 📚"
                data["caption"] = f"{overlay}\n\nAny mentorship, study guidance, or advice in the comments means the world to me 👇\n\n#collegestudent #studymotivation #studentlife #education #engineering #studentjourney #supercars #superbikes"
            else:
                data["format"] = f"Zenith Motors • {overlay[:35]}... 🏁"
                data["caption"] = f"{overlay}\n\nBehind the wheel of pure mechanical perfection. Drop a comment below 👇"
        else:
            # Clean visuals: never draw text on standard automated reels unless explicitly asked
            for sc in data["scenes"]:
                sc["text_overlay"] = ""
        return data
    except Exception as exc:
        print(f"  External LLM API unavailable ({exc}). Using curated cohesive single-machine edit generator...")
        return generate_fallback_reel(persona, custom_prompt=custom_prompt)

def generate_upload_caption(video_path: Path, persona: dict, env: dict) -> str:
    """Use Gemini to write a caption for a user-uploaded reel."""
    prompt = f"""You are the creative director of a world-famous supercar & superbike media brand named "{persona['name']}".
Write an Instagram Reel caption (2-3 sentences, max one emoji) for an adrenaline-fueled automotive video reel.
The filename is: {video_path.name}
NEVER mention AI, algorithm, or code. End with an interactive question to spark comments. Return ONLY the caption text.
"""
    try:
        return call_gemini(prompt, env).strip()
    except Exception:
        return "Pure mechanical adrenaline where visionary automotive engineering meets raw power. Would you take the keys for a weekend run? Drop your thoughts below 👇"


# ---------------------------------------------------------------------------
# Image generation (Pollinations.ai)
# ---------------------------------------------------------------------------

VEHICLES_ASSETS_DIR = Path(__file__).parent / "assets" / "vehicles"
VEHICLE_INDEX_FILE = Path(__file__).parent / "data" / "vehicle_index.json"
ARCH_INDEX_FILE = VEHICLE_INDEX_FILE  # backwards compatibility
ERIKA_INDEX_FILE = VEHICLE_INDEX_FILE # backwards compatibility


def get_next_curated_vehicle_image() -> Path:
    """Cycle sequentially through the verified supercar and superbike gallery."""
    VEHICLES_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    images = sorted(list(VEHICLES_ASSETS_DIR.glob("*.jpg")) + list(VEHICLES_ASSETS_DIR.glob("*.png")))
    if not images:
        raise FileNotFoundError(f"No curated vehicle images found in {VEHICLES_ASSETS_DIR}")

    idx = 0
    if VEHICLE_INDEX_FILE.exists():
        try:
            with open(VEHICLE_INDEX_FILE, encoding="utf-8") as f:
                data = json.load(f)
                idx = data.get("index", 0)
        except Exception:
            idx = 0

    chosen = images[idx % len(images)]
    next_idx = (idx + 1) % len(images)
    try:
        VEHICLE_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(VEHICLE_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({"index": next_idx, "last_image": chosen.name}, f, indent=2)
    except Exception:
        pass
    return chosen

get_next_curated_architecture_image = get_next_curated_vehicle_image


# Photorealistic models to cycle through (all produce natural-looking results)
REALISTIC_MODELS = [
    "Realistic Vision",
    "AbsoluteReality",
    "ICBINP - I Can't Believe It's Not Photography",
    "Photon",
    "Edge Of Realism",
    "majicMIX realistic",
]


def generate_image_pollinations(scene: str, output_path: Path, width: int = 1080, height: int = 1920) -> bool:
    """Generate high-res 4K automotive photography via Pollinations.ai (Flux/SDXL)."""
    auto_prompt = f"4K ultra-realistic automotive photography, Speedhunters and Car and Driver magazine cover style, sharp focus, 8k resolution, cinematic lighting: {scene}"
    seed = random.randint(10000, 9999999)
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(auto_prompt)}?width={width}&height={height}&nologo=true&seed={seed}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=25)
        if r.status_code == 200 and len(r.content) > 20000:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(r.content)
            print(f"  Live AI supercar/bike photo generated via Pollinations: {output_path.name} ({len(r.content)//1024} KB)")
            return True
        else:
            print(f"  Pollinations returned status {r.status_code}")
    except Exception as exc:
        print(f"  Pollinations image error: {exc}")
    return False


def generate_image_aihorde(persona: dict, scene: str, output_path: Path, width: int = 1080, height: int = 1350) -> bool:
    """Generate hyper-realistic automotive photography via AI Horde."""
    headers = {
        "apikey": "0000000000",
        "Client-Agent": "ZenithMotors:1.0:automotive@example.com"
    }

    # High-end 4K automotive cinematography prompt
    auto_prefix = (
        "RAW 4K cinematic automotive photography, Speedhunters feature, supercar and superbike culture, "
        "shot on 35mm anamorphic lens, reflections on wet asphalt, carbon fiber details, aggressive stance, "
    )
    style_suffix = (
        "crisp mechanical detail, cinematic lighting, reflections on wet road, "
        "high-end automotive cinematography, 8k resolution, photorealistic"
    )
    negative = (
        "people, humans, crowd, man, woman, face, "
        "villa, building, interior, room, house, swimming pool, furniture, living room, bedroom, "
        "ugly, distorted, crooked, cartoon, anime, painting, illustration, 3d render, cgi, video game graphics, "
        "blurry, low resolution, watermark, text, signature, oversaturated"
    )

    prompt = f"{auto_prefix} {scene[:170]}, {style_suffix} ### {negative}"

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
        print(f"  Generating automotive photography via AI Horde (seed: {seed})...")
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
    height: int = 1920,
) -> None:
    """Select verified 100% human-shot DSLR/mirrorless photography from the automotive gallery."""
    print(f"  Selecting authentic human-shot automotive photography for: {output_path.name}")
    chosen = get_next_curated_vehicle_image()
    from PIL import Image
    img = Image.open(chosen).convert("RGB")
    if img.size != (width, height):
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=95)
    print(f"  Authentic human-shot photo saved: {chosen.name} ({output_path.stat().st_size // 1024} KB)")


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


def publish_youtube_short(video_path: Path, title: str, description: str, tags: list[str], env: dict) -> tuple[str | None, str]:
    """Upload vertical MP4 video to YouTube as a YouTube Short via YouTube Data API v3 resumable upload."""
    client_id = env.get("yt_client_id", "").strip()
    client_secret = env.get("yt_client_secret", "").strip()
    refresh_token = env.get("yt_refresh_token", "").strip()

    if not (client_id and client_secret and refresh_token):
        print("  [YouTube] Notice: YouTube credentials (YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN) not set. Skipping YouTube upload.")
        return None, "credentials_not_set"

    print("  [YouTube] Refreshing Google OAuth2 access token...")
    try:
        t_res = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=25,
        )
        if t_res.status_code != 200:
            err_msg = f"token_refresh_failed_{t_res.status_code}_{t_res.text[:100]}"
            print(f"  [YouTube] Token refresh error ({t_res.status_code}): {t_res.text[:200]}")
            return None, err_msg
        access_token = t_res.json().get("access_token")
    except Exception as exc:
        print(f"  [YouTube] Token refresh exception: {exc}")
        return None, f"token_refresh_exception_{exc}"

    # Title must include #Shorts for automatic placement in Shorts feed
    yt_title = title if "#shorts" in title.lower() else f"{title[:80]} #Shorts"
    yt_desc = f"{description}\n\n#Shorts #Supercars #Cars #Reels"

    snippet = {
        "title": yt_title,
        "description": yt_desc,
        "tags": [t for t in (tags or []) if t][:15] + ["Shorts", "supercars", "cars"],
        "categoryId": "2",  # 2 = Autos & Vehicles
    }
    status = {
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
    }

    file_size = video_path.stat().st_size
    print(f"  [YouTube] Initiating resumable upload for '{yt_title}' ({file_size // 1024} KB)...")
    try:
        init_headers = {
            "Authorization": f"Bearer {access_token}",
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
            err_msg = f"init_upload_{init_res.status_code}_{init_res.text[:100]}"
            print(f"  [YouTube] Resumable init error ({init_res.status_code}): {init_res.text[:250]}")
            return None, err_msg
        upload_url = init_res.headers.get("Location")
        if not upload_url:
            print("  [YouTube] Missing Location header from YouTube upload API.")
            return None, "missing_location_header"
    except Exception as exc:
        print(f"  [YouTube] Resumable init exception: {exc}")
        return None, f"resumable_init_exception_{exc}"

    print(f"  [YouTube] Uploading video binary chunks...")
    try:
        with open(video_path, "rb") as f:
            up_res = requests.put(
                upload_url,
                data=f,
                headers={"Content-Type": "video/mp4"},
                timeout=300,
            )
        if up_res.status_code in (200, 201):
            data = up_res.json()
            video_id = data.get("id")
            print(f"  [YouTube] Successfully published YouTube Short! ID: {video_id}")
            print(f"  [YouTube] URL: https://youtube.com/shorts/{video_id}")
            return video_id, ""
        else:
            err_msg = f"binary_upload_{up_res.status_code}_{up_res.text[:100]}"
            print(f"  [YouTube] Binary upload error ({up_res.status_code}): {up_res.text[:250]}")
            return None, err_msg
    except Exception as exc:
        print(f"  [YouTube] Binary upload exception: {exc}")
        return None, f"binary_upload_exception_{exc}"


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

MUSIC_STATE_FILE = Path(__file__).parent / "data" / "music_state.json"


def pick_diverse_music_track() -> Path | None:
    """Select a fresh, authentic music track differing from the last run for maximum variety."""
    music_files = list(MUSIC_DIR.glob("*.mp3"))
    if not music_files:
        return None
    last_played = ""
    if MUSIC_STATE_FILE.exists():
        try:
            with open(MUSIC_STATE_FILE, encoding="utf-8") as f:
                last_played = json.load(f).get("last_track", "")
        except Exception:
            pass
    # Avoid repeating the immediate previous track
    eligible = [f for f in music_files if f.name != last_played] or music_files
    chosen = random.choice(eligible)
    try:
        MUSIC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MUSIC_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_track": chosen.name}, f, indent=2)
    except Exception:
        pass
    print(f"  Selected authentic music track: {chosen.name}")
    return chosen


ENGINE_SFX_DIR = Path(__file__).parent / "assets" / "sfx" / "engine"
VEHICLE_SETS_DIR = Path(__file__).parent / "assets" / "vehicles" / "sets"
VEHICLE_VIDEO_SETS_DIR = Path(__file__).parent / "assets" / "vehicles" / "video_sets"
VEHICLE_STATE_FILE = Path(__file__).parent / "data" / "vehicle_state.json"
REEL_PATTERN_FILE = Path(__file__).parent / "data" / "reel_pattern_state.json"
JOURNEY_STATE_FILE = Path(__file__).parent / "data" / "journey_state.json"


def pick_engine_sfx(theme_hint: str = "", is_bike: bool = False) -> Path | None:
    """Pick the exact authentic engine starting ignition and exhaust rev sound matching the machine."""
    if not ENGINE_SFX_DIR.exists():
        return None

    th = theme_hint.lower()

    # Exact matching by vehicle make/model and engine architecture:
    if any(k in th for k in ["bmw", "bmw m4", "m4 competition", "m4", "g82"]):
        target = ENGINE_SFX_DIR / "bmw_m4_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: BMW M4 Competition S58 Twin-Turbo I6 ({target.name})")
            return target

    if any(k in th for k in ["porsche", "911 gt3", "gt3 rs", "gt3"]):
        target = ENGINE_SFX_DIR / "porsche_gt3rs_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Porsche 911 GT3 RS Naturally Aspirated 9000 RPM Flat-6 ({target.name})")
            return target

    if any(k in th for k in ["lamborghini", "revuelto", "huracan", "v12", "aventador"]):
        target = ENGINE_SFX_DIR / "lamborghini_v12_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Lamborghini High-RPM V12 Exhaust Scream ({target.name})")
            return target

    if any(k in th for k in ["ferrari", "sf90", "f8", "stradale", "488"]):
        target = ENGINE_SFX_DIR / "ferrari_v8_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Ferrari Twin-Turbo Flat-Plane V8 Exhaust ({target.name})")
            return target

    if any(k in th for k in ["diablo", "lamborghini_diablo"]):
        target = ENGINE_SFX_DIR / "lamborghini_v12_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Lamborghini Diablo High-RPM V12 ({target.name})")
            return target

    if any(k in th for k in ["red_sportscar", "sportscar", "ferrari_f80"]):
        target = ENGINE_SFX_DIR / "ferrari_v8_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: High-Performance V8 Twin-Turbo Exhaust ({target.name})")
            return target

    if any(k in th for k in ["superbike_thrill"]):
        target = ENGINE_SFX_DIR / "bike_rev_high_rpm.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Superbike 15000 RPM Scream ({target.name})")
            return target

    if any(k in th for k in ["audi", "r8", "v10"]):
        target = ENGINE_SFX_DIR / "lamborghini_v12_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Audi R8 5.2L Naturally Aspirated V10 ({target.name})")
            return target

    if any(k in th for k in ["mclaren", "765lt", "720s", "spider"]):
        target = ENGINE_SFX_DIR / "mclaren_v8_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: McLaren Twin-Turbo V8 Top-Exit Flame Rev ({target.name})")
            return target

    if any(k in th for k in ["ducati", "panigale", "v4"]):
        target = ENGINE_SFX_DIR / "ducati_panigale_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Ducati Panigale V4 Desmosedici Stradale ({target.name})")
            return target

    if any(k in th for k in ["s1000rr", "bmw s1000", "bmw motorrad"]):
        target = ENGINE_SFX_DIR / "bmw_s1000rr_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: BMW S1000RR 14500 RPM ShiftCam Inline-4 ({target.name})")
            return target

    if any(k in th for k in ["kawasaki", "ninja", "h2", "zx6r", "zx10r"]):
        target = ENGINE_SFX_DIR / "kawasaki_superbike_rev.mp3"
        if target.exists():
            print(f"  Matched exact engine sound: Kawasaki Superbike High-RPM Exhaust ({target.name})")
            return target

    # Fallback to category-matched pool:
    if is_bike or any(k in th for k in ["bike", "motorcycle", "yamaha", "r1"]):
        candidates = [
            ENGINE_SFX_DIR / "ducati_panigale_rev.mp3",
            ENGINE_SFX_DIR / "bmw_s1000rr_rev.mp3",
            ENGINE_SFX_DIR / "kawasaki_superbike_rev.mp3",
            ENGINE_SFX_DIR / "bike_rev_high_rpm.mp3"
        ]
    else:
        candidates = [
            ENGINE_SFX_DIR / "bmw_m4_rev.mp3",
            ENGINE_SFX_DIR / "porsche_gt3rs_rev.mp3",
            ENGINE_SFX_DIR / "lamborghini_v12_rev.mp3",
            ENGINE_SFX_DIR / "ferrari_v8_rev.mp3",
            ENGINE_SFX_DIR / "mclaren_v8_rev.mp3"
        ]

    existing = [c for c in candidates if c.exists()]
    chosen = random.choice(existing) if existing else None
    if chosen:
        print(f"  Selected category-matched engine sound: {chosen.name}")
    return chosen


def get_last_reel_timestamp() -> datetime | None:
    """Return datetime of the last successful weekly_reel or reel in log.json."""
    if not LOG_FILE.exists():
        return None
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            entries = json.load(f)
        for entry in reversed(entries):
            if entry.get("kind") in ("weekly_reel", "reel") and entry.get("status") == "ok":
                t_str = entry.get("time")
                if t_str:
                    return datetime.fromisoformat(t_str)
    except Exception:
        pass
    return None


# ── Interleaved Diverse Rotation Order (Never Clusters Same Brand) ────────
CURATED_ROTATION_ORDER = [
    "porsche_911_gt3rs",      # German track supercar (photo)
    "lamborghini_diablo",     # Italian vintage V12 (video)
    "bmw_s1000rr",            # German hypersport superbike (photo)
    "ferrari_f80",            # Italian hypercar (video)
    "nissan_gtr_r35",         # Japanese twin-turbo AWD (photo)
    "aston_martin_valour",    # British twin-turbo V12 manual (video)
    "ducati_panigale_v4s",    # Italian MotoGP-derived superbike (photo)
    "audi_r8_v10",            # German naturally aspirated V10 (photo)
    "white_mustang_sunset",   # American V8 muscle (video)
    "ferrari_sf90",           # Italian hybrid hypercar (photo)
    "bmw_m4_black",           # German twin-turbo coupe (photo)
    "red_sportscar",          # Crimson roadster (video)
    "lamborghini_revuelto",   # Italian V12 hybrid flagship (photo)
]

SET_BRAND_MAP = {
    "aston_martin_valour": "aston_martin",
    "audi_r8_v10": "audi",
    "bmw_m4_black": "bmw",
    "bmw_s1000rr": "bmw_bike",
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

def get_next_curated_vehicle_set(theme_hint: str = "") -> tuple[str, list[Path], bool]:
    """Return (set_name, media_paths, is_bike) from the curated vehicle library.
    Loads all 13 authentic sets (both video motion clip sets and DSLR photo cuts)
    and strictly enforces NO REPEATS using the last 5 posts in log.json and
    ensures NO SAME-BRAND repeats back-to-back.
    """
    available_dict: dict[str, list[Path]] = {}

    # 1. Video clip sets
    if VEHICLE_VIDEO_SETS_DIR.exists():
        for d in sorted(VEHICLE_VIDEO_SETS_DIR.iterdir()):
            if d.is_dir():
                clips = sorted(list(d.glob("clip_*.mp4")))
                if len(clips) >= 3:
                    available_dict[d.name] = clips

    # 2. Curated DSLR photo sets
    if VEHICLE_SETS_DIR.exists():
        for d in sorted(VEHICLE_SETS_DIR.iterdir()):
            if d.is_dir():
                shots = sorted(list(d.glob("shot_*.jpg")))
                if len(shots) == 5:
                    available_dict[d.name] = shots

    if not available_dict:
        v_dir = Path(__file__).parent / "assets" / "vehicles"
        imgs = sorted(list(v_dir.glob("*.jpg")))[:5]
        return "single_car", imgs, False

    # Use ordered interleaved list for diversity
    active_order = [s for s in CURATED_ROTATION_ORDER if s in available_dict]
    if not active_order:
        active_order = sorted(list(available_dict.keys()))

    # Map caption keywords to set names for log inspection
    _CAPTION_TO_SET: dict[str, str] = {
        "aston": "aston_martin_valour", "valour": "aston_martin_valour",
        "audi": "audi_r8_v10", "r8": "audi_r8_v10", "suzuka grey": "audi_r8_v10",
        "bmw m4": "bmw_m4_black", "m4": "bmw_m4_black", "g82": "bmw_m4_black",
        "s1000rr": "bmw_s1000rr",
        "ducati": "ducati_panigale_v4s", "panigale": "ducati_panigale_v4s", "v4 s": "ducati_panigale_v4s", "v4s": "ducati_panigale_v4s",
        "ferrari f80": "ferrari_f80", "f80": "ferrari_f80",
        "sf90": "ferrari_sf90", "stradale": "ferrari_sf90",
        "diablo": "lamborghini_diablo", "giallo fly": "lamborghini_diablo",
        "revuelto": "lamborghini_revuelto", "arancio apodis": "lamborghini_revuelto",
        "nissan": "nissan_gtr_r35", "gtr": "nissan_gtr_r35", "godzilla": "nissan_gtr_r35",
        "porsche": "porsche_911_gt3rs", "gt3": "porsche_911_gt3rs", "shark blue": "porsche_911_gt3rs", "weissach": "porsche_911_gt3rs",
        "crimson": "red_sportscar", "underground": "red_sportscar", "roadster": "red_sportscar",
        "mustang": "white_mustang_sunset", "oxford white": "white_mustang_sunset", "sunset velocity": "white_mustang_sunset",
    }

    # Find recently posted vehicle sets from vehicle_state.json AND log.json
    recent_posted: list[str] = []
    if VEHICLE_STATE_FILE.exists():
        try:
            with open(VEHICLE_STATE_FILE, encoding="utf-8") as _vf:
                _vdata = json.load(_vf)
                for _s in _vdata.get("recent_sets", []):
                    if _s in active_order and _s not in recent_posted:
                        recent_posted.append(_s)
                if not recent_posted and "last_set" in _vdata and _vdata["last_set"] in active_order:
                    recent_posted.append(_vdata["last_set"])
        except Exception:
            pass

    try:
        if LOG_FILE.exists():
            with open(LOG_FILE, encoding="utf-8") as _lf:
                _log_entries = json.load(_lf)
            for _entry in reversed(_log_entries):
                _det = _entry.get("detail", "")
                _cap = _entry.get("caption", "").lower()
                m_set = re.search(r'set=([a-z0-9_]+)', _det)
                if m_set and m_set.group(1) in active_order:
                    s_found = m_set.group(1)
                    if s_found not in recent_posted:
                        recent_posted.append(s_found)
                else:
                    for _kw, _sname in _CAPTION_TO_SET.items():
                        if _kw in _cap and _sname in active_order:
                            if _sname not in recent_posted:
                                recent_posted.append(_sname)
                            break
                if len(recent_posted) >= 11:
                    break
    except Exception:
        pass

    last_posted_set = recent_posted[0] if recent_posted else ""
    last_posted_brand = SET_BRAND_MAP.get(last_posted_set, "")

    # 1. If user provided a specific theme hint (from custom reel or prompt)
    th = theme_hint.lower().strip()
    if th:
        KEYWORD_MAP = {
            "porsche": ["porsche_911_gt3rs"],
            "gt3": ["porsche_911_gt3rs"],
            "shark blue": ["porsche_911_gt3rs"],
            "ducati": ["ducati_panigale_v4s"],
            "panigale": ["ducati_panigale_v4s"],
            "s1000rr": ["bmw_s1000rr"],
            "bike": ["bmw_s1000rr", "ducati_panigale_v4s"],
            "motorcycle": ["bmw_s1000rr", "ducati_panigale_v4s"],
            "superbike": ["bmw_s1000rr", "ducati_panigale_v4s"],
            "bmw": ["bmw_m4_black", "bmw_s1000rr"],
            "m4": ["bmw_m4_black"],
            "audi": ["audi_r8_v10"],
            "r8": ["audi_r8_v10"],
            "nissan": ["nissan_gtr_r35"],
            "gtr": ["nissan_gtr_r35"],
            "godzilla": ["nissan_gtr_r35"],
            "sf90": ["ferrari_sf90"],
            "ferrari": ["ferrari_sf90", "ferrari_f80"],
            "f80": ["ferrari_f80"],
            "revuelto": ["lamborghini_revuelto"],
            "diablo": ["lamborghini_diablo"],
            "lamborghini": ["lamborghini_revuelto", "lamborghini_diablo"],
            "aston": ["aston_martin_valour"],
            "valour": ["aston_martin_valour"],
            "mustang": ["white_mustang_sunset"],
            "red": ["red_sportscar", "ducati_panigale_v4s"],
            "sportscar": ["red_sportscar", "audi_r8_v10", "porsche_911_gt3rs"],
        }
        matched_names: set[str] = set()
        for kw, target_sets in KEYWORD_MAP.items():
            if kw in th:
                matched_names.update(target_sets)

        if matched_names:
            matched_candidates = [s for s in active_order if s in matched_names and s in available_dict]
            # Prioritize candidate NOT recently posted and NOT same brand as last
            fresh = [c for c in matched_candidates if c not in recent_posted and SET_BRAND_MAP.get(c) != last_posted_brand]
            if not fresh:
                fresh = [c for c in matched_candidates if c not in recent_posted]
            if fresh:
                chosen_name = fresh[0]
                is_bike = any(b in chosen_name for b in ["bike", "ducati", "s1000rr"])
                return chosen_name, available_dict[chosen_name], is_bike
            # If all were recent, pick the least recently posted
            for r in reversed(recent_posted):
                if r in matched_candidates:
                    chosen_name = r
                    is_bike = any(b in chosen_name for b in ["bike", "ducati", "s1000rr"])
                    return chosen_name, available_dict[chosen_name], is_bike

    # 2. General round-robin through active_order: pick next set NOT in recent_posted AND NOT same brand
    start_idx = (active_order.index(last_posted_set) + 1) % len(active_order) if last_posted_set in active_order else 0

    chosen_name = None
    for step in range(len(active_order)):
        candidate_idx = (start_idx + step) % len(active_order)
        cand_name = active_order[candidate_idx]
        # AGENT_SPEC §3: Check 7-day model cooldown
        if REGISTRY_AVAILABLE and is_set_on_cooldown(cand_name, cooldown_days=7):
            continue
        if cand_name not in recent_posted and SET_BRAND_MAP.get(cand_name) != last_posted_brand:
            chosen_name = cand_name
            break

    # If all non-recent share the brand, relax brand check
    if not chosen_name:
        for step in range(len(active_order)):
            candidate_idx = (start_idx + step) % len(active_order)
            cand_name = active_order[candidate_idx]
            if cand_name not in recent_posted:
                chosen_name = cand_name
                break

    if not chosen_name:
        chosen_name = active_order[start_idx]

    # Cache recent_sets queue (keeps last 11 so all 13 cycle through before repeating)
    updated_recent = [chosen_name] + [s for s in recent_posted if s != chosen_name]
    updated_recent = updated_recent[:11]
    try:
        VEHICLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(VEHICLE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_set": chosen_name, "recent_sets": updated_recent}, f, indent=2)
    except Exception:
        pass

    is_bike = any(b in chosen_name for b in ["bike", "ducati", "yamaha", "kawasaki", "s1000rr"])
    return chosen_name, available_dict[chosen_name], is_bike


# ---------------------------------------------------------------------------
# Dynamic Split-Screen Comparison & Deep-Dive Engine
# - Comparison reels: One-by-one car with image on TOP HALF (1080x980) and points on BOTTOM HALF (1080x940)
# - Normal reels: 100% CLEAN full-screen cinematic footage with ZERO text overlays!
# ---------------------------------------------------------------------------

def get_clean_font(size: int = 36, bold: bool = True):
    """Find a crisp, bold sans-serif font across Windows and Linux runners."""
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans.ttf"
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def ensure_image_path(media_path: Path) -> Path:
    """If media_path is an mp4 video, extract a pristine 1080p frame snapshot. Otherwise return as is."""
    if not media_path or not media_path.exists():
        return media_path
    if media_path.suffix.lower() == ".mp4":
        frame_out = media_path.parent / f"{media_path.stem}_frame.jpg"
        if not frame_out.exists():
            import subprocess
            subprocess.run([
                "ffmpeg", "-y", "-ss", "00:00:01.000",
                "-i", str(media_path),
                "-vframes", "1", "-q:v", "2",
                str(frame_out)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if frame_out.exists():
            return frame_out
    return media_path


def make_split_comparison_slide(car_img_path: Path, contender_num: int, total_contenders: int, car_name: str, points: list[tuple[str, str]], out_path: Path) -> Path:
    """Render a 9:16 vertical slide with image on top half (1080x980) and specs points on bottom half (1080x940)."""
    car_img_path = ensure_image_path(car_img_path)
    from PIL import Image, ImageDraw
    W, H = 1080, 1920
    frame = Image.new("RGB", (W, H), (11, 14, 21))
    draw = ImageDraw.Draw(frame)

    # 1. Top half: 1080x980 car image (unobstructed, zero text over car)
    target_w, target_h = 1080, 980
    if car_img_path and car_img_path.exists():
        with Image.open(car_img_path).convert("RGB") as car_im:
            car_ratio = car_im.width / car_im.height
            if car_ratio > (target_w / target_h):
                new_h = target_h
                new_w = int(target_h * car_ratio)
            else:
                new_w = target_w
                new_h = int(target_w / car_ratio)
            resized = car_im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = (new_w - target_w) // 2
            top = (new_h - target_h) // 2
            cropped = resized.crop((left, top, left + target_w, top + target_h))
            frame.paste(cropped, (0, 0))

    # Divider line with gradient neon glow
    draw.line([(0, target_h - 1), (W, target_h - 1)], fill=(99, 102, 241), width=3)
    draw.line([(0, target_h + 2), (W, target_h + 2)], fill=(139, 92, 246), width=2)

    # Fonts
    font_badge = get_clean_font(26, bold=True)
    font_title = get_clean_font(42, bold=True)
    font_label = get_clean_font(28, bold=False)
    font_val = get_clean_font(30, bold=True)

    # Contender Pill Badge (e.g. [ CONTENDER 1 OF 3 ])
    badge_text = f"CONTENDER {contender_num} OF {total_contenders}" if contender_num > 0 else "THE SHOWDOWN"
    badge_w = 420
    badge_h = 46
    draw.rounded_rectangle(
        [(W - badge_w) // 2, 1020, (W + badge_w) // 2, 1020 + badge_h],
        radius=23,
        fill=(26, 32, 48),
        outline=(139, 92, 246),
        width=2
    )
    draw.text((W // 2, 1020 + badge_h // 2), badge_text, fill=(216, 180, 254), font=font_badge, anchor="mm")

    # Main Car Title
    draw.text((W // 2, 1110), car_name.upper(), fill=(255, 255, 255), font=font_title, anchor="mm")

    # Points list (stacked neat rows)
    y_start = 1170
    row_h = 92
    for i, (label, val) in enumerate(points[:6]):
        py = y_start + i * (row_h + 12)
        draw.rounded_rectangle([60, py, W - 60, py + row_h], radius=14, fill=(18, 22, 32), outline=(255, 255, 255, 25), width=1)
        draw.text((95, py + row_h // 2), label, fill=(160, 175, 205), font=font_label, anchor="lm")
        draw.text((W - 95, py + row_h // 2), val, fill=(255, 255, 255), font=font_val, anchor="rm")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.save(out_path, quality=95)
    return out_path

def make_split_showdown_slide(car_img_path: Path, car_names: list[str], out_path: Path) -> Path:
    """Render the outro voting slide with image on top half and 3-car selection on bottom half."""
    car_img_path = ensure_image_path(car_img_path)
    from PIL import Image, ImageDraw
    W, H = 1080, 1920
    frame = Image.new("RGB", (W, H), (11, 14, 21))
    draw = ImageDraw.Draw(frame)

    target_w, target_h = 1080, 980
    if car_img_path and car_img_path.exists():
        with Image.open(car_img_path).convert("RGB") as car_im:
            car_ratio = car_im.width / car_im.height
            if car_ratio > (target_w / target_h):
                new_h = target_h
                new_w = int(target_h * car_ratio)
            else:
                new_w = target_w
                new_h = int(target_w / car_ratio)
            resized = car_im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = (new_w - target_w) // 2
            top = (new_h - target_h) // 2
            cropped = resized.crop((left, top, left + target_w, top + target_h))
            frame.paste(cropped, (0, 0))

    draw.line([(0, target_h), (W, target_h)], fill=(239, 68, 68), width=4)

    font_badge = get_clean_font(28, bold=True)
    font_title = get_clean_font(44, bold=True)
    font_opt = get_clean_font(32, bold=True)
    font_sub = get_clean_font(30, bold=False)

    badge_w = 380
    badge_h = 48
    draw.rounded_rectangle([(W - badge_w) // 2, 1025, (W + badge_w) // 2, 1025 + badge_h], radius=24, fill=(239, 68, 68, 50), outline=(239, 68, 68), width=2)
    draw.text((W // 2, 1025 + badge_h // 2), "⚔️ THE SHOWDOWN", fill=(254, 202, 202), font=font_badge, anchor="mm")

    draw.text((W // 2, 1120), "WHICH ONE ARE YOU TAKING?", fill=(255, 255, 255), font=font_title, anchor="mm")

    y_start = 1195
    row_h = 105
    emojis = ["1️⃣", "2️⃣", "3️⃣"]
    for i, name in enumerate(car_names[:3]):
        py = y_start + i * (row_h + 18)
        draw.rounded_rectangle([60, py, W - 60, py + row_h], radius=16, fill=(22, 26, 38), outline=(255, 255, 255, 35), width=1)
        draw.text((100, py + row_h // 2), f"{emojis[i]}  {name.upper()}", fill=(255, 255, 255), font=font_opt, anchor="lm")

    draw.text((W // 2, 1630), "Vote 1, 2, or 3 in the comments below! 👇", fill=(160, 175, 205), font=font_sub, anchor="mm")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.save(out_path, quality=95)
    return out_path


CAR_SPECS_DATABASE = {
    "porsche_911_gt3rs": {
        "name": "Porsche 911 GT3 RS",
        "points": [
            ("🐎 Horsepower", "518 HP @ 9,000 RPM"),
            ("⚡ Engine", "4.0L Naturally Aspirated Flat-6"),
            ("⏱️ 0-100 km/h", "3.2 Seconds"),
            ("🌪️ Downforce", "860 kg (Active Swan-Neck DRS)"),
            ("🏁 Top Speed", "296 km/h (184 mph)"),
            ("💰 Starting Price", "$241,300 USD")
        ]
    },
    "lamborghini_revuelto": {
        "name": "Lamborghini Revuelto",
        "points": [
            ("🐎 Horsepower", "1,001 HP Total Output"),
            ("⚡ Engine", "6.5L Naturally Aspirated V12 + 3 e-Motors"),
            ("⏱️ 0-100 km/h", "2.5 Seconds"),
            ("🏎️ Chassis", "Forged Composite Monofuselage"),
            ("🏁 Top Speed", "350+ km/h (217+ mph)"),
            ("💰 Starting Price", "$608,000 USD")
        ]
    },
    "ferrari_sf90": {
        "name": "Ferrari SF90 Stradale",
        "points": [
            ("🐎 Horsepower", "986 HP Total Output"),
            ("⚡ Engine", "4.0L Twin-Turbo V8 + 3 e-Motors"),
            ("⏱️ 0-100 km/h", "2.4 Seconds (AWD Launch)"),
            ("🏎️ Drivetrain", "RAC-e Electric Torque Vectoring"),
            ("🏁 Top Speed", "340 km/h (211 mph)"),
            ("💰 Starting Price", "$528,000 USD")
        ]
    },
    "audi_r8_v10": {
        "name": "Audi R8 V10 Performance",
        "points": [
            ("🐎 Horsepower", "602 HP @ 8,700 RPM"),
            ("⚡ Engine", "5.2L Naturally Aspirated V10"),
            ("⏱️ 0-100 km/h", "3.1 Seconds"),
            ("🏎️ Drivetrain", "Quattro All-Wheel Drive"),
            ("🏁 Top Speed", "331 km/h (205 mph)"),
            ("💰 Starting Price", "$210,000 USD")
        ]
    },
    "white_mustang_sunset": {
        "name": "Ford Mustang GT 5.0",
        "points": [
            ("🐎 Horsepower", "480 HP @ 7,150 RPM"),
            ("⚡ Engine", "5.0L Gen-4 Coyote V8"),
            ("⏱️ 0-100 km/h", "4.3 Seconds"),
            ("🏎️ Drivetrain", "Rear-Wheel Drive American Muscle"),
            ("🏁 Top Speed", "250 km/h (155 mph)"),
            ("💰 Starting Price", "$43,000 USD")
        ]
    },
    "nissan_gtr_r35": {
        "name": "Nissan GT-R Nismo",
        "points": [
            ("🐎 Horsepower", "600 HP @ 6,800 RPM"),
            ("⚡ Engine", "3.8L Handbuilt Twin-Turbo VR38 V6"),
            ("⏱️ 0-100 km/h", "2.7 Seconds"),
            ("🏎️ Drivetrain", "ATTESA E-TS All-Wheel Drive"),
            ("🏁 Top Speed", "330 km/h (205 mph)"),
            ("💰 Starting Price", "$221,000 USD")
        ]
    },
    "ducati_panigale_v4s": {
        "name": "Ducati Panigale V4 S",
        "points": [
            ("🐎 Horsepower", "215.5 HP @ 13,000 RPM"),
            ("⚡ Engine", "1,103cc Desmosedici Stradale V4"),
            ("⏱️ 0-100 km/h", "2.9 Seconds"),
            ("🏎️ Weight", "175 kg Dry (1.23 HP / kg)"),
            ("🏁 Top Speed", "305 km/h (190 mph)"),
            ("💰 Starting Price", "$31,500 USD")
        ]
    },
    "bmw_s1000rr": {
        "name": "BMW S1000RR M Package",
        "points": [
            ("🐎 Horsepower", "205 HP @ 13,750 RPM"),
            ("⚡ Engine", "999cc ShiftCam Inline-4"),
            ("⏱️ 0-100 km/h", "2.8 Seconds"),
            ("🏎️ Wheels", "M Carbon Fiber Wheels"),
            ("🏁 Top Speed", "303 km/h (188 mph)"),
            ("💰 Starting Price", "$24,500 USD")
        ]
    },
    "ferrari_f80": {
        "name": "Ferrari F80 (2026)",
        "points": [
            ("🐎 Horsepower", "1,184 HP Total Output"),
            ("⚡ Engine", "3.0L Twin-Turbo 120° V6 + Le Mans Hybrid"),
            ("⏱️ 0-100 km/h", "2.15 Seconds"),
            ("🏎️ Production", "799 Units Worldwide"),
            ("🏁 Top Speed", "350 km/h (217 mph)"),
            ("💰 Starting Price", "$3,900,000 USD")
        ]
    },
    "aston_martin_valour": {
        "name": "Aston Martin Valour",
        "points": [
            ("🐎 Horsepower", "705 HP @ 7,000 RPM"),
            ("⚡ Engine", "5.2L Twin-Turbo V12"),
            ("🕹️ Gearbox", "Bespoke 6-Speed Manual"),
            ("🏎️ Production", "110 Units Worldwide"),
            ("🏁 Top Speed", "322 km/h (200 mph)"),
            ("💰 Starting Price", "$1,500,000 USD")
        ]
    },
    "lamborghini_diablo": {
        "name": "Lamborghini Diablo VT",
        "points": [
            ("🐎 Horsepower", "492 HP @ 7,000 RPM"),
            ("⚡ Engine", "5.7L Naturally Aspirated V12"),
            ("🕹️ Gearbox", "5-Speed Gated Manual"),
            ("⏱️ 0-100 km/h", "4.0 Seconds"),
            ("🏁 Top Speed", "328 km/h (204 mph)"),
            ("💰 Valuation", "Classic Collector Icon ($450k+)")
        ]
    },
    "bmw_m4_black": {
        "name": "BMW M4 Competition",
        "points": [
            ("🐎 Horsepower", "503 HP @ 6,250 RPM"),
            ("⚡ Engine", "3.0L S58 Twin-Turbo Inline-6"),
            ("⏱️ 0-100 km/h", "3.4 Seconds (M xDrive)"),
            ("🏎️ Drivetrain", "M xDrive with 2WD Drift Mode"),
            ("🏁 Top Speed", "290 km/h (180 mph)"),
            ("💰 Starting Price", "$83,200 USD")
        ]
    }
}

COMPARISON_BATTLES = [
    {
        "id": "battle_hyper_track",
        "title": "Track Titans Battle • GT3 RS vs Revuelto vs SF90 ⚔️🏁",
        "cars": ["porsche_911_gt3rs", "lamborghini_revuelto", "ferrari_sf90"],
        "captions": [
            "3 philosophies of speed. The aerodynamic precision of the Porsche 911 GT3 RS (518 HP, 9,000 RPM). The thunderous 1,001 HP V12 hybrid fury of the Lamborghini Revuelto. The surgical 2.4s AWD launch of the Ferrari SF90 Stradale. Which key are you grabbing for a weekend on the track? Drop 1, 2, or 3 below! 🔑👇",
            "German precision vs Italian V12 fury vs Maranello hybrid speed. Porsche 911 GT3 RS, Lamborghini Revuelto, or Ferrari SF90 Stradale? Vote your champion below 👇🔥"
        ],
        "tags": ["carbattle", "supercars", "hypercars", "porsche", "lamborghini", "ferrari", "gt3rs", "revuelto", "sf90", "carreels", "carlifestyle"]
    },
    {
        "id": "battle_sound_symphony",
        "title": "Exhaust Symphony Battle • V10 vs V8 vs Twin-Turbo 🔊⚔️",
        "cars": ["audi_r8_v10", "white_mustang_sunset", "nissan_gtr_r35"],
        "captions": [
            "The ultimate exhaust sound battle. 5.2 liters of screaming naturally aspirated German V10 (Audi R8). 5.0 liters of deep naturally aspirated American muscle rumble (Mustang GT). Or the twin-turbo whistle and all-wheel drive launch of Godzilla (Nissan GT-R). Which soundtrack reigns supreme? Turn your volume UP and vote below! 🔊👇"
        ],
        "tags": ["soundbattle", "exhaustsound", "v10", "v8", "twinturbo", "audir8", "mustang", "nissangtr", "carreels", "carlifestyle"]
    },
    {
        "id": "battle_two_vs_four",
        "title": "Two Wheels vs Four Wheels • Ducati vs BMW vs Porsche ⚔️🔥",
        "cars": ["ducati_panigale_v4s", "bmw_s1000rr", "porsche_911_gt3rs"],
        "captions": [
            "Two wheels vs four wheels: where does the true rush live? The Ducati Panigale V4 S packs 215 HP into 175 kg. The BMW S1000RR M brings ShiftCam precision. The Porsche 911 GT3 RS brings 860 kg of active aerodynamic downforce. Would you rather lean into the apex on two wheels or pull 2G in the GT3 RS? Vote below! 🔥👇"
        ],
        "tags": ["bikesvscars", "ducati", "bmwmotorrad", "porsche", "panigale", "s1000rr", "gt3rs", "superbikes", "supercars", "carlifestyle"]
    },
    {
        "id": "battle_modern_vs_analog",
        "title": "Le Mans Vanguard vs Pure Analog V12 • F80 vs Valour vs Diablo ⚔️🇮🇹",
        "cars": ["ferrari_f80", "aston_martin_valour", "lamborghini_diablo"],
        "captions": [
            "The clash of eras. The 1,184 HP Ferrari F80 hypercar with Le Mans hybrid technology. The ultra-exclusive 705 HP Aston Martin Valour with a 6-speed manual V12. Or the poster-car icon: the 1990s Lamborghini Diablo VT with naturally aspirated V12 purity. If you could own one forever, which keys are you taking? Vote below! 👇"
        ],
        "tags": ["analogvsmodern", "ferrarif80", "astonvalour", "lamborghinidiablo", "v12", "manualsupercar", "hypercars", "carreels", "carlifestyle"]
    }
]

DEEP_DIVE_TOPICS = {
    "porsche_911_gt3rs": {
        "title": "Porsche 911 GT3 RS • Active DRS Aero Breakdown 🏁",
        "component": "Active Swan-Neck DRS Aero",
        "points": [
            ("🌪️ Downforce", "860 kg @ 285 km/h (2x 991.2 Gen)"),
            ("⚡ DRS Wing", "Hydraulic Upper Blade Flattens on Straights"),
            ("🕹️ Cockpit Control", "Dedicated Steering Wheel DRS Button"),
            ("🏎️ Engine", "4.0L Naturally Aspirated Flat-6 (9,000 RPM)"),
            ("⏱️ Track Metric", "Nürburgring Nordschleife: 6:49.328"),
            ("💰 Price", "$241,300 USD (Street-Legal Scalpel)")
        ],
        "captions": [
            "Under the skin of the Porsche 911 GT3 RS lies an aerodynamic marvel: swan-neck active DRS generating an insane 860 kg of downforce. At speed, a hydraulic actuator tilts the upper wing blade flat, dropping drag instantly for high-speed straights. Does any road-legal machine rival Porsche's track engineering? Drop your rating 1-10 below 👇"
        ],
        "tags": ["porsche", "gt3rs", "activedrs", "aerodynamics", "trackweapon", "speedhunters", "supercars", "carlifestyle"]
    },
    "ferrari_sf90": {
        "title": "Ferrari SF90 • RAC-e Torque Vectoring Breakdown 🔴⚡",
        "component": "RAC-e Triple Motor Torque Vectoring",
        "points": [
            ("🐎 Total Output", "986 Horsepower (V8 + 3 e-Motors)"),
            ("⚡ Front Axle", "RAC-e Dual Independent Electric Motors"),
            ("⏱️ Acceleration", "0-100 km/h in 2.4s (Instant Torque-Fill)"),
            ("🌪️ Active Aero", "Shut-Off Gurney Flap Lowers in Bends"),
            ("🏁 Top Speed", "340 km/h (211 mph)"),
            ("💰 Price", "$528,000 USD (Maranello Flagship)")
        ],
        "captions": [
            "How does the Ferrari SF90 Stradale hit 0-100 km/h in 2.4 seconds? The secret is Ferrari's RAC-e torque vectoring system. Dual independent electric motors on the front axle pull the nose through turns before the 4.0L twin-turbo V8 even hits peak boost. Is this Maranello's most brilliant powertrain? Drop your thoughts below 👇"
        ],
        "tags": ["ferrari", "sf90", "sf90stradale", "torquevectoring", "hybridhypercar", "maranello", "supercars", "carlifestyle"]
    },
    "lamborghini_revuelto": {
        "title": "Lamborghini Revuelto • V12 Hybrid Monofuselage Breakdown 🟡⚡",
        "component": "Forged Composite Monofuselage",
        "points": [
            ("🐎 Total Output", "1,001 Horsepower (6.5L V12 + 3 e-Motors)"),
            ("⚡ Redline", "Naturally Aspirated V12 Screaming to 9,500 RPM"),
            ("🏎️ Monofuselage", "10% Lighter, 25% Stiffer than Aventador"),
            ("🕹️ Gearbox", "Transverse 8-Speed Dual-Clutch Behind Engine"),
            ("⏱️ 0-100 km/h", "2.5 Seconds (Electric AWD Launch)"),
            ("💰 Price", "$608,000 USD (New Hypercar Dawn)")
        ],
        "captions": [
            "While others downsized, Lamborghini doubled down. The Revuelto pairs a screaming 6.5L naturally aspirated V12 (9,500 RPM redline) with triple electric motors for 1,001 horsepower. Mounted in a fighter-jet inspired carbon monofuselage, it defines the new hypercar era. Rate this V12 hybrid beast 1-10 👇"
        ],
        "tags": ["lamborghini", "revuelto", "v12", "monofuselage", "hybridhypercar", "hypercars", "supercars", "carlifestyle"]
    },
    "ducati_panigale_v4s": {
        "title": "Ducati Panigale V4 S • MotoGP Counter-Rotating Crank 🏍️🔥",
        "component": "MotoGP Counter-Rotating Crankshaft",
        "points": [
            ("🐎 Horsepower", "215.5 HP @ 13,000 RPM"),
            ("⚡ Counter Crank", "Spins Reverse to Wheels to Cancel Inertia"),
            ("🌪️ Biplane Wings", "37 kg Downforce @ 270 km/h"),
            ("🏎️ Dry Weight", "175 kg (Power-to-Weight: 1.23 HP/kg)"),
            ("⏱️ 0-100 km/h", "2.9 Seconds (Launch Control)"),
            ("💰 Price", "$31,500 USD (Two-Wheel Royalty)")
        ],
        "captions": [
            "Why does the Ducati Panigale V4 S turn in faster than any road bike? It features a MotoGP-derived counter-rotating crankshaft that spins in reverse relative to the wheels, cancelling out gyroscopic inertia. Paired with biplane carbon wings, it glues 215.5 HP to the tarmac. Is this the ultimate superbike? Drop your thoughts below 👇"
        ],
        "tags": ["ducati", "panigalev4s", "superbikes", "motogp", "bikersofinstagram", "twowheels", "sportbike", "bikelife"]
    }
}

FUTURE_HYPERCARS_TOPIC = {
    "title": "2025/2026 Hypercar Vanguard • F80 vs Tourbillon vs Temerario 🔮⚡",
    "cars": [
        ("ferrari_f80", "Ferrari F80 (2026)", [
            ("🐎 Horsepower", "1,184 HP Total Hybrid Output"),
            ("⚡ Powertrain", "3.0L Twin-Turbo 120° V6 + Le Mans MGU-K"),
            ("⏱️ 0-100 km/h", "2.15 Seconds (Fastest Ferrari Ever)"),
            ("🏎️ Production", "Strictly Limited to 799 Units"),
            ("🏁 Top Speed", "350 km/h (217 mph)"),
            ("💰 Price", "$3,900,000 USD")
        ]),
        ("aston_martin_valour", "Bugatti Tourbillon (2026)", [
            ("🐎 Horsepower", "1,800 HP Total Output"),
            ("⚡ Engine", "8.3L Naturally Aspirated Cosworth V16"),
            ("🏎️ Redline", "9,000 RPM Naturally Aspirated Scream"),
            ("🕹️ Interior", "Skeletonized Swiss Titanium Gauge Cluster"),
            ("⏱️ 0-100 km/h", "2.0 Seconds (AWD Electric Torque)"),
            ("💰 Price", "$4,600,000 USD")
        ]),
        ("lamborghini_diablo", "Lamborghini Temerario (2026)", [
            ("🐎 Horsepower", "907 Horsepower"),
            ("⚡ Engine", "4.0L Twin-Turbo Flat-Plane V8"),
            ("🏎️ Redline", "Astounding 10,000 RPM Rev Limit"),
            ("⏱️ 0-100 km/h", "2.7 Seconds"),
            ("🏁 Top Speed", "343 km/h (213 mph)"),
            ("💰 Price", "$357,000 USD")
        ])
    ],
    "captions": [
        "The 2025/2026 hypercar war has officially ignited. The 1,184 HP Ferrari F80 Le Mans hybrid hypercar. The 1,800 HP naturally aspirated Bugatti Tourbillon V16. And the 907 HP Lamborghini Temerario revving to 10,000 RPM. Which of these future release titans are you crowning king? Drop your verdict below! 🔮👇"
    ],
    "tags": ["futurecars", "2026hypercars", "ferrarif80", "bugattitourbillon", "temerario", "hypercars", "supercars", "carreels", "carlifestyle"]
}



# ---------------------------------------------------------------------------
# Automated Mixed Pattern Cycler
# Ensures EVERY reel alternates format on both Instagram and YouTube:
#   1. comparison (3-Car Comparison Battle with split screen: top image, bottom specs)
#   2. deep_dive (Deep engineering breakdown: top image, bottom technical analysis)
#   3. pure_cinematic (100% clean full-screen supercar/superbike cinematography, zero text)
#   4. future_release (2026 Upcoming Hypercars breakdown)
# ---------------------------------------------------------------------------
REEL_PATTERN_CYCLE = ["comparison", "deep_dive", "pure_cinematic", "future_release"]

def get_next_reel_pattern() -> tuple[str, int]:
    """Return (chosen_format, sub_index) ensuring automated scheduled runs never repeat.
    Rotates in a continuous round-robin cycle through all 4 formats.
    """
    state = {
        "last_format": "",
        "format_idx": 0,
        "battle_idx": 0,
        "deep_dive_idx": 0,
        "history": []
    }
    if REEL_PATTERN_FILE.exists():
        try:
            with open(REEL_PATTERN_FILE, encoding="utf-8") as f:
                state.update(json.load(f))
        except Exception:
            pass

    last_fmt = state.get("last_format", "")
    if not last_fmt and LOG_FILE.exists():
        try:
            with open(LOG_FILE, encoding="utf-8") as lf:
                logs = json.load(lf)
            for entry in reversed(logs):
                det = entry.get("detail", "")
                m = re.search(r'format=([a-z0-9_]+)', det)
                if m and m.group(1) in REEL_PATTERN_CYCLE:
                    last_fmt = m.group(1)
                    break
        except Exception:
            pass

    if last_fmt in REEL_PATTERN_CYCLE:
        cur_idx = (REEL_PATTERN_CYCLE.index(last_fmt) + 1) % len(REEL_PATTERN_CYCLE)
    else:
        cur_idx = state.get("format_idx", 0) % len(REEL_PATTERN_CYCLE)

    chosen_format = REEL_PATTERN_CYCLE[cur_idx]

    sub_idx = 0
    if chosen_format == "comparison":
        sub_idx = state.get("battle_idx", 0) % len(COMPARISON_BATTLES)
        state["battle_idx"] = (sub_idx + 1) % len(COMPARISON_BATTLES)
    elif chosen_format == "deep_dive":
        deep_keys = list(DEEP_DIVE_TOPICS.keys())
        sub_idx = state.get("deep_dive_idx", 0) % len(deep_keys)
        state["deep_dive_idx"] = (sub_idx + 1) % len(deep_keys)

    state["last_format"] = chosen_format
    state["format_idx"] = (cur_idx + 1) % len(REEL_PATTERN_CYCLE)
    hist = state.get("history", [])
    hist.append(chosen_format)
    state["history"] = hist[-10:]

    try:
        REEL_PATTERN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REEL_PATTERN_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

    return chosen_format, sub_idx

def build_reel_video(
    media_paths: list[Path],
    output_path: Path,
    theme_hint: str = "",
    is_bike: bool = False,
    text_overlay: str = "",
    is_fast_motion: bool = False,
    is_static_slides: bool = False
) -> None:
    """Build a ~10-14s 9:16 vertical viral reel from real human-shot video clips, DSLR photo cuts,
    or static split-screen comparison slides.
    - Normal reels: 100% CLEAN with ZERO text overlays on screen.
    - Comparison reels: Smooth crossfade transitions between split-screen slides (image on top half, points on bottom half).
    """
    width, height = 1080, 1920
    is_video_clips = media_paths[0].suffix.lower() == ".mp4"

    music_path = pick_diverse_music_track()
    if is_fast_motion and not text_overlay and not is_static_slides:
        engine_sfx_path = pick_engine_sfx(theme_hint=theme_hint, is_bike=is_bike)
        if engine_sfx_path:
            print(f"  Fast motion detected: engine rev audio armed ({engine_sfx_path.name})")
    else:
        engine_sfx_path = None
        print("  Stationary / showcase / comparison video: engine rev omitted (clean soundtrack only)")

    has_music = music_path is not None
    has_engine = engine_sfx_path is not None

    n = len(media_paths)
    inputs: list[str] = []

    if is_video_clips:
        # Pure authentic human-shot video clip compilation (100% clean, no cards)
        clip_duration = 2.2
        for p in media_paths:
            inputs += ["-i", str(p)]

        music_input_idx = n
        engine_input_idx = n + (1 if has_music else 0)

        if has_music:
            inputs += ["-i", str(music_path)]
        if has_engine:
            inputs += ["-i", str(engine_sfx_path)]

        def get_clip_dur(p: Path) -> float:
            try:
                res = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                    text=True
                )
                return float(res.strip())
            except Exception:
                return 2.2

        total_dur = sum(get_clip_dur(p) for p in media_paths)

        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        for i in range(n):
            filter_parts.append(
                f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30,format=yuv420p[v{i}]"
            )
            concat_inputs.append(f"[v{i}]")

        concat_clause = "".join(concat_inputs) + f"concat=n={n}:v=1:a=0[vconcat]"
        filter_parts.append(concat_clause)
        last_v = "vconcat"

        txt_file: Path | None = None
        # Only Daily Study Journey Reel uses text_overlay (12 AM daily)
        if text_overlay:
            def _strip_emoji(text: str) -> str:
                return re.sub(r"[^\u0000-\u00FF\u2000-\u206F\u2E00-\u2E7F\s]", "", text).strip()
            lines_list: list[str] = []
            for raw_line in text_overlay.split("\n"):
                raw_line = _strip_emoji(raw_line.strip())
                if raw_line:
                    lines_list.extend(textwrap.wrap(raw_line, width=32))
            wrapped_text = "\n".join(lines_list)
            txt_file = output_path.with_suffix(".overlay.txt")
            txt_file.write_text(wrapped_text, encoding="utf-8")
            try:
                rel_txt_path = txt_file.relative_to(ROOT).as_posix()
            except Exception:
                rel_txt_path = txt_file.as_posix().replace(":", "\\:")
            filter_parts.append(
                f"[{last_v}]drawtext=textfile={rel_txt_path}:fontcolor=white:fontsize=36:"
                f"line_spacing=14:box=1:boxcolor=black@0.75:boxborderw=24:x=(w-text_w)/2:y=(h-text_h)/2[vfinal]"
            )
            last_v = "vfinal"

        if has_music and has_engine:
            m_idx = music_input_idx
            s_idx = engine_input_idx
            filter_parts.append(f"[{m_idx}:a]volume=\'if(lt(t,1.8),0.25,if(lt(t,3.2),0.25+(t-1.8)*0.45,0.75))':eval=frame[bgm]")
            filter_parts.append(f"[{s_idx}:a]volume=1.5[eng]")
            filter_parts.append(f"[bgm][eng]amix=inputs=2:duration=first:dropout_transition=2[aout]")
            audio_map = "[aout]"
        elif has_music:
            audio_map = f"{music_input_idx}:a"
        elif has_engine:
            audio_map = f"{engine_input_idx}:a"
        else:
            audio_map = None

        full_filter = ";".join(filter_parts)
        cmd = [*inputs, "-filter_complex", full_filter, "-map", f"[{last_v}]"]
        if audio_map:
            cmd += ["-map", audio_map]
        cmd += [
            "-t", f"{total_dur:.2f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "22",
            "-preset", "veryfast",
            "-threads", "0",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path),
            "-y",
        ]
    else:
        # Photography sets or Split-Screen Comparison Slides
        clip_duration = 3.2
        fade_duration = 0.35
        for img in media_paths:
            inputs += ["-loop", "1", "-t", str(clip_duration), "-i", str(img)]
        if has_music:
            inputs += ["-i", str(music_path)]
        if has_engine:
            inputs += ["-i", str(engine_sfx_path)]

        filter_parts = []
        if is_static_slides:
            # Comparison split-screen slides: exact 1080x1920 scaling, zero panning motion so text is 100% crisp
            for i in range(n):
                filter_parts.append(f"[{i}:v]scale={width}:{height},setsar=1,format=yuv420p[v{i}]")
        else:
            # Normal photography sets: smooth subtle cinematic pan/zoom
            motions = [
                "x=\'(in_w-out_w)/2\':y=\'(in_h-out_h)*(1-0.7*t/3.2)\'",
                "x=\'(in_w-out_w)*(0.2+0.6*t/3.2)\':y=\'(in_h-out_h)/2\'",
                "x=\'(in_w-out_w)*(0.8-0.6*t/3.2)\':y=\'(in_h-out_h)*(0.3+0.4*t/3.2)\'",
                "x=\'(in_w-out_w)/2\':y=\'(in_h-out_h)*(0.8-0.6*t/3.2)\'",
                "x=\'(in_w-out_w)*(0.1+0.8*t/3.2)\':y=\'(in_h-out_h)/2\'",
            ]
            for i in range(n):
                motion = motions[i % len(motions)]
                filter_parts.append(
                    f"[{i}:v]scale=1242:2208:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}:{motion},setsar=1,format=yuv420p[v{i}]"
                )

        last_out = "v0"
        for i in range(1, n):
            offset = i * (clip_duration - fade_duration)
            out_label = f"x{i}" if i < n - 1 else "vout"
            filter_parts.append(f"[{last_out}][v{i}]xfade=transition=fade:duration={fade_duration}:offset={offset:.2f}[{out_label}]")
            last_out = f"x{i}"
        last_v = "vout" if n > 1 else "v0"

        if has_music and has_engine:
            m_idx = n
            s_idx = n + 1
            filter_parts.append(f"[{m_idx}:a]volume=\'if(lt(t,1.8),0.25,if(lt(t,3.2),0.25+(t-1.8)*0.45,0.85))':eval=frame[bgm]")
            filter_parts.append(f"[{s_idx}:a]volume=1.4[eng]")
            filter_parts.append(f"[bgm][eng]amix=inputs=2:duration=first:dropout_transition=2[aout]")
            audio_map = "[aout]"
        elif has_music:
            audio_map = f"{n}:a"
        elif has_engine:
            audio_map = f"{n}:a"
        else:
            audio_map = None

        full_filter = ";".join(filter_parts)
        cmd = [*inputs, "-filter_complex", full_filter, "-map", f"[{last_v}]"]
        if audio_map:
            cmd += ["-map", audio_map, "-shortest", "-c:a", "aac", "-b:a", "160k"]
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
    try:
        result = subprocess.run(["ffmpeg", *cmd], capture_output=True, text=True, cwd=str(ROOT))
        if result.returncode != 0:
            print(f"FFmpeg stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"FFmpeg failed with code {result.returncode}")
    finally:
        if "txt_file" in locals() and txt_file and txt_file.exists():
            try:
                txt_file.unlink()
            except Exception:
                pass
    print(f"  Video built: {output_path} ({output_path.stat().st_size // 1024} KB)")


def load_journey_state() -> dict:
    """Load or initialize the daily study journey state."""
    default_state = {
        "day": 1,
        "start_date": "2026-11-04",
        "topic": "seeking help for my studies",
        "last_post_date": None
    }
    if not JOURNEY_STATE_FILE.exists():
        save_journey_state(default_state)
        return default_state
    try:
        with open(JOURNEY_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_state


def save_journey_state(state: dict) -> None:
    """Save journey state to disk."""
    JOURNEY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNEY_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_current_journey_day() -> int:
    """Calculate the current day number based on start date and elapsed calendar days."""
    state = load_journey_state()
    try:
        from datetime import datetime, timezone, timedelta
        start_date = datetime.strptime(state.get("start_date", "2026-11-04"), "%Y-%m-%d").date()
        today_local = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        days_elapsed = (today_local - start_date).days
        return max(1, days_elapsed + 1)
    except Exception:
        return state.get("day", 1)


def mode_journey_reel(persona: dict, env: dict, dry_run: bool) -> None:
    """Generate and post the daily 12 AM 'Day X of seeking help to complete my ug studies' reel (once per day)."""
    state = load_journey_state()
    from datetime import datetime, timezone, timedelta
    today_local_str = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")

    # If already posted today, strictly skip (NEVER duplicate daily study reel)
    if not dry_run and state.get("last_post_date") == today_local_str:
        print(f"\n[STRICT ONCE-PER-DAY] Daily study reel for today ({today_local_str}) was ALREADY published (Day {state.get('day', 1) - 1}).")
        print("Skipping to prevent duplicate.")
        return

    day = get_current_journey_day()
    print(f"\n=== MODE: journey_reel (Day {day} - 12 AM Daily Study Reel) ===")

    # Purely asking for help to complete UG studies — ZERO follower/1k requests, no "men to men"
    overlay = (
        f"Day {day} of seeking help to complete my ug studies\n\n"
        "I am an undergraduate student working hard to finish my education and graduate.\n"
        "If you have guidance, mentorship, or support to share, every bit helps me keep moving forward."
    )
    custom_prompt = f"Day {day} ug studies"

    mode_weekly_reel(persona, env, dry_run, custom_prompt=custom_prompt)

    if not dry_run:
        state["day"] = day + 1
        state["last_post_date"] = today_local_str
        save_journey_state(state)
        try:
            git_commit_and_push([JOURNEY_STATE_FILE], f"chore: advance study journey to Day {day + 1}")
        except Exception as exc:
            print(f"  Warning: failed to commit journey state: {exc}")

# ---------------------------------------------------------------------------
# Mode: weekly_reel
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Curated Video Set Metadata (Guarantees 100% video-to-caption-to-audio sync)
# ---------------------------------------------------------------------------
CURATED_SET_METADATA = {
    # ── 5 Authentic Human-Shot Video Clip Sets ────────────────────────────────
    "aston_martin_valour": {
        "title": "Aston Martin Valour • 705 HP Twin-Turbo V12 Manual 🇬🇧🔥",
        "make_model": "Aston Martin Valour",
        "color": "British Racing Green",
        "captions": [
            "Pure analog violence. The ultra-rare Aston Martin Valour commanding Monterey in British Racing Green. 705 horsepower of twin-turbo V12 fury mated strictly to a 6-speed manual gearbox. Rate this modern manual legend 1-10 👇",
            "A retro-modern titan. The Aston Martin Valour in British Racing Green with circular halo lights and a quad-exhaust soundtrack. Drop your dream spec below 🔑🔥",
            "110 units built worldwide. The Aston Martin Valour showcasing its aggressive front aero and retro ducktail in Monterey. Rate this clean rolling pass 1-10 👇"
        ],
        "tags": ["astonmartin", "astonmartinvalour", "valour", "v12", "britishracinggreen", "supercars", "hypercars", "manualgearbox", "montereycarweek", "exoticcars", "carlifestyle"],
        "audio": "lamborghini_v12_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "ferrari_f80": {
        "title": "Ferrari F80 • 1184 HP Hypercar Vanguard 🔴🇮🇹",
        "make_model": "Ferrari F80",
        "color": "Rosso Corsa",
        "captions": [
            "The dawn of Maranello's next hypercar era. The Ferrari F80 turning heads in iconic Rosso Corsa with 1,184 horsepower of hybrid twin-turbo engineering. Rate this hypercar 1-10 👇",
            "Maranello’s latest 1,184 hp hypercar flagship. The Ferrari F80 in Rosso Corsa cruising through Monterey. True automotive sculpture in motion. Drop your dream spec below 🔑🔥",
            "Rosso Corsa excellence in motion. 1,184 horsepower engineered for pure downforce and speed. The Ferrari F80 commanding Monterey's tarmac. Is this the ultimate modern hypercar? 👇"
        ],
        "tags": ["ferrari", "ferrarif80", "f80", "rossocorsa", "hypercars", "maranello", "supercars", "exoticcars", "carspotting", "carlifestyle", "carreels"],
        "audio": "ferrari_v8_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "lamborghini_diablo": {
        "title": "Lamborghini Diablo VT • Pure 90s V12 Heritage 🟡🇮🇹",
        "make_model": "Lamborghini Diablo VT",
        "color": "Giallo Fly Yellow",
        "captions": [
            "Pure analog supercar royalty. The 1990s Lamborghini Diablo VT along Monterey's coastal curves in iconic Giallo Yellow. 5.7 liters of naturally aspirated Italian V12 fury. Rate this 90s legend 1-10 👇",
            "Before turbos and hybrid batteries, this was the undisputed king of the road. The Lamborghini Diablo VT in Giallo Fly Yellow on 17-Mile Drive. Drop your dream 90s supercar below 🔑🇮🇹",
            "The quintessential wedge icon. Giallo Fly Yellow paint, pop-up lights, and 12 screaming cylinders echoing off the Pacific coast. Drop your favorite 90s poster car 👇"
        ],
        "tags": ["lamborghini", "diablo", "lamborghinidiablo", "v12", "retrosupercar", "90ssupercars", "montereycarweek", "exoticcars", "supercars", "carlifestyle", "vintageexotics"],
        "audio": "lamborghini_v12_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "red_sportscar": {
        "title": "Underground Velocity • High-Performance Roadster 🔴🔥",
        "make_model": "High-Performance Roadster",
        "color": "Crimson Red",
        "captions": [
            "Underground sanctuary. Crimson red lines and deep concave forged wheels in the subterranean bunker. Drop your dream spec below 👇",
            "Low-slung aggression waiting in the shadows. Crimson paint, carbon aero, and sculpted widebody contours. Rate this clean spec 1-10 🔑🔥",
            "Pure automotive aesthetics. Resting in the subterranean concrete vault before tearing up the midnight tarmac. Rate this clean build 1-10 👇"
        ],
        "tags": ["supercars", "sportscar", "carsofinstagram", "exoticcars", "carlifestyle", "carreels", "speedhunters", "cleanbuild", "forgedwheels"],
        "audio": "ferrari_v8_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "white_mustang_sunset": {
        "title": "Sunset Velocity • 5.0L V8 American Muscle 🌅🇺🇸",
        "make_model": "Ford Mustang GT 5.0",
        "color": "Oxford White",
        "captions": [
            "Chasing the golden hour. The Oxford White 5.0L V8 tearing down the coastal highway as the sun dips below the horizon. Sound up for that naturally aspirated American V8 roar! 🔊 Drop your dream muscle spec below 👇",
            "Golden rays and American muscle. Oxford White paint gleaming against the sunset glow with aggressive triple LED headlights. Would you take this on a cross-country sprint? 🔑🔥",
            "There's nothing quite like a naturally aspirated V8 singing into the sunset. Triple DRL headlights cutting through the dusk. Rate this clean rolling pass 1-10 👇"
        ],
        "tags": ["mustang", "mustanggt", "americanmuscle", "v8", "goldenhour", "carreels", "speedhunters", "carlifestyle", "sunsetdrive", "musclecars"],
        "audio": "car_v8_start.mp3",
        "is_bike": False,
        "is_fast_motion": True
    },

    # ── 8 Authentic DSLR Multi-Angle Photography Sets ─────────────────────────
    "audi_r8_v10": {
        "title": "Audi R8 V10 Performance • Naturally Aspirated Perfection ⚪🇩🇪",
        "make_model": "Audi R8 V10 Performance",
        "color": "Suzuka Grey Metallic",
        "captions": [
            "The twilight of naturally aspirated perfection. 5.2 liters of mid-engine German engineering screaming up to 8,700 RPM. Rate this Suzuka Grey R8 spec 1-10 👇",
            "Quattro grip and pure naturally aspirated V10 symphony. The Audi R8 V10 Performance standing tall with carbon sideblades and forged wheels. Drop your dream everyday supercar below 🔑🔥",
            "A timeless silhouette that will never be forgotten. Pure gated-era soul evolved into modern precision. Rate this clean R8 build 1-10 👇"
        ],
        "tags": ["audir8", "r8v10", "v10", "audi", "quattro", "supercars", "carlifestyle", "germanmuscle", "exoticcars"],
        "audio": "car_v8_start.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "bmw_m4_black": {
        "title": "BMW M4 Competition G82 • Stealth Carbon Aggression ⚫🇩🇪",
        "make_model": "BMW M4 Competition (G82)",
        "color": "Black Sapphire Metallic",
        "captions": [
            "Stealth mode engaged. The G82 BMW M4 Competition in Black Sapphire with exposed carbon fiber aero splitters and deep-dish forged wheels. Rate this dark spec 1-10 👇",
            "Twin-turbo S58 inline-six pushing unrelenting Bavarian power. The aggressive contours of the G82 M4 cutting through the night. Drop your favorite M car below 🔑🔥",
            "Blacked-out presence from every single angle. Laser headlights and a low-slung stance built strictly for high-speed corners. Rate this aggressive M4 spec 1-10 👇"
        ],
        "tags": ["bmwm4", "m4competition", "g82", "bmwm", "mpower", "bimmer", "carsofinstagram", "cleanbuild", "supercars"],
        "audio": "bmw_m4_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "bmw_s1000rr": {
        "title": "BMW S1000RR M Package • 205 HP Hypersport Superbike ⚪🏍️",
        "make_model": "BMW S1000RR M Package",
        "color": "Alpine White / M Motorsport Livery",
        "captions": [
            "205 horsepower. 14,500 RPM. The BMW S1000RR M Package in iconic tri-color motorsport livery. The undisputed king of the mountain switchbacks. Drop your dream two-wheel missile below 🔑🏍️",
            "Carbon aerodynamic winglets, ShiftCam inline-four power, and track-honed precision. The S1000RR commands absolute respect on the tarmac. Rate this superbike 1-10 👇",
            "Zero to sixty in under three seconds. The BMW S1000RR M Package carving through morning apexes. Turn the sound up and drop your dream bike below 👇"
        ],
        "tags": ["bmws1000rr", "s1000rr", "bmwmotorrad", "superbike", "bikelife", "hypersport", "motorcyclesofinstagram", "twowheels", "sportbike"],
        "audio": "bmw_s1000rr_rev.mp3",
        "is_bike": True,
        "is_fast_motion": False
    },
    "ducati_panigale_v4s": {
        "title": "Ducati Panigale V4 S • Pure Borgo Panigale Italian Fury 🔴🏍️",
        "make_model": "Ducati Panigale V4 S",
        "color": "Ducati Red",
        "captions": [
            "Desmosedici Stradale V4 engineering born directly from MotoGP. The Ducati Panigale V4 S in iconic Ducati Red with biplane aerodynamic wings. Rate this Italian masterpiece 1-10 👇",
            "Pure rolling sculpture. 214 horsepower singing through an Akrapovic titanium exhaust at 13,000 RPM. Is this the most beautiful superbike ever built? Drop your thoughts below 🔑🇮🇹",
            "Italian passion on two wheels. Brembo Stylema calipers, Ohlins active suspension, and the ferocious sound of a MotoGP-derived V4. Rate this red rocket 1-10 👇"
        ],
        "tags": ["ducatipanigale", "panigalev4s", "ducati", "v4s", "superbike", "bikelife", "italianmotorcycles", "sportbike", "twowheels"],
        "audio": "ducati_panigale_rev.mp3",
        "is_bike": True,
        "is_fast_motion": False
    },
    "ferrari_sf90": {
        "title": "Ferrari SF90 Stradale • 1000 HP Maranello Hybrid Flagship 🔴🇮🇹",
        "make_model": "Ferrari SF90 Stradale",
        "color": "Rosso Corsa / Nero Roof",
        "captions": [
            "1,000 metric horsepower from Maranello. A twin-turbo V8 paired with three electric motors catapulting from 0 to 60 in 2.0 seconds flat. Rate this SF90 Stradale spec 1-10 👇",
            "The pinnacle of contemporary Prancing Horse performance. Rosso Corsa coachwork with carbon fiber rear diffuser and active aerodynamic shut-off Gurney. Drop your dream spec below 🔑🔥",
            "Electric whisper in the city, savage twin-turbo V8 roar on the open tarmac. The Ferrari SF90 Stradale rewriting the laws of physics. Rate this spec 1-10 👇"
        ],
        "tags": ["ferrarisf90", "sf90stradale", "ferrari", "maranello", "hypercars", "hybridhypercar", "supercars", "exoticcars", "carlifestyle"],
        "audio": "ferrari_v8_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "lamborghini_revuelto": {
        "title": "Lamborghini Revuelto • 1001 HP V12 Hybrid Flagship 🟠🇮🇹",
        "make_model": "Lamborghini Revuelto",
        "color": "Arancio Apodis",
        "captions": [
            "Sant'Agata's first High Performance Electrified Vehicle. A naturally aspirated 6.5L V12 screaming to 9,500 RPM combined with three electric motors for 1,001 horsepower. Rate this Arancio Apodis spec 1-10 👇",
            "The Y-shaped LED signature slicing through the dusk. The Lamborghini Revuelto carrying sixty years of V12 heritage into the electrified future. Drop your dream bull below 🔑🐂",
            "Exposed titanium dual high-exit exhausts and aeronautical monocoque architecture. The Lamborghini Revuelto is pure vehicular theatre. Rate this hypercar 1-10 👇"
        ],
        "tags": ["lamborghinirevuelto", "revuelto", "lamborghini", "v12hybrid", "hypercars", "santagatabolognese", "supercars", "carlifestyle"],
        "audio": "lamborghini_v12_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "nissan_gtr_r35": {
        "title": "Nissan GT-R Nismo • Godzilla Twin-Turbo AWD Precision ⚪🇯🇵",
        "make_model": "Nissan GT-R Nismo (R35)",
        "color": "Stealth Grey / Carbon Aero",
        "captions": [
            "Godzilla unleashed. 600 horsepower of hand-crafted twin-turbo VR38DETT fury mated to the world's most sophisticated ATTESA E-TS all-wheel drive. Rate this clean GT-R Nismo 1-10 👇",
            "From the legendary Nurburgring to the midnight highway. Carbon fiber roof, vented fenders, and iconic circular quad taillights. Drop your dream Japanese performance car below 🔑🇯🇵",
            "Relentless downforce and analog precision. The Nissan GT-R Nismo standing as a permanent icon of modern performance engineering. Rate this spec 1-10 👇"
        ],
        "tags": ["nissangtr", "gtrr35", "nismo", "godzilla", "jdm", "twinturbo", "supercars", "trackweapon", "carreels"],
        "audio": "car_rev_driveby.mp3",
        "is_bike": False,
        "is_fast_motion": False
    },
    "porsche_911_gt3rs": {
        "title": "Porsche 911 GT3 RS (992) • Weissach Aero Perfection 🔵🇩🇪",
        "make_model": "Porsche 911 GT3 RS (992)",
        "color": "Shark Blue / Weissach Package",
        "captions": [
            "Motorsport DNA for the road. The 992 GT3 RS in Shark Blue with full exposed carbon Weissach Package and active swan-neck DRS wing. Rate this track weapon 1-10 👇",
            "4.0 liters of naturally aspirated flat-six perfection singing all the way to 9,000 RPM. No turbos, no filters, pure mechanical bliss. Drop your dream 911 spec below 🔑🔥",
            "860 kilograms of downforce at speed. Central radiator concept and race-derived double-wishbone front axle. The Porsche 911 GT3 RS is untouchable. Rate this spec 1-10 👇"
        ],
        "tags": ["porsche911gt3rs", "gt3rs", "992gt3rs", "porsche", "weissachpackage", "tracktool", "flatsix", "supercars", "carlifestyle"],
        "audio": "porsche_gt3rs_rev.mp3",
        "is_bike": False,
        "is_fast_motion": False
    }
}


def mode_weekly_reel(persona: dict, env: dict, dry_run: bool, custom_prompt: str = "") -> None:
    """Generate and publish automotive reels:
    - Normal reels (default): 100% CLEAN footage with ZERO text overlays on screen.
    - Comparison reels: Split screen (image on top half, specs points on bottom half) comparing cars one-by-one.
    - Deep Dive: Split screen explaining a part deeply.
    - Future Hypercars: Split screen breaking down 2026 upcoming releases.
    - Study Journey: 12 AM IST daily progress reel with study overlay.
    """
    print("\n=== MODE: reel ===")

    event_name = os.environ.get("GITHUB_EVENT_NAME", "").lower()
    is_manual = bool(custom_prompt) or (event_name == "workflow_dispatch")
    if not is_manual:
        # AGENT_SPEC §2 & §3: Slot idempotency (prevent duplicate posts in same 2-hour UTC slot)
        if REGISTRY_AVAILABLE:
            slot_hour = (datetime.now(timezone.utc).hour // 2) * 2
            if is_slot_occupied(slot_hour):
                print(f"\n[IDEMPOTENCY] Slot {slot_hour:02d}:00–{(slot_hour+2)%24:02d}:00 UTC already has a confirmed post. Skipping.")
                return

        last_time = get_last_reel_timestamp()
        if last_time:
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds() / 60.0
            if elapsed < 90:
                print(f"\n[SKIP SCHEDULED POST] A reel was already posted {elapsed:.1f} minutes ago.")
                print("Minimum interval between automated reels is 90 minutes. Skipping to prevent duplicate/spamming.")
                return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Load available vehicle assets
    available_dict: dict[str, list[Path]] = {}
    if VEHICLE_VIDEO_SETS_DIR.exists():
        for d in sorted(VEHICLE_VIDEO_SETS_DIR.iterdir()):
            if d.is_dir():
                clips = sorted(list(d.glob("clip_*.mp4")))
                if len(clips) >= 3:
                    available_dict[d.name] = clips
    if VEHICLE_SETS_DIR.exists():
        for d in sorted(VEHICLE_SETS_DIR.iterdir()):
            if d.is_dir():
                shots = sorted(list(d.glob("shot_*.jpg")))
                if len(shots) == 5:
                    available_dict[d.name] = shots

    prompt_lower = custom_prompt.lower().strip()
    active_format = ""
    sub_idx = 0
    study_overlay = ""
    is_static_slides = False
    temp_slide_files: list[Path] = []

    # Determine format: explicit user prompt OR automatic mixed pattern scheduler
    if custom_prompt and ("day" in prompt_lower or "ug studies" in prompt_lower):
        active_format = "journey_reel"
    elif custom_prompt and (
        prompt_lower.startswith("comparison") or
        "compare" in prompt_lower or
        " vs " in prompt_lower or
        "battle" in prompt_lower
    ):
        active_format = "comparison"
    elif custom_prompt and (
        prompt_lower.startswith("deep_dive") or
        "deep dive" in prompt_lower or
        "part of car" in prompt_lower or
        "explain" in prompt_lower
    ):
        active_format = "deep_dive"
    elif custom_prompt and (
        prompt_lower.startswith("future_release") or
        "future" in prompt_lower or
        "2026" in prompt_lower or
        "concept" in prompt_lower or
        "release" in prompt_lower
    ):
        active_format = "future_release"
    elif custom_prompt and (
        prompt_lower.startswith("pure_cinematic") or
        "cinematic" in prompt_lower or
        "clean" in prompt_lower
    ):
        active_format = "pure_cinematic"
    else:
        # AUTOMATED MIXUP PATTERN SCHEDULER:
        # When running on schedule or without explicit format override,
        # automatically rotate formats so every reel is completely distinct!
        active_format, sub_idx = get_next_reel_pattern()
        print(f"  [Mixup Pattern Engine] Automated mixed format selected: '{active_format}' (sub_idx={sub_idx})")

    # 1. Study Journey Reel (Only at 12 AM IST or explicit Day X trigger)
    if active_format == "journey_reel":
        active_format = "journey_reel"
        day_match = re.search(r'Day\s+(\d+)', custom_prompt, re.IGNORECASE)
        day_num = day_match.group(1) if day_match else "1"
        study_overlay = (
            f"Day {day_num} of seeking help to complete my ug studies\n\n"
            "I am an undergraduate student working hard to finish my education and graduate.\n"
            "If you have guidance, mentorship, or support to share, every bit helps me keep moving forward."
        )
        caption_text = (
            f"Day {day_num} of seeking help to complete my ug studies.\n\n"
            "I am an undergraduate student working hard to finish my education and graduate. "
            "If you have guidance, mentorship, or support to share, every bit helps me keep moving forward. 📚🎓\n\n"
            "Thank you for being part of this journey."
        )
        hashtags = ["ugstudies", "collegejourney", "studentlife", "graduationgoals", "educationmatters", "nevergiveup", "dailyjourney"]
        set_name, set_shots, is_bike = get_next_curated_vehicle_set()
        title = f"Day {day_num} • Seeking Help To Complete UG Studies"

    # 2. Comparison Battle (Image on Top Half, Points on Bottom Half, One-by-One Car)
    elif active_format == "comparison":
        battle = None
        for b in COMPARISON_BATTLES:
            if b["id"] in prompt_lower:
                battle = b
                break
        if not battle:
            battle = COMPARISON_BATTLES[sub_idx % len(COMPARISON_BATTLES)]
        set_name = battle["id"]
        title = battle["title"]
        hashtags = battle["tags"]
        is_bike = any("ducati" in c or "s1000rr" in c for c in battle["cars"])
        is_static_slides = True

        car_keys = battle["cars"]
        car_names = [CAR_SPECS_DATABASE.get(k, {}).get("name", k.replace("_", " ").title()) for k in car_keys]

        # Generate split-screen slide for each car one by one
        for idx, k in enumerate(car_keys):
            spec = CAR_SPECS_DATABASE.get(k, {
                "name": k.replace("_", " ").title(),
                "points": [("🐎 Power", "High Performance"), ("⚡ Engine", "V8 / V10 / V12")]
            })
            car_assets = available_dict.get(k, [])
            car_img = car_assets[0] if car_assets else get_next_curated_vehicle_image()
            slide_path = MEDIA_DIR / f"slide_{ts}_{idx+1}.jpg"
            make_split_comparison_slide(car_img, idx + 1, len(car_keys), spec["name"], spec["points"], slide_path)
            temp_slide_files.append(slide_path)

        # Generate the showdown outro slide
        first_assets = available_dict.get(car_keys[0], [])
        outro_img = first_assets[-1] if first_assets else get_next_curated_vehicle_image()
        outro_slide_path = MEDIA_DIR / f"slide_{ts}_outro.jpg"
        make_split_showdown_slide(outro_img, car_names, outro_slide_path)
        temp_slide_files.append(outro_slide_path)

        set_shots = temp_slide_files

        # Caption
        prompt = (
            f"Write a debate-sparking Instagram Reel caption comparing these 3 cars: {', '.join(car_names)}.\n"
            f"Compare their power, engine symphony, and driving feel in 2-3 sentences.\n"
            f"End with an interactive voting question: 'Which one are you taking? Vote 1, 2, or 3 below! 👇'.\n"
            f"Do not include hashtags in the caption body."
        )
        try:
            ai_caption = call_gemini(prompt, env).strip()
            caption_text = ai_caption if len(ai_caption) > 20 else random.choice(battle["captions"])
        except Exception:
            caption_text = random.choice(battle["captions"])

    # 3. Deep Dive (Part of Car Explained Deeply in Caption, 100% CLEAN FULL-SCREEN Normal Reel)
    # Split screen is ONLY for multi-car battles! Single cars are always 100% clean full-screen reels.
    elif active_format == "deep_dive":
        deep_keys = list(DEEP_DIVE_TOPICS.keys())
        matched_key = None
        for k in deep_keys:
            if k in prompt_lower or k.replace("_", " ") in prompt_lower:
                matched_key = k
                break
        if not matched_key:
            matched_key = deep_keys[sub_idx % len(deep_keys)]
        topic = DEEP_DIVE_TOPICS.get(matched_key, DEEP_DIVE_TOPICS[deep_keys[0]])
        set_name = matched_key
        title = topic["title"]
        hashtags = topic["tags"]
        is_bike = "ducati" in set_name or "bike" in set_name
        is_static_slides = False  # 100% CLEAN FULL SCREEN! ZERO split-screen for a single car.

        car_assets = available_dict.get(set_name, [])
        if car_assets:
            set_shots = car_assets
        else:
            set_shots = [get_next_curated_vehicle_image()] * 5

        points_summary = "\n".join([f"• {label}: {val}" for label, val in topic.get("points", [])[:5]])
        prompt = (
            f"You are the automotive engineering director of Zenith Motors.\n"
            f"Write an in-depth, viral Instagram Reel & YouTube Shorts caption breaking down this engineering marvel:\n"
            f"Machine: {topic['title']}\n"
            f"Component in focus: {topic['component']}\n"
            f"Key Specs:\n{points_summary}\n\n"
            f"Requirements:\n"
            f"- 1 killer opening hook line explaining why this tech is revolutionary\n"
            f"- 2-3 sentences explaining exactly how the {topic['component']} performs on track\n"
            f"- End with an interactive question for enthusiasts (e.g. 'Drop your thoughts below 👇')\n"
            f"- Do NOT include hashtags in the caption body."
        )
        try:
            ai_caption = call_gemini(prompt, env).strip()
            caption_text = ai_caption if len(ai_caption) > 20 else random.choice(topic["captions"])
        except Exception:
            caption_text = random.choice(topic["captions"])

    # 4. Future Release Cars (Split Screen Layout)
    elif active_format == "future_release":
        active_format = "future_release"
        set_name = "future_hypercars_2026"
        topic = FUTURE_HYPERCARS_TOPIC
        title = topic["title"]
        hashtags = topic["tags"]
        is_bike = False
        is_static_slides = True

        car_names = [c[1] for c in topic["cars"]]
        for idx, (source_key, c_name, c_points) in enumerate(topic["cars"]):
            car_assets = available_dict.get(source_key, [])
            car_img = car_assets[0] if car_assets else get_next_curated_vehicle_image()
            slide_path = MEDIA_DIR / f"slide_future_{ts}_{idx+1}.jpg"
            make_split_comparison_slide(car_img, idx + 1, len(topic["cars"]), c_name, c_points, slide_path)
            temp_slide_files.append(slide_path)

        # Outro slide
        first_assets = available_dict.get(topic["cars"][0][0], [])
        outro_img = first_assets[-1] if first_assets else get_next_curated_vehicle_image()
        outro_slide_path = MEDIA_DIR / f"slide_future_{ts}_outro.jpg"
        make_split_showdown_slide(outro_img, car_names, outro_slide_path)
        temp_slide_files.append(outro_slide_path)

        set_shots = temp_slide_files
        try:
            prompt = (
                f"Write a viral Instagram Reel caption about upcoming 2026 hypercars ({', '.join(car_names)}).\n"
                f"Highlight the astronomical horsepower and engines in 2-3 sentences.\n"
                f"End with a question asking which future hypercar they would choose. No hashtags in caption body."
            )
            ai_caption = call_gemini(prompt, env).strip()
            caption_text = ai_caption if len(ai_caption) > 20 else random.choice(topic["captions"])
        except Exception:
            caption_text = random.choice(topic["captions"])

    # 5. NORMAL REELS (Default for all automated scheduled runs and standard curation)
    # 100% CLEAN: ZERO text overlay on screen! Pure raw cinematic footage.
    else:
        active_format = "pure_cinematic"
        hint = custom_prompt if custom_prompt else ""
        set_name, set_shots, is_bike = get_next_curated_vehicle_set(hint)
        meta = CURATED_SET_METADATA.get(set_name, {})
        title = meta.get("title", f"{set_name.replace('_', ' ').title()} Supercar Reel")
        hashtags = meta.get("tags") or ["supercars", "hypercars", "carreels", "carlifestyle", "speedhunters", "exoticcars"]
        is_bike = meta.get("is_bike", False)

        make_model = meta.get("make_model", set_name.replace("_", " ").title())
        color = meta.get("color", "")
        theme_desc = f"Machine: {make_model} in {color}"
        prompt = (
            f"You are the creative director of Zenith Motors.\n"
            f"Write an original, punchy, high-energy Instagram caption for a viral reel.\n"
            f"{theme_desc}\n"
            f"Requirements:\n"
            f"- 1 killer opening hook line\n"
            f"- 2 short sentences capturing the raw performance, exhaust symphony, or aerodynamic styling\n"
            f"- End with an engagement call-to-action (e.g. 'Rate this spec 1-10 👇')\n"
            f"- Do NOT include hashtags in the caption body."
        )
        try:
            ai_caption = call_gemini(prompt, env).strip()
            caption_text = ai_caption if len(ai_caption) > 20 else (random.choice(meta["captions"]) if "captions" in meta else f"The {set_name.replace('_', ' ').title()} in its purest element. Rate this 1-10 👇")
        except Exception:
            caption_text = random.choice(meta["captions"]) if meta and "captions" in meta else f"The {set_name.replace('_', ' ').title()} in its purest element. Rate this 1-10 👇"

    full_caption = build_full_caption(caption_text, hashtags)

    res_id = None
    if REGISTRY_AVAILABLE and not dry_run:
        res_id = reserve_slot(set_name, active_format, full_caption)

    print(f"\n[FORMAT]:    {active_format.upper()}")
    print(f"Vehicle Set: {set_name}")
    print(f"Title:       {title}")
    print(f"Caption:     {caption_text[:100]}...")
    if is_static_slides:
        print(f"Slides:      {len(set_shots)} split-screen slides (image on top, points on bottom)")
    else:
        print("Aesthetic:   100% CLEAN normal reel (ZERO text overlay on screen)")

    video_path = MEDIA_DIR / f"reel_{ts}.mp4"

    if dry_run:
        print(f"\n[DRY RUN] Would assemble {len(set_shots)} items for format '{active_format}' → {video_path}")
        print(f"[DRY RUN] Vehicle focus: {title} (is_bike={is_bike}, is_static_slides={is_static_slides})")
        print("[DRY RUN] Would commit and publish reel")
        return

    is_video_mode = set_shots[0].suffix.lower() == ".mp4"
    meta = CURATED_SET_METADATA.get(set_name, {})
    is_fast_motion = meta.get("is_fast_motion", False)

    if is_static_slides:
        # Split-screen comparison or deep dive slides
        build_reel_video(
            set_shots,
            video_path,
            theme_hint=set_name,
            is_bike=is_bike,
            text_overlay="",
            is_fast_motion=False,
            is_static_slides=True
        )
        files_to_commit = list(set_shots) + [video_path]
    elif is_video_mode:
        # Normal video compilation: 100% clean video clips, zero overlay
        clips_to_use = list(set_shots)
        if len(clips_to_use) >= 3 and not study_overlay:
            shift = (datetime.now(timezone.utc).minute) % len(clips_to_use)
            clips_to_use = clips_to_use[shift:] + clips_to_use[:shift]

        print(f"\nAssembling {len(clips_to_use)} clean video clips for {title}...")
        for i, clip in enumerate(clips_to_use):
            print(f"  Angle {i + 1}: {clip.name} ({clip.stat().st_size // 1024} KB)")
        build_reel_video(
            clips_to_use,
            video_path,
            theme_hint=set_name,
            is_bike=is_bike,
            text_overlay=study_overlay,
            is_fast_motion=is_fast_motion,
            is_static_slides=False
        )
        files_to_commit = [video_path]
    else:
        # Normal DSLR photography cuts: 100% clean photos, zero overlay
        print(f"\nAssembling {len(set_shots)} clean DSLR photography cuts for {title}...")
        import shutil
        image_paths: list[Path] = []
        for i, source_photo in enumerate(set_shots):
            img_path = MEDIA_DIR / f"reel_{ts}_scene{i + 1}.jpg"
            shutil.copyfile(source_photo, img_path)
            if study_overlay:
                apply_text_overlay(img_path, study_overlay)
            image_paths.append(img_path)

        build_reel_video(
            image_paths,
            video_path,
            theme_hint=set_name,
            is_bike=is_bike,
            text_overlay=study_overlay,
            is_fast_motion=is_fast_motion,
            is_static_slides=False
        )
        files_to_commit = image_paths + [video_path]

    # Save state files
    if VEHICLE_STATE_FILE.exists() and VEHICLE_STATE_FILE not in files_to_commit:
        files_to_commit.append(VEHICLE_STATE_FILE)
    if REEL_PATTERN_FILE.exists() and REEL_PATTERN_FILE not in files_to_commit:
        files_to_commit.append(REEL_PATTERN_FILE)
    git_commit_and_push(files_to_commit, f"chore: add {active_format} reel {ts}")

    print("Waiting for raw URL...")
    raw_url = wait_for_raw_url(env["gh_repo"], env["gh_branch"], video_path)

    print("Publishing reel to Instagram...")
    media_id = publish_reel(raw_url, full_caption, env)
    print(f"Published to Instagram! media_id={media_id}")

    # Cross-post to YouTube Shorts
    yt_id = None
    yt_err_msg = ""
    try:
        yt_id, yt_err_msg = publish_youtube_short(video_path, title, full_caption, hashtags, env)
    except Exception as yt_err:
        yt_err_msg = str(yt_err)
        print(f"  [YouTube] Notice: YouTube upload bypassed ({yt_err})")

    yt_detail = f", yt_id={yt_id}" if yt_id else (f", yt_err={yt_err_msg}" if yt_err_msg else "")
    log_ok("weekly_reel", full_caption, video_path.name, f"set={set_name}, format={active_format}, media_id={media_id}{yt_detail}")

    if REGISTRY_AVAILABLE:
        confirm_publish(
            set_name=set_name,
            format_name=active_format,
            caption=full_caption,
            media_id=media_id,
            yt_id=yt_id,
            media_file=video_path.name,
            reservation_id=res_id
        )
    _write_daily_summary(env)

    files_to_log = [LOG_FILE]
    if REGISTRY_FILE.exists():
        files_to_log.append(REGISTRY_FILE)
    summary_file = ROOT / "logs" / "daily_summary.md"
    if summary_file.exists():
        files_to_log.append(summary_file)
    if ARCH_INDEX_FILE.exists():
        files_to_log.append(ARCH_INDEX_FILE)
    if VEHICLE_STATE_FILE.exists():
        files_to_log.append(VEHICLE_STATE_FILE)
    if REEL_PATTERN_FILE.exists():
        files_to_log.append(REEL_PATTERN_FILE)
    if JOURNEY_STATE_FILE.exists():
        files_to_log.append(JOURNEY_STATE_FILE)
    git_commit_and_push(files_to_log, f"chore: log {active_format} reel {ts}")


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
            print(f"  Published to Instagram! media_id={media_id}")
            yt_id = None
            yt_err_msg = ""
            try:
                yt_id, yt_err_msg = publish_youtube_short(dest_video, title or "Supercar Reel", full_caption, hashtags or ["Shorts"], env)
            except Exception as yt_err:
                yt_err_msg = str(yt_err)
                print(f"  [YouTube] Notice: YouTube upload bypassed ({yt_err})")
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

# ---------------------------------------------------------------------------
# Mode: collect_metrics — Daily Instagram Reel Insights Collection (AGENT_SPEC §6)
# ---------------------------------------------------------------------------

def mode_collect_metrics(persona: dict, env: dict, dry_run: bool) -> None:
    """Pull per-Reel views, likes, shares, saves from Instagram API and store in registry."""
    print("\n=== MODE: collect_metrics ===")
    if not REGISTRY_AVAILABLE:
        print("  Registry not available. Skipping metrics collection.")
        return

    from scripts.registry import _load, update_metrics

    data = _load()
    posts_to_update = [
        p for p in data.get("posts", [])
        if p.get("status") == "published"
        and p.get("media_id")
        and p.get("metrics", {}).get("views", 0) == 0
    ][:20]

    if not posts_to_update:
        print("  No posts needing metrics update.")
        _write_daily_summary(env)
        return

    print(f"  Collecting metrics for {len(posts_to_update)} posts...")
    updated = 0
    for post in posts_to_update:
        media_id = post["media_id"]
        try:
            resp = ig_get(
                f"{media_id}/insights",
                env,
                params={"metric": "impressions,reach,likes_count,shares,saved"}
            )
            metrics_data = {item["name"]: item["values"][0]["value"] for item in resp.get("data", [])}
            views = metrics_data.get("impressions", metrics_data.get("reach", 0))
            likes = metrics_data.get("likes_count", 0)
            shares = metrics_data.get("shares", 0)
            saves = metrics_data.get("saved", 0)
            update_metrics(media_id, views, likes, shares, saves)
            print(f"  Updated {media_id}: views={views}, likes={likes}, shares={shares}, saves={saves}")
            updated += 1
        except Exception as exc:
            print(f"  Notice: metrics for {media_id} skipped ({exc})")
        time.sleep(1)

    _write_daily_summary(env)
    print(f"  Metrics collection complete. Updated {updated} posts.")
    if not dry_run:
        git_commit_and_push([REGISTRY_FILE, ROOT / "logs" / "daily_summary.md"], "chore: update reel metrics and daily summary")


# ---------------------------------------------------------------------------
# Mode: youtube_top10 — Daily Compilation Short (AGENT_SPEC §2, §6)
# ---------------------------------------------------------------------------

def mode_youtube_top10(persona: dict, env: dict, dry_run: bool) -> None:
    """Build a Top 10 compilation from yesterday's best reels and upload to YouTube (under 3 mins)."""
    print("\n=== MODE: youtube_top10 ===")
    if not REGISTRY_AVAILABLE:
        print("  Registry not available. Skipping Top 10.")
        return

    top_posts = get_top_posts(day_offset=1, limit=10, min_posts=5)
    if not top_posts:
        print("  Fewer than 5 qualifying posts found for a Top 10. Skipping today.")
        _write_daily_summary(env)
        return

    n = len(top_posts)
    title = f"Top {n} Supercars & Superbikes of the Day 🔥 #Shorts"
    print(f"  Building '{title}' from {n} clips...")

    video_paths: list[Path] = []
    for post in top_posts:
        media_name = post.get("media_file", "")
        if media_name:
            path = MEDIA_DIR / media_name
            if path.exists() and path.suffix.lower() == ".mp4":
                video_paths.append(path)

    # Fallback to authentic video sets if rendered files were cleaned up
    if len(video_paths) < 5:
        available_clips = list(VEHICLE_VIDEO_SETS_DIR.glob("*/*.mp4"))
        if available_clips:
            video_paths = available_clips[:n]

    if len(video_paths) < 5:
        print(f"  Only {len(video_paths)} video sources found (need at least 5). Skipping Top 10.")
        return

    if dry_run:
        print(f"  [DRY RUN] Would build Top {n} Short ({len(video_paths)} clips, ~150s) → top10_compilation.mp4")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = MEDIA_DIR / f"top10_{ts}.mp4"

    target_per_clip = min(15, max(8, 150 // len(video_paths)))
    trimmed_clips: list[Path] = []
    for i, vp in enumerate(video_paths):
        trimmed = MEDIA_DIR / f"trim_{ts}_{i}.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(vp),
            "-t", str(target_per_clip),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            str(trimmed)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if trimmed.exists():
            trimmed_clips.append(trimmed)

    if not trimmed_clips:
        print("  Failed to trim clips. Skipping Top 10.")
        return

    concat_list = MEDIA_DIR / f"concat_{ts}.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for tc in trimmed_clips:
            f.write(f"file '{tc.resolve()}'\n")

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        str(output_path)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for tc in trimmed_clips:
        tc.unlink(missing_ok=True)
    concat_list.unlink(missing_ok=True)

    if not output_path.exists():
        print("  Failed to concatenate Top 10 video. Skipping.")
        return

    desc_lines = [
        f"Top {n} Supercars & Superbikes of the Day ranked by performance!",
        "",
    ]
    for i, p in enumerate(top_posts[:len(trimmed_clips)], 1):
        car_title = p.get("set_name", "").replace("_", " ").title()
        desc_lines.append(f"{i}. {car_title}")
    desc_lines.append("\nFollow on Instagram @apex_rich_ for 2-hour daily reels!")
    description = "\n".join(desc_lines)
    tags = ["supercars", "superbike", "carreels", "shorts", "top10", "speedhunters", "carlifestyle"]

    print("  Uploading Top 10 Short to YouTube...")
    yt_id, yt_err = publish_youtube_short(output_path, title, description, tags, env)
    if yt_id:
        print(f"  Successfully published YouTube Top 10 Short! ID: {yt_id}")
    else:
        print(f"  YouTube upload skipped/notice: {yt_err}")

    output_path.unlink(missing_ok=True)
    _write_daily_summary(env)


# ---------------------------------------------------------------------------
# Daily Summary Writer (AGENT_SPEC §8)
# ---------------------------------------------------------------------------

def _write_daily_summary(env: dict) -> None:
    """Write logs/daily_summary.md with today's posting activity."""
    if not REGISTRY_AVAILABLE:
        return
    from scripts.registry import _load
    data = _load()
    today = datetime.now(timezone.utc).date()
    today_posts = [
        p for p in data.get("posts", [])
        if p.get("status") == "published"
        and datetime.fromisoformat(p["publish_timestamp"]).date() == today
    ]

    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = logs_dir / "daily_summary.md"

    lines = [
        f"# Daily Summary — {today.isoformat()}",
        "",
        f"- **Posts published today:** {len(today_posts)}",
        f"- **Status:** Operational (12 Reels/Day schedule)",
        "",
        "## Today's Published Reels",
        "| Time (UTC) | Vehicle Set | Format | IG Media ID | YouTube Short ID |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    if today_posts:
        for p in today_posts:
            t = datetime.fromisoformat(p["publish_timestamp"]).strftime("%H:%M")
            lines.append(f"| {t} | {p.get('set_name', '?')} | {p.get('format', '?')} | {p.get('media_id', '?')} | {p.get('yt_id') or '—'} |")
    else:
        lines.append("| — | No posts yet today | — | — | — |")

    lines.append("")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Main entry point
# ---------------------------------------------------------------------------

MODES = {"post", "weekly_reel", "reel", "upload_reel", "journey_reel", "deep_dive", "comparison", "future_release", "youtube_top10", "collect_metrics"}


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    custom_prompt = ""
    # Parse --prompt "some text"
    for i, a in enumerate(args):
        if a in ("--prompt", "-p") and i + 1 < len(args):
            custom_prompt = args[i + 1].strip()
            args = args[:i] + args[i + 2:]
            break

    # Also check CUSTOM_PROMPT environment variable
    if not custom_prompt:
        custom_prompt = os.environ.get("CUSTOM_PROMPT", "").strip()

    # Also check if custom_prompt.txt exists in root
    prompt_file = ROOT / "custom_prompt.txt"
    if not custom_prompt and prompt_file.exists():
        try:
            custom_prompt = prompt_file.read_text(encoding="utf-8").strip()
            prompt_file.unlink()  # Consume single-use file
            print(f"  Read and consumed custom prompt from {prompt_file.name}")
        except Exception:
            pass

    if not args or args[0] not in MODES:
        print(f"Usage: python main.py <mode> [--prompt <text>] [--dry-run]")
        print(f"Modes: {', '.join(sorted(MODES))}")
        sys.exit(1)

    mode = args[0]
    if custom_prompt:
        print(f"Custom prompt: '{custom_prompt}'")

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
            "gemini_key": _env("GEMINI_API_KEY", required=False),
            "gh_repo": os.environ.get("GITHUB_REPOSITORY", "owner/repo"),
            "gh_branch": os.environ.get("GITHUB_REF_NAME", "main"),
            "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
        }
    else:
        env = load_env()

    try:
        # Auto-prioritize Daily Study Journey Reel if it hasn't posted today
        # (Triggers anywhere in evening window >= 18:00 UTC / 11:30 PM IST)
        if mode in ("reel", "weekly_reel") and not custom_prompt:
            from datetime import timedelta
            now_utc = datetime.now(timezone.utc)
            today_local_str = (now_utc + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
            j_state = load_journey_state()
            if now_utc.hour >= 18 and j_state.get("last_post_date") != today_local_str:
                print(f"\n[AUTO-PRIORITY] 12 AM Midnight window detected (UTC hour {now_utc.hour}) and today's study reel ({today_local_str}) has not been posted yet.")
                print("Switching mode from 'reel' to 'journey_reel' to guarantee daily study post!")
                mode = "journey_reel"

        if mode == "post":
            mode_post(persona, env, dry_run, custom_prompt=custom_prompt)
        elif mode == "journey_reel":
            mode_journey_reel(persona, env, dry_run)
        elif mode in {"weekly_reel", "reel"}:
            mode_weekly_reel(persona, env, dry_run, custom_prompt=custom_prompt)
        elif mode == "deep_dive":
            mode_weekly_reel(persona, env, dry_run, custom_prompt="deep_dive " + custom_prompt)
        elif mode == "comparison":
            mode_weekly_reel(persona, env, dry_run, custom_prompt="comparison " + custom_prompt)
        elif mode == "future_release":
            mode_weekly_reel(persona, env, dry_run, custom_prompt="future_release " + custom_prompt)
        elif mode == "upload_reel":
            mode_upload_reel(persona, env, dry_run)
        elif mode == "youtube_top10":
            mode_youtube_top10(persona, env, dry_run)
        elif mode == "collect_metrics":
            mode_collect_metrics(persona, env, dry_run)
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
