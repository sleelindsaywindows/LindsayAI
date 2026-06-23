# Iteration Notes — What Changed and Where It Came From

This doc exists so the work account session knows exactly which parts of the codebase
were built by Peyton vs. pulled in from a separate team prototype. Do not conflate them.

---

## Iteration 1 — Peyton's Original Build (LyndsayWindowsLLC)

Everything below was built from scratch in the personal account session.

**`app.py`** — Full Streamlit UI:
- Two-tab layout (Add Orders / Load Plan)
- Sidebar fleet/depot config that writes back to config.yaml
- NL order input with parse + verify flow
- CSV upload with column validation
- Order table, clear button
- Load plan display with delivery sequence + LIFO load sequence side by side
- Google Maps URL per stop in the .txt export
- Graceful degradation (disabled UI elements when API key or geopy is missing)

**`src/parser.py`** — Two-agent NL parsing pipeline:
- `_parse_order()`: claude-haiku converts free-text → structured JSON
- `_verify_parse()`: second independent claude-haiku call re-reads original text and
  parsed result; returns `{confident, issues, summary}`
- Verifier never shares context with parser — catches hallucinated values
- `parse_and_verify()` public interface used by app.py

**`src/models.py`** — Data models:
- `Order` dataclass (order_id, customer_name, address, capacity_units, priority, notes, lat, lon)
- `Truck` dataclass (name, truck_type, max_capacity)
- `RouteStop` dataclass (order, stop_number)
- `TruckAssignment` with computed properties:
  - `load_sequence` — LIFO: `reversed(delivery_stops)`
  - `total_capacity_used`
  - `utilization_pct`

**`src/optimizer.py`** (partial) — Core OR-Tools solver:
- Haversine distance matrix
- Basic CVRP with capacity dimension
- Priority weighting in distance callback (urgent orders pulled to earlier stops)
- Optional geocoding via Nominatim/geopy with graceful fallback
- Integer scaling (SCALE = 100) for OR-Tools

**`config.yaml`** — Fleet and depot config pattern (no code changes needed to update fleet)

**`sample_data/example_orders.csv`** — Kansas City area test orders

---

## Iteration 2 — AWLindsay Team Prototype (LW-Storage-and-Shipping-Basic)

This is **not Peyton's code**. It's a separate prototype from the Lindsay Windows team
GitHub (`AWLindsay/LW-Storage-and-Shipping-Basic`). It was reviewed and specific solver
improvements were extracted. Nothing was copied wholesale — the patterns were re-implemented
into the existing architecture.

**Solver improvements taken from this prototype:**

| What | Why it matters |
|---|---|
| `SetFixedCostOfVehicle` | Solver was activating 53-ft trailers for tiny orders. Fixed cost steers it to fill 26-ft trucks first. |
| `AddDimension("Distance")` | No per-driver mileage cap existed. Added HOS compliance ceiling. |
| `AddDisjunction` + penalty | Solver was returning empty list on over-capacity. Now drops orders explicitly and reports them. |
| `check_route_cap()` | No pre-flight check existed. Now warns about orders that will exceed the cap before the solver runs. |
| `validate_inputs()` | No input validation before OR-Tools. Now catches bad data with readable errors. |
| `DISTANCE_SCALE = 1000` | Was using meters. Switched to thousandths-of-miles — matches domain language, better precision on dense stops. |
| Dropped order detection | `solution.Value(routing.NextVar(idx)) == idx` pattern to surface which orders were dropped. |
| `route_distance_miles` per truck | Total miles per truck was not tracked or displayed. |

**What was NOT taken from the team prototype:**
- No UI (it was CLI only) — kept Streamlit
- No NL parsing — kept two-agent pipeline
- No LIFO load sequence (they didn't implement it) — kept and enforced in model
- No priority weighting — kept and preserved
- No customer_name field — kept
- No config.yaml pattern — kept
- No CSV pipeline — kept
- No graceful degradation — kept

---

## truck-project-01 — Merge Synthesis (this session)

New things that didn't exist in either prior iteration:

**`src/models.py` additions:**
- `Truck.fixed_cost` — activation penalty field (feeds SetFixedCostOfVehicle)
- `Truck.cost_per_mile` — placeholder for future cost objective
- `TruckAssignment.route_distance_miles` — stores solver output per truck

**`config.yaml` changes:**
- Capacities corrected to practical sq ft: 53-ft = 384, 26-ft = 176 (was linear feet before)
- `fixed_cost` and `cost_per_mile` added per truck
- `routing` section added: `max_route_miles` (400 — placeholder, needs team validation),
  `solver_time_limit_seconds`
- Depot updated to Georgia plant context (address still blank — needs real address)

**`app.py` additions:**
- Dropped orders banner (red alert with order IDs)
- Route distance display per truck in expander label
- 4th metric column: Fleet Utilization %
- Pre-flight warnings surface in UI when geocoding is on
- `validate_inputs()` called before solver
- Sidebar exposes `max_route_miles` as an editable field
- `fixed_cost`/`cost_per_mile` preserved when saving sidebar config

**`sample_data/example_orders.csv`** — replaced Kansas City data with Georgia-area addresses

**`CLAUDE.md`** — written from scratch this session with full business context,
design history, field mapping table, and placeholders for sample data and team skills

---

## Phase 1 — FeneVision Import + Truck Restrictions + UI Overhaul (2026-06-17 / 2026-06-22)

First run against Geoff's real 6/17 historical data. Everything below was confirmed with Geoff.

**`src/import_fenevision.py`** — new file:
- `import_fenevision()` — generic FeneVision CSV → Order objects
- `import_geoffs_xlsx()` (later renamed) — parses "Orders by Route" sheet from GA Trucks xlsx; aggregates line items into stops; reads TruckTypeDesc for per-customer truck restrictions

**`src/models.py`** additions:
- `Order.allowed_truck_types` — list of truck type strings (e.g. `["straight"]`) or None; enforces homebuilder/subdivision delivery restrictions

**`src/optimizer.py`** additions:
- `SetAllowedVehiclesForIndex` — enforces `allowed_truck_types` per stop; homebuilder customers physically can't receive 53-ft trailers

**`src/analysis.py`** — new file:
- `generate_report()` — writes 4-sheet Excel workbook: Summary (utilization bar chart), Per-Route Detail (homebuilder stops highlighted yellow), Cost Comparison vs Joseph's 13 trucks, Constraint Audit (PASS/FAIL per restricted stop)

**`config.yaml`** corrections (confirmed by Geoff from FeneVision Trucks sheet):
- 53-ft trailer: 431.06 sq ft (was 384)
- 26-ft straight: 208.00 sq ft (was 176)
- `max_route_miles`: 600 (extended for NC/SC routes like BFS Hillsborough, Carter Lumber Columbia)
- Fleet expanded: 10× 26-ft straight + 4× 53-ft trailer

**`scripts/verify_617.py`** — new file:
- One-command smoke test: loads Geoff's 6/17 xlsx, runs optimizer, generates Excel report, prints PASS/FAIL summary
- Result: 50 stops, 6 trucks used (vs Joseph's 13), homebuilder PASS, 0 dropped

**`requirements.txt`** additions:
- `openpyxl>=3.1.0` — Excel report generation
- `pyarrow>=10.0.1,<19` — pinned; pyarrow 19+ conflicts with ortools 9.15 on macOS via shared libprotobuf (ABSL_LOG FATAL on duplicate descriptor registration)

**`.streamlit/config.toml`** — gitignored local dev config:
- `enableCORS = false`, `enableXsrfProtection = false` — fixes VS Code simple browser 403 on file upload

---

## Phase 2 — In-App Import, Animation, HTML Export (2026-06-23)

Branch: `phase2-import-ui-html-export`. Joseph can now run the full workflow from the browser — no terminal required.

**`src/import_fenevision.py`** changes:
- Renamed `import_geoffs_xlsx()` → `import_fenevision_xlsx()` — function handles any date/plant's FeneVision xlsx, not just Geoff's 6/17 file
- `exclude_routes` exact-match parameter replaced with `exclude_route_patterns` — list of substrings matched case-insensitively against RouteName; empty list = no exclusions
- Returns 3-tuple: `(orders, skipped, excluded_route_names)` — UI can report how many routes were filtered
- Note: pattern `"MO"` was too broad (matches "Raymond"); use `" MO "` with spaces to target interplant route names specifically

**`src/export_html.py`** — new file:
- `generate_html_routes(assignments, depot_name, date_str)` → complete HTML string
- One page per truck via `@media print { page-break-before: always }`
- QR code per truck via `qrcode[pil]` — base64-embedded PNG, no internet needed at print time
- Google Maps multi-stop URL: depot → all stops; splits into legs if > 10 stops (Google Maps limit)
- Per-stop cards: company name, address, sq ft, notes/gate codes, priority tag
- LIFO loading order at bottom of each page
- Falls back to plain URL text if `qrcode` not installed

**`app.py`** changes:
- **Tab reorder**: Add Orders | Load Plan | Analysis (was Load Plan first — wrong for a session-stateless app)
- **Plan-ready indicator**: Load Plan tab label becomes "Load Plan ✓" when assignments exist
- **FeneVision upload section**: top of Add Orders tab; accepts `.xlsx`; calls `import_fenevision_xlsx()` directly; shows excluded route count; sets `auto_run_pending = True`
- **CSS animation**: replaces `st.spinner()` during solve — SVG window icon with pulsing blue panes + 5 status messages cycling over 20s via pure CSS `@keyframes`; completion message directs user to Load Plan tab
- **Onboarding modal**: `@st.dialog` with 5 slides (welcome → upload → load plan → analysis → print/dispatch); shows once per session; "Need Help?" sidebar button reopens at slide 1
- **HTML export**: replaces `.txt` download with "Export Route Sheets (HTML)" button; `urllib.parse` import removed (no more raw Maps URLs in driver output)

**`config.yaml`** additions:
- `routing.exclude_route_patterns: []` — configurable interplant exclusion list; empty by default

**`requirements.txt`** additions:
- `qrcode[pil]>=7.4.2`

**`scripts/verify_617.py`** updates:
- Updated to use `import_fenevision_xlsx` and `exclude_route_patterns`
- Preflight check #5 updated: validates patterns match at least one route (warns on typos) rather than checking exact route names

**Key design decisions recorded:**
- Interplant routes not hardcoded to exclude — future plants (PA, MN, etc.) may or may not need exclusion depending on business context; keep in config
- Session persistence deferred — file-based JSON for single-machine, OAuth upgrade path for multi-user (post-Joseph meeting)
- Driver name assignment deferred — Joseph routes by driver name; optimizer uses generic truck names; needs roster or manual assignment UI
