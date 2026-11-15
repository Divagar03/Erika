"""
Expands photo media sets for all Zenith Motors supercars, hypercars, and superbikes
by sourcing high-resolution CC-licensed DSLR photos from Wikimedia Commons.
Increases library from 5 shots per car up to 12-15 shots per car so the shot tracker
always has a large, fresh pool of unposted shots to pick from.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image, ImageOps

BASE_DIR = Path(__file__).resolve().parent.parent
SETS_DIR = BASE_DIR / "assets" / "vehicles" / "sets"
LICENSES_FILE = BASE_DIR / "data" / "licenses.json"

HEADERS = {
    "User-Agent": "ZenithMotorsBot/1.2 (https://github.com/Divagar03/Erika; educational-automotive-showcase)"
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
)

# All vehicles to expand with query terms, target count is 10-12 shots
TARGET_SHOTS_PER_VEHICLE = 10

ALL_VEHICLES = [
    ("audi_r8_v10", ["Audi R8 V10", "Audi R8 4S", "Audi R8 Type 42"], "Audi R8 V10", "Audi"),
    ("bmw_m4_black", ["BMW M4 G82", "BMW M4 Competition", "BMW M4 Coupe"], "BMW M4 Competition", "BMW"),
    ("bmw_s1000rr", ["BMW S1000RR", "BMW S 1000 RR"], "BMW S1000RR", "BMW"),
    ("ducati_panigale_v4s", ["Ducati Panigale V4 S", "Ducati Panigale V4"], "Ducati Panigale V4 S", "Ducati"),
    ("ferrari_sf90", ["Ferrari SF90 Stradale", "Ferrari SF90"], "Ferrari SF90 Stradale", "Ferrari"),
    ("lamborghini_revuelto", ["Lamborghini Revuelto"], "Lamborghini Revuelto", "Lamborghini"),
    ("nissan_gtr_r35", ["Nissan GT-R R35", "Nissan GT-R Nismo"], "Nissan GT-R Nismo", "Nissan"),
    ("porsche_911_gt3rs", ["Porsche 911 GT3 RS 992", "Porsche 992 GT3 RS"], "Porsche 911 GT3 RS", "Porsche"),
    ("bugatti_chiron", ["Bugatti Chiron", "Bugatti Chiron Pur Sport"], "Bugatti Chiron", "Bugatti"),
    ("koenigsegg_jesko", ["Koenigsegg Jesko"], "Koenigsegg Jesko", "Koenigsegg"),
    ("pagani_huayra", ["Pagani Huayra", "Pagani Huayra BC"], "Pagani Huayra", "Pagani"),
    ("mclaren_p1", ["McLaren P1"], "McLaren P1", "McLaren"),
    ("ferrari_laferrari", ["Ferrari LaFerrari"], "Ferrari LaFerrari", "Ferrari"),
    ("lamborghini_aventador_svj", ["Lamborghini Aventador SVJ", "Aventador SVJ"], "Lamborghini Aventador SVJ", "Lamborghini"),
    ("porsche_carrera_gt", ["Porsche Carrera GT"], "Porsche Carrera GT", "Porsche"),
    ("mercedes_amg_one", ["Mercedes-AMG One", "Mercedes AMG One"], "Mercedes-AMG One", "Mercedes-AMG"),
    ("bmw_m5_cs", ["BMW M5 CS", "BMW M5 F90"], "BMW M5 CS", "BMW"),
    ("bmw_m2_coupe", ["BMW M2 G87", "BMW M2 Competition"], "BMW M2 Coupe", "BMW"),
    ("kawasaki_ninja_h2r", ["Kawasaki Ninja H2R", "Kawasaki Ninja H2"], "Kawasaki Ninja H2R", "Kawasaki"),
    ("yamaha_yzf_r1m", ["Yamaha YZF-R1M", "Yamaha YZF-R1"], "Yamaha YZF-R1M", "Yamaha"),
    ("mclaren_senna", ["McLaren Senna"], "McLaren Senna", "McLaren"),
    ("porsche_918_spyder", ["Porsche 918 Spyder"], "Porsche 918 Spyder", "Porsche"),
    ("ford_gt_supercar", ["Ford GT 2017", "Ford GT 2019", "Ford GT Heritage"], "Ford GT", "Ford"),
    ("aston_martin_valkyrie", ["Aston Martin Valkyrie"], "Aston Martin Valkyrie", "Aston Martin"),
    ("bugatti_veyron", ["Bugatti Veyron Super Sport", "Bugatti Veyron 16.4"], "Bugatti Veyron", "Bugatti"),
    ("lamborghini_diablo", ["Lamborghini Diablo", "Lamborghini Diablo VT"], "Lamborghini Diablo", "Lamborghini"),
    ("aston_martin_valour", ["Aston Martin Valour", "Aston Martin Victor"], "Aston Martin Valour", "Aston Martin"),
    ("ferrari_f80", ["Ferrari F80 Concept", "Ferrari F80"], "Ferrari F80", "Ferrari"),
]


def _fetch_bytes(url: str, max_retries: int = 4) -> bytes:
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < max_retries - 1:
                wait_s = 4 * (2 ** attempt)
                print(f"  [WAIT {e.code}] rate limited, sleeping {wait_s}s...")
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

    results.sort(key=lambda x: (x["width"] >= x["height"], x["width"] * x["height"]), reverse=True)
    return results


def convert_to_vertical(img_bytes: bytes, dest: Path) -> tuple[int, int]:
    img = Image.open(io.BytesIO(img_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    orig_w, orig_h = img.size

    target_w, target_h = 1080, 1920
    scale = max(target_w / orig_w, target_h / orig_h)
    new_w, new_h = max(target_w, int(round(orig_w * scale))), max(target_h, int(round(orig_h * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    cropped = resized.crop((left, top, left + target_w, top + target_h))

    dest.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(dest, "JPEG", quality=92, optimize=True)
    return orig_w, orig_h


def _file_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def expand_all_sets(target_count: int = TARGET_SHOTS_PER_VEHICLE) -> None:
    SETS_DIR.mkdir(parents=True, exist_ok=True)
    licenses_data: dict = {}
    if LICENSES_FILE.exists():
        try:
            licenses_data = json.loads(LICENSES_FILE.read_text(encoding="utf-8"))
        except Exception:
            licenses_data = {}

    for set_id, queries, display_name, brand in ALL_VEHICLES:
        target_dir = SETS_DIR / set_id
        target_dir.mkdir(parents=True, exist_ok=True)

        existing_shots = {p.name: p for p in sorted(target_dir.glob("shot_*.jpg")) if p.stat().st_size > 15000}
        current_count = len(existing_shots)
        if current_count >= target_count:
            print(f"[OK] {set_id}: already has {current_count}/{target_count} shots")
            continue

        needed_slots = [f"shot_{i}.jpg" for i in range(1, target_count + 1) if f"shot_{i}.jpg" not in existing_shots]
        print(f"\n[FETCH] {display_name} ({set_id}) currently has {current_count} shots -> adding {len(needed_slots)}...")

        existing_hashes = {_file_md5(p) for p in existing_shots.values()}
        existing_urls = {
            e.get("url") for e in licenses_data.get(set_id, {}).get("shots", []) if e.get("url")
        }

        candidates: list[dict] = []
        seen_titles: set[str] = set()
        for q in queries:
            try:
                found = search_commons(q, limit=20)
                for item in found:
                    if item["title"] not in seen_titles:
                        seen_titles.add(item["title"])
                        candidates.append(item)
                time.sleep(1.0)
            except Exception as e:
                print(f"  Search error for '{q}': {e}")

        shot_licenses = list(licenses_data.get(set_id, {}).get("shots", []))
        slot_idx = 0

        for cand in candidates:
            if slot_idx >= len(needed_slots):
                break
            if cand["url"] in existing_urls or cand.get("orig_url") in existing_urls:
                continue

            slot_name = needed_slots[slot_idx]
            try:
                raw_bytes = _fetch_bytes(cand["url"])
                if len(raw_bytes) < 25000:
                    continue
                img_hash = hashlib.md5(raw_bytes).hexdigest()
                if img_hash in existing_hashes:
                    continue

                dest = target_dir / slot_name
                ow, oh = convert_to_vertical(raw_bytes, dest)
                if dest.stat().st_size < 15000:
                    dest.unlink(missing_ok=True)
                    continue

                existing_hashes.add(img_hash)
                existing_urls.add(cand["url"])
                shot_licenses.append({
                    "file": slot_name,
                    "source": "Wikimedia Commons",
                    "title": cand["title"],
                    "url": cand.get("orig_url", cand["url"]),
                    "thumb_url": cand["url"],
                    "license": cand["license"],
                    "artist": cand["artist"],
                    "attribution_url": cand["desc_url"],
                })
                slot_idx += 1
                print(f"  + Saved {slot_name} ({ow}x{oh} -> 1080x1920) [{cand['license']}]")
                time.sleep(1.5)
            except Exception as e:
                print(f"  Download error: {e}")
                time.sleep(1.5)

        total_now = len(list(target_dir.glob("shot_*.jpg")))
        licenses_data[set_id] = {
            "display_name": display_name,
            "brand": brand,
            "shots": shot_licenses,
        }
        LICENSES_FILE.write_text(json.dumps(licenses_data, indent=2), encoding="utf-8")
        print(f"  [RESULT] {set_id}: total {total_now} shots in library")


if __name__ == "__main__":
    expand_all_sets(TARGET_SHOTS_PER_VEHICLE)
