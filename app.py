import os
import base64
from datetime import datetime, timedelta

import requests
from flask import Flask, request, render_template

app = Flask(__name__)

# ====== ENV VARS (Railway Variables) ======
EVOCON_TENANT = os.getenv("EVOCON_TENANT", "")
EVOCON_SECRET = os.getenv("EVOCON_SECRET", "")

# ====== MAPPING: itemname -> field key (we use itemname directly in matrix) ======
ORDERED_ITEMS = [
    "Θερμοκρασία λαμινατορίου (°C)",
    "Είδος μαργαρίνης",
    "Θερμοκρασία μαργαρίνης (°C)",
    "Λαμάκι μαργαρίνης (mm)",
    "Λαμάκι recupero (mm)",
    "Διάκενο μαχαιριών (cm)",
    "Πάχος extruder (1η)",
    "Πάχος extruder (2η)",
    "Ποσοστό μαργαρίνης (%)",
    "Ποσοστό ανακύκλωσης ζύμης recupero (%)",
]

ALLOWED_ITEMS = set(ORDERED_ITEMS)

SHIFT_START = {"A": "06:00", "B": "14:00", "Γ": "22:00"}  # as you said


# -------------------------
# Helpers
# -------------------------
def basic_auth_header():
    if not EVOCON_TENANT or not EVOCON_SECRET:
        raise RuntimeError("Missing EVOCON_TENANT / EVOCON_SECRET env vars")
    token = base64.b64encode(f"{EVOCON_TENANT}:{EVOCON_SECRET}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}


def normalize_value(v):
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("-", "N/A", "n/a"):
        return ""
    return s.replace(",", ".")


def parse_hhmm(s: str):
    try:
        return datetime.strptime(s.strip(), "%H:%M").time()
    except Exception:
        return None


def minutes(t):
    return t.hour * 60 + t.minute


def sort_donetime_list(times, shift_name):
    start_str = SHIFT_START.get(shift_name, "00:00")
    start_t = parse_hhmm(start_str) or datetime.strptime("00:00", "%H:%M").time()
    start_m = minutes(start_t)

    def key(tstr):
        t = parse_hhmm(tstr) or datetime.strptime("00:00", "%H:%M").time()
        m = minutes(t)
        return (m - start_m) % (24 * 60)

    return sorted(times, key=key)


def fetch_checklists_json(start_iso: str, end_iso: str):
    """
    Calls:
      https://api.evocon.com/api/reports/checklists_json

    IMPORTANT:
    Depending on your Evocon setup, params might be startTime/endTime or startDate/endDate.
    Your screenshot/export suggests startTime/endTime works in your environment.
    """
    url = "https://api.evocon.com/api/reports/checklists_json"
    headers = {
        "Accept": "application/json",
        **basic_auth_header(),
    }
    params = {
        "startTime": start_iso,
        "endTime": end_iso,
    }

    r = requests.get(url, headers=headers, params=params, timeout=45)
    r.raise_for_status()
    data = r.json()
    # Expecting list of rows (dicts)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected API response: expected a JSON list")
    return data


def build_shift_index(rows):
    """
    Find unique (shiftDate, shift, station) combos and their latest donetime
    so we can preselect the latest shift.
    """
    idx = {}
    for r in rows:
        sd = str(r.get("shiftDate") or "").strip()
        sh = str(r.get("shift") or "").strip()
        st = str(r.get("station") or "").strip()
        dt = str(r.get("donetime") or "").strip()

        if not (sd and sh and st and dt):
            continue

        t = parse_hhmm(dt)
        key = (sd, sh, st)
        if key not in idx:
            idx[key] = {"shiftDate": sd, "shift": sh, "station": st, "last_time": t}
        else:
            if t and (idx[key]["last_time"] is None or t > idx[key]["last_time"]):
                idx[key]["last_time"] = t

    def sort_key(x):
        try:
            d = datetime.strptime(x["shiftDate"], "%Y-%m-%d").date()
        except Exception:
            d = datetime.min.date()
        t = x["last_time"] or datetime.min.time()
        return (d, t)

    out = sorted(idx.values(), key=sort_key, reverse=True)
    return out


def build_report(rows, shiftDate, shiftName, station):
    """
    Group rows by donetime (submission-time) and build:
      columns: sorted list of donetime strings
      matrix: rows aligned to columns
    """
    filtered = [
        r for r in rows
        if str(r.get("shiftDate") or "").strip() == shiftDate
        and str(r.get("shift") or "").strip() == shiftName
        and str(r.get("station") or "").strip() == station
    ]

    submissions = {}  # donetime -> { itemname -> itemresult }
    meta = {}         # donetime -> operator/product/order

    for r in filtered:
        donetime = str(r.get("donetime") or "").strip()
        itemname = str(r.get("itemname") or "").strip()
        if not donetime or itemname not in ALLOWED_ITEMS:
            continue

        submissions.setdefault(donetime, {})
        submissions[donetime][itemname] = normalize_value(r.get("itemresult"))

        if donetime not in meta:
            meta[donetime] = {
                "operator": str(r.get("operator") or "").strip(),
                "product": str(r.get("productproduced") or "").strip(),
                "productionOrder": str(r.get("productionOrder") or "").strip(),
            }

    columns = sort_donetime_list(list(submissions.keys()), shiftName)

    matrix = []
    for item in ORDERED_ITEMS:
        row_vals = []
        for t in columns:
            row_vals.append(submissions.get(t, {}).get(item, ""))
        matrix.append({"label": item, "values": row_vals})

    header = {"operator": "", "product": "", "productionOrder": ""}
    if columns:
        header = meta.get(columns[-1], header)

    return {
        "columns": columns,
        "matrix": matrix,
        "header": header,
        "shiftDate": shiftDate,
        "shift": shiftName,
        "station": station,
    }


# -------------------------
# Routes
# -------------------------
@app.get("/")
def home():
    return "<a href='/print'>Go to Print</a>"


@app.get("/print")
def picker():
    # Fetch last 3 days so we can preselect the latest shift
    now = datetime.now()
    start = (now - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now

    rows = fetch_checklists_json(start.isoformat(), end.isoformat())
    shifts = build_shift_index(rows)

    if not shifts:
        return "No shifts found in last 3 days."

    # First option is preselected in picker.html
    return render_template("picker.html", shifts=shifts)


@app.get("/print/render")
def render_print():
    """
    Called with:
      /print/render?key=YYYY-MM-DD|Γ|4η ΓΡΑΜΜΗ
    """
    key = request.args.get("key", "")
    parts = key.split("|")
    if len(parts) != 3:
        return "Invalid selection", 400

    shiftDate, shiftName, station = parts[0].strip(), parts[1].strip(), parts[2].strip()

    # Wider window around shift
