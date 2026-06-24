import base64
import io
import urllib.parse
from datetime import date as _date
from typing import List

from .models import TruckAssignment

try:
    import qrcode as _qrcode
    _QR_AVAILABLE = True
except ImportError:
    _QR_AVAILABLE = False

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Arial, sans-serif; font-size: 13px; color: #111; }

.truck-page { padding: 24px; max-width: 760px; margin: 0 auto; }
@media print {
    .truck-page { page-break-before: always; padding: 16px; }
    .truck-page:first-child { page-break-before: auto; }
}

.header { border-bottom: 3px solid #1a5fa8; padding-bottom: 10px; margin-bottom: 16px; }
.header-title { font-size: 22px; font-weight: bold; color: #1a5fa8; }
.header-meta { font-size: 13px; color: #555; margin-top: 4px; }

.qr-section { display: flex; gap: 24px; margin-bottom: 20px; flex-wrap: wrap; align-items: flex-start; }
.qr-block { text-align: center; }
.qr-label { font-size: 11px; color: #444; margin-bottom: 6px; font-weight: bold;
            text-transform: uppercase; letter-spacing: 0.5px; }
.qr-img { width: 130px; height: 130px; border: 1px solid #ddd; display: block; }
.qr-fallback { font-size: 10px; color: #888; word-break: break-all; max-width: 300px; }

.maps-links { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
.maps-btn { display: inline-block; padding: 4px 10px; border-radius: 4px;
            font-size: 11px; text-decoration: none; font-weight: bold; }
.gmaps-btn { background: #4285f4; color: #fff !important; }
.apple-btn { background: #555; color: #fff !important; display: none; }
@media print { .maps-links { display: none; } }

.stop-nav { margin-left: 8px; white-space: nowrap; }
.stop-nav-btn { text-decoration: none; font-size: 14px; opacity: 0.75; }
.stop-nav-btn:hover { opacity: 1; }
.apple-stop-btn { display: none; }
@media print { .stop-nav { display: none; } }

h2 { font-size: 12px; font-weight: bold; text-transform: uppercase;
     letter-spacing: 0.5px; color: #333; margin-bottom: 10px;
     border-bottom: 1px solid #ddd; padding-bottom: 4px; }

.stops-section { margin-bottom: 20px; }
.stop-card { display: flex; gap: 12px; padding: 8px 0; border-bottom: 1px solid #eee; }
.stop-num { font-size: 18px; font-weight: bold; color: #1a5fa8; min-width: 36px; line-height: 1.2; }
.stop-body { flex: 1; }
.company { font-size: 14px; font-weight: bold; line-height: 1.3; }
.address { font-size: 12px; color: #444; margin-top: 3px; line-height: 1.6; }
.sqft { font-size: 11px; color: #777; margin-top: 2px; }
.stop-notes { font-size: 11px; color: #b85c00; margin-top: 3px; }
.priority-tag { background: #d32f2f; color: #fff; font-size: 10px;
                padding: 1px 6px; border-radius: 3px; margin-left: 6px;
                vertical-align: middle; font-weight: normal; }

.lifo-section { background: #f5f5f5; border: 1px solid #ddd;
                border-radius: 4px; padding: 12px; margin-top: 8px; }
.lifo-section h2 { color: #1a5fa8; border-bottom-color: #bbb; }
.load-row { padding: 5px 0; border-bottom: 1px solid #e0e0e0; font-size: 12px; }
.load-row:last-child { border-bottom: none; }
.load-num { font-weight: bold; color: #1a5fa8; }
"""


def _qr_b64(url: str) -> str:
    if not _QR_AVAILABLE:
        return ""
    img = _qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _maps_url(depot_name: str, stop_addresses: List[str]) -> str:
    valid = [a for a in stop_addresses if a and a.strip()]
    if not valid:
        return ""
    waypoints = [depot_name] + valid
    encoded = "/".join(urllib.parse.quote(w, safe="") for w in waypoints)
    return f"https://www.google.com/maps/dir/{encoded}/"


def _apple_maps_url(stop_addresses: List[str]) -> str:
    """Apple Maps deep link — first stop only; multi-waypoint not supported via URL."""
    valid = [a for a in stop_addresses if a and a.strip()]
    if not valid:
        return ""
    return f"https://maps.apple.com/?daddr={urllib.parse.quote(valid[0], safe='')}&dirflg=d"


def _truck_page_html(assignment: TruckAssignment, depot_name: str, date_str: str) -> str:
    stops = assignment.stops
    MAX_WAYPOINTS = 10

    legs = [stops[i:i + MAX_WAYPOINTS] for i in range(0, len(stops), MAX_WAYPOINTS)]

    qr_html = '<div class="qr-section">'
    for i, leg in enumerate(legs):
        addresses = [s.order.address for s in leg]
        url = _maps_url(depot_name, addresses)
        apple_url = _apple_maps_url(addresses)
        qr_data = _qr_b64(url) if url else ""
        label = (
            f"Leg {i + 1} &nbsp;(Stops {leg[0].stop_number}–{leg[-1].stop_number})"
            if len(legs) > 1
            else "Scan for Google Maps Route"
        )
        if qr_data and url:
            media_html = (
                f'<a href="{url}" target="_blank" rel="noopener">'
                f'<img src="data:image/png;base64,{qr_data}" class="qr-img" alt="QR code">'
                f'</a>'
            )
        elif url:
            media_html = f'<p class="qr-fallback"><a href="{url}">{url}</a></p>'
        else:
            media_html = '<p class="qr-fallback" style="color:#c00">No valid addresses for map link</p>'

        maps_links = ""
        if url:
            maps_links += f'<a href="{url}" class="maps-btn gmaps-btn" target="_blank" rel="noopener">📍 Google Maps</a>'
        if apple_url:
            maps_links += (
                f'<a href="{apple_url}" class="maps-btn apple-btn" target="_blank" rel="noopener" '
                f'title="Apple Maps — first stop only">🍎 Apple Maps</a>'
            )

        qr_html += (
            f'<div class="qr-block">'
            f'<p class="qr-label">{label}</p>'
            f'{media_html}'
            f'<div class="maps-links">{maps_links}</div>'
            f'</div>'
        )
    qr_html += "</div>"

    stops_html = '<div class="stops-section"><h2>Delivery Stops</h2>'
    for stop in stops:
        o = stop.order
        addr_lines = o.address.replace(", ", "<br>")
        pri_html = f'<span class="priority-tag">PRIORITY {o.priority}</span>' if o.priority > 0 else ""
        notes_html = f'<div class="stop-notes">&#9888; {o.notes}</div>' if o.notes else ""
        stop_apple_url = _apple_maps_url([o.address]) if o.address and o.address.strip() else ""
        stop_gmaps_url = _maps_url(depot_name, [o.address]) if o.address and o.address.strip() else ""
        nav_links = ""
        if stop_gmaps_url:
            nav_links += f'<a href="{stop_gmaps_url}" class="stop-nav-btn gmaps-stop-btn" target="_blank" rel="noopener">📍</a>'
        if stop_apple_url:
            nav_links += (
                f'<a href="{stop_apple_url}" class="stop-nav-btn apple-stop-btn" '
                f'target="_blank" rel="noopener">🍎</a>'
            )
        stops_html += (
            f'<div class="stop-card">'
            f'<div class="stop-num">{stop.stop_number}.</div>'
            f'<div class="stop-body">'
            f'<div class="company">{o.customer_name}{pri_html}'
            f'<span class="stop-nav">{nav_links}</span></div>'
            f'<div class="address">{addr_lines}</div>'
            f'<div class="sqft">{o.capacity_units:.0f} sq ft</div>'
            f'{notes_html}'
            f'</div>'
            f'</div>'
        )
    stops_html += "</div>"

    lifo_html = '<div class="lifo-section"><h2>Loading Order — Load #1 first (loads deepest into truck)</h2>'
    for i, stop in enumerate(assignment.load_sequence, 1):
        lifo_html += (
            f'<div class="load-row">'
            f'<span class="load-num">Load {i}:</span> '
            f'{stop.order.customer_name} — {stop.order.capacity_units:.0f} sq ft'
            f'</div>'
        )
    lifo_html += "</div>"

    header_html = (
        f'<div class="header">'
        f'<div class="header-title">Lindsay Windows</div>'
        f'<div class="header-meta">'
        f'{date_str} &nbsp;|&nbsp; {assignment.truck.name} &nbsp;|&nbsp; '
        f'{assignment.utilization_pct:.0f}% utilized &nbsp;|&nbsp; '
        f'{len(stops)} stop{"s" if len(stops) != 1 else ""}'
        f'</div>'
        f'</div>'
    )

    return (
        f'<div class="truck-page">'
        f'{header_html}'
        f'{qr_html}'
        f'{stops_html}'
        f'{lifo_html}'
        f'</div>'
    )


def generate_html_routes(
    assignments: List[TruckAssignment],
    depot_name: str = "Lindsay Windows",
    date_str: str = "",
) -> str:
    """Return a complete printable HTML string with one page per truck.

    Args:
        assignments: Non-empty list of TruckAssignment objects from solve().
        depot_name: First waypoint in Google Maps URLs (plant name or address).
        date_str: ISO date string shown in header; defaults to today.

    Returns:
        UTF-8 HTML string. Pass to st.download_button with mime="text/html".
    """
    if not date_str:
        date_str = _date.today().isoformat()

    pages = "\n".join(
        _truck_page_html(a, depot_name, date_str) for a in assignments
    )

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>Lindsay Windows Route Sheets — {date_str}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{pages}\n"
        "<script>\n"
        "(function(){\n"
        "  var ua=navigator.userAgent;\n"
        "  var isApple=/iPad|iPhone|iPod/.test(ua)||(ua.indexOf('Mac')>-1&&navigator.maxTouchPoints>1);\n"
        "  if(isApple){\n"
        "    document.querySelectorAll('.apple-btn,.apple-stop-btn').forEach(function(b){\n"
        "      b.style.display='inline-block';\n"
        "    });\n"
        "  }\n"
        "})();\n"
        "</script>\n"
        "</body>\n"
        "</html>"
    )
