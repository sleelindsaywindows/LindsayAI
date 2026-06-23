# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project

Lindsay Windows Load Planner — assigns customer window orders to delivery trucks and sequences
delivery stops. Built as an internship demo for Geoff Roise at Lindsay Windows LLC.

The live demo workflow: Lee brings this to the Georgia plant, Joseph (manual route planner)
routes his way, the optimizer routes its way, and the team compares. Discrepancies surface
missing constraints and build trust before any process change.

---

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in API key (required for NL parsing only — CSV + optimizer work without it)
cp .env.example .env

# Run the app
streamlit run app.py

# Test the optimizer standalone (no UI, no API key needed)
python -c "
from src.models import Order, Truck
from src.optimizer import solve
orders = [Order('O1','Test','Atlanta GA',80), Order('O2','Test2','Macon GA',60)]
trucks = [Truck('26ft','straight',176)]
assignments, dropped = solve(orders, trucks)
for a in assignments:
    print(a.truck.name, [s.order.order_id for s in a.stops])
print('Dropped:', [o.order_id for o in dropped])
"
```

No linting or test suite is set up yet — the above one-liner is the fastest solver smoke test.

---

## Architecture

**Entry point:** `app.py` — Streamlit UI. All mutable state (orders, pending parse, assignments,
dropped orders) lives in `st.session_state`. Config is read from `config.yaml` on every render;
sidebar changes write back to disk via `save_config()`.

**Configuration (`config.yaml`):** Defines measurement unit, truck fleet (with `fixed_cost` and
`cost_per_mile` per truck), depot address, and routing constraints. The unit label is purely
cosmetic — the optimizer works on raw floats. To switch display labels: edit `config.yaml`,
no code changes needed.

**Two-agent parse pipeline (`src/parser.py`):**
1. `_parse_order()` — claude-haiku converts free-text to structured JSON
2. `_verify_parse()` — a separate claude-haiku call independently re-reads original text and
   the parsed result; returns `{confident, issues, summary}`. Never shares context with the
   parser, so it catches hallucinated values.

**Optimizer (`src/optimizer.py`):** Google OR-Tools Capacitated VRP. Node 0 = depot, nodes
1–N = orders. Key design decisions merged from two prior iterations:
- `SetFixedCostOfVehicle` — higher activation penalty on 53-ft trailers so solver fills
  26-ft straight trucks before opening trailers
- `SetAllowedVehiclesForIndex` — enforces per-order truck type restrictions
  (e.g. homebuilder customers that physically cannot receive a 53-ft trailer)
- `AddDimension("Distance")` — hard per-driver daily mileage cap (HOS compliance)
- `AddDisjunction` with `UNASSIGNED_ORDER_PENALTY` — dropped orders are reported explicitly
  rather than causing a hard solver failure; `solve()` returns `(assignments, dropped)`
- Priority weighting in `_dist_cb` — urgent orders (priority 1–10) are pulled to earlier stops
- Separate raw distance callback for the Distance dimension (no priority penalty in mileage cap)
- Distance in miles × 1000 (DISTANCE_SCALE = 1_000) for integer precision on dense urban stops

**Models (`src/models.py`):** `Order`, `Truck`, `RouteStop`, `TruckAssignment`.
`TruckAssignment.load_sequence` (LIFO) and `.utilization_pct` are computed properties.
`Order.allowed_truck_types` is a list of `Truck.truck_type` strings (e.g. `["straight"]`)
or `None` (any truck). Populated by the FeneVision importer from `TruckTypeDesc`; eventually
will come from a customer-level lookup table once Geoff confirms which customers can't take
53-ft trailers.

---

## Key Constraints — Never Change Without a Reason

**LIFO loading:** Last delivery stop loads first (goes deepest in truck).
`TruckAssignment.load_sequence` = `reversed(delivery_stops)`. Always derived from delivery
sequence — never set independently.

**Capacity math:** Windows stand upright individually, not palletized. Space scales linearly.
- 53-ft trailer: 8.25 ft × 52.25 ft = **431.06 sq ft** practical (from Geoff's FeneVision Trucks sheet, confirmed 2026-06-17)
- 26-ft straight: 8.00 ft × 26.00 ft = **208 sq ft** practical (same source)
- Screens = zero floor allocation (placed on top of windows)
- Earlier placeholder values (384 / 176) are WRONG — do not revert

**Truck type restrictions (confirmed by Geoff, 2026-06-17):** Certain customers cannot
physically receive a 53-ft trailer — tight residential driveways, subdivision streets, etc.
These customers always appear with `TruckTypeDesc = "GA-26' ST"` in FeneVision exports.
Modeled as `Order.allowed_truck_types = ["straight"]`. The optimizer enforces this via
`routing.SetAllowedVehiclesForIndex()` so the solver never assigns a restricted stop to
a trailer. Source of truth is currently the route's TruckTypeDesc from FeneVision; a
customer-level lookup table is the right long-term fix (pending Geoff's confirmation).

**Integer scaling:** OR-Tools requires integers. Capacity × 100 (`SCALE`). Distance × 1000
(`DISTANCE_SCALE`). These are independent.

**HOS cap (`max_route_miles`):** Set to **600 miles** to accommodate NC/SC routes (e.g. BFS
Hillsborough, Carter Lumber Columbia). Confirm real cap with Joseph — daily vs. two-day runs
are different scheduling problems. Change via `config.yaml` → `routing.max_route_miles`.

**Planning modes (Geoff confirmed, 2026-06-17):** Two distinct use cases:
- **Pre-planning (weekly horizon):** Done days in advance. All orders known. Optimize truck
  assignments and stop sequences across the full week's load. Accuracy over speed.
  Optimizer should be given all orders for the week; constraint relaxation (e.g. fewer trucks)
  is acceptable if it surfaces real-world trade-offs.
- **Day-of (daily execution):** Morning-of routing. Some orders may have changed (adds,
  cancels, priority bumps since pre-plan). Optimize within the already-committed truck set.
  Speed matters — Joseph needs the output before drivers leave the dock. Consider freezing
  vehicle assignments and only re-sequencing stops for day-of mode.
The code does not yet distinguish these modes; document the distinction here until the UI
supports explicit mode selection.

**Geocoding:** Optional. If geopy is not installed, distances are all zero — optimizer still
produces valid truck assignments, just unoptimized stop order. App never hard-fails without it.

---

## Data Schema

### App CSV format
```
order_id, customer_name, address, capacity_units, priority, notes
```
`capacity_units` = floor sq ft. `priority` = 0–10. `notes` = gate codes, dock info, trailer
exchange flag.

### Geoff's real FeneVision xlsx export (GA Trucks format, confirmed 2026-06-17)

Primary sheet: `Orders by Route`. Each row is a **line item**, not a stop — one stop has
many rows (multiple window sizes / orders). Aggregate to stop level before feeding the optimizer.

Key columns used by `import_fenevision_xlsx()`:
```
RouteID, RouteName, Stop               → route grouping
shpaddr_companyname                    → customer_name
ShpAddr_Address1/City/State/ZipCode    → address (concatenated)
sqftShippedQty                         → capacity_units (pre-calculated by FeneVision, sum per stop)
TruckTypeDesc                          → allowed_truck_types ("GA-26' ST" → "straight", "53' Trailer" → "trailer")
```

`Route Truck Summary` sheet = Joseph's actual routing ground truth for comparison.
`Trucks` sheet = fleet definitions (capacity, cost/mile, quantity available).

The MO route (`6/17 Lindsay MO 53'`) is an interplant transfer, not a customer delivery.
Exclude it via the `exclude_route_patterns` parameter in `import_fenevision_xlsx()` — a list of substrings matched case-insensitively against RouteName (e.g. `[" MO "]`). Configured in `config.yaml → routing.exclude_route_patterns`.

---

## Sample Data — PLACEHOLDER

`sample_data/example_orders.csv` is synthetic Georgia-area data for local testing only.

When the team account is active, replace with Geoff's historical Wednesday (6/17) run:
- Keep the synthetic file as `example_orders_synthetic.csv`
- Run the optimizer against the historical data and compare to Joseph's actual routes
- Discrepancies are the signal — surface them, don't paper over them

---

## Claude Skills — PLACEHOLDER

Two team-defined Claude Code skills are planned. Wire them in once the team account is active.

**caveman** — purpose and invocation TBD. Add usage notes here when available.

**ponytail** — purpose and invocation TBD. Add usage notes here when available.

Invocation pattern in Claude Code: `/skill-name`

---

## Staged Roadmap

### Done
- Solver: bin assignment + route sequencing + truck preference + HOS cap + dropped order reporting
- UI: NL parse (two-agent), CSV upload, verification card, LIFO display
- Pre-flight: `validate_inputs()` + `check_route_cap()` for multi-day candidates
- `src/import_fenevision.py`: FeneVision CSV mapper (`import_fenevision`) + xlsx mapper (`import_fenevision_xlsx`)
- `Order.allowed_truck_types`: per-customer truck restriction, enforced in optimizer via `SetAllowedVehiclesForIndex`
- Real truck capacities in config (431.06 / 208 sq ft from Geoff's Trucks sheet)
- First optimizer run against Geoff's 6/17 historical data; comparison vs Joseph's routes documented
- Phase 2: in-app FeneVision xlsx upload (no terminal required), CSS solve animation, first-time onboarding modal, printable HTML route sheets with QR codes, tab reorder (Add Orders | Load Plan | Analysis)

### Next (in priority order)
1. **Customer truck restriction lookup table** — currently derived from route's TruckTypeDesc;
   needs a per-customer table (Geoff to confirm) so restrictions hold even when a customer
   appears on a new route or is entered via NL/CSV upload
2. **Real driving distances** — swap `_haversine_miles` in `_build_distance_matrix` for OSRM
   (free) or Google Maps Distance Matrix API (~$5/1000 pairs). Nothing else changes.
3. **Unload time buffer per stop** — add configurable `unload_minutes_per_stop` to config.yaml;
   use it in HOS modeling alongside mileage. Check with [Names] in Minnesota for realistic values.
4. **2D packing check** — add `length`/`width` to `Order`; use `rectpack` to catch loads that
   pass the sq ft check but physically won't fit
5. **Delivery time windows** — `time_window_open`/`close` on `Order`; Time dimension in solver
6. **Constraint relaxation** — re-run with soft time windows if primary solve drops orders
7. **Session persistence** — file-based JSON for single-machine use; when app goes multi-user
   or cloud, add Streamlit OAuth (Google/Microsoft SSO). Deferred until after Joseph meeting.
8. **Driver name assignment** — Joseph names routes by driver (Kristin, Juan, Raymond, etc.).
   Optimizer uses generic truck names. Needs a driver roster or manual assignment UI — defer
   until after Joseph meeting.
9. **FeneVision live feed** — VPN + stored procedures + scheduled job; proceed in parallel once
   the optimizer output has been manually validated for several weeks

### Intentional simplifications (not bugs)
- Screen space = 0
- Haversine straight-line distances until real routing is validated
- No time windows
- Trailer exchange stops not modeled (no special unload-time logic)
- `cost_per_mile` stored in config but not yet used in output or objective function

---

## API Key

`ANTHROPIC_API_KEY` in `.env` — required for NL parsing only. Corporate vs. individual
account decision pending with Geoff.
