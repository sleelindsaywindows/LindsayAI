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
- `AddDimension("Distance")` — hard per-driver daily mileage cap (HOS compliance)
- `AddDisjunction` with `UNASSIGNED_ORDER_PENALTY` — dropped orders are reported explicitly
  rather than causing a hard solver failure; `solve()` returns `(assignments, dropped)`
- Priority weighting in `_dist_cb` — urgent orders (priority 1–10) are pulled to earlier stops
- Separate raw distance callback for the Distance dimension (no priority penalty in mileage cap)
- Distance in miles × 1000 (DISTANCE_SCALE = 1_000) for integer precision on dense urban stops

**Models (`src/models.py`):** `Order`, `Truck`, `RouteStop`, `TruckAssignment`.
`TruckAssignment.load_sequence` (LIFO) and `.utilization_pct` are computed properties.

---

## Key Constraints — Never Change Without a Reason

**LIFO loading:** Last delivery stop loads first (goes deepest in truck).
`TruckAssignment.load_sequence` = `reversed(delivery_stops)`. Always derived from delivery
sequence — never set independently.

**Capacity math:** Windows stand upright individually, not palletized. Space scales linearly.
- 53-ft trailer: 8 ft × 48 ft = **384 sq ft** practical (theoretical 8.25 × 52 = 429)
- 26-ft straight: 8 ft × 22 ft = **176 sq ft** practical
- Screens = zero floor allocation (placed on top of windows)

**Integer scaling:** OR-Tools requires integers. Capacity × 100 (`SCALE`). Distance × 1000
(`DISTANCE_SCALE`). These are independent.

**HOS cap (`max_route_miles`):** Currently **400 miles** — this is a placeholder. Confirm
the real number with Joseph or Geoff based on actual driver schedules before treating it as
a hard constraint. Change via `config.yaml` → `routing.max_route_miles` or the sidebar.

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

### Real Lindsay Windows FeneVision fields (when Geoff's CSV arrives)
```
route_id, route_description, target_ship_date, stop_number, order_number,
window_width, window_height, ship_qty, max_ship_qty,
ship_to_name, ship_to_street, ship_to_city, ship_to_state, ship_to_zip
```
Mapping needed: combine `ship_to_*` → `address`; compute `capacity_units` from
`window_width × window_height × ship_qty` (sq ft per unit, windows standing upright).
Write this mapper as `src/import_fenevision.py`.

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
- UI: NL parse (two-agent), CSV upload, verification card, LIFO display, .txt export
- Pre-flight: `validate_inputs()` + `check_route_cap()` for multi-day candidates

### Next (in priority order)
1. **Real order data** — write `src/import_fenevision.py` field mapper; validate sq ft math
2. **Real driving distances** — swap `_haversine_miles` in `_build_distance_matrix` for OSRM
   (free) or Google Maps Distance Matrix API (~$5/1000 pairs). Nothing else changes.
3. **Driver-ready output** — Google Maps multi-stop URL per truck; CSV summary for dispatcher
4. **2D packing check** — add `length`/`width` to `Order`; use `rectpack` to catch loads that
   pass the sq ft check but physically won't fit
5. **Delivery time windows** — `time_window_open`/`close` on `Order`; Time dimension in solver
6. **Constraint relaxation** — re-run with soft time windows if primary solve drops orders
7. **FeneVision live feed** — VPN + stored procedures + scheduled job; proceed in parallel once
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
