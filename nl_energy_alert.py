import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# =========================
# SETTINGS
# =========================

LOW_PRICE = 40
HIGH_PRICE = 50

LOW_PRICE_THRESHOLD = 50
HIGH_PRICE_THRESHOLD = 250
NEGATIVE_PRICE_THRESHOLD = 0

# TEST_MODE options:
# None
# "telegram"
# "current_alert"
# "tomorrow_summary"
TEST_MODE = None

ENTSOE_TOKEN = os.getenv("ENTSOE_TOKEN")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not ENTSOE_TOKEN:
    raise ValueError("ENTSOE_TOKEN is not set")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set")
if not CHAT_ID:
    raise ValueError("CHAT_ID is not set")

STATE_FILE = Path("alert_state.json")

DOMAIN = "10YNL----------L"
API_URL = "https://web-api.tp.entsoe.eu/api"

CONNECT_TIMEOUT = 20
READ_TIMEOUT = 90
MAX_RETRIES = 3

NL_TZ = ZoneInfo("Europe/Amsterdam")

# =========================
# BUILD QUERY WINDOW
# =========================

def build_period_strings():
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=3)
    return start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M")

# =========================
# FETCH DATA FROM ENTSOE
# =========================

def fetch_xml():
    period_start, period_end = build_period_strings()

    params = {
        "securityToken": ENTSOE_TOKEN,
        "documentType": "A44",
        "in_Domain": DOMAIN,
        "out_Domain": DOMAIN,
        "periodStart": period_start,
        "periodEnd": period_end,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Request attempt {attempt}/{MAX_RETRIES}...")

            response = requests.get(
                API_URL,
                params=params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )

            print("HTTP status:", response.status_code)
            response.raise_for_status()

            if not response.text.strip():
                raise ValueError("ENTSO-E returned an empty response.")

            return response.text

        except Exception as e:
            print(f"Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5)

    raise Exception("Failed to retrieve ENTSO-E data")

# =========================
# RESOLUTION HELPER
# =========================

def resolution_to_timedelta(resolution):
    if resolution == "PT60M":
        return timedelta(hours=1)
    if resolution == "PT30M":
        return timedelta(minutes=30)
    if resolution == "PT15M":
        return timedelta(minutes=15)
    raise ValueError(f"Unsupported resolution: {resolution}")

# =========================
# PARSE ALL INTERVALS
# =========================

def parse_all_prices(xml_text):
    root = ET.fromstring(xml_text)
    intervals = []

    for elem in root.iter():
        if not elem.tag.endswith("TimeSeries"):
            continue

        for period in elem.iter():
            if not period.tag.endswith("Period"):
                continue

            start_text = None
            resolution = None

            for child in period:
                if child.tag.endswith("timeInterval"):
                    for t in child:
                        if t.tag.endswith("start"):
                            start_text = t.text
                elif child.tag.endswith("resolution"):
                    resolution = child.text

            if not start_text or not resolution:
                continue

            period_start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            step = resolution_to_timedelta(resolution)

            for point in period:
                if not point.tag.endswith("Point"):
                    continue

                position = None
                price = None

                for pchild in point:
                    if pchild.tag.endswith("position"):
                        position = int(pchild.text)
                    elif pchild.tag.endswith("price.amount"):
                        price = float(pchild.text)

                if position is None or price is None:
                    continue

                interval_start_utc = period_start + (position - 1) * step
                interval_end_utc = interval_start_utc + step

                intervals.append({
                    "start_utc": interval_start_utc,
                    "end_utc": interval_end_utc,
                    "start_local": interval_start_utc.astimezone(NL_TZ),
                    "end_local": interval_end_utc.astimezone(NL_TZ),
                    "price": price,
                })

    intervals.sort(key=lambda x: x["start_utc"])
    return intervals

# =========================
# CURRENT PRICE
# =========================

def get_current_price(intervals):
    now = datetime.now(timezone.utc)

    for item in intervals:
        if item["start_utc"] <= now < item["end_utc"]:
            return item["price"]

    return None

# =========================
# TOMORROW DATA
# =========================

def get_tomorrow_intervals(intervals):
    tomorrow = (datetime.now(NL_TZ) + timedelta(days=1)).date()
    return [x for x in intervals if x["start_local"].date() == tomorrow]

def format_interval(start, end):
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"

def find_low_price_hours(intervals):
    return [x for x in intervals if x["price"] < LOW_PRICE_THRESHOLD]

def find_high_price_hours(intervals):
    return [x for x in intervals if x["price"] > HIGH_PRICE_THRESHOLD]

def find_negative_windows(intervals):
    windows = []
    current = []

    for item in intervals:
        if item["price"] <= NEGATIVE_PRICE_THRESHOLD:
            if not current:
                current = [item]
            else:
                prev = current[-1]
                if item["start_utc"] == prev["end_utc"]:
                    current.append(item)
                else:
                    windows.append(current)
                    current = [item]
        else:
            if current:
                windows.append(current)
                current = []

    if current:
        windows.append(current)

    return windows

# =========================
# BEST WINDOW
# =========================

def find_best_4h_window(intervals):
    # Only consider slots that START between 08:00 and 21:00,
    # so a 4-hour block can still end by 22:00.
    eligible = [
        x for x in intervals
        if 8 <= x["start_local"].hour < 22
    ]

    if len(eligible) < 4:
        return None

    best_window = None
    best_avg = float("inf")

    for i in range(len(eligible) - 3):
        a = eligible[i]
        b = eligible[i + 1]
        c = eligible[i + 2]
        d = eligible[i + 3]

        # Must be 4 consecutive time slots
        if not (
            a["end_utc"] == b["start_utc"] and
            b["end_utc"] == c["start_utc"] and
            c["end_utc"] == d["start_utc"]
        ):
            continue

        # Make sure the whole 4-hour window stays within 08:00–22:00 local time
        if a["start_local"].hour < 8:
            continue
        if d["end_local"].hour > 22 or (d["end_local"].hour == 22 and d["end_local"].minute > 0):
            continue

        avg = (a["price"] + b["price"] + c["price"] + d["price"]) / 4

        if avg < best_avg:
            best_avg = avg
            best_window = (a, b, c, d)

    if best_window is None:
        return None

    return best_window, best_avg

# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    response = requests.post(url, data=payload, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    response.raise_for_status()

# =========================
# STATE
# =========================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# =========================
# TOMORROW SUMMARY
# =========================

def maybe_send_tomorrow_summary(intervals, state):
    if not intervals:
        print("No tomorrow intervals found yet.")
        return

    tomorrow_key = intervals[0]["start_local"].strftime("%Y-%m-%d")

    if state.get("tomorrow_summary_sent_for") == tomorrow_key:
        print("Tomorrow summary already sent.")
        return

    low_hours = find_low_price_hours(intervals)
    high_hours = find_high_price_hours(intervals)
    negative_windows = find_negative_windows(intervals)
    best_window = find_best_4h_window(intervals)

    lines = [f"📅 NL Prices tomorrow ({tomorrow_key})", ""]

    lines.append(f"🔻 Low price hours (< {LOW_PRICE_THRESHOLD})")
    if low_hours:
        for h in low_hours:
            lines.append(
                f"{format_interval(h['start_local'], h['end_local'])} — {h['price']:.2f} EUR/MWh"
            )
    else:
        lines.append("None")
    lines.append("")

    lines.append(f"🔺 High price hours (> {HIGH_PRICE_THRESHOLD})")
    if high_hours:
        for h in high_hours:
            lines.append(
                f"{format_interval(h['start_local'], h['end_local'])} — {h['price']:.2f} EUR/MWh"
            )
    else:
        lines.append("None")
    lines.append("")

    lines.append(f"🟢 Negative price windows (<= {NEGATIVE_PRICE_THRESHOLD})")
    if negative_windows:
        for window in negative_windows:
            start = window[0]["start_local"]
            end = window[-1]["end_local"]
            min_price = min(x["price"] for x in window)
            lines.append(
                f"{format_interval(start, end)} — from {min_price:.2f} EUR/MWh"
            )
    else:
        lines.append("None")

    if best_window:
        window, avg = best_window
        start = window[0]["start_local"]
        end = window[-1]["end_local"]

        lines.append("")
        lines.append("🔋 Best 4-hour charging window")
        lines.append(
            f"{format_interval(start, end)} — avg {avg:.2f} EUR/MWh"
        )

    message = "\n".join(lines)

    send_telegram(message)
    print("Tomorrow summary sent.")

    state["tomorrow_summary_sent_for"] = tomorrow_key

# =========================
# MAIN
# =========================

def main():
    xml_text = fetch_xml()
    intervals = parse_all_prices(xml_text)

    price = get_current_price(intervals)

    if price is None:
        print("No current price found.")
        return

    print(f"Current NL energy price: {price:.2f} EUR/MWh")

    state = load_state()

    was_in_range = state.get("in_range", False)
    in_range = LOW_PRICE <= price <= HIGH_PRICE

    if in_range and not was_in_range:
        message = (
            "⚡ NL Energy Price Alert\n\n"
            f"Price: {price:.2f} EUR/MWh\n"
            f"Target range: {LOW_PRICE}-{HIGH_PRICE}"
        )

        send_telegram(message)
        print("Telegram alert sent.")
    else:
        print("No current-price alert needed.")

    tomorrow_intervals = get_tomorrow_intervals(intervals)
    maybe_send_tomorrow_summary(tomorrow_intervals, state)

    save_state({
        "in_range": in_range,
        "last_price": price,
        "tomorrow_summary_sent_for": state.get("tomorrow_summary_sent_for"),
    })

if __name__ == "__main__":
    main()

