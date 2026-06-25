# Lindsay Windows Load Planner — Phase 2 Design Spec
**Date:** 2026-06-23  
**Status:** Approved for implementation

---

## Scope

Five coordinated changes to `app.py`, `src/import_fenevision.py`, and a new HTML export module:

1. FeneVision xlsx upload built into the app (removes terminal script requirement)
2. Tab reorder + visual "plan ready" indicator on Load Plan tab
3. CSS animation with "Need Help?" onboarding
4. First-time onboarding modal (shows once per session)
5. Printable HTML export with QR codes replacing the .txt export

---

## 1. FeneVision xlsx Upload (In-App)

### Problem
Joseph currently needs a terminal command to convert xlsx → CSV before uploading. Joseph does not run terminal scripts.

### Solution
Add a **"FeneVision Import" section** at the top of the Add Orders tab, above the existing CSV uploader. Accepts `.xlsx` files. Calls `import_fenevision_xlsx()` directly. No conversion step, no terminal.

### Interplant Route Exclusion
No routes are auto-excluded by default. Exclusion is driven by `exclude_route_patterns` in `config.yaml` — a list of substring patterns matched case-insensitively against `RouteName`. Example: `["MO 53'", "MO Interplant"]`. Empty by default.

If any routes are excluded, a small info note shows: _"2 routes excluded (matched configured patterns)."_ Joseph never sees a decision prompt.

Rationale for config-driven vs. hardcoded "MO": future plants (PA, MN, etc.) can appear as interplant routes. Whether a route should be excluded depends on business context at the time — Lindsay may later want to include an interplant leg as part of a multi-stop route. Keeping the exclusion list in config.yaml means no code change when the answer changes.

### Rename
`import_geoffs_xlsx()` → `import_fenevision_xlsx()` throughout codebase.  
Reason: the function handles any FeneVision xlsx export in the "Orders by Route" format, from any date or plant. The function is not GA-specific — GA is just where it was first validated.

### Fallback
The existing app CSV uploader stays. Non-FeneVision data (hand-entered orders, test data) still works the same way.

### Data Flow
```
Joseph uploads GA_Trucks_YYYY-MM-DD.xlsx
  → import_fenevision_xlsx() parses "Orders by Route" sheet
  → Routes matching config.exclude_route_patterns are excluded (silently noted if any)
  → Orders loaded into session_state.orders
  → auto_run_pending = True → optimizer runs
  → "✅ Routes ready — head to Load Plan tab" message
```

---

## 2. Tab Order + Load Plan Visual Indicator

### Tab Order
`Add Orders | Load Plan | Analysis`  
Rationale: every session starts with uploading data. The app has no cross-session persistence yet. Add Orders must be first.

### Load Plan Tab Indicator
When `session_state.assignments` is non-empty, inject CSS via `st.markdown(unsafe_allow_html=True)` to add a green dot badge to the Load Plan tab button. Implemented with a CSS attribute selector targeting the active tab button element. Falls back gracefully if Streamlit's internal DOM structure changes (dot just doesn't show, no error).

---

## 3. CSS Animation

Plays in a `st.empty()` container while `solve()` runs. Three layers:

**Layer 1 — SVG window icon** cycling through three states via CSS `@keyframes`:
- Empty frame → windows appearing → loaded (simple geometric SVG, no external library)

**Layer 2 — Status messages** cycling every ~4 seconds with `animation-delay` stagger per character:
- "Reading stops across your routes…"
- "Checking homebuilder truck restrictions…"
- "Assigning stops to trucks…"
- "Sequencing delivery stops by distance…"
- "Building LIFO load order…"

Each message fades in letter-by-letter left to right, dissolves right to left before the next appears. Pure CSS keyframes, no JavaScript.

**Layer 3 — Completion** (replaces the entire container when `solve()` returns):
> ✅ Routes are ready — **head to the Load Plan tab** to see truck assignments and export for drivers.

The container replacement is instant when `solve()` returns — no "done early" overlap issue since `st.empty()` replacement is atomic.

---

## 4. First-Time Onboarding Modal

Uses `st.dialog()` (Streamlit ≥ 1.32). Triggered when `session_state.first_visit == True`.

Contains **5 slides** navigated with Back / Next buttons:
1. Welcome — what this tool does
2. Upload your FeneVision file
3. Review the optimized Load Plan
4. Read the Analysis tab
5. Print routes for drivers (QR code tip)

Morning workflow is embedded in slide 5 as a step-by-step. Dismissed with "Got it" → `session_state.first_visit = False` for the rest of the session.

**"Need Help?" button** in the sidebar sets `first_visit = True` and reruns, reopening the modal at slide 1. Always accessible.

**Note on persistence:** `first_visit` resets each browser session (expected for now). Full cross-session persistence (remembering Joseph has seen it before) is deferred — see Deferred section below.

---

## 5. Printable HTML Export (Replaces .txt)

### Format
One HTML file per "Generate" click, containing all trucks. Each truck is a separate page via CSS `@media print { page-break-before: always }`.

**Per-truck page layout:**
```
Header: Lindsay Windows | Date | Truck name | Utilization %
QR code: one per truck, opens full Google Maps multi-stop route
Stops: each stop as a card — company name large, address on separate lines,
       sq ft, notes (gate codes etc.)
LIFO section: bottom of page, clearly labeled "LOADING ORDER — Load #1 first"
```

### QR Code Generation
Library: `qrcode[pil]` → generates PIL image → base64-encoded → embedded as `<img src="data:image/png;base64,...">` in HTML. No external image hosting. Prints anywhere without internet.

### Google Maps URL
```
https://www.google.com/maps/dir/DEPOT_ADDRESS/STOP1/STOP2/.../LAST_STOP/
```
Addresses URL-encoded. Google Maps limit: 10 waypoints per URL. If a truck has > 10 stops, generate two QR codes labeled "Leg 1 (Stops 1–10)" and "Leg 2 (Stops 11+)". On Geoff's data all trucks had ≤ 9 stops — this is an edge case guard only.

### Download
Button label changes to "⬇ Export Route Sheets (HTML)". File opens in browser for print (Cmd+P). No raw URLs in the output anywhere.

### Dependencies to add
`qrcode[pil]>=7.4.2` → `requirements.txt`

---

## State Bug Fix (Already Patched)

`session_state.plan_text_cache` stores plan text at generation time. Bottom download button reads from cache. Prevents state loss on download re-render. `bottom_download` key added to deduplicate widget IDs. Committed in session 2026-06-23.

---

## Deferred (Out of Scope for This Implementation)

- **Cross-session persistence** (save orders/plan to disk, reload on app start): no sign-in needed for single-machine use; file-based JSON. When app goes multi-user or cloud, add Streamlit OAuth (Google/Microsoft). Document in CLAUDE.md.
- **Driver name assignment**: Joseph's routes are named by driver (Kristin, Juan, etc.). Our optimizer uses generic truck names. Needs a driver roster or manual assignment UI — defer until after Joseph meeting.
- **Direct FeneVision connection** (VPN + stored procedures): in roadmap, proceed after several weeks of manual xlsx validation.

---

## Files Changed

| File | Change |
|------|--------|
| `app.py` | FeneVision upload section, tab reorder, tab badge CSS, animation, onboarding modal, HTML export button |
| `src/import_fenevision.py` | Rename `import_geoffs_xlsx` → `import_fenevision_xlsx`, add MO auto-exclusion |
| `src/export_html.py` | New module: `generate_html_routes(assignments, dropped, date)` → returns HTML string |
| `scripts/verify_617.py` | Update import name |
| `requirements.txt` | Add `qrcode[pil]>=7.4.2` |
| `CLAUDE.md` | Update function name, add deferred persistence + driver roster notes |
