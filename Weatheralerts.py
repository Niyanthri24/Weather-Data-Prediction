"""Fetch the current weather for each city, store the readings in a CSV,
and send an alert when a city's temperature crosses the limit.

The script runs once and exits. GitHub Actions does the scheduling
(see .github/workflows/weather.yml). It needs no extra packages.

Settings come from environment variables:
  OWM_API_KEY    OpenWeatherMap API key (required)
  ALERT_WEBHOOK  Slack or Discord webhook URL (optional: without it, alerts are only printed)
  CITIES         comma-separated city names
  TEMP_LIMIT_C   alert when a city goes above this temperature, in Celsius
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
FIELDS = ["city", "temperature", "description", "timestamp"]  # temperature is in Kelvin, as in the notebook

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


def last_temperatures_c():
    """The most recent stored temperature for each city, in Celsius."""
    last = {}
    if DATA_FILE.exists():
        with DATA_FILE.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    last[row["city"]] = float(row["temperature"]) - 273.15
                except (KeyError, TypeError, ValueError):
                    continue  # skip a damaged row
    return last


def save(rows):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    is_new = not DATA_FILE.exists()
    with DATA_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def main():
    api_key = os.environ.get("OWM_API_KEY", "").strip()
    webhook = os.environ.get("ALERT_WEBHOOK", "").strip()
    cities = [c.strip() for c in os.environ.get("CITIES", "Kolkata").split(",") if c.strip()]
    limit_c = float(os.environ.get("TEMP_LIMIT_C", "30"))

    if not api_key:
        sys.exit("::error::OWM_API_KEY is not set")

    last = last_temperatures_c()
    rows, problems = [], []

    for city in cities:
        query = urllib.parse.urlencode({"q": city, "appid": api_key})
        try:
            data = json.loads(http(f"{API_URL}?{query}"))
            name = data["name"]
            kelvin = float(data["main"]["temp"])
            description = data["weather"][0]["description"]
        except (RuntimeError, KeyError, IndexError, TypeError, ValueError) as error:
            problems.append(f"{city}: weather fetch failed ({error})")
            continue

        temp_c = kelvin - 273.15
        print(f"{name}: {temp_c:.1f}°C, {description}")

        # Alert only when the city crosses the limit, not on every hot reading.
        # A city with no stored history alerts on its first hot reading.
        crossed = temp_c > limit_c and (name not in last or last[name] <= limit_c)
        if crossed:
            try:
                send_alert(
                    f"🌡️ High temperature in {name}: {temp_c:.1f}°C ({description}). "
                    f"Limit is {limit_c:g}°C.",
                    webhook,
                )
            except RuntimeError as error:
                # Don't store this reading, so the next run sees the crossing again and retries.
                problems.append(f"{name}: alert could not be sent ({error})")
                continue

        last[name] = temp_c
        rows.append({
            "city": name,
            "temperature": kelvin,
            "description": description,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    if rows:
        save(rows)
    print(f"Stored {len(rows)} reading(s) in {DATA_FILE}")

    # Fail the run if anything went wrong, so GitHub shows it as failed and notifies you.
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
