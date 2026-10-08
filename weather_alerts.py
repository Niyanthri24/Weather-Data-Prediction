"""Fetch the current weather for each city, validate it, store it in a CSV,
and send an alert when a city crosses into extreme conditions (heat or storm).

The script runs once and exits. GitHub Actions does the scheduling
(see .github/workflows/weather.yml). It needs no extra packages.

Settings come from environment variables:
  OWM_API_KEY    OpenWeatherMap API key (required)
  ALERT_WEBHOOK  Slack or Discord webhook URL (optional: without it, alerts are only printed)
  CITIES         comma-separated city names
  TEMP_LIMIT_C   heat alert when a city goes above this temperature, in Celsius (default 30)
  WIND_LIMIT_MS  storm alert when the wind reaches this speed, in metres per second
                 (default 17.2, a gale on the Beaufort scale)
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.openweathermap.org/data/2.5/weather"
DATA_FILE = Path("data/weather_data.csv")

# Column order of the CSV. temperature is in Kelvin, as in the original notebook.
# New columns go at the end, so files written by older versions can be upgraded in place.
FIELDS = ["city", "temperature", "description", "timestamp", "condition_id", "wind_speed"]

# OpenWeatherMap condition codes that count as a storm (https://openweathermap.org/weather-conditions):
# every thunderstorm (2xx), heavy or freezing rain, heavy snow, squalls and tornadoes.
STORM_CODES = set(range(200, 300)) | {502, 503, 504, 511, 522, 602, 622, 771, 781}

# Readings outside these ranges are treated as bad data and not stored.
KELVIN_RANGE = (180.0, 340.0)   # about -93°C to 67°C, beyond the most extreme ever recorded
WIND_RANGE = (0.0, 120.0)       # m/s

RETRY_STATUSES = {429, 500, 502, 503, 504}
HINTS = {401: "check OWM_API_KEY", 404: "not found"}


def http(url, payload=None, attempts=3):
    """GET the url, or POST payload to it as JSON. Returns the response text.

    Retries temporary failures. Error messages never include the url,
    because it carries the API key or the webhook address.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": "weather-alerts/1.0"}
    if data:
        headers["Content-Type"] = "application/json"

    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.read().decode()
        except urllib.error.HTTPError as error:
            if error.code not in RETRY_STATUSES or attempt == attempts:
                hint = f", {HINTS[error.code]}" if error.code in HINTS else ""
                raise RuntimeError(f"HTTP {error.code}{hint}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts:
                reason = getattr(error, "reason", error)
                raise RuntimeError(f"network error: {reason}") from None
        time.sleep(2 * attempt)


def alert_payload(webhook, message):
    """Slack expects {"text": ...}; Discord expects {"content": ...}."""
    host = urllib.parse.urlparse(webhook).hostname or ""
    is_discord = host.endswith("discord.com") or host.endswith("discordapp.com")
    return {"content" if is_discord else "text": message}


def send_alert(message, webhook):
    print(f"ALERT: {message}")
    if not webhook:
        print("::warning::ALERT_WEBHOOK is not set, so this alert was only printed.")
        return
    http(webhook, alert_payload(webhook, message))


def is_storm(condition_id, wind_speed, wind_limit):
    return condition_id in STORM_CODES or (wind_speed is not None and wind_speed >= wind_limit)


def parse_reading(data):
    """Pick the fields we keep from an API response and check they make sense.

    Raises ValueError with a short reason when the reading is incomplete or implausible.
    """
    try:
        name = str(data["name"]).strip()
        kelvin = float(data["main"]["temp"])
        description = str(data["weather"][0]["description"]).strip()
        condition_id = int(data["weather"][0]["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError("response is missing city, temperature or conditions") from None
    wind = (data.get("wind") or {}).get("speed")
    wind_speed = float(wind) if wind is not None else None

    if not name:
        raise ValueError("response has no city name")
    if not KELVIN_RANGE[0] <= kelvin <= KELVIN_RANGE[1]:
        raise ValueError(f"implausible temperature {kelvin} K")
    if wind_speed is not None and not WIND_RANGE[0] <= wind_speed <= WIND_RANGE[1]:
        raise ValueError(f"implausible wind speed {wind_speed} m/s")
    return {"city": name, "kelvin": kelvin, "description": description,
            "condition_id": condition_id, "wind_speed": wind_speed}


def read_rows():
    """All stored rows and the header they were written with."""
    if not DATA_FILE.exists():
        return [], None
    with DATA_FILE.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


def last_state(rows, wind_limit):
    """The most recent stored temperature (Celsius) and storm state for each city."""
    last = {}
    for row in rows:  # rows are in time order, so later ones overwrite earlier ones
        try:
            temp_c = float(row["temperature"]) - 273.15
        except (KeyError, TypeError, ValueError):
            continue  # skip a damaged row
        try:
            condition_id = int(row.get("condition_id") or 0)
            wind = row.get("wind_speed")
            wind_speed = float(wind) if wind else None
        except ValueError:
            condition_id, wind_speed = 0, None
        last[row["city"]] = {"temp_c": temp_c, "storm": is_storm(condition_id, wind_speed, wind_limit)}
    return last


def save(new_rows, old_rows, old_header):
    """Append new readings. If the file has an older set of columns, rewrite it with the
    current columns first (old rows get blanks in the new columns); extra columns are kept."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = FIELDS + [c for c in (old_header or []) if c not in FIELDS]
    if old_header is not None and old_header != header:
        with DATA_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header, restval="", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(old_rows)
        print(f"Upgraded {DATA_FILE} to columns: {', '.join(header)}")
    is_new = not DATA_FILE.exists()
    with DATA_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, restval="")
        if is_new:
            writer.writeheader()
        writer.writerows(new_rows)


def main():
    api_key = os.environ.get("OWM_API_KEY", "").strip()
    webhook = os.environ.get("ALERT_WEBHOOK", "").strip()
    cities = [c.strip() for c in os.environ.get("CITIES", "Kolkata").split(",") if c.strip()]
    temp_limit = float(os.environ.get("TEMP_LIMIT_C") or 30)
    wind_limit = float(os.environ.get("WIND_LIMIT_MS") or 17.2)

    if not api_key:
        sys.exit("::error::OWM_API_KEY is not set")

    old_rows, old_header = read_rows()
    last = last_state(old_rows, wind_limit)
    print(f"Loaded {len(old_rows)} stored reading(s); checking {len(cities)} cities "
          f"(heat above {temp_limit:g}°C, storms or wind from {wind_limit:g} m/s)")

    new_rows, problems, seen = [], [], set()
    for city in cities:
        query = urllib.parse.urlencode({"q": city, "appid": api_key})
        try:
            reading = parse_reading(json.loads(http(f"{API_URL}?{query}")))
        except (RuntimeError, ValueError) as error:
            problems.append(f"{city}: reading rejected ({error})")
            continue

        name = reading["city"]
        if name in seen:  # two entries in CITIES resolved to the same place
            print(f"{city}: same place as {name}, already stored this run, skipped")
            continue
        seen.add(name)

        temp_c = reading["kelvin"] - 273.15
        wind = reading["wind_speed"]
        storm = is_storm(reading["condition_id"], wind, wind_limit)
        wind_text = f", wind {wind:.1f} m/s" if wind is not None else ""
        print(f"{name}: {temp_c:.1f}°C, {reading['description']}{wind_text}")

        # Alert only when a city crosses into a condition, not on every reading while it lasts.
        # A city with no stored history alerts on its first extreme reading.
        before = last.get(name)
        messages = []
        if temp_c > temp_limit and (before is None or before["temp_c"] <= temp_limit):
            messages.append(f"🌡️ High temperature in {name}: {temp_c:.1f}°C "
                            f"({reading['description']}). Limit is {temp_limit:g}°C.")
        if storm and (before is None or not before["storm"]):
            messages.append(f"⛈️ Storm conditions in {name}: {reading['description']}{wind_text}.")
        if messages:
            try:
                send_alert("\n".join(messages), webhook)
            except RuntimeError as error:
                # Don't store this reading, so the next run sees the change again and retries.
                problems.append(f"{name}: alert could not be sent ({error})")
                continue

        last[name] = {"temp_c": temp_c, "storm": storm}
        new_rows.append({
            "city": name,
            "temperature": reading["kelvin"],
            "description": reading["description"],
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "condition_id": reading["condition_id"],
            "wind_speed": "" if wind is None else wind,
        })

    if new_rows:
        save(new_rows, old_rows, old_header)
    print(f"Stored {len(new_rows)} new reading(s) in {DATA_FILE}")

    # Fail the run if anything went wrong, so GitHub shows it as failed and notifies you.
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
