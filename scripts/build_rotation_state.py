"""
build_rotation_state.py
Reads car_models_master_list.xlsx, normalises it, merges existing fleet models,
and writes data/rotation_state.json according to MASTER PROMPT specifications:
  - Outer circle: circular queue of brands with usable clips.
  - Inner circle: circular queue of models per brand with usable clips.
  - Colour tracking per model.
  - Full master catalog of all 171 brands + 2311 models preserved in state['brands'].
"""
import openpyxl, json, os, random
from collections import defaultdict
from pathlib import Path

XLSX_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "car_models_master_list.xlsx")
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "rotation_state.json")

# Verified fleet with local asset folders
FLEET_MODELS = [
    ("Aston Martin", "Valkyrie", "aston_martin_valkyrie"),
    ("Aston Martin", "Valour", "aston_martin_valour"),
    ("Audi", "R8 V10 Plus", "audi_r8_v10"),
    ("BMW", "G82 M4", "bmw_m4_blue"),
    ("BMW", "M2 Competition", "bmw_m2_coupe"),
    ("BMW", "M5 CS", "bmw_m5_cs"),
    ("BMW", "S1000RR", "bmw_s1000rr"),
    ("Bugatti", "Chiron", "bugatti_chiron"),
    ("Bugatti", "Veyron", "bugatti_veyron"),
    ("Ducati", "Panigale V4 S", "ducati_panigale_v4s"),
    ("Ferrari", "F80", "ferrari_f80"),
    ("Ferrari", "LaFerrari", "ferrari_laferrari"),
    ("Ferrari", "SF90", "ferrari_sf90"),
    ("Ford", "GT", "ford_gt_supercar"),
    ("Ford", "Mustang S550", "white_mustang_sunset"),
    ("Kawasaki", "Ninja H2R", "kawasaki_ninja_h2r"),
    ("Koenigsegg", "Jesko", "koenigsegg_jesko"),
    ("Lamborghini", "Aventador SVJ", "lamborghini_aventador_svj"),
    ("Lamborghini", "Diablo", "lamborghini_diablo"),
    ("Lamborghini", "Revuelto", "lamborghini_revuelto"),
    ("McLaren", "P1", "mclaren_p1"),
    ("McLaren", "Senna", "mclaren_senna"),
    ("Mercedes-AMG", "ONE Hypercar", "mercedes_amg_one"),
    ("Nissan", "GT-R R35", "nissan_gtr_r35"),
    ("Pagani", "Huayra", "pagani_huayra"),
    ("Porsche", "911 GT3 RS", "porsche_911_gt3rs"),
    ("Porsche", "918 Spyder", "porsche_918_spyder"),
    ("Porsche", "Carrera GT", "porsche_carrera_gt"),
    ("Yamaha", "YZF-R1M", "yamaha_yzf_r1m"),
]

OTHERS_MAP = {
    "Rimac Nevera":         ("Rimac",        "Nevera"),
    "Lotus Evija":          ("Lotus",         "Evija"),
    "Lexus LFA":            ("Lexus",         "LFA"),
    "Ford GT":              ("Ford",          "GT"),
    "Czinger 21C":          ("Czinger",       "21C"),
    "Gordon Murray T.50":   ("Gordon Murray", "T.50"),
    "Devel Sixteen":        ("Devel",         "Sixteen"),
    "Pininfarina Battista": ("Pininfarina",   "Battista"),
    "Hennessey Venom F5":   ("Hennessey",     "Venom F5"),
    "SSC Tuatara":          ("SSC",           "Tuatara"),
    "De Tomaso Pantera":    ("De Tomaso",     "Pantera"),
    "Alfa Romeo 33 Stradale": ("Alfa Romeo",  "33 Stradale"),
    "Lancia Stratos":       ("Lancia",        "Stratos"),
    "DeLorean DMC-12":      ("DeLorean",      "DMC-12"),
    "Batmobile (Tumbler)":  None,
}

RR_KEYWORDS = ["Phantom","Ghost","Wraith","Silver Shadow","Silver Ghost","Silver Spirit",
               "Corniche","Silver Seraph","Silver Dawn","Cullinan","Spectre"]
PLYMOUTH_KEYWORDS = ["Barracuda","Road Runner","Superbird","GTX","Cuda","Duster",
                     "Belvedere","Fury","Valiant"]


def load_xlsx(path):
    if not os.path.exists(path):
        fallback = r"C:/Users/Lenovo/.gemini/antigravity/brain/dc53076c-b9fc-44e1-8f4a-4aba7c3119b0/.user_uploaded/media_1795010009926_a9f01113.xlsx"
        path = fallback if os.path.exists(fallback) else path
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[1] and row[2]:
            cat = str(row[3]).strip() if len(row) > 3 and row[3] else "Supercar"
            rows.append({
                "brand": str(row[1]).strip(),
                "model": str(row[2]).strip(),
                "category": cat,
            })
    return rows


def normalise_row(brand, model):
    if brand == "Others":
        return OTHERS_MAP.get(model)
    if brand == "Bentley/Rolls-Royce":
        for kw in RR_KEYWORDS:
            if kw in model:
                return ("Rolls-Royce", model)
        return ("Bentley", model)
    if brand == "Dodge/Plymouth":
        for kw in PLYMOUTH_KEYWORDS:
            if kw in model:
                return ("Plymouth", model)
        return ("Dodge", model)
    if brand == "Opel/Vauxhall":
        return ("Opel", model)
    if brand == "SEAT/Cupra":
        return ("SEAT", model)
    if brand == "Ford Australia":
        return ("Ford", model)
    return (brand, model)


def empty_model_state(category: str = "Supercar"):
    return {
        "category": category,
        "used_colours": [],
        "last_colour": None,
        "unavailable_until_next_cycle": False
    }


def _extract_catalog(rows):
    brand_models = defaultdict(list)
    model_categories = {}
    seen = set()
    for r in rows:
        res = normalise_row(r["brand"], r["model"])
        if not res:
            continue
        b, m = res
        key = (b, m)
        if key not in seen:
            seen.add(key)
            brand_models[b].append(m)
            model_categories[key] = r.get("category", "Supercar")

    for b, m, slug in FLEET_MODELS:
        if m not in brand_models[b]:
            brand_models[b].append(m)
        if (b, m) not in model_categories:
            is_bike = any(k in b.lower() or k in m.lower() for k in ("ducati", "kawasaki", "yamaha", "s1000rr"))
            model_categories[(b, m)] = "Superbike" if is_bike else "Supercar"

    return brand_models, model_categories


def build_state(rows):
    brand_models, model_categories = _extract_catalog(rows)

    brands_data = {}
    for b, models in brand_models.items():
        active_m_queue = list(models)
        random.shuffle(active_m_queue)

        brands_data[b] = {
            "model_queue": active_m_queue,
            "model_pointer": 0,
            "last_model": None,
            "models": {
                m: empty_model_state(model_categories.get((b, m), "Supercar"))
                for m in models
            }
        }

    # Outer circle: ALL brands from the master Excel + superbikes
    active_brands = list(brand_models.keys())
    random.shuffle(active_brands)

    state = {
        "brand_queue": active_brands,
        "brand_pointer": 0,
        "last_brand": None,
        "brands": brands_data,
        "used_clip_hashes": [],
        "used_source_urls": [],
        "post_log": []
    }
    return state


def sync_existing_state(rows, existing_state: dict) -> dict:
    """Merge ALL Excel brands & models into existing rotation_state.json
    while preserving post_log, used_colours, used_clip_hashes, and history."""
    brand_models, model_categories = _extract_catalog(rows)
    brands_data = existing_state.get("brands", {})

    for b, models in brand_models.items():
        if b not in brands_data:
            shuffled_m = list(models)
            random.shuffle(shuffled_m)
            brands_data[b] = {
                "model_queue": shuffled_m,
                "model_pointer": 0,
                "last_model": None,
                "models": {}
            }

        b_entry = brands_data[b]
        m_dict = b_entry.setdefault("models", {})
        for m in models:
            cat = model_categories.get((b, m), "Supercar")
            if m not in m_dict:
                m_dict[m] = empty_model_state(cat)
            else:
                m_dict[m]["category"] = cat
                # Reset stale unavailable flags from pre-dynamic-sourcing runs
                m_dict[m]["unavailable_until_next_cycle"] = False

        # Ensure EVERY model in m_dict is active in model_queue
        cur_q = list(b_entry.get("model_queue", []))
        cur_set = set(cur_q)
        new_models = [m for m in m_dict.keys() if m not in cur_set]
        random.shuffle(new_models)
        b_entry["model_queue"] = cur_q + new_models

    # Expand brand_queue to include ALL catalog brands, placing newly unlocked brands next
    old_q = list(existing_state.get("brand_queue", []))
    old_set = set(old_q)
    new_brands = [b for b in brands_data.keys() if b not in old_set and brands_data[b].get("model_queue")]
    random.shuffle(new_brands)

    last_b = existing_state.get("last_brand")
    if new_brands:
        # Advance pointer cleanly right into the newly unlocked brands first
        combined_q = old_q + new_brands
        existing_state["brand_queue"] = combined_q
        existing_state["brand_pointer"] = len(old_q)
    else:
        existing_state["brand_queue"] = old_q

    if last_b and existing_state["brand_queue"] and existing_state["brand_queue"][existing_state["brand_pointer"] % len(existing_state["brand_queue"])] == last_b:
        existing_state["brand_pointer"] = (existing_state["brand_pointer"] + 1) % len(existing_state["brand_queue"])

    existing_state["brands"] = brands_data
    return existing_state


if __name__ == "__main__":
    import sys
    rows = load_xlsx(XLSX_PATH)
    if "--sync" in sys.argv and os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            cur_state = json.load(f)
        state = sync_existing_state(rows, cur_state)
        action = "synced (history preserved)"
    else:
        state = build_state(rows)
        action = "built fresh"

    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    total_m = sum(len(b.get("models", {})) for b in state["brands"].values())
    active_m = sum(len(b.get("model_queue", [])) for b in state["brands"].values())
    print(f"Master rotation state {action} successfully!")
    print(f"Total brands in catalog: {len(state['brands'])} | Active brands in brand_queue: {len(state['brand_queue'])}")
    print(f"Total models in catalog: {total_m} | Active models in model_queues: {active_m}")
