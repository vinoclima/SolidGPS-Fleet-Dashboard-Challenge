"""
fleet_dashboard_generator.py
=============================
Reads fleet_status.csv and produces a single self-contained fleet_dashboard.html.

Author  : Mervin T. Oclima
Purpose : SolidGPS Fleet Dashboard Challenge

How it works:
  1. Read and validate every row in the CSV
  2. Flag dirty / anomalous records instead of silently dropping them
  3. Build a self-contained HTML file with:
       - Summary cards  (count per status)
       - Interactive map (Leaflet.js via CDN embedded as inline script)
       - Device list     (sortable, colour-coded)
  4. Write everything to fleet_dashboard.html — no external files needed

Standard library only — no pandas, folium, requests, or other third-party packages.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

INPUT_FILE  = "fleet_status.csv"
OUTPUT_FILE = "fleet_dashboard.html"

# Colour scheme — used on the map markers and status badges
STATUS_COLOURS = {
    "active":      "#22c55e",   # green
    "idle":        "#f59e0b",   # amber
    "offline":     "#ef4444",   # red
    "low_battery": "#f97316",   # orange
    "maintenance": "#8b5cf6",   # purple
    "unknown":     "#6b7280",   # grey  (catch-all for unexpected values)
}

# Battery % thresholds
BATTERY_MIN = 0
BATTERY_MAX = 100

# How we decide a timestamp is "in the future" — anything after right now
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# STEP 1 — READ AND VALIDATE THE CSV
# ---------------------------------------------------------------------------

def parse_csv(filepath):
    """
    Read fleet_status.csv row by row.

    For each row we:
      - Check required fields are present
      - Validate lat/lon are real numbers and within Australia's bounding box
      - Clamp battery to 0-100 (handles the 150% anomaly)
      - Flag future timestamps as anomalies but still include the device
      - Assign status = 'unknown' for unrecognised status values

    Returns two lists:
      devices   — clean-enough records ready for the dashboard
      anomalies — human-readable warnings shown at the bottom of the page
    """
    devices   = []
    anomalies = []

    # Australia bounding box (rough) — used to catch nonsense coordinates
    AUS_LAT = (-44.0, -10.0)
    AUS_LON = (113.0, 154.0)

    required_fields = ["device_id", "lat", "lon", "last_seen"]

    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row_num, row in enumerate(reader, start=2):   # start=2 because row 1 is the header

            device_id = row.get("device_id", "").strip()
            name      = row.get("name",      "").strip() or device_id   # fall back to ID if name missing
            status    = row.get("status",    "").strip().lower()
            location  = row.get("location",  "").strip()
            warnings  = []   # collect all issues found in this row

            # ── Required field check ──────────────────────────────────────
            missing = [f for f in required_fields if not row.get(f, "").strip()]
            if missing:
                anomalies.append(
                    f"Row {row_num} ({device_id or 'NO ID'}): missing required fields: {', '.join(missing)} — skipped."
                )
                continue   # cannot plot a device without coordinates or ID

            # ── Latitude / longitude ──────────────────────────────────────
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except ValueError:
                anomalies.append(
                    f"{device_id}: invalid coordinates (lat='{row['lat']}', lon='{row['lon']}') — skipped."
                )
                continue

            if not (AUS_LAT[0] <= lat <= AUS_LAT[1]) or not (AUS_LON[0] <= lon <= AUS_LON[1]):
                anomalies.append(
                    f"{device_id}: coordinates ({lat}, {lon}) are outside Australia's bounding box — skipped."
                )
                continue

            # ── Battery ───────────────────────────────────────────────────
            try:
                battery = int(float(row.get("battery_pct", 0)))
            except ValueError:
                battery = 0
                warnings.append("battery value unreadable — defaulted to 0")

            if battery < BATTERY_MIN:
                warnings.append(f"battery {battery}% is below 0 — clamped to 0")
                battery = BATTERY_MIN
            elif battery > BATTERY_MAX:
                warnings.append(f"battery {battery}% exceeds 100 — clamped to 100")
                battery = BATTERY_MAX

            # ── Status ────────────────────────────────────────────────────
            if status not in STATUS_COLOURS:
                warnings.append(f"unrecognised status '{status}' — shown as 'unknown'")
                status = "unknown"

            # ── Last seen timestamp ───────────────────────────────────────
            last_seen_raw = row.get("last_seen", "").strip()
            try:
                last_seen_dt = datetime.strptime(last_seen_raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                anomalies.append(f"{device_id}: unparseable timestamp '{last_seen_raw}' — skipped.")
                continue

            if last_seen_dt > NOW:
                warnings.append(f"last_seen '{last_seen_raw}' is in the future — displayed as-is")

            # How long ago was the device last seen?
            delta_seconds = max(0, int((NOW - last_seen_dt).total_seconds()))
            last_seen_ago = format_time_ago(delta_seconds)

            # ── Record any warnings against this device ───────────────────
            if warnings:
                anomalies.append(f"{device_id} ({name}): " + "; ".join(warnings) + ".")

            devices.append({
                "device_id":    device_id,
                "name":         name,
                "status":       status,
                "battery":      battery,
                "lat":          lat,
                "lon":          lon,
                "last_seen":    last_seen_raw,
                "last_seen_ago": last_seen_ago,
                "location":     location,
                "colour":       STATUS_COLOURS[status],
            })

    return devices, anomalies


def format_time_ago(seconds):
    """Convert a number of seconds into a human-readable 'X ago' string."""
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"


# ---------------------------------------------------------------------------
# STEP 2 — SUMMARISE BY STATUS
# ---------------------------------------------------------------------------

def build_summary(devices):
    """
    Count devices per status.
    Returns a dict like: { 'active': 10, 'idle': 4, ... }
    """
    summary = {status: 0 for status in STATUS_COLOURS}
    for device in devices:
        summary[device["status"]] = summary.get(device["status"], 0) + 1
    # Remove zero-count statuses for a cleaner display
    return {k: v for k, v in summary.items() if v > 0}


# ---------------------------------------------------------------------------
# STEP 3 — BUILD THE HTML
# ---------------------------------------------------------------------------

def build_html(devices, summary, anomalies):
    """
    Assemble the complete self-contained HTML string.

    Structure:
      <head>  — inline CSS + Leaflet loaded from CDN (one external call only)
      <body>
        ├── Header
        ├── Summary cards
        ├── Map          (Leaflet — all marker data injected as inline JSON)
        ├── Device table
        └── Anomalies    (only shown if dirty records were found)
    """

    # Serialise device data to JSON so JavaScript can read it directly
    devices_json = json.dumps(devices, indent=2)

    # Build summary card HTML
    summary_cards_html = ""
    labels = {
        "active":      "Active",
        "idle":        "Idle",
        "offline":     "Offline",
        "low_battery": "Low Battery",
        "maintenance": "Maintenance",
        "unknown":     "Unknown",
    }
    for status, count in summary.items():
        colour = STATUS_COLOURS.get(status, "#6b7280")
        label  = labels.get(status, status.title())
        summary_cards_html += f"""
        <div class="summary-card" style="border-top: 4px solid {colour};">
            <div class="summary-count" style="color:{colour};">{count}</div>
            <div class="summary-label">{label}</div>
        </div>"""

    # Build device table rows
    table_rows_html = ""
    for d in sorted(devices, key=lambda x: x["status"]):
        colour  = d["colour"]
        battery = d["battery"]

        # Battery bar colour — red below 20%, amber below 40%, green above
        if battery <= 20:
            bar_colour = "#ef4444"
        elif battery <= 40:
            bar_colour = "#f59e0b"
        else:
            bar_colour = "#22c55e"

        table_rows_html += f"""
        <tr>
            <td><strong>{d['device_id']}</strong><br>
                <span class="device-name">{d['name']}</span></td>
            <td><span class="badge" style="background:{colour};">{d['status'].replace('_',' ').title()}</span></td>
            <td>
                <div class="battery-bar-wrap">
                    <div class="battery-bar" style="width:{battery}%; background:{bar_colour};"></div>
                </div>
                <span class="battery-pct">{battery}%</span>
            </td>
            <td>{d['last_seen_ago']}</td>
            <td>{d['location']}</td>
        </tr>"""

    # Build anomalies section (only rendered if there are issues)
    anomalies_html = ""
    if anomalies:
        items = "".join(f"<li>{a}</li>" for a in anomalies)
        anomalies_html = f"""
        <div class="anomalies">
            <h2>⚠️ Data Anomalies ({len(anomalies)} found)</h2>
            <p>The following records had missing, invalid, or unexpected values.
               They were either corrected automatically or skipped.</p>
            <ul>{items}</ul>
        </div>"""

    total = len(devices)
    generated_at = NOW.strftime("%d %b %Y %H:%M UTC")

    # ------------------------------------------------------------------
    # Full HTML template
    # Leaflet.js is loaded from CDN — this is the only external dependency.
    # All map data is injected inline as a JavaScript variable.
    # ------------------------------------------------------------------
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>Fleet Dashboard — SolidGPS</title>

    <!-- Leaflet CSS — loaded from CDN, no local files needed -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>

    <style>
        /* ── Reset & base ── */
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f1f5f9;
            color: #1e293b;
            font-size: 15px;
        }}

        /* ── Header ── */
        .header {{
            background: #0f172a;
            color: #fff;
            padding: 1rem 2rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .header h1 {{ font-size: 1.3rem; font-weight: 600; letter-spacing: -0.3px; }}
        .header .meta {{ font-size: 13px; color: #94a3b8; }}

        /* ── Main layout ── */
        .container {{ max-width: 1400px; margin: 0 auto; padding: 1.5rem 2rem; }}

        /* ── Summary cards ── */
        .summary-grid {{
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
            margin-bottom: 1.5rem;
        }}
        .summary-card {{
            background: #fff;
            border-radius: 10px;
            padding: 1rem 1.5rem;
            min-width: 130px;
            box-shadow: 0 1px 3px rgba(0,0,0,.07);
        }}
        .summary-count {{ font-size: 2rem; font-weight: 700; line-height: 1; }}
        .summary-label {{ font-size: 13px; color: #64748b; margin-top: 4px; }}

        /* ── Map ── */
        .map-wrap {{
            background: #fff;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,.07);
            margin-bottom: 1.5rem;
        }}
        #map {{ height: 480px; width: 100%; }}

        /* ── Device table ── */
        .table-wrap {{
            background: #fff;
            border-radius: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,.07);
            overflow: hidden;
            margin-bottom: 1.5rem;
        }}
        .table-header {{
            padding: .85rem 1.25rem;
            border-bottom: 1px solid #e2e8f0;
            font-weight: 600;
            font-size: 15px;
        }}
        table {{ width: 100%; border-collapse: collapse; }}
        thead th {{
            text-align: left;
            padding: .65rem 1.25rem;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: .5px;
            color: #64748b;
            background: #f8fafc;
            border-bottom: 1px solid #e2e8f0;
        }}
        tbody tr {{ border-bottom: 1px solid #f1f5f9; transition: background .1s; }}
        tbody tr:hover {{ background: #f8fafc; }}
        tbody td {{ padding: .7rem 1.25rem; vertical-align: middle; }}
        .device-name {{ font-size: 13px; color: #64748b; }}

        /* ── Badge ── */
        .badge {{
            display: inline-block;
            padding: 3px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            color: #fff;
            white-space: nowrap;
        }}

        /* ── Battery bar ── */
        .battery-bar-wrap {{
            background: #e2e8f0;
            border-radius: 4px;
            height: 6px;
            width: 80px;
            display: inline-block;
            vertical-align: middle;
            margin-right: 6px;
        }}
        .battery-bar {{
            height: 6px;
            border-radius: 4px;
            transition: width .3s;
        }}
        .battery-pct {{ font-size: 13px; color: #475569; }}

        /* ── Anomalies ── */
        .anomalies {{
            background: #fffbeb;
            border: 1px solid #fcd34d;
            border-radius: 10px;
            padding: 1.25rem 1.5rem;
            margin-bottom: 1.5rem;
        }}
        .anomalies h2 {{ font-size: 15px; font-weight: 600; margin-bottom: .5rem; }}
        .anomalies p  {{ font-size: 13px; color: #92400e; margin-bottom: .75rem; }}
        .anomalies ul {{ padding-left: 1.25rem; }}
        .anomalies li {{ font-size: 13px; color: #78350f; margin-bottom: 4px; }}

        /* ── Footer ── */
        .footer {{
            text-align: center;
            font-size: 12px;
            color: #94a3b8;
            padding: 1rem 0 2rem;
        }}
    </style>
</head>
<body>

<!-- ── HEADER ── -->
<div class="header">
    <h1>🛰️ SolidGPS — Fleet Dashboard</h1>
    <div class="meta">{total} devices &nbsp;·&nbsp; Generated {generated_at}</div>
</div>

<div class="container">

    <!-- ── SUMMARY CARDS ── -->
    <div class="summary-grid">
        {summary_cards_html}
    </div>

    <!-- ── MAP ── -->
    <div class="map-wrap">
        <div id="map"></div>
    </div>

    <!-- ── DEVICE TABLE ── -->
    <div class="table-wrap">
        <div class="table-header">Device List</div>
        <table>
            <thead>
                <tr>
                    <th>Device</th>
                    <th>Status</th>
                    <th>Battery</th>
                    <th>Last Seen</th>
                    <th>Location</th>
                </tr>
            </thead>
            <tbody>
                {table_rows_html}
            </tbody>
        </table>
    </div>

    <!-- ── ANOMALIES (only shown when dirty records exist) ── -->
    {anomalies_html}

</div>

<div class="footer">
    Fleet Dashboard · Generated {generated_at} · SolidGPS
</div>

<!-- ── LEAFLET JS — loaded from CDN ── -->
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<script>
    // ── All device data injected directly from Python ──
    // This means the HTML file is fully self-contained after generation.
    const devices = {devices_json};

    // ── Colour map mirrors Python STATUS_COLOURS ──
    const STATUS_COLOURS = {json.dumps(STATUS_COLOURS)};

    // ── Initialise map centred on Australia ──
    const map = L.map('map').setView([-27.0, 134.0], 5);

    // OpenStreetMap tiles — free, no API key required
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        maxZoom: 18
    }}).addTo(map);

    // ── Plot each device as a coloured circle marker ──
    devices.forEach(function(device) {{

        // Circle colour comes from the device's status
        const colour = STATUS_COLOURS[device.status] || '#6b7280';

        const marker = L.circleMarker([device.lat, device.lon], {{
            radius:      9,
            fillColor:   colour,
            color:       '#fff',        // white border makes markers stand out on the map
            weight:      2,
            opacity:     1,
            fillOpacity: 0.9
        }});

        // Popup shown when the marker is clicked
        marker.bindPopup(`
            <strong>${{device.name}}</strong><br>
            <span style="color:${{colour}};">● ${{device.status.replace('_',' ')}}</span><br>
            Battery: ${{device.battery}}%<br>
            Last seen: ${{device.last_seen_ago}}<br>
            Location: ${{device.location}}
        `);

        marker.addTo(map);
    }});
</script>

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# STEP 4 — MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    # Check the input file exists before doing anything
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: '{INPUT_FILE}' not found. Place it in the same folder as this script.")
        sys.exit(1)

    print(f"Reading {INPUT_FILE}...")
    devices, anomalies = parse_csv(INPUT_FILE)

    if not devices:
        print("ERROR: No valid device records found. Check the CSV and try again.")
        sys.exit(1)

    print(f"  {len(devices)} valid devices loaded.")
    if anomalies:
        print(f"  {len(anomalies)} anomalies detected — they will be shown in the dashboard.")

    print("Building summary...")
    summary = build_summary(devices)

    print("Generating HTML...")
    html = build_html(devices, summary, anomalies)

    print(f"Writing {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Done. Open '{OUTPUT_FILE}' in any browser.")
    print(f"   {len(devices)} devices plotted · {len(anomalies)} anomalies flagged.")


if __name__ == "__main__":
    main()
