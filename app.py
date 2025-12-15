import os
import base64
import traceback
from datetime import datetime, timedelta

import requests
from flask import Flask, request, render_template

app = Flask(__name__)
APP_VERSION = "evoconprint-shiftdate-shift-only-v1-2025-12-16"

# ===== Railway Variables =====
EVOCON_TENANT = os.getenv("EVOCON_TENANT", "")
EVOCON_SECRET = os.getenv("EVOCON_SECRET", "")

# ===== Items to print (rows) =====
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

# For ordering donetime columns in a human way per shift
SHIFT_START = {"A": "06:00", "B": "14:00", "Γ": "22:00"}


# ======================================================
# GLOBAL ERROR HANDLER -> always show real error
# ======================================================
@app.errorhandler(Exception)
def handle_any_exception(e):
    tb = traceback.format_exc()
    return (
        "<pre style='white-space:pre-wrap;font-family:ui-monospace,Consolas;'>"
        f"VERSION: {APP_VERSION}\n\n"
        f"EXCEPTION: {type(e).__name__}: {e}\n\n"
        f"TRACEBACK:\n{tb}\n"
        "</pre>",
        500,
    )


# ======================================================
# BASIC ROUTES
# ======================================================
@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.get("/")
def home():
    return "<a href='/print'>Go to Print</a> | <a href='/health'>Health</a>"


# ======================================================
# HELPERS
# ======================================================
def basic_auth_header():
    if not EVOCON_TENANT or not EVOCON_SECRET:
        raise RuntimeError("Missing EVOCON_TENANT or EVOCON_SECRET (Railway Variables)")

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
    """
    Sort times by "minutes since shift start", so Γ shift (22:00->06:00)
    doesn't sort 00:20 before 23:40.
    """
    start_str = SHIFT_START.get(shift_name, "00:00")
    start_t = parse_hhmm(start_str) or datetime.strptime("00:00", "%H:%M").time()
    start_m = minutes(start_t)

    def key(tstr):
        t = parse_hhmm(tstr) or datetime.strptime("00:00", "%H:%M").time()
        m = minutes(t)
        return (m - start_m) % (24 * 60)

    return sorted(times, key=key)


# ======================================================
# EVOCON API (DATE ONLY: YYYY-MM-DD)
# ======================================================
def fetch_checklists_json(start_date: str, end_date: str):
    """
    Evocon endpoint expects startTime/endTime in DATE-ONLY format:
      YYYY-MM-DD
    """
    url = "https://api.evocon.com/api/reports/checklists_json"
    headers = {"Accept": "application/json", **basic_auth_header()}
    params = {"startTime": start_date, "endTime": end_date}

    r = requests.get(url, headers=headers, params=params, timeout=45)

    if r.status_code != 200:
        raise RuntimeError(
            f"Evocon API ERROR\n"
            f"URL: {url}\n"
            f"PARAMS: {params}\n"
            f"STATUS: {r.status_code}\n"
            f"BODY:\n{(r.text or '')[:1500]}"
        )

    try:
        data = r.json()
    except Exception as e:
        raise RuntimeError(
            f"Evocon returned NON-JSON\n"
            f"ERROR: {e}\n"
            f"BODY:\n{(r.text or '')[:1500]}"
        )

    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected API response type: {type(data)}")

    return data


# ======================================================
# DATA PROCESSING
# ======================================================
def build_shift_index(rows):
    """
    Build unique list of (shiftDate, shift) seen in data.
    Used to populate dropdown. Preselect latest by shiftDate + last donetime.
    """
    idx = {}
    for r in rows:
        sd = str(r.get("shiftDate") or "").strip()
        sh = str(r.get("shift") or "").strip()
        dt = str(r.get("donetime") or "").strip()
        if not (sd and sh and dt):
            continue

        t = parse_hhmm(dt)
        key = (sd, sh)
        if key not in idx:
            idx[key] = {"shiftDate": sd, "shift": sh, "last_time": t}
        else:
            if t and (idx[key]["last_time"] is None or t > idx[key]["last_time"]):
                idx[key]["last_time"] = t

    def sort_key(x):
        d = datetime.strptime(x["shiftDate"], "%Y-%m-%d").date()
        t = x["last_time"] or datetime.min.time()
        return (d, t)

    return sorted(idx.values(), key=sort_key, reverse=True)


def build_report(rows, shiftDate, shiftName):
    """
    We trust Evocon allocation:
      filter strictly by shiftDate + shift
    Then group values by donetime, create dynamic columns.
    """
    filtered = [
        r for r in rows
        if str(r.get("shiftDate") or "").strip() == shiftDate
        and str(r.get("shift") or "").strip() == shiftName
    ]

    submissions = {}  # donetime -> { itemname -> value }
    meta = {}         # donetime -> header info (station/factory/operator/product/order)

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
                "station": str(r.get("station") or "").strip(),
                "factory": str(r.get("factoryName") or "").strip(),
            }

    columns = sort_donetime_list(list(submissions.keys()), shiftName)

    matrix = []
    for item in ORDERED_ITEMS:
        matrix.append({
            "label": item,
            "values": [submissions.get(t, {}).get(item, "") for t in columns]
        })

    header = {"operator": "", "product": "", "productionOrder": "", "station": "", "factory": ""}
    if columns:
        header = meta.get(columns[-1], header)

    return {
        "columns": columns,
        "matrix": matrix,
        "header": header,
        "shiftDate": shiftDate,
        "shift": shiftName,
    }


# ======================================================
# UI ROUTES
# ======================================================
@app.get("/print")
def picker():
    """
    Fetch last 4 days (date-only) to populate shiftDate+shift dropdown.
    """
    today = datetime.now().date()
    start_date = (today - timedelta(days=4)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    rows = fetch_checklists_json(start_date, end_date)
    shifts = build_shift_index(rows)

    if not shifts:
        return "No shifts found in last days."

    return render_template("picker.html", shifts=shifts)


@app.get("/print/render")
def render_print():
    """
    Called with:
      /print/render?key=YYYY-MM-DD|Γ
    We fetch shiftDate -> shiftDate+1 to include rows after midnight for Γ.
    Then filter strictly by shiftDate+shift (Evocon allocation).
    """
    key = request.args.get("key", "")
    parts = key.split("|")
    if len(parts) != 2:
        return "Invalid selection", 400

    shiftDate, shiftName = parts[0].strip(), parts[1].strip()

    d = datetime.strptime(shiftDate, "%Y-%m-%d").date()
    start_date = d.strftime("%Y-%m-%d")
    end_date = (d + timedelta(days=1)).strftime("%Y-%m-%d")

    rows = fetch_checklists_json(start_date, end_date)
    report = build_report(rows, shiftDate, shiftName)

    if not report["columns"]:
        return (
            "<pre>No data found\n"
            f"shiftDate={shiftDate}\nshift={shiftName}\n"
            f"range={start_date} → {end_date}\n"
            f"rows_fetched={len(rows)}</pre>"
        )

    return render_template("print_form.html", **report)
