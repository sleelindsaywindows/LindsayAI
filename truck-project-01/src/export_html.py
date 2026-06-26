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
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
       font-size: 13px; color: #111; }

.truck-page { padding: 24px; max-width: 760px; margin: 0 auto; }
@media print {
    .truck-page { page-break-before: always; padding: 10px; }
    .truck-page:first-child { page-break-before: auto; }
    .header { padding-bottom: 6px; margin-bottom: 10px; }
    .header-title { font-size: 16px; }
    .qr-section { margin-bottom: 10px; }
    .qr-img { width: 90px; height: 90px; }
    .stop-card { padding: 5px 0; }
    .company { font-size: 12px; }
    .address { font-size: 11px; }
    .fv-ids { font-size: 10px; }
    .coords { font-size: 10px; }
    .lifo-section { padding: 8px; margin-top: 4px; }
    .load-row { padding: 3px 0; font-size: 11px; }
    h2 { font-size: 11px; margin-bottom: 6px; padding-bottom: 2px; }
}

/* ── Header: dark navy ── */
.header { background: #1a1a2e; color: #fff; padding: 14px 18px; margin-bottom: 16px;
          border-radius: 6px; display: flex; align-items: flex-start;
          justify-content: space-between; }
.header-left {}
.header-title { font-size: 18px; font-weight: 800; color: #fff; }
.header-meta { font-size: 12px; color: rgba(255,255,255,0.55); margin-top: 4px; }
.header-right { text-align: right; }
.header-driver { font-size: 14px; font-weight: 800; color: #F58220; }
.header-depart { font-size: 11px; color: rgba(255,255,255,0.45); margin-top: 3px; }
.overnight-badge { display: inline-block; background: #fff3cd; color: #7a5700;
                   font-size: 10px; font-weight: 800; padding: 2px 8px;
                   border-radius: 8px; margin-top: 6px; }
@media print { .header { -webkit-print-color-adjust: exact; print-color-adjust: exact; } }

.qr-section { display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; align-items: flex-start; }
.qr-block { text-align: center; }
.qr-label { font-size: 11px; color: #444; margin-bottom: 6px; font-weight: bold;
            text-transform: uppercase; letter-spacing: 0.5px; }
.qr-img { width: 130px; height: 130px; border: 1px solid #ddd; display: block; }
.qr-fallback { font-size: 10px; color: #888; word-break: break-all; max-width: 300px; }

.maps-links { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.maps-btn { display: inline-flex; align-items: center; justify-content: center;
            padding: 10px 16px; min-height: 44px; border-radius: 6px;
            font-size: 13px; text-decoration: none; font-weight: bold; }
.gmaps-btn { background: #4285f4; color: #fff !important; }
.share-btn { background: #1a7cb8; color: #fff; border: none; border-radius: 6px;
             padding: 10px 16px; min-height: 44px; font-size: 13px; font-weight: bold; cursor: pointer; }
.copy-btn  { background: #f4f4f4; color: #333; border: 1px solid #bbb; border-radius: 6px;
             padding: 9px 14px; min-height: 44px; font-size: 12px; font-weight: bold; cursor: pointer; }
@media print { .maps-links { display: none; } }

.stop-nav { margin-left: 6px; white-space: nowrap; }
.stop-nav-btn { text-decoration: none; font-size: 14px; opacity: 0.75; }
.stop-nav-btn:hover { opacity: 1; }
.apple-stop-btn { display: none; }
@media print { .stop-nav { display: none; } }

h2 { font-size: 12px; font-weight: bold; text-transform: uppercase;
     letter-spacing: 0.5px; color: #333; margin-bottom: 10px;
     border-bottom: 1px solid #ddd; padding-bottom: 4px; }

/* ── Stop cards: blue left border + circle number ── */
.stops-section { margin-bottom: 20px; }
.stop-card { display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid #eee;
             border-left: 3px solid #1a7cb8; padding-left: 10px; }
.stop-num { width: 26px; height: 26px; border-radius: 50%; background: #1a7cb8;
            color: #fff; font-size: 11px; font-weight: 800; display: flex;
            align-items: center; justify-content: center; flex-shrink: 0;
            margin-top: 2px; }
.stop-body { flex: 1; }
.stop-header-row { display: flex; align-items: baseline; justify-content: space-between; }
.company { font-size: 14px; font-weight: bold; line-height: 1.3; }
.stop-eta { font-size: 11px; font-weight: 800; color: #1a7cb8; white-space: nowrap;
            margin-left: 8px; }
.address { font-size: 12px; color: #444; margin-top: 3px; line-height: 1.6; }
.fv-ids { font-family: monospace; background: #eef4fb; color: #1a7cb8;
          border-radius: 4px; padding: 1px 6px; font-size: 10px;
          display: inline-block; margin-top: 4px; }
.coords { font-size: 11px; color: #555; margin-top: 2px; font-family: monospace; }
.coords a { color: #1a7cb8; text-decoration: none; }
.stop-notes { font-size: 11px; color: #b85c00; margin-top: 3px; }
.priority-tag { background: #d32f2f; color: #fff; font-size: 10px;
                padding: 1px 6px; border-radius: 3px; margin-left: 6px;
                vertical-align: middle; font-weight: normal; }

/* ── LIFO: orange left border ── */
.lifo-section { background: #fff8f2; border-left: 4px solid #F58220;
                padding: 12px 14px; margin-top: 8px; border-radius: 0 4px 4px 0; }
.lifo-section h2 { color: #F58220; border-bottom-color: #f5ece4; }
.load-row { padding: 5px 0; border-bottom: 1px solid #f5ece4; font-size: 12px; }
.load-row:last-child { border-bottom: none; }
.load-num { font-weight: bold; color: #1a7cb8; }
.load-orders { font-family: monospace; font-size: 10px; color: #1a7cb8;
               background: #eef4fb; border-radius: 3px; padding: 0 4px; margin-left: 4px; }
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


def _truck_page_html(assignment: TruckAssignment, depot_name: str, date_str: str,
                     is_overnight: bool = False) -> str:
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
            leg_label = f"Lindsay Route — {label}" if len(legs) > 1 else f"Lindsay Route {date_str}"
            safe_label = leg_label.replace("'", "\\'")
            safe_url = url.replace("'", "\\'")
            maps_links += f'<a href="{url}" class="maps-btn gmaps-btn" target="_blank" rel="noopener">📍 Open in Maps</a>'
            maps_links += f"<button class='share-btn' onclick=\"shareRoute('{safe_url}','{safe_label}')\">📤 Share Route</button>"
            maps_links += f"<button class='copy-btn'  onclick=\"copyUrl('{safe_url}',this)\">🔗 Copy Link</button>"

        qr_html += (
            f'<div class="qr-block">'
            f'<p class="qr-label">{label}</p>'
            f'{media_html}'
            f'<div class="maps-links">{maps_links}</div>'
            f'</div>'
        )
    qr_html += "</div>"

    # ── Per-stop ETA (linear estimate from 6:30 AM departure) ──
    _route_hrs = getattr(assignment, "route_time_hours", 0.0) or 0.0
    _n_stops = len(stops)
    _depart_minutes = 6 * 60 + 30  # 6:30 AM
    def _eta_str(stop_idx: int) -> str:
        if not _route_hrs or not _n_stops:
            return ""
        mins = _depart_minutes + int(stop_idx / _n_stops * _route_hrs * 60)
        h, m = divmod(mins, 60)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"~{h12}:{m:02d} {suffix}"

    stops_html = '<div class="stops-section"><h2>Delivery Stops</h2>'
    for stop in stops:
        o = stop.order
        addr_lines = o.address.replace(", ", "<br>")
        pri_html = f'<span class="priority-tag">PRIORITY {o.priority}</span>' if o.priority > 0 else ""
        notes_html = f'<div class="stop-notes">{o.notes}</div>' if o.notes else ""
        stop_apple_url = _apple_maps_url([o.address]) if o.address and o.address.strip() else ""
        stop_gmaps_url = _maps_url(depot_name, [o.address]) if o.address and o.address.strip() else ""
        fv_ids = getattr(o, "fenevision_ids", None)
        fv_html = (
            f'<span class="fv-ids">{fv_ids}</span>' if fv_ids else ""
        )
        rname = getattr(o, "route_name", None)
        route_tag_html = (
            f'<span style="display:inline-block;font-size:10px;font-weight:700;'
            f'color:#1a7cb8;background:#eef4fb;border:1px solid #c5ddf5;'
            f'border-radius:10px;padding:1px 8px;margin-left:6px;vertical-align:middle;">'
            f'{rname}</span>'
            if rname else ""
        )
        nav_links = ""
        if stop_gmaps_url:
            nav_links += f'<a href="{stop_gmaps_url}" class="stop-nav-btn gmaps-stop-btn" target="_blank" rel="noopener">📍</a>'
        if stop_apple_url:
            nav_links += (
                f'<a href="{stop_apple_url}" class="stop-nav-btn apple-stop-btn" '
                f'target="_blank" rel="noopener">🍎</a>'
            )
        eta = _eta_str(stop.stop_number - 1)
        eta_html = f'<span class="stop-eta">{eta}</span>' if eta else ""
        stops_html += (
            f'<div class="stop-card">'
            f'<div class="stop-num">{stop.stop_number}</div>'
            f'<div class="stop-body">'
            f'<div class="stop-header-row">'
            f'<div class="company">{o.customer_name}{pri_html}'
            f'{route_tag_html}'
            f'<span class="stop-nav">{nav_links}</span></div>'
            f'{eta_html}'
            f'</div>'
            f'<div class="address">{addr_lines}</div>'
            f'{fv_html}'
            f'{notes_html}'
            f'</div>'
            f'</div>'
        )
    stops_html += "</div>"

    lifo_html = '<div class="lifo-section"><h2>Loading Order — Load #1 first (deepest in truck)</h2>'
    for i, stop in enumerate(assignment.load_sequence, 1):
        fv_ids_lifo = getattr(stop.order, "fenevision_ids", None) or stop.order.order_id
        lifo_html += (
            f'<div class="load-row">'
            f'<span class="load-num">Load {i}:</span> '
            f'{stop.order.customer_name}'
            f'<span class="load-orders">{fv_ids_lifo}</span>'
            f'</div>'
        )
    lifo_html += "</div>"

    driver_label = assignment.truck.driver or "Unassigned"
    emp_badge = (
        ' <span style="background:#fff3e8;color:#b45309;font-size:10px;font-weight:700;'
        'padding:2px 7px;border-radius:10px;">CONTRACT</span>'
        if assignment.truck.employment_type == "contract" else ""
    )
    dist_str = f" &nbsp;·&nbsp; {assignment.route_distance_miles:.0f} mi" if assignment.route_distance_miles else ""
    time_str = f" &nbsp;·&nbsp; ~{_route_hrs:.1f} hr" if _route_hrs else ""
    # Depart / return estimate
    if _route_hrs:
        _ret_m = _depart_minutes + int(_route_hrs * 60)
        _rh, _rm = divmod(_ret_m, 60)
        _rs = "AM" if _rh < 12 else "PM"
        _rh12 = _rh % 12 or 12
        depart_html = (
            f'<div class="header-depart">'
            f'Depart 6:30 AM &rarr; Back ~{_rh12}:{_rm:02d} {_rs}'
            f'</div>'
        )
    else:
        depart_html = ""
    overnight_html = '<div class="overnight-badge">🌙 OVERNIGHT RUN</div>' if is_overnight else ""
    header_html = (
        f'<div class="header">'
        f'<div class="header-left">'
        f'<div class="header-title">🚛 Lindsay Windows</div>'
        f'<div class="header-meta">'
        f'{assignment.truck.name} &nbsp;·&nbsp; '
        f'{len(stops)} stop{"s" if len(stops) != 1 else ""}'
        f'{dist_str}{time_str} &nbsp;·&nbsp; {date_str}'
        f'</div>'
        f'{overnight_html}'
        f'</div>'
        f'<div class="header-right">'
        f'<div class="header-driver">{driver_label}{emp_badge}</div>'
        f'{depart_html}'
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
    is_overnight: bool = False,
) -> str:
    """Return a complete printable HTML string with one page per truck."""
    if not date_str:
        date_str = _date.today().isoformat()

    pages = "\n".join(
        _truck_page_html(a, depot_name, date_str, is_overnight=is_overnight)
        for a in assignments
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
        "    document.querySelectorAll('.apple-stop-btn').forEach(function(b){\n"
        "      b.style.display='inline-block';\n"
        "    });\n"
        "  }\n"
        "})();\n"
        "\n"
        "function shareRoute(url, title) {\n"
        "  if (navigator.share) {\n"
        "    navigator.share({ title: title, url: url }).catch(function(){});\n"
        "  } else {\n"
        "    copyUrlFallback(url);\n"
        "    alert('Link copied to clipboard — paste into Google Maps or send to driver.');\n"
        "  }\n"
        "}\n"
        "\n"
        "function copyUrl(url, btn) {\n"
        "  var orig = btn ? btn.textContent : '';\n"
        "  function done() {\n"
        "    if (btn) { btn.textContent = '\\u2713 Copied!'; setTimeout(function(){ btn.textContent = orig; }, 2000); }\n"
        "  }\n"
        "  if (navigator.clipboard) {\n"
        "    navigator.clipboard.writeText(url).then(done).catch(function(){ copyUrlFallback(url); done(); });\n"
        "  } else { copyUrlFallback(url); done(); }\n"
        "}\n"
        "\n"
        "function copyUrlFallback(url) {\n"
        "  var ta = document.createElement('textarea');\n"
        "  ta.value = url; ta.style.position = 'fixed'; ta.style.opacity = '0';\n"
        "  document.body.appendChild(ta); ta.select();\n"
        "  try { document.execCommand('copy'); } catch(e) {}\n"
        "  document.body.removeChild(ta);\n"
        "}\n"
        "</script>\n"
        "</body>\n"
        "</html>"
    )
