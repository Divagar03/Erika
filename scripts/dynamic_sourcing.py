"""
scripts/dynamic_sourcing.py
===========================
On-demand Wikimedia Commons media sourcing for ALL models in the master Excel catalog
(174 brands / 2,315+ models), enabling 100% single-car & single-color Reels without
needing every model pre-downloaded in git.

Key guarantees:
  1. Two-stage availability verification:
     - Stage A (Probe): Fast API metadata query ONLY (~0.4s, zero image downloads) checks
       whether >= 3 deterministic entity-locked exterior photos exist.
     - Stage B (Fetch): Downloads high-res thumbnails into data/dynamic_cache/<slug>/,
       converts safely with EXIF orientation + vertical framing, deduplicates against
       used hashes/URLs, and selects a mutually color-cohesive cluster via Color Guard.
  2. Strict Deterministic Entity Lock:
     - Positive match required for BOTH target brand (or approved alias) AND model tokens.
     - Negative blocklist rejects competitor brands, wrong vehicle types (car vs bike),
       interiors, badges, engine bays, scale models, toys, diagrams, and wrecks.
  3. Zero Git Bloat: Cache folder data/dynamic_cache/ stays gitignored; only the final
     rendered MP4 and JSON state/license metadata are committed by main.py.
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
from typing import Any
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DYNAMIC_CACHE_DIR = ROOT / "data" / "dynamic_cache"
LICENSES_FILE = ROOT / "data" / "licenses.json"

HEADERS = {
    "User-Agent": "ZenithMotorsBot/2.0 (https://github.com/Divagar03/Erika; automotive-pipeline)"
}

ALLOWED_LICENSES = {
    "cc0", "cc by 2.0", "cc by 3.0", "cc by 4.0",
    "cc by-sa 2.0", "cc by-sa 3.0", "cc by-sa 4.0",
    "cc-by", "cc-by-sa", "public domain", "pd",
}

SKIP_TITLE_TERMS = (
    "interior", "cockpit", "dashboard", "steering", "seat", "engine", "motor",
    "wheel", "rim", "badge", "emblem", "logo", "embleme", "key", "diagram",
    "sketch", "drawing", "blueprint", "patent", "toy", "diecast", "hot wheels",
    "hotwheels", "lego", "model car", "scale model", "1:18", "1:24", "1:43",
    "1:64", "miniature", "replica", "brake", "caliper", "taillight detail",
    "headlight detail", "door handle", "plate", "wreck", "crash", "burned",
    "damaged", "salvage", "accident", "brochure", "manual", "advert", "ad ",
    "poster", "showroom sign", "dealer", "graveyard", "junkyard", "rusty"
)

# Unambiguous competitor brand blocklist (avoids city/generic words like "bristol", "austin", "ram", "force")
COMPETITOR_BRANDS = {
    "acura", "alfa romeo", "alpine", "aston martin", "audi", "bentley", "bmw",
    "bugatti", "buick", "cadillac", "caterham", "chery", "chevrolet", "chevy",
    "chrysler", "citroen", "cupra", "dacia", "daihatsu", "de tomaso", "delorean",
    "dodge", "donkervoort", "ducati", "ferrari", "fiat", "fisker", "ford",
    "geely", "genesis", "gmc", "harley", "hennessey", "holden", "honda",
    "hongqi", "hummer", "hyundai", "infiniti", "isuzu", "jaguar", "jeep",
    "kawasaki", "kia", "koenigsegg", "lada", "lamborghini", "lancia", "land rover",
    "lexus", "lotus", "lucid", "mahindra", "maruti", "maserati", "maybach",
    "mazda", "mclaren", "mercedes", "mercedes-benz", "mercedes-amg", "mitsubishi",
    "nio", "nissan", "opel", "pagani", "perodua", "peugeot", "pininfarina",
    "plymouth", "polestar", "pontiac", "porsche", "proton", "radical", "renault",
    "rezvani", "rimac", "rivian", "rolls royce", "rolls-royce", "ruf", "saab",
    "saleen", "seat", "shelby", "skoda", "spyker", "ssc", "studebaker",
    "subaru", "suzuki", "tata", "tesla", "toyota", "triumph", "tvr", "vauxhall",
    "vinfast", "volkswagen", "vw", "volvo", "wiesmann", "wuling", "xiaomi",
    "yamaha", "zeekr", "zenvo", "harley davidson", "aprilia", "mv agusta"
}

BIKE_TERMS = ("motorcycle", "motorbike", "superbike", "bike", "scooter", "moped")
CAR_TERMS = ("sedan", "suv", "hatchback", "pickup truck", "limousine", "minivan")

BRAND_ALIASES: dict[str, set[str]] = {
    "aston martin": {"aston martin", "aston"},
    "alfa romeo": {"alfa romeo", "alfa"},
    "mercedes-benz": {"mercedes", "mercedes benz", "mercedes-benz", "mercedes amg", "mercedes-amg", "amg", "benz"},
    "mercedes-amg": {"mercedes", "mercedes benz", "mercedes-benz", "mercedes amg", "mercedes-amg", "amg", "benz"},
    "rolls-royce": {"rolls royce", "rolls-royce", "rolls"},
    "chevrolet": {"chevrolet", "chevy", "corvette", "camaro"},
    "ford": {"ford", "mustang", "shelby", "gt40"},
    "dodge": {"dodge", "viper", "challenger", "charger"},
    "volkswagen": {"volkswagen", "vw"},
    "nissan": {"nissan", "datsun", "skyline", "gt-r", "gtr"},
    "land rover": {"land rover", "range rover"},
    "maruti suzuki": {"maruti", "suzuki", "maruti suzuki"},
    "premier padmini": {"premier", "padmini"},
    "hindustan ambassador": {"hindustan", "ambassador"},
    "gordon murray": {"gordon murray", "t.50", "t50", "gma"},
    "pininfarina": {"pininfarina", "battista"},
    "de tomaso": {"de tomaso", "detomaso", "pantera"},
    "w motors": {"w motors", "lykan", "fenyr"},
    "talbot-lago": {"talbot", "talbot lago", "talbot-lago"},
    "hispano-suiza": {"hispano suiza", "hispano-suiza", "hispano"},
    "isotta fraschini": {"isotta fraschini", "isotta"},
    "austin-healey": {"austin healey", "austin-healey"},
}

STOP_MODEL_TOKENS = {"the", "and", "car", "coupe", "sedan", "concept", "edition", "series", "special", "ii", "iii", "iv"}

# Process-local metadata probe cache so duplicate checks cost 0 network calls
_PROBE_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


def slugify_vehicle(brand: str, model: str) -> str:
    raw = f"{brand}_{model}".lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return slug


def _normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _fetch_bytes(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def is_clean_model_title(
    title_or_meta: str,
    target_brand: str,
    target_model: str,
    is_bike: bool = False
) -> tuple[bool, str]:
    """Deterministic positive + negative entity lock on candidate image title/filename."""
    raw_low = title_or_meta.lower()
    norm = f" {_normalize_text(title_or_meta)} "

    # 1. Skip non-exterior / toy / interior / damage terms
    model_norm = _normalize_text(target_model)
    for term in SKIP_TITLE_TERMS:
        t_clean = term.strip()
        if t_clean in model_norm:
            continue
        if re.search(r"\b" + re.escape(t_clean) + r"\b", norm) or t_clean in raw_low:
            # Skip only when matching whole word or clear compound phrase like '1:18', 'scale model'
            if " " in t_clean or ":" in t_clean or re.search(r"\b" + re.escape(t_clean) + r"\b", norm):
                return False, f"Skip term '{t_clean}'"

    # 2. Vehicle type separation (car vs superbike)
    if not is_bike:
        for bt in BIKE_TERMS:
            if bt not in model_norm and re.search(r"\b" + re.escape(bt) + r"\b", norm):
                return False, f"Bike term '{bt}' in car query"
    else:
        for ct in CAR_TERMS:
            if ct not in model_norm and re.search(r"\b" + re.escape(ct) + r"\b", norm):
                return False, f"Car term '{ct}' in bike query"

    # 3. Competitor brand rejection
    b_low = target_brand.lower().strip()
    allowed = set(BRAND_ALIASES.get(b_low, set()))
    allowed.add(b_low)
    allowed.add(_normalize_text(b_low))
    # Also allow words belonging to target model (e.g. "Ford GT" has "gt", etc.)
    model_words = set(_normalize_text(target_model).split())

    for comp in COMPETITOR_BRANDS:
        comp_norm = _normalize_text(comp)
        if comp in allowed or comp_norm in allowed:
            continue
        if comp_norm in model_words or comp_norm in _normalize_text(target_brand):
            continue
        # Prevent subgroup overlap (e.g., "alfa romeo" vs "romeo", "land rover" vs "rover")
        if any(comp_norm in a for a in allowed):
            continue
        if re.search(r"\b" + re.escape(comp_norm) + r"\b", norm):
            return False, f"Competitor brand '{comp}'"

    # 4. Positive match requirement: brand (or alias) AND key model token(s)
    brand_hit = any(re.search(r"\b" + re.escape(_normalize_text(a)) + r"\b", norm) for a in allowed if _normalize_text(a))
    if not brand_hit:
        return False, "Missing target brand in filename/title"

    # Remove parentheticals like "(E46)" when deriving core tokens, but keep alphanumeric words inside if needed
    core_model_clean = re.sub(r"\([^)]*\)", " ", target_model)
    brand_word_set = set(_normalize_text(target_brand).split())
    core_tokens = [
        t for t in _normalize_text(core_model_clean).split()
        if t not in brand_word_set and t not in STOP_MODEL_TOKENS
    ]
    if not core_tokens:
        core_tokens = [t for t in _normalize_text(target_model).split() if t not in brand_word_set]

    if core_tokens:
        # Reject conflicting Mk generation tags (e.g. Mk5 / GR Supra when target is Mk4)
        mk_req = re.findall(r"\bmk\s*([0-9]+)\b", model_norm)
        if mk_req:
            mk_found = re.findall(r"\bmk\s*([0-9]+)\b", norm)
            if mk_found and not any(m in mk_req for m in mk_found):
                return False, f"Conflicting generation Mk{mk_found} vs Mk{mk_req}"
            if "4" in mk_req and (" gr " in norm or "mk5" in norm or "a90" in norm or "2020" in norm or "2021" in norm or "2022" in norm):
                return False, "Mk5/GR model in Mk4 query"

        # Require at least one significant core model token (or longest distinguishing token)
        significant = [t for t in core_tokens if len(t) >= 2] or core_tokens
        if not any(re.search(r"\b" + re.escape(t) + r"\b", norm) for t in significant):
            # Also try concatenated token (e.g., "GT-R" -> "gtr", "911" -> "911")
            joined = "".join(core_tokens)
            if len(joined) < 2 or joined not in norm.replace(" ", ""):
                return False, f"Missing model token ({significant}) in '{title_or_meta}'"

    return True, "Matched"


def clean_model_display(model: str) -> str:
    """Strip catalog annotations like '(all generations)' or '(1964 to present)' for clean display/queries."""
    m = re.sub(r"\((?:all generations|1964 to present|all variants)\)", "", model, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", m).strip()


def _build_search_queries(brand: str, model: str, colour_hint: str = "", fast_probe: bool = False) -> list[str]:
    clean_m = clean_model_display(model)
    # Remove parens and handle slash variants like '240Z/Fairlady Z' or 'R/T'
    no_paren = re.sub(r"[()]", " ", clean_m)
    no_paren = re.sub(r"\s+", " ", no_paren).strip()

    # Core without MkN / generation tag as clean fallback
    core_no_mk = re.sub(r"\bmk\s*[0-9ivx]+\b", "", no_paren, flags=re.IGNORECASE)
    core_no_mk = re.sub(r"\b[12][09]\d{2}\b", "", core_no_mk).strip()
    core_no_mk = re.sub(r"\s+", " ", core_no_mk).strip()

    # Handle slash alternatives ("240Z/Fairlady Z" -> "Nissan 240Z")
    slash_primary = no_paren.split("/")[0].strip() if "/" in no_paren else no_paren
    rt_clean = no_paren.replace("/", "")

    # Avoid duplicating brand if model starts with brand (e.g. "W Motors Lykan", "KTM X-Bow")
    prefix = "" if no_paren.lower().startswith(brand.lower()) else f"{brand} "

    queries = []
    if colour_hint and colour_hint.lower() not in ("standard", "any"):
        queries.append(f"{prefix}{rt_clean} {colour_hint}")
    queries.append(f'"{prefix}{rt_clean}"')
    if not fast_probe:
        if slash_primary != rt_clean:
            queries.append(f"{prefix}{slash_primary}")
        if core_no_mk and len(core_no_mk) >= 2 and core_no_mk.lower() != rt_clean.lower():
            queries.append(f"{prefix}{core_no_mk}")
        if len(rt_clean.split()) >= 2:
            queries.append(f"{prefix}{rt_clean}")

    # Deduplicate preserving order
    seen = set()
    uniq = []
    for q in queries:
        qn = q.strip()
        if qn and qn not in seen:
            seen.add(qn)
            uniq.append(qn)
    return uniq


def search_wikimedia_candidates(
    brand: str,
    model: str,
    colour_hint: str = "",
    limit: int = 25,
    is_bike: bool = False,
    fast_probe: bool = False
) -> list[dict[str, Any]]:
    """Query Wikimedia Commons JSON metadata ONLY (fast, no downloads) with entity-lock filter."""
    queries = _build_search_queries(brand, model, colour_hint=colour_hint, fast_probe=fast_probe)

    results: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for q in queries:
        if len(results) >= (10 if fast_probe else 16):
            break
        encoded = urllib.parse.quote(f"{q} filetype:bitmap")
        api_url = (
            f"https://commons.wikimedia.org/w/api.php?action=query&generator=search"
            f"&gsrnamespace=6&gsrsearch={encoded}&gsrlimit={limit}"
            f"&prop=imageinfo&iiprop=url|size|mime|extmetadata&iiurlwidth=1280&format=json"
        )
        try:
            raw = _fetch_bytes(api_url, timeout=8)
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            continue

        pages = data.get("query", {}).get("pages", {})
        for p in pages.values():
            title = p.get("title", "")
            if title in seen_titles:
                continue
            ii = p.get("imageinfo", [{}])[0]
            w, h = ii.get("width", 0), ii.get("height", 0)
            mime = ii.get("mime", "")
            if "jpeg" not in mime and "png" not in mime:
                continue
            if w < 800 or h < 500:
                continue
            ar = w / float(max(1, h))
            if ar < 0.45 or ar > 2.2:
                continue

            meta = ii.get("extmetadata", {})
            lic = meta.get("LicenseShortName", {}).get("value", "")
            lic_low = lic.lower().strip()
            if lic_low and not any(ok in lic_low for ok in ALLOWED_LICENSES) and "cc" not in lic_low and "public" not in lic_low:
                continue

            obj_name = meta.get("ObjectName", {}).get("value", "")
            obj_clean = re.sub(r"<[^>]+>", "", obj_name).strip()
            corpus = f"{title} {obj_clean}"

            ok, _ = is_clean_model_title(corpus, brand, model, is_bike=is_bike)
            if not ok:
                continue

            download_url = ii.get("thumburl") or ii.get("url")
            if not download_url:
                continue

            artist_raw = meta.get("Artist", {}).get("value", "Wikimedia Commons")
            artist = re.sub(r"<[^>]+>", "", artist_raw).strip() or "Wikimedia Commons"

            seen_titles.add(title)
            has_colour = bool(colour_hint and colour_hint.lower() in title.lower())
            results.append({
                "title": title,
                "url": download_url,
                "orig_url": ii.get("url", download_url),
                "width": w,
                "height": h,
                "ar": ar,
                "license": lic or "CC BY-SA",
                "artist": artist[:100],
                "desc_url": ii.get("descriptionurl", ""),
                "colour_match_title": has_colour,
            })

    # Prefer: title colour match -> aspect ratio closest to vertical/4:3 -> higher res
    results.sort(
        key=lambda x: (
            x["colour_match_title"],
            x["ar"] <= 1.45,
            -abs(x["ar"] - 0.8),
            x["width"] * x["height"],
        ),
        reverse=True,
    )
    return results


def probe_wikimedia_availability(brand: str, model: str, is_bike: bool = False, min_needed: int = 3) -> bool:
    """Fast Stage-A metadata check. Returns True if >= min_needed clean shots exist on Wikimedia."""
    key = (brand.lower(), model.lower())
    if key not in _PROBE_CACHE:
        cands = search_wikimedia_candidates(brand, model, colour_hint="", limit=25, is_bike=is_bike, fast_probe=True)
        # Fallback broader unquoted query only if exact quoted query returned < min_needed
        if len(cands) < min_needed and len(model.split()) >= 1:
            cands = search_wikimedia_candidates(brand, model, colour_hint="", limit=25, is_bike=is_bike, fast_probe=False)
        _PROBE_CACHE[key] = cands
    return len(_PROBE_CACHE[key]) >= min_needed


def _convert_to_vertical(raw_bytes: bytes, dest: Path) -> tuple[int, int]:
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
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


def _hue_angle_to_colour_name(hue_deg: float) -> str:
    h = hue_deg % 360.0
    if h < 18 or h >= 338:
        return "red"
    if h < 44:
        return "orange"
    if h < 70:
        return "yellow"
    if h < 162:
        return "green"
    if h < 252:
        return "blue"
    if h < 305:
        return "purple"
    return "pink"


def detect_cluster_colour(shots: list[Path], fallback_hint: str = "") -> str:
    """Detect true dominant body colour across cohesive shots using circular hue mode (avoids 0/360 wrap bugs)."""
    import colorsys
    try:
        from scripts.color_guard import analyze_shot_color
    except ImportError:
        from color_guard import analyze_shot_color

    types = []
    hist_bins: dict[int, int] = {}
    for p in shots:
        try:
            ct, _ = analyze_shot_color(p)
            types.append(ct)
            if ct == "COLORED":
                with Image.open(p) as img:
                    rgb = img.convert("RGB")
                    w, h = rgb.size
                    crop = rgb.crop((int(w * 0.25), int(h * 0.25), int(w * 0.75), int(h * 0.75))).resize((48, 48))
                    for r, g, b in crop.getdata():
                        hn, ln, sn = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
                        if sn > 0.25 and 0.14 < ln < 0.85:
                            b_idx = int((hn * 360.0) // 15) * 15
                            hist_bins[b_idx] = hist_bins.get(b_idx, 0) + 1
        except Exception:
            pass

    if not types:
        return fallback_hint or "standard"
    dominant = max(set(types), key=types.count)
    if dominant == "WHITE":
        return "white"
    if dominant == "BLACK":
        return "black"
    if dominant == "GRAY":
        return "silver"
    if hist_bins:
        peak_bin = max(hist_bins.items(), key=lambda kv: kv[1])[0]
        return _hue_angle_to_colour_name(peak_bin + 7.5)
    return fallback_hint or "standard"


def fetch_dynamic_cohesive_shots(
    brand: str,
    model: str,
    target_colour: str = "",
    is_bike: bool = False,
    min_required: int = 3,
    needed_count: int = 5,
    used_source_urls: set[str] | None = None,
    used_clip_hashes: set[str] | None = None,
) -> tuple[str, list[Path], str]:
    """
    Stage-B: Downloads up to 10 verified exterior shots concurrently for (brand, model) into
    data/dynamic_cache/<slug>/, runs Color Guard clustering, and returns:
      (slug, selected_paths, detected_colour)
    Returns ("", [], "") if fewer than min_required cohesive shots can be assembled.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    slug = slugify_vehicle(brand, model)
    target_dir = DYNAMIC_CACHE_DIR / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    used_urls = set(used_source_urls or ())
    used_hashes = set(used_clip_hashes or ())

    # Reuse already-cached clean shots in data/dynamic_cache/<slug> if sufficient
    cached_jpgs = sorted(list(target_dir.glob("shot_*.jpg")))
    if len(cached_jpgs) < 6:
        candidates = []
        seen_urls = set()
        if target_colour and target_colour not in ("standard", "any"):
            for c in search_wikimedia_candidates(brand, model, colour_hint=target_colour, limit=20, is_bike=is_bike, fast_probe=True):
                if c["url"] not in seen_urls:
                    seen_urls.add(c["url"])
                    candidates.append(c)

        base_cands = _PROBE_CACHE.get((brand.lower(), model.lower()))
        if base_cands is None:
            base_cands = search_wikimedia_candidates(brand, model, colour_hint="", limit=25, is_bike=is_bike)
            _PROBE_CACHE[(brand.lower(), model.lower())] = base_cands
        for c in base_cands:
            if c["url"] not in seen_urls:
                seen_urls.add(c["url"])
                candidates.append(c)

        licenses_data = {}
        if LICENSES_FILE.exists():
            try:
                licenses_data = json.loads(LICENSES_FILE.read_text(encoding="utf-8"))
            except Exception:
                licenses_data = {}
        shot_licenses = list(licenses_data.get(slug, {}).get("shots", []))
        existing_lic_urls = {
            u for e in shot_licenses for u in (e.get("url"), e.get("thumb_url")) if u
        }

        filtered_cands = [
            c for c in candidates
            if c["url"] not in used_urls
            and c.get("orig_url") not in used_urls
            and c["url"] not in existing_lic_urls
            and c.get("orig_url") not in existing_lic_urls
        ][:14]

        existing_md5 = {hashlib.md5(p.read_bytes()).hexdigest() for p in cached_jpgs if p.stat().st_size > 15000}
        next_idx = len(cached_jpgs) + 1
        downloaded_new = 0

        def _dl(cand_item: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
            try:
                raw = _fetch_bytes(cand_item["url"], timeout=8)
                return cand_item, raw if len(raw) >= 20000 else None
            except Exception:
                return cand_item, None

        fetched_pairs = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(_dl, c) for c in filtered_cands]
            for fut in as_completed(futs):
                fetched_pairs.append(fut.result())

        for cand, raw in fetched_pairs:
            if len(cached_jpgs) + downloaded_new >= 14:
                break
            if not raw:
                continue
            try:
                md5 = hashlib.md5(raw).hexdigest()
                if md5 in existing_md5:
                    continue
                out_path = target_dir / f"shot_{next_idx}.jpg"
                _convert_to_vertical(raw, out_path)
                if out_path.stat().st_size < 15000:
                    out_path.unlink(missing_ok=True)
                    continue

                try:
                    import imagehash
                    with Image.open(out_path) as im_check:
                        ph = str(imagehash.phash(im_check))
                    if ph in used_hashes:
                        out_path.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass

                existing_md5.add(md5)
                next_idx += 1
                downloaded_new += 1
                shot_licenses.append({
                    "file": out_path.name,
                    "source": "Wikimedia Commons",
                    "title": cand["title"],
                    "url": cand.get("orig_url", cand["url"]),
                    "thumb_url": cand["url"],
                    "license": cand["license"],
                    "artist": cand["artist"],
                    "attribution_url": cand["desc_url"],
                })
            except Exception:
                continue

        if downloaded_new > 0:
            licenses_data[slug] = {
                "display_name": f"{brand} {model}",
                "brand": brand,
                "shots": shot_licenses,
            }
            try:
                LICENSES_FILE.parent.mkdir(parents=True, exist_ok=True)
                LICENSES_FILE.write_text(json.dumps(licenses_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    all_shots = sorted(list(target_dir.glob("shot_*.jpg")))
    if len(all_shots) < min_required:
        return "", [], ""

    try:
        from scripts.shot_tracker import _find_cohesive_subset
        from scripts.color_guard import verify_color_cohesion
    except ImportError:
        from shot_tracker import _find_cohesive_subset
        from color_guard import verify_color_cohesion

    selected = _find_cohesive_subset(all_shots, min_required=min_required, needed_count=needed_count)
    if not selected or len(selected) < min_required:
        return "", [], ""

    try:
        verify_color_cohesion(selected)
    except Exception:
        return "", [], ""

    actual_colour = detect_cluster_colour(selected, fallback_hint=target_colour)
    return slug, selected, actual_colour
