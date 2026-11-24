"""
scripts/replenish_fleet.py
==========================
Automated Media Replenishment Engine for Zenith Motors.
Ensures that EVERY vehicle in the fleet always maintains at least TARGET_UNPOSTED (default 6)
brand-new, high-resolution unposted shots that strictly match the vehicle's locked color spec.

Guarantees:
  1. Strict Zero Repeat: Newly fetched shots receive new sequential filenames (shot_N.jpg)
     and are never in used_shots.json.
  2. Single-Color Spec Cohesion: Color Guard analyzes candidate shots before saving.
     Only shots matching the vehicle's color cluster are accepted.
  3. CC License Compliance: License metadata, photographer credit, and source URLs
     are automatically saved to data/licenses.json.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

SETS_DIR = ROOT / "assets" / "vehicles" / "sets"
LICENSES_FILE = ROOT / "data" / "licenses.json"
USAGE_FILE = ROOT / "data" / "used_shots.json"

HEADERS = {
    "User-Agent": "ZenithMotorsBot/1.3 (https://github.com/Divagar03/Erika; educational-automotive-showcase)"
}

ALLOWED_LICENSES = {
    "cc0", "cc by 2.0", "cc by 3.0", "cc by 4.0",
    "cc by-sa 2.0", "cc by-sa 3.0", "cc by-sa 4.0",
    "public domain", "pd",
}

SKIP_TITLE_TERMS = (
    "interior", "cockpit", "dashboard", "steering", "seat", "engine",
    "wheel", "rim", "badge", "emblem", "logo", "embleme", "key", "diagram",
    "sketch", "toy", "diecast", "hot wheels", "hotwheels", "lego", "model car",
    "scale model", "1:18", "1:24", "1:43", "1:64", "miniature", "brake",
    "caliper", "taillight detail", "headlight detail", "door handle", "plate",
    "wreck", "crash", "burned", "damaged", "salvage"
)

# Vehicle configurations with search queries and locked color specs
FLEET_CONFIGS = [
    {
        "id": "porsche_911_gt3rs",
        "display_name": "Porsche 911 GT3 RS",
        "queries": ["Porsche 911 GT3 RS 992 blue", "Porsche 992 GT3 RS blue", "Porsche 911 GT3 RS blue"],
        "expected_color_type": "COLORED",
        "target_hue": 210.0,     # Normalized blue cluster for Shark Blue
        "hue_tolerance": 50.0,
    },
    {
        "id": "bmw_m4_blue",
        "display_name": "BMW M4 Competition",
        "queries": ["BMW M4 G82 blue", "BMW M4 Competition blue", "BMW M4 Yas Marina Blue"],
        "expected_color_type": "COLORED",
        "target_hue": 210.0,
        "hue_tolerance": 50.0,
    },
    {
        "id": "audi_r8_v10",
        "display_name": "Audi R8 V10",
        "queries": ["Audi R8 V10 blue", "Audi R8 Kemora Blue", "Audi R8 4S blue"],
        "expected_color_type": "COLORED",
        "target_hue": 205.0,
        "hue_tolerance": 50.0,
    },
    {
        "id": "ferrari_f80",
        "display_name": "Ferrari F80",
        "queries": ["Ferrari F80", "Ferrari F80 concept red", "Ferrari hypercar red"],
        "expected_color_type": "COLORED",
        "target_hue": 355.0,    # Red
        "hue_tolerance": 40.0,
    },
    {
        "id": "ferrari_sf90",
        "display_name": "Ferrari SF90 Stradale",
        "queries": ["Ferrari SF90 Stradale blue", "Ferrari SF90 blue", "Ferrari SF90 Blu Elettrico"],
        "expected_color_type": "COLORED",
        "target_hue": 210.0,
        "hue_tolerance": 45.0,
    },
    {
        "id": "ferrari_laferrari",
        "display_name": "Ferrari LaFerrari",
        "queries": ["Ferrari LaFerrari red", "Ferrari LaFerrari Rosso Corsa"],
        "expected_color_type": "COLORED",
        "target_hue": 355.0,
        "hue_tolerance": 40.0,
    },
    {
        "id": "bugatti_chiron",
        "display_name": "Bugatti Chiron",
        "queries": ["Bugatti Chiron blue", "Bugatti Chiron Atlantic Blue"],
        "expected_color_type": "COLORED",
        "target_hue": 215.0,
        "hue_tolerance": 50.0,
    },
    {
        "id": "bugatti_veyron",
        "display_name": "Bugatti Veyron",
        "queries": ["Bugatti Veyron blue", "Bugatti Veyron 16.4 two-tone"],
        "expected_color_type": "COLORED",
        "target_hue": 215.0,
        "hue_tolerance": 50.0,
    },
    {
        "id": "lamborghini_diablo",
        "display_name": "Lamborghini Diablo VT",
        "queries": ["Lamborghini Diablo yellow", "Lamborghini Diablo VT yellow", "Lamborghini Diablo Giallo"],
        "expected_color_type": "COLORED",
        "target_hue": 55.0,     # Yellow
        "hue_tolerance": 35.0,
    },
    {
        "id": "lamborghini_revuelto",
        "display_name": "Lamborghini Revuelto",
        "queries": ["Lamborghini Revuelto orange", "Lamborghini Revuelto Arancio"],
        "expected_color_type": "COLORED",
        "target_hue": 25.0,     # Orange
        "hue_tolerance": 35.0,
    },
    {
        "id": "lamborghini_aventador_svj",
        "display_name": "Lamborghini Aventador SVJ",
        "queries": ["Lamborghini Aventador SVJ green", "Lamborghini Aventador SVJ Verde Ithaca"],
        "expected_color_type": "COLORED",
        "target_hue": 95.0,     # Green
        "hue_tolerance": 40.0,
    },
    {
        "id": "mclaren_p1",
        "display_name": "McLaren P1",
        "queries": ["McLaren P1 orange", "McLaren P1 Volcano Orange"],
        "expected_color_type": "COLORED",
        "target_hue": 25.0,     # Orange
        "hue_tolerance": 35.0,
    },
    {
        "id": "mclaren_senna",
        "display_name": "McLaren Senna",
        "queries": ["McLaren Senna orange", "McLaren Senna Mira Orange"],
        "expected_color_type": "COLORED",
        "target_hue": 25.0,
        "hue_tolerance": 40.0,
    },
    {
        "id": "mercedes_amg_one",
        "display_name": "Mercedes-AMG ONE",
        "queries": ["Mercedes-AMG One silver", "Mercedes AMG One silver", "Mercedes AMG Project One"],
        "expected_color_type": "GRAY",
        "target_hue": 0.0,
        "hue_tolerance": 0.0,
    },
    {
        "id": "nissan_gtr_r35",
        "display_name": "Nissan GT-R Nismo",
        "queries": ["Nissan GT-R Nismo grey", "Nissan GT-R Nismo stealth grey", "Nissan GT-R R35 grey"],
        "expected_color_type": "GRAY",
        "target_hue": 0.0,
        "hue_tolerance": 0.0,
    },
    {
        "id": "pagani_huayra",
        "display_name": "Pagani Huayra",
        "queries": ["Pagani Huayra blue", "Pagani Huayra BC blue", "Pagani Huayra Blu Francia"],
        "expected_color_type": "COLORED",
        "target_hue": 210.0,
        "hue_tolerance": 50.0,
    },
    {
        "id": "porsche_carrera_gt",
        "display_name": "Porsche Carrera GT",
        "queries": ["Porsche Carrera GT silver", "Porsche Carrera GT GT Silver"],
        "expected_color_type": "GRAY",
        "target_hue": 0.0,
        "hue_tolerance": 0.0,
    },
    {
        "id": "porsche_918_spyder",
        "display_name": "Porsche 918 Spyder",
        "queries": ["Porsche 918 Spyder silver", "Porsche 918 Spyder Weissach silver"],
        "expected_color_type": "GRAY",
        "target_hue": 0.0,
        "hue_tolerance": 0.0,
    },
    {
        "id": "ford_gt_supercar",
        "display_name": "Ford GT",
        "queries": ["Ford GT blue", "Ford GT Liquid Blue", "Ford GT 2017 blue"],
        "expected_color_type": "COLORED",
        "target_hue": 215.0,
        "hue_tolerance": 45.0,
    },
    {
        "id": "aston_martin_valkyrie",
        "display_name": "Aston Martin Valkyrie",
        "queries": ["Aston Martin Valkyrie green", "Aston Martin Valkyrie British Racing Green"],
        "expected_color_type": "COLORED",
        "target_hue": 140.0,    # Green
        "hue_tolerance": 45.0,
    },
    {
        "id": "aston_martin_valour",
        "display_name": "Aston Martin Valour",
        "queries": ["Aston Martin Valour green", "Aston Martin Victor green"],
        "expected_color_type": "COLORED",
        "target_hue": 140.0,
        "hue_tolerance": 45.0,
    },
    {
        "id": "bmw_m2_coupe",
        "display_name": "BMW M2 Coupe",
        "queries": ["BMW M2 G87 Zandvoort Blue", "BMW M2 Zandvoort Blue", "BMW M2 blue"],
        "expected_color_type": "COLORED",
        "target_hue": 200.0,
        "hue_tolerance": 45.0,
    },
    {
        "id": "bmw_m5_cs",
        "display_name": "BMW M5 CS",
        "queries": ["BMW M5 CS green", "BMW M5 CS Frozen Deep Green"],
        "expected_color_type": "COLORED",
        "target_hue": 130.0,
        "hue_tolerance": 45.0,
    },
    {
        "id": "ducati_panigale_v4s",
        "display_name": "Ducati Panigale V4 S",
        "queries": ["Ducati Panigale V4 S red", "Ducati Panigale V4 red", "Ducati Panigale red"],
        "expected_color_type": "COLORED",
        "target_hue": 355.0,
        "hue_tolerance": 40.0,
    },
    {
        "id": "bmw_s1000rr",
        "display_name": "BMW S1000RR",
        "queries": ["BMW S1000RR white", "BMW S 1000 RR white", "BMW S1000RR Motorsport"],
        "expected_color_type": "WHITE",
        "target_hue": 0.0,
        "hue_tolerance": 0.0,
    },
    {
        "id": "kawasaki_ninja_h2r",
        "display_name": "Kawasaki Ninja H2R",
        "queries": ["Kawasaki Ninja H2R", "Kawasaki Ninja H2 black"],
        "expected_color_type": "BLACK",
        "target_hue": 0.0,
        "hue_tolerance": 0.0,
    },
    {
        "id": "yamaha_yzf_r1m",
        "display_name": "Yamaha YZF-R1M",
        "queries": ["Yamaha YZF-R1M blue", "Yamaha YZF-R1 blue", "Yamaha R1M carbon"],
        "expected_color_type": "COLORED",
        "target_hue": 215.0,
        "hue_tolerance": 50.0,
    },
]


def _fetch_bytes(url: str, max_retries: int = 4) -> bytes:
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < max_retries - 1:
                wait_s = 3 * (2 ** attempt)
                time.sleep(wait_s)
                continue
            raise


def search_commons(query: str, limit: int = 25) -> list[dict]:
    q = urllib.parse.quote(f"{query} filetype:bitmap")
    url = (
        f"https://commons.wikimedia.org/w/api.php?action=query&generator=search"
        f"&gsrnamespace=6&gsrsearch={q}&gsrlimit={limit}"
        f"&prop=imageinfo&iiprop=url|size|mime|extmetadata&iiurlwidth=1280&format=json"
    )
    raw = _fetch_bytes(url)
    data = json.loads(raw.decode("utf-8"))

    pages = data.get("query", {}).get("pages", {})
    results = []
    for p in pages.values():
        title = p.get("title", "")
        t_low = title.lower()
        if any(term in t_low for term in SKIP_TITLE_TERMS):
            continue

        ii = p.get("imageinfo", [{}])[0]
        w, h = ii.get("width", 0), ii.get("height", 0)
        mime = ii.get("mime", "")
        if "jpeg" not in mime and "png" not in mime:
            continue
        if w < 1000 or h < 650:
            continue

        meta = ii.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        lic_low = lic.lower().strip()
        if not any(ok in lic_low for ok in ALLOWED_LICENSES) and "cc" not in lic_low:
            continue

        artist_raw = meta.get("Artist", {}).get("value", "Wikimedia Commons")
        artist = re.sub(r"<[^>]+>", "", artist_raw).strip()

        download_url = ii.get("thumburl") or ii.get("url")
        if not download_url:
            continue

        results.append({
            "title": title,
            "url": download_url,
            "orig_url": ii.get("url", download_url),
            "width": w,
            "height": h,
            "license": lic or "CC BY-SA",
            "artist": artist[:100],
            "desc_url": ii.get("descriptionurl", ""),
        })
    return results


def convert_to_vertical(raw_bytes: bytes, dest: Path) -> tuple[int, int]:
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    target_w, target_h = 1080, 1920
    scale = max(target_w / orig_w, target_h / orig_h)
    new_w = max(target_w, int(round(orig_w * scale)))
    new_h = max(target_h, int(round(orig_h * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    cropped = resized.crop((left, top, left + target_w, top + target_h))

    dest.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(dest, "JPEG", quality=92, optimize=True)
    return orig_w, orig_h


def _check_color_match(candidate_path: Path, expected_type: str, target_hue: float, tolerance: float) -> bool:
    """Analyze candidate shot and verify it strictly matches the vehicle spec."""
    try:
        from scripts.color_guard import analyze_shot_color
        c_type, hue = analyze_shot_color(candidate_path)
        if expected_type in ("WHITE", "BLACK", "GRAY"):
            return c_type == expected_type
        if expected_type == "COLORED":
            if c_type != "COLORED":
                return False
            # Compute circular hue distance on 360-degree wheel
            diff = abs(hue - target_hue) % 360
            dist = min(diff, 360 - diff)
            return dist <= tolerance
        return True
    except Exception as e:
        print(f"  [Replenish] Color match failed on {candidate_path.name}: {e}")
        return False


def replenish_vehicle(config: dict, target_unposted: int = 6) -> int:
    """Replenishes a single vehicle folder if its unposted shot count is below target_unposted."""
    set_id = config["id"]
    target_dir = SETS_DIR / set_id
    target_dir.mkdir(parents=True, exist_ok=True)

    # Load usage
    used_shots: list[str] = []
    if USAGE_FILE.exists():
        try:
            with open(USAGE_FILE, encoding="utf-8") as f:
                used_shots = json.load(f).get(set_id, [])
        except Exception:
            pass

    existing_files = sorted(target_dir.glob("shot_*.jpg"))
    unposted = [p for p in existing_files if p.name not in used_shots]

    # Verify unposted shots match the vehicle's locked color spec; purge invalid clutter
    valid_unposted = []
    for p in unposted:
        if _check_color_match(p, config["expected_color_type"], config["target_hue"], config["hue_tolerance"]):
            valid_unposted.append(p)
        else:
            print(f"  [Replenish] Purging off-spec unposted shot: {p.name}")
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    if len(valid_unposted) >= target_unposted:
        return 0

    needed = target_unposted - len(valid_unposted)
    print(f"\n[REPLENISH] {config['display_name']} ({set_id}): {len(valid_unposted)} valid unposted left -> fetching {needed} fresh shots...")

    # Load licenses
    licenses_data: dict = {}
    if LICENSES_FILE.exists():
        try:
            with open(LICENSES_FILE, encoding="utf-8") as f:
                licenses_data = json.load(f)
        except Exception:
            pass

    shot_licenses = list(licenses_data.get(set_id, {}).get("shots", []))
    existing_urls = {e.get("url") for e in shot_licenses if e.get("url")}
    existing_hashes = {hashlib.md5(p.read_bytes()).hexdigest() for p in existing_files if p.stat().st_size > 10000}

    # Find next available shot number on disk
    numbers = []
    for p in existing_files:
        m = re.search(r'shot_(\d+)\.jpg', p.name)
        if m:
            numbers.append(int(m.group(1)))
    next_idx = (max(numbers) if numbers else 0) + 1

    # Search candidates across all queries
    candidates: list[dict] = []
    seen_titles: set[str] = set()
    for q in config.get("queries", []):
        try:
            found = search_commons(q, limit=25)
            for item in found:
                if item["title"] not in seen_titles:
                    seen_titles.add(item["title"])
                    candidates.append(item)
            time.sleep(0.5)
        except Exception as e:
            print(f"  Search error for '{q}': {e}")

    added = 0
    temp_dir = target_dir / "_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    for cand in candidates:
        if added >= needed:
            break
        if cand["url"] in existing_urls or cand.get("orig_url") in existing_urls:
            continue

        try:
            raw = _fetch_bytes(cand["url"])
            if len(raw) < 25000:
                continue
            h = hashlib.md5(raw).hexdigest()
            if h in existing_hashes:
                continue

            temp_path = temp_dir / f"cand_{added}.jpg"
            convert_to_vertical(raw, temp_path)

            # Check color match before accepting
            if not _check_color_match(temp_path, config["expected_color_type"], config["target_hue"], config["hue_tolerance"]):
                temp_path.unlink(missing_ok=True)
                continue

            # Accepted: move to final slot name
            final_name = f"shot_{next_idx}.jpg"
            final_path = target_dir / final_name
            temp_path.replace(final_path)

            next_idx += 1
            existing_hashes.add(h)
            existing_urls.add(cand["url"])
            shot_licenses.append({
                "file": final_name,
                "source": "Wikimedia Commons",
                "title": cand["title"],
                "url": cand.get("orig_url", cand["url"]),
                "thumb_url": cand["url"],
                "license": cand["license"],
                "artist": cand["artist"],
                "attribution_url": cand["desc_url"],
            })
            added += 1
            print(f"  + Added {final_name} for {config['display_name']} ({cand['title'][:40]}...)")
            time.sleep(0.8)
        except Exception as e:
            print(f"  Download error: {e}")

    # Cleanup temp dir
    for f in temp_dir.glob("*"):
        f.unlink(missing_ok=True)
    temp_dir.rmdir()

    # Save updated licenses
    licenses_data[set_id] = {
        "display_name": config["display_name"],
        "shots": shot_licenses,
    }
    with open(LICENSES_FILE, "w", encoding="utf-8") as f:
        json.dump(licenses_data, f, indent=2, ensure_ascii=False)

    print(f"  [DONE] {config['display_name']}: added {added} new unposted shots.")
    return added


def replenish_all(target_unposted: int = 5) -> None:
    """Scan all fleet vehicles and replenish any that have fewer than target_unposted shots."""
    print(f"=== Starting Fleet Shot Replenishment (Target Unposted: {target_unposted}) ===")
    total_added = 0
    for cfg in FLEET_CONFIGS:
        try:
            added = replenish_vehicle(cfg, target_unposted=target_unposted)
            total_added += added
        except Exception as exc:
            print(f"Error replenishing {cfg['id']}: {exc}")
    print(f"\n=== Replenishment Complete: {total_added} total shots added across fleet ===")


if __name__ == "__main__":
    replenish_all(target_unposted=5)
