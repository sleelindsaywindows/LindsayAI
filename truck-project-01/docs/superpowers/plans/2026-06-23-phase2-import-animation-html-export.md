# Phase 2 — Import UI, Animation, HTML Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the terminal script requirement by adding in-app FeneVision xlsx upload, replace the .txt export with a printable HTML route sheet with QR codes, add a CSS animation during solve, and add a first-time onboarding modal.

**Architecture:** All UI changes live in `app.py`. The HTML export is a new standalone module `src/export_html.py` with no Streamlit dependency. The import rename + pattern exclusion change touches `src/import_fenevision.py`, `config.yaml`, and `scripts/verify_617.py`.

**Tech Stack:** Streamlit ≥ 1.40 (st.dialog, st.empty), OR-Tools, qrcode[pil] ≥ 7.4.2, openpyxl, pandas, PyYAML

## Global Constraints

- `streamlit>=1.40.0` — `st.dialog()` requires ≥ 1.32; we have ≥ 1.40 so it's available
- `pyarrow>=10.0.1,<19` — do not bump; pyarrow 19+ crashes ortools on macOS
- `qrcode[pil]>=7.4.2` — must include `[pil]` extra for PNG support
- Function name is `import_fenevision_xlsx` throughout — never `import_geoffs_xlsx` or `import_fenevision_ga_trucks`
- `exclude_route_patterns` is a list of substrings (case-insensitive); empty list = no exclusions
- No raw Google Maps URLs anywhere in driver-facing output
- Every `st.download_button` must have an explicit `key=` parameter
- Never commit `.env` or `.streamlit/config.toml`

---

### Task 1: Rename import_geoffs_xlsx → import_fenevision_xlsx + pattern-based exclusion

**Files:**
- Modify: `src/import_fenevision.py:24-123`
- Modify: `config.yaml:99-102`
- Modify: `app.py:107-113` (save_config routing block in render_sidebar)
- Modify: `scripts/verify_617.py:22,109`

**Interfaces:**
- Produces: `import_fenevision_xlsx(xlsx_path, sheet_name, exclude_route_patterns, min_sqft) -> (List[Order], List[dict], List[str])`
  - Third return value is `excluded_route_names: List[str]` — unique RouteName strings that were filtered

- [ ] **Step 1: Update src/import_fenevision.py**

Replace the entire `import_geoffs_xlsx` function (lines 24–123) with:

```python
def import_fenevision_xlsx(
    xlsx_path,
    sheet_name: str = "Orders by Route",
    exclude_route_patterns: Optional[List[str]] = None,
    min_sqft: float = 0.01,
) -> Tuple[List[Order], List[dict], List[str]]:
    """
    Import a FeneVision xlsx export (Orders by Route format) and return
    one Order per delivery stop (aggregated from line-item rows).

    Each stop = unique (RouteID, Stop, shpaddr_companyname) combination.
    sqftShippedQty (pre-calculated by FeneVision) is summed across all line items.
    Truck type restriction is read from TruckTypeDesc and stored in allowed_truck_types.

    Args:
        xlsx_path: Path string or file-like object for the xlsx file.
        sheet_name: Sheet with order line items (default "Orders by Route").
        exclude_route_patterns: Substrings matched case-insensitively against RouteName.
            Any route whose name contains a pattern is excluded. Empty list = no exclusions.
        min_sqft: Stops with total sqft below this are skipped (placeholder stops).

    Returns:
        (orders, skipped, excluded_route_names)
        - orders: one Order per stop
        - skipped: list of dicts describing stops excluded for low sqft
        - excluded_route_names: unique RouteName values removed by pattern matching
    """
    df = pd.read_excel(xlsx_path, sheet_name=sheet_name, header=0)

    required = {
        "RouteID", "RouteName", "Stop",
        "shpaddr_companyname", "ShpAddr_Address1", "ShpAddr_City",
        "ShpAddr_State", "ShpAddr_ZipCode",
        "sqftShippedQty",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"xlsx sheet '{sheet_name}' missing columns: {missing}")

    excluded_route_names: List[str] = []
    if exclude_route_patterns:
        patterns_lower = [p.lower() for p in exclude_route_patterns]
        mask = df["RouteName"].apply(
            lambda name: any(p in str(name).lower() for p in patterns_lower)
        )
        excluded_route_names = list(df.loc[mask, "RouteName"].dropna().unique())
        df = df[~mask]

    stop_df = (
        df.groupby(
            ["RouteID", "RouteName", "Stop",
             "shpaddr_companyname", "ShpAddr_Address1",
             "ShpAddr_City", "ShpAddr_State", "ShpAddr_ZipCode"],
            dropna=False,
        )
        .agg(
            sqft_total=("sqftShippedQty", "sum"),
            truck_type_desc=("TruckTypeDesc", "first"),
        )
        .reset_index()
    )

    orders = []
    skipped = []

    for _, row in stop_df.iterrows():
        sqft = float(row["sqft_total"]) if pd.notna(row["sqft_total"]) else 0.0

        if sqft < min_sqft:
            skipped.append({
                "route": row["RouteName"],
                "stop": row["Stop"],
                "customer": row["shpaddr_companyname"],
                "sqft": sqft,
                "reason": "sqft below threshold",
            })
            continue

        parts = [
            str(row["ShpAddr_Address1"]).strip(),
            str(row["ShpAddr_City"]).strip(),
            str(row["ShpAddr_State"]).strip(),
            str(row["ShpAddr_ZipCode"]).strip(),
        ]
        address = " ".join(p for p in parts if p and p.lower() != "nan")

        order_id = f"R{int(row['RouteID'])}-S{int(row['Stop']):02d}"
        if any(o.order_id == order_id for o in orders):
            suffix = sum(1 for o in orders if o.order_id.startswith(order_id))
            order_id = f"{order_id}-{suffix}"

        truck_type = _fenevision_truck_type(row.get("truck_type_desc"))
        allowed = [truck_type] if truck_type else None

        orders.append(Order(
            order_id=order_id,
            customer_name=str(row["shpaddr_companyname"]).strip(),
            address=address,
            capacity_units=round(sqft, 3),
            priority=0,
            notes=str(row["RouteName"]),
            allowed_truck_types=allowed,
        ))

    return orders, skipped, excluded_route_names
```

Also update the docstring of `import_fenevision` (the CSV function, line 126) to reference `import_fenevision_xlsx` instead of `import_geoffs_xlsx`.

- [ ] **Step 2: Add exclude_route_patterns to config.yaml**

In `config.yaml`, replace the routing block (lines 99–102):

```yaml
routing:
  max_route_miles: 600          # extended for NC/SC routes; confirm real HOS cap with Joseph
  solver_time_limit_seconds: 30
  exclude_route_patterns: []    # substrings matched case-insensitively against RouteName; empty = no exclusions
```

- [ ] **Step 3: Preserve exclude_route_patterns in render_sidebar save_config**

In `app.py`, both `save_config` call sites in `render_sidebar` (around lines 99 and 107) build a `routing` dict. Add the missing key so it isn't dropped on save:

```python
"routing": {
    "max_route_miles": max_miles,
    "solver_time_limit_seconds": routing.get("solver_time_limit_seconds", 15),
    "exclude_route_patterns": routing.get("exclude_route_patterns", []),
},
```

Apply this to BOTH the `if st.sidebar.button("＋ Add Truck")` block (line ~101) and the `if st.sidebar.button("Save Config")` block (line ~111).

- [ ] **Step 4: Update scripts/verify_617.py**

Change line 22:
```python
from src.import_fenevision import import_fenevision_xlsx
```

Change `EXCLUDE_ROUTES` constant and usage (lines 26–27):
```python
EXCLUDE_PATTERNS = ["MO"]   # matches any RouteName containing "MO" (interplant transfers)
```

Change line 109:
```python
orders, skipped, excluded = import_fenevision_xlsx(XLSX_PATH, exclude_route_patterns=EXCLUDE_PATTERNS)
print(f"Loaded {len(orders)} stops ({len(skipped)} skipped). Excluded routes: {excluded}.")
```

Also update the `_preflight_checks` call at line 113 (the function checks `exclude_routes` against route names — rename the parameter and update the check to use pattern matching):

In `_preflight_checks`, change the signature and the validation block (lines 68–81):
```python
def _preflight_checks(orders, trucks, xlsx_path, exclude_patterns):
    # ... (keep all existing checks) ...

    # 5. Check exclude_patterns match at least one route
    try:
        import pandas as pd
        df = pd.read_excel(xlsx_path, sheet_name="Orders by Route", header=0)
        route_names = list(df["RouteName"].dropna().unique())
        for pat in (exclude_patterns or []):
            matched = [r for r in route_names if pat.lower() in r.lower()]
            if not matched:
                warnings.append(
                    f"exclude_route_patterns entry '{pat}' matched no RouteName in xlsx "
                    f"(possible typo). Available routes: {sorted(route_names)}"
                )
    except Exception as e:
        warnings.append(f"Could not verify exclude_route_patterns against xlsx: {e}")
```

And update the call at line 113:
```python
errors = _preflight_checks(orders, trucks, XLSX_PATH, EXCLUDE_PATTERNS)
```

- [ ] **Step 5: Smoke test**

```bash
cd /Users/peytonbaker/Desktop/LindsayAI/truck-project-01
python scripts/verify_617.py
```

Expected output contains:
```
Loaded 50 stops (0 skipped). Excluded routes: ["6/17 Lindsay MO 53'"].
Optimizer used 6 trucks. Joseph used 13.
Homebuilder constraint: PASS
Dropped orders: 0
```

- [ ] **Step 6: Commit**

```bash
git add src/import_fenevision.py config.yaml scripts/verify_617.py app.py
git commit -m "feat: rename import_geoffs_xlsx → import_fenevision_xlsx, add pattern-based route exclusion"
```

---

### Task 2: HTML export module + qrcode dependency

**Files:**
- Modify: `requirements.txt`
- Create: `src/export_html.py`

**Interfaces:**
- Produces: `generate_html_routes(assignments, depot_name, date_str) -> str`
  - `assignments: List[TruckAssignment]`
  - `depot_name: str` — appears in Google Maps URL as first waypoint
  - `date_str: str` — ISO date string, e.g. `"2026-06-23"`, defaults to today if empty
  - Returns: complete HTML string, UTF-8 safe, no external dependencies at print time

- [ ] **Step 1: Add qrcode[pil] to requirements.txt**

Add after `openpyxl>=3.1.0`:
```
qrcode[pil]>=7.4.2
```

- [ ] **Step 2: Install the new dependency**

```bash
pip install "qrcode[pil]>=7.4.2"
```

Expected: `Successfully installed qrcode-...`

- [ ] **Step 3: Create src/export_html.py**

```python
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

.qr-section { display: flex; gap: 24px; margin-bottom: 20px; flex-wrap: wrap; }
.qr-block { text-align: center; }
.qr-label { font-size: 11px; color: #444; margin-bottom: 6px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.5px; }
.qr-img { width: 120px; height: 120px; border: 1px solid #ddd; }
.qr-fallback { font-size: 10px; color: #888; word-break: break-all; max-width: 300px; }

h2 { font-size: 13px; font-weight: bold; text-transform: uppercase;
     letter-spacing: 0.5px; color: #333; margin-bottom: 10px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }

.stops-section { margin-bottom: 20px; }
.stop-card { display: flex; gap: 12px; align-items: baseline;
             padding: 8px 0; border-bottom: 1px solid #eee; }
.stop-num { font-size: 16px; font-weight: bold; color: #1a5fa8; min-width: 44px; }
.stop-body { flex: 1; }
.company { font-size: 14px; font-weight: bold; }
.address { font-size: 12px; color: #444; margin-top: 2px; line-height: 1.5; }
.sqft { font-size: 11px; color: #777; margin-top: 2px; }
.stop-notes { font-size: 11px; color: #b85c00; margin-top: 3px; }
.priority-tag { background: #d32f2f; color: #fff; font-size: 10px;
                padding: 1px 5px; border-radius: 3px; margin-left: 6px; vertical-align: middle; }

.lifo-section { background: #f5f5f5; border: 1px solid #ddd;
                border-radius: 4px; padding: 12px; margin-top: 8px; }
.lifo-section h2 { color: #1a5fa8; border-bottom-color: #bbb; }
.load-row { padding: 4px 0; border-bottom: 1px solid #e0e0e0; font-size: 12px; }
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
    waypoints = [depot_name] + stop_addresses
    encoded = "/".join(urllib.parse.quote(w, safe="") for w in waypoints)
    return f"https://www.google.com/maps/dir/{encoded}/"


def _truck_page_html(assignment: TruckAssignment, depot_name: str, date_str: str) -> str:
    stops = assignment.stops
    MAX_WAYPOINTS = 10

    # Split into legs if stop count exceeds Google Maps limit
    legs = [stops[i:i + MAX_WAYPOINTS] for i in range(0, len(stops), MAX_WAYPOINTS)]

    qr_html = '<div class="qr-section">'
    for i, leg in enumerate(legs):
        addresses = [s.order.address for s in leg]
        url = _maps_url(depot_name, addresses)
        qr_data = _qr_b64(url)
        if len(legs) > 1:
            label = f"Leg {i + 1} &nbsp;(Stops {leg[0].stop_number}–{leg[-1].stop_number})"
        else:
            label = "Scan for Google Maps Route"
        if qr_data:
            qr_html += (
                f'<div class="qr-block">'
                f'<p class="qr-label">{label}</p>'
                f'<img src="data:image/png;base64,{qr_data}" class="qr-img" alt="QR code">'
                f'</div>'
            )
        else:
            qr_html += (
                f'<div class="qr-block">'
                f'<p class="qr-label">{label}</p>'
                f'<p class="qr-fallback">{url}</p>'
                f'</div>'
            )
    qr_html += "</div>"

    stops_html = '<div class="stops-section"><h2>Delivery Stops</h2>'
    for stop in stops:
        o = stop.order
        addr_lines = o.address.replace(", ", "<br>")
        pri_html = f'<span class="priority-tag">PRIORITY {o.priority}</span>' if o.priority > 0 else ""
        notes_html = f'<div class="stop-notes">&#9888; {o.notes}</div>' if o.notes else ""
        stops_html += (
            f'<div class="stop-card">'
            f'<div class="stop-num">{stop.stop_number}.</div>'
            f'<div class="stop-body">'
            f'<div class="company">{o.customer_name}{pri_html}</div>'
            f'<div class="address">{addr_lines}</div>'
            f'<div class="sqft">{o.capacity_units:.0f} sq ft</div>'
            f'{notes_html}'
            f'</div>'
            f'</div>'
        )
    stops_html += "</div>"

    lifo_html = '<div class="lifo-section"><h2>Loading Order — Load #1 first (goes in deepest)</h2>'
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
        f'{len(stops)} stops'
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
    """Return a complete printable HTML string — one page per truck.

    Args:
        assignments: Non-empty list of TruckAssignment objects from solve().
        depot_name: First waypoint in Google Maps URLs (plant name or address).
        date_str: ISO date string shown in header; defaults to today.

    Returns:
        UTF-8 HTML string. Embed in st.download_button with mime="text/html".
    """
    if not date_str:
        date_str = _date.today().isoformat()

    pages = "\n".join(
        _truck_page_html(a, depot_name, date_str) for a in assignments
    )

    return (
        f'<!DOCTYPE html>\n'
        f'<html lang="en">\n'
        f'<head>\n'
        f'<meta charset="UTF-8">\n'
        f'<title>Lindsay Windows Route Sheets — {date_str}</title>\n'
        f'<style>{_CSS}</style>\n'
        f'</head>\n'
        f'<body>\n'
        f'{pages}\n'
        f'</body>\n'
        f'</html>'
    )
```

- [ ] **Step 4: Smoke test the module standalone**

```bash
cd /Users/peytonbaker/Desktop/LindsayAI/truck-project-01
python -c "
from src.models import Order, Truck, RouteStop, TruckAssignment
from src.export_html import generate_html_routes

o = Order('O1', 'Test Customer', '123 Main St Atlanta GA 30301', 50.0)
t = Truck('26ft Straight #1', 'straight', 208.0)
stop = RouteStop(order=o, stop_number=1, load_position=1)
a = TruckAssignment(truck=t, stops=[stop])
html = generate_html_routes([a], depot_name='Lindsay Windows GA', date_str='2026-06-23')
print(html[:200])
print('OK — length:', len(html))
"
```

Expected: prints first 200 chars of HTML and `OK — length: <number>`.

- [ ] **Step 5: Commit**

```bash
git add src/export_html.py requirements.txt
git commit -m "feat: add HTML route sheet export module with QR codes (qrcode[pil])"
```

---

### Task 3: Tab reorder + plan-ready indicator

**Files:**
- Modify: `app.py:621-627` (main() tab block)

**Interfaces:**
- Consumes: `st.session_state.assignments` (non-empty = plan ready)

- [ ] **Step 1: Swap tab order and add plan-ready label**

Replace lines 621–627 in `app.py`:

```python
    plan_label = "Load Plan ✓" if st.session_state.assignments else "Load Plan"
    tab_orders, tab_plan, tab_analysis = st.tabs(["Add Orders", plan_label, "Analysis"])
    with tab_orders:
        render_add_orders(cfg)
    with tab_plan:
        render_load_plan(cfg)
    with tab_analysis:
        render_analysis(cfg)
```

- [ ] **Step 2: Verify in browser**

Start the app:
```bash
pkill -f "streamlit run"; streamlit run app.py --server.port 8501 &
```

Open `http://localhost:8501`. Confirm: "Add Orders" is the first (active) tab. Load a CSV, run the optimizer, confirm "Load Plan ✓" appears on the tab.

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat: reorder tabs Add Orders | Load Plan | Analysis; show checkmark when plan ready"
```

---

### Task 4: FeneVision xlsx upload in Add Orders tab

**Files:**
- Modify: `app.py` — add import at top, update `render_add_orders()`

**Interfaces:**
- Consumes: `import_fenevision_xlsx(file_obj, exclude_route_patterns=[...]) -> (orders, skipped, excluded_route_names)`
- Consumes: `cfg["routing"].get("exclude_route_patterns", [])` from config
- Produces: sets `st.session_state.orders`, `st.session_state.auto_run_pending = True`

- [ ] **Step 1: Add import to app.py**

After the existing `from src.models import Order, Truck` line at the top of `app.py`, add:

```python
from src.import_fenevision import import_fenevision_xlsx
```

- [ ] **Step 2: Add FeneVision upload section at top of render_add_orders()**

Insert the following block at the very beginning of the `render_add_orders(cfg)` function body (before the existing `st.subheader("Upload CSV")` line):

```python
    st.subheader("Import from FeneVision")
    st.caption("Upload the xlsx export from FeneVision (GA Trucks format — 'Orders by Route' sheet).")
    fv_file = st.file_uploader(
        "Choose FeneVision xlsx",
        type=["xlsx"],
        key=f"fv_uploader_{st.session_state.uploader_key}",
        label_visibility="collapsed",
    )
    if fv_file is not None:
        exclude_patterns = cfg.get("routing", {}).get("exclude_route_patterns", [])
        try:
            with st.spinner("Reading FeneVision file…"):
                new_orders, skipped, excluded = import_fenevision_xlsx(
                    fv_file,
                    exclude_route_patterns=exclude_patterns,
                )
        except Exception as e:
            st.error(f"Could not read FeneVision file: {e}")
        else:
            st.session_state.orders = new_orders
            st.session_state.assignments = []
            st.session_state.dropped = []
            st.session_state.uploader_key += 1
            st.session_state.auto_run_pending = True
            msg = f"Loaded {len(new_orders)} stops from FeneVision."
            if excluded:
                msg += f" {len(excluded)} route(s) excluded by pattern filter."
            if skipped:
                msg += f" {len(skipped)} placeholder stop(s) skipped (sqft below threshold)."
            st.success(msg)
            st.rerun()

    st.divider()
```

- [ ] **Step 3: Manual test with sample data**

Upload `sample_data/GA_Trucks_2026-06-17.xlsx` via the FeneVision uploader in the app. Confirm:
- Success message shows "Loaded 50 stops"
- App switches to showing orders in the table below
- Optimizer auto-runs and "Load Plan ✓" appears on the tab

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: add FeneVision xlsx upload to Add Orders tab — no terminal script required"
```

---

### Task 5: CSS animation during solve

**Files:**
- Modify: `app.py` — add module-level constant, update both `st.spinner` blocks in `render_load_plan()`

**Interfaces:**
- No new interfaces — replaces inline `st.spinner()` context managers in `render_load_plan`

- [ ] **Step 1: Add SOLVE_ANIMATION_HTML constant to app.py**

Add this constant at module level in `app.py`, after the `CONFIG_PATH = Path("config.yaml")` line:

```python
_SOLVE_ANIMATION_HTML = """
<div style="text-align:center;padding:2.5rem 0;font-family:Arial,sans-serif;">
  <svg width="72" height="72" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg"
       style="display:block;margin:0 auto 1.25rem;">
    <style>
      @keyframes _fp { 0%,100%{opacity:.2} 50%{opacity:.9} }
      .wp { fill:#c8e6f7; animation:_fp 2.5s ease-in-out infinite; }
      .wf { fill:none; stroke:#1a5fa8; stroke-width:3; }
      .wb { stroke:#1a5fa8; stroke-width:2; }
    </style>
    <rect class="wf" x="10" y="10" width="60" height="60" rx="3"/>
    <rect class="wp" x="14" y="14" width="24" height="24" style="animation-delay:0s"/>
    <rect class="wp" x="42" y="14" width="24" height="24" style="animation-delay:.35s"/>
    <rect class="wp" x="14" y="42" width="24" height="24" style="animation-delay:.7s"/>
    <rect class="wp" x="42" y="42" width="24" height="24" style="animation-delay:1.05s"/>
    <line class="wb" x1="40" y1="10" x2="40" y2="70"/>
    <line class="wb" x1="10" y1="40" x2="70" y2="40"/>
  </svg>
  <div style="position:relative;height:1.8rem;overflow:hidden;">
    <style>
      @keyframes _m1{0%{opacity:0}3%{opacity:1}17%{opacity:1}20%{opacity:0}100%{opacity:0}}
      @keyframes _m2{0%,20%{opacity:0}23%{opacity:1}37%{opacity:1}40%{opacity:0}100%{opacity:0}}
      @keyframes _m3{0%,40%{opacity:0}43%{opacity:1}57%{opacity:1}60%{opacity:0}100%{opacity:0}}
      @keyframes _m4{0%,60%{opacity:0}63%{opacity:1}77%{opacity:1}80%{opacity:0}100%{opacity:0}}
      @keyframes _m5{0%,80%{opacity:0}83%{opacity:1}97%{opacity:1}100%{opacity:0}}
      .sm{position:absolute;width:100%;text-align:center;font-size:.95rem;color:#555;
          animation-duration:20s;animation-iteration-count:infinite;animation-fill-mode:both;}
    </style>
    <p class="sm" style="animation-name:_m1">Reading stops across your routes…</p>
    <p class="sm" style="animation-name:_m2">Checking homebuilder truck restrictions…</p>
    <p class="sm" style="animation-name:_m3">Assigning stops to trucks…</p>
    <p class="sm" style="animation-name:_m4">Sequencing delivery stops by distance…</p>
    <p class="sm" style="animation-name:_m5">Building LIFO load order…</p>
  </div>
</div>
"""

_SOLVE_DONE_HTML = """
<div style="text-align:center;padding:1.5rem 0;font-family:Arial,sans-serif;color:#1a7a1a;font-size:1rem;">
  &#10003; Routes are ready &mdash; head to the <strong>Load Plan</strong> tab to see assignments and export for drivers.
</div>
"""
```

- [ ] **Step 2: Replace auto-run spinner in render_load_plan (lines ~288-304)**

Find the block:
```python
    if st.session_state.get("auto_run_pending") and st.session_state.orders:
        orders_copy = copy.deepcopy(st.session_state.orders)
        errors = validate_inputs(orders_copy, trucks)
        if not errors:
            st.session_state.auto_run_pending = False
            with st.spinner("Auto-optimizing routes from CSV…"):
                assignments, dropped = solve(
                    orders_copy, trucks, depot_coords,
                    max_route_miles=max_route_miles,
                    solver_time_limit=solver_time_limit,
                )
                st.session_state.assignments = assignments
                st.session_state.dropped = dropped
        else:
            st.session_state.auto_run_pending = False
            for e in errors:
                st.error(e)
```

Replace with:
```python
    if st.session_state.get("auto_run_pending") and st.session_state.orders:
        orders_copy = copy.deepcopy(st.session_state.orders)
        errors = validate_inputs(orders_copy, trucks)
        if not errors:
            st.session_state.auto_run_pending = False
            anim = st.empty()
            anim.markdown(_SOLVE_ANIMATION_HTML, unsafe_allow_html=True)
            assignments, dropped = solve(
                orders_copy, trucks, depot_coords,
                max_route_miles=max_route_miles,
                solver_time_limit=solver_time_limit,
            )
            st.session_state.assignments = assignments
            st.session_state.dropped = dropped
            anim.markdown(_SOLVE_DONE_HTML, unsafe_allow_html=True)
        else:
            st.session_state.auto_run_pending = False
            for e in errors:
                st.error(e)
```

- [ ] **Step 3: Replace manual-run spinner in render_load_plan (lines ~307-338)**

Find the block:
```python
    if st.button(btn_label, type="primary"):
        orders_copy = copy.deepcopy(st.session_state.orders)

        errors = validate_inputs(orders_copy, trucks)
        if errors:
            for e in errors:
                st.error(e)
            return

        if geocode_on:
            with st.spinner("Geocoding addresses…"):
                ...

            warnings = check_route_cap(orders_copy, depot_coords, max_route_miles)
            for w in warnings:
                st.warning(f"Multi-day route flag: {w}")

        with st.spinner("Optimizing routes…"):
            assignments, dropped = solve(
                orders_copy, trucks, depot_coords,
                max_route_miles=max_route_miles,
                solver_time_limit=solver_time_limit,
            )
            st.session_state.assignments = assignments
            st.session_state.dropped = dropped
```

Replace the `with st.spinner("Optimizing routes…"):` block only (keep the geocode spinner as-is):
```python
    if st.button(btn_label, type="primary"):
        orders_copy = copy.deepcopy(st.session_state.orders)

        errors = validate_inputs(orders_copy, trucks)
        if errors:
            for e in errors:
                st.error(e)
            return

        if geocode_on:
            with st.spinner("Geocoding addresses…"):
                if depot_addr:
                    result = geocode_address(depot_addr)
                    if result:
                        depot_coords = result
                for order in orders_copy:
                    coords = geocode_address(order.address)
                    if coords:
                        order.lat, order.lon = coords

            warnings = check_route_cap(orders_copy, depot_coords, max_route_miles)
            for w in warnings:
                st.warning(f"Multi-day route flag: {w}")

        anim = st.empty()
        anim.markdown(_SOLVE_ANIMATION_HTML, unsafe_allow_html=True)
        assignments, dropped = solve(
            orders_copy, trucks, depot_coords,
            max_route_miles=max_route_miles,
            solver_time_limit=solver_time_limit,
        )
        st.session_state.assignments = assignments
        st.session_state.dropped = dropped
        anim.markdown(_SOLVE_DONE_HTML, unsafe_allow_html=True)
```

- [ ] **Step 4: Verify animation in browser**

Upload the FeneVision file (or any CSV) and watch the Add Orders tab trigger the auto-run. The SVG window panes should pulse and messages should cycle. After solve completes, the green completion message should appear.

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat: CSS solve animation with SVG window icon and cycling status messages"
```

---

### Task 6: First-time onboarding modal + Need Help? button

**Files:**
- Modify: `app.py` — `init_state()`, new `_onboarding_modal()` dialog, `main()`, `render_sidebar()`

**Interfaces:**
- Consumes: `st.session_state.first_visit` (bool), `st.session_state.onboarding_slide` (int 0–4)
- `st.dialog()` requires Streamlit ≥ 1.32 (we have ≥ 1.40, so available)

- [ ] **Step 1: Add onboarding_slide to init_state()**

In `init_state()`, add to the `defaults` dict:
```python
        "onboarding_slide": 0,
```

- [ ] **Step 2: Add the dialog function to app.py**

Add this function after `init_state()` and before `render_sidebar()`:

```python
@st.dialog("How it works — Lindsay Windows Load Planner")
def _onboarding_modal():
    slides = [
        (
            "Welcome!",
            "This tool takes your FeneVision delivery data and builds an optimized load plan "
            "for each truck — automatically. It sequences stops, enforces truck restrictions "
            "for homebuilder customers, and prints route sheets drivers can scan with their phone.",
        ),
        (
            "Step 1 — Upload your FeneVision file",
            "Go to **Add Orders** and upload the xlsx export from FeneVision (GA Trucks format). "
            "The tool reads the 'Orders by Route' sheet, groups line items into stops, and "
            "loads them automatically. Interplant routes are excluded based on your config.",
        ),
        (
            "Step 2 — Review the Load Plan",
            "Head to **Load Plan**. The optimizer runs automatically after upload. "
            "You'll see each truck's delivery sequence and LIFO loading order. "
            "Click **Regenerate Load Plan** anytime to re-run with different settings.",
        ),
        (
            "Step 3 — Check the Analysis tab",
            "**Analysis** shows utilization per truck, a homebuilder constraint audit "
            "(flags any stop on the wrong truck type), and a comparison to Joseph's manual routes. "
            "You can also export an Excel report from here.",
        ),
        (
            "Step 4 — Print for drivers (morning dispatch workflow)",
            "Click **Export Route Sheets (HTML)** in the Load Plan tab. Open the file in your "
            "browser and print (Cmd+P or Ctrl+P). Each truck gets its own page with:\n\n"
            "- A QR code drivers scan to open the full route in Google Maps\n"
            "- Stop cards with company name and address\n"
            "- Loading order at the bottom (Load #1 goes in deepest)\n\n"
            "**Morning workflow:**\n"
            "1. Open the app → upload today's FeneVision file\n"
            "2. Wait ~30 seconds for the optimizer\n"
            "3. Export route sheets → print one page per driver\n"
            "4. Hand sheets to drivers before they leave the dock",
        ),
    ]

    idx = st.session_state.get("onboarding_slide", 0)
    title, body = slides[idx]

    st.markdown(f"**{title}**")
    st.markdown(body)
    st.caption(f"Slide {idx + 1} of {len(slides)}")

    col_back, col_spacer, col_next = st.columns([1, 3, 1])
    if idx > 0:
        if col_back.button("← Back", key="ob_back"):
            st.session_state.onboarding_slide = idx - 1
            st.rerun()
    if idx < len(slides) - 1:
        if col_next.button("Next →", key="ob_next"):
            st.session_state.onboarding_slide = idx + 1
            st.rerun()
    else:
        if col_next.button("Got it!", type="primary", key="ob_done"):
            st.session_state.first_visit = False
            st.session_state.onboarding_slide = 0
            st.rerun()
```

- [ ] **Step 3: Trigger modal in main()**

In `main()`, after `init_state()` and before `cfg = load_config()`, add:

```python
    if st.session_state.first_visit:
        _onboarding_modal()
```

- [ ] **Step 4: Add Need Help? button to render_sidebar()**

At the bottom of `render_sidebar()`, before the `return cfg` line, add:

```python
    st.sidebar.divider()
    if st.sidebar.button("Need Help?", use_container_width=True):
        st.session_state.first_visit = True
        st.session_state.onboarding_slide = 0
        st.rerun()
```

- [ ] **Step 5: Verify in browser**

- Reload the app → modal should appear automatically (first_visit = True)
- Click through all 5 slides with Next → / ← Back
- Click "Got it!" → modal closes, does not reappear on next action
- Click "Need Help?" in sidebar → modal reopens at slide 1

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "feat: first-time onboarding modal with 5 slides and Need Help? sidebar button"
```

---

### Task 7: HTML export replaces .txt download

**Files:**
- Modify: `app.py` — `render_load_plan()`: remove plan_text block, add HTML export button
- Modify: `app.py` — `init_state()`: remove plan_text_cache (no longer needed)

**Interfaces:**
- Consumes: `generate_html_routes(assignments, depot_name, date_str) -> str` from Task 2
- Consumes: `st.session_state.assignments` (already populated)

- [ ] **Step 1: Add import for generate_html_routes at top of app.py**

After the existing `from src.import_fenevision import import_fenevision_xlsx` line (added in Task 4), add:

```python
import datetime
from src.export_html import generate_html_routes
```

- [ ] **Step 2: Remove plan_text_cache from init_state()**

In `init_state()`, remove this line from the defaults dict:
```python
        "plan_text_cache": "",   # persists plan text so download never wipes state
```

- [ ] **Step 3: Replace the plan_text block and both download buttons in render_load_plan()**

Find the block beginning at (approximately) `# Build plan_text once; reused by both banner and bottom download button` through the final `st.download_button(... key="bottom_download")`.

Replace the entire `lines = [...]` / `plan_text = ...` / banner download / bottom download block with:

```python
    # Build and offer HTML export when assignments exist
    if st.session_state.assignments:
        depot_name = cfg["depot"].get("name", "Lindsay Windows")
        html_str = generate_html_routes(
            st.session_state.assignments,
            depot_name=depot_name,
            date_str=datetime.date.today().isoformat(),
        )
        col_banner, col_dl = st.columns([3, 1])
        col_banner.success("✅ Plan ready — export route sheets for drivers below.")
        col_dl.download_button(
            "⬇ Export Route Sheets (HTML)",
            data=html_str.encode("utf-8"),
            file_name="route_sheets.html",
            mime="text/html",
            key="banner_html_download",
        )
```

Then at the bottom of `render_load_plan()` (where the old `.txt` bottom download button was), replace:
```python
    st.divider()
    st.download_button(
        "⬇ Download Load Plan (.txt)",
        data=st.session_state.plan_text_cache or plan_text,
        file_name="load_plan.txt",
        mime="text/plain",
        key="bottom_download",
    )
```

with:
```python
    st.divider()
    if st.session_state.assignments:
        depot_name = cfg["depot"].get("name", "Lindsay Windows")
        html_str = generate_html_routes(
            st.session_state.assignments,
            depot_name=depot_name,
            date_str=datetime.date.today().isoformat(),
        )
        st.download_button(
            "⬇ Export Route Sheets (HTML)",
            data=html_str.encode("utf-8"),
            file_name="route_sheets.html",
            mime="text/html",
            key="bottom_html_download",
        )
```

Note: `generate_html_routes` is called twice per render (banner + bottom). For 6–10 trucks with ≤9 stops each, QR generation takes ~0.5s total — acceptable. If it becomes slow, cache in `st.session_state["html_cache"]`.

- [ ] **Step 4: Verify in browser**

Upload the FeneVision file, let the optimizer run, confirm:
- Export button appears in the banner and at the bottom of Load Plan
- Clicking downloads `route_sheets.html`
- Opening the file in a browser shows one section per truck with QR code, stop cards, and LIFO section
- Print preview (Cmd+P) shows each truck on its own page

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat: replace .txt export with printable HTML route sheets containing QR codes"
```

---

### Task 8: CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update function name reference in Models section**

Search CLAUDE.md for `import_geoffs_xlsx` and replace every occurrence with `import_fenevision_xlsx`.

- [ ] **Step 2: Add deferred notes**

In the `## Staged Roadmap → Done` section, add these two items:

```
- Phase 2 complete: in-app FeneVision xlsx upload, CSS solve animation, onboarding modal, HTML route sheet export with QR codes
```

In the `## Staged Roadmap → Next` section, add after the existing items:

```
8. **Session persistence** — file-based JSON for single-machine use; when app goes multi-user or cloud, add Streamlit OAuth (Google/Microsoft SSO). Deferred until after Joseph meeting.
9. **Driver name assignment** — Joseph names routes by driver (Kristin, Juan, etc.). Our optimizer uses generic truck names. Needs a driver roster or manual assignment UI.
```

- [ ] **Step 3: Update import_fenevision_xlsx() description in Data Schema section**

Find the line that says `import_geoffs_xlsx()` in the FeneVision xlsx schema section and update it:

```
Key columns used by `import_fenevision_xlsx()`:
```

Also update the function signature note to reflect the new third return value:
```
Returns (orders, skipped, excluded_route_names). exclude_route_patterns is a list of
substrings matched case-insensitively against RouteName; empty list = no exclusions.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for import_fenevision_xlsx rename and Phase 2 completion"
```

---

## Self-Review

**Spec coverage check:**
- ✅ FeneVision xlsx upload in-app (Task 4)
- ✅ Tab reorder Add Orders | Load Plan | Analysis (Task 3)
- ✅ Load Plan tab indicator when plan ready (Task 3 — ✓ in label)
- ✅ CSS animation during solve (Task 5)
- ✅ First-time onboarding modal, 5 slides (Task 6)
- ✅ Need Help? button (Task 6)
- ✅ HTML export with QR codes (Tasks 2 + 7)
- ✅ MO exclusion config-driven, not hardcoded (Task 1)
- ✅ import_geoffs_xlsx renamed to import_fenevision_xlsx (Tasks 1 + 8)
- ✅ qrcode[pil] added to requirements.txt (Task 2)
- ✅ exclude_route_patterns preserved by save_config (Task 1)

**Placeholder scan:** No TBDs, all code blocks are complete.

**Type consistency:**
- `import_fenevision_xlsx` returns `(List[Order], List[dict], List[str])` — used as 3-tuple in verify_617.py and as 3-tuple in app.py (Task 4). Consistent.
- `generate_html_routes(assignments, depot_name, date_str) -> str` — called with same signature in Task 7. Consistent.
- `_onboarding_modal()` is a `@st.dialog` decorated function — called in `main()` with no arguments. Consistent.
