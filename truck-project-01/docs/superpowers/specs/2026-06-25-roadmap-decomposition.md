# Roadmap Decomposition — Lindsay Windows Truck Planner
**Date:** 2026-06-25
**Source:** CLAUDE.md Staged Roadmap + team conversations through 2026-06-25

Items are listed in confirmed priority order. Status and effort reflect the current state of the codebase as of this date.

---

## Item 1 — Customer Truck Restriction Lookup Table

**Status:** In progress (partial — currently derived from TruckTypeDesc per route)
**Effort:** M
**Blocked by:** Geoff confirming which customers are permanently restricted to straight trucks

**Key decisions needed:**
- File format and storage location for the lookup table (CSV alongside the app? embedded in config.yaml? SQLite?)
- What to do when a customer appears in both the lookup table and FeneVision with conflicting TruckTypeDesc values (table wins, or warn and ask?)
- Whether the builder vs. distribution split maps cleanly to the truck restriction dimension: builder screens = separate route (already excluded), distribution screens = with windows (no restriction). Confirm that "builder customer" is always synonymous with "straight-only" or whether there are exceptions.

**Implementation approach:**
Add a `data/customer_restrictions.csv` with columns `customer_name` (normalized), `allowed_truck_types` (comma-separated, e.g. `straight`), and `notes`. In `import_fenevision_xlsx()` and the NL/CSV import paths, after constructing each `Order`, look up the customer name in this table and override `allowed_truck_types` if a match is found — TruckTypeDesc from the route remains a fallback when no table entry exists. The lookup should normalize whitespace and case to survive small name variations. Add a UI warning in the Load Plan tab when an order's restriction came from TruckTypeDesc (less reliable) rather than the lookup table.

---

## Item 2 — Real Driving Distances (OSRM)

**Status:** DONE — implemented, config key `osrm_server` in config.yaml
**Effort:** —
**Blocked by:** —

No further work needed. The haversine fallback remains active when `osrm_server` is not set, preserving the zero-dependency path.

---

## Item 3 — Unload Time Buffer per Stop

**Status:** DONE — `stop_time_minutes: 45` is live in config.yaml and fully wired into the optimizer's HOS model
**Effort:** —
**Blocked by:** —

No further work needed. Value is configurable from the sidebar without a code change.

---

## Item 4 — 2D Packing Check (frame depth)

**Status:** Partially done — `max_window_width` warning exists in the UI; `frame_depth_inches` field is missing from `Order`
**Effort:** M
**Blocked by:** Nothing blocking. Joseph confirmed depth values.

**Key decisions needed:**
- Whether to surface the depth check as a warning only (non-blocking solve) or a hard constraint that feeds into the optimizer's capacity dimension. Warning-only is safer for the demo phase.
- Whether `frame_depth_inches` comes from FeneVision line items (it should be on the item type) or is entered manually. If FeneVision does not expose it, we need a product-type-to-depth lookup table similar to the restriction table.

**Confirmed values from Joseph:**
- Nail fin: 3"
- 4-9/16" jamb: 6"
- 6-9/16" jamb: 8"
- Windows load along side walls, larger windows in back (already enforced by LIFO)

**Implementation approach:**
Add `frame_depth_inches: float | None = None` to `Order` in `src/models.py`. In `import_fenevision_xlsx()`, read the window type column (confirm the exact FeneVision column name with Geoff) and map to depth via a small dict or `data/frame_depths.csv`. Add a `check_truck_depth_fit(assignment)` function in `src/optimizer.py` or a new `src/packing.py` that, given a `TruckAssignment`, walks the LIFO load sequence and checks whether the cumulative depth of simultaneously-loaded windows exceeds the truck's interior width (8 ft straight / 8.25 ft trailer). Surface failures as orange warnings on the route card — "Stop 3 (Carter Lumber): depth check failed, re-sequence or split." Do not fail the solve; this is a post-solve advisory.

---

## Item 5 — Delivery Time Windows

**Status:** Not started
**Effort:** L
**Blocked by:** Joseph meeting — need real customer time window data before implementing. Also benefits from item 2 (real drive times) being validated first.

**Key decisions needed:**
- Source of time window data: does Joseph track open/close times per customer? Are these in FeneVision or in his head?
- Hard vs. soft time windows: hard windows cause dropped orders when the math doesn't work out; soft windows add a penalty to lateness and let the solver stretch. Soft is better for the demo phase.
- Whether to expose time windows in the NL/CSV upload path or only via FeneVision import.

**Implementation approach:**
Add `time_window_open: int | None` and `time_window_close: int | None` (minutes from midnight) to `Order`. Add a `Time` dimension to the OR-Tools model in `src/optimizer.py` using `AddDimension` with per-vehicle start time anchored at depot departure. Use `CumulVar` bounds per node to enforce windows. Start with soft windows (`SetCumulVarSoftUpperBound`) so the solver reports lateness rather than dropping orders. Wire `time_window_open/close` into the FeneVision importer once Joseph confirms the column source. Surface time window violations on route cards alongside the existing capacity and mileage warnings.

---

## Item 6 — Constraint Relaxation

**Status:** Not started
**Effort:** M
**Blocked by:** Item 5 (time windows must exist before there is something to relax)

**Key decisions needed:**
- Relaxation sequence: which constraints to loosen first? Recommended order: soft time windows -> mileage cap increase -> trailer assignment for restricted customers (last resort, with explicit user confirmation).
- Whether to run relaxation automatically on drop or require a user-triggered "retry with relaxed constraints" button. A button is safer — it makes the trade-off visible.

**Implementation approach:**
After `solve()` returns dropped orders, if any orders were dropped, show a "Retry with relaxed constraints" expander in the Load Plan tab. On click, re-run the solver with a stepped relaxation: first extend `max_route_hours` by 10%, then widen time windows by 30 minutes each side, then remove soft window penalties entirely. Log which relaxation level resolved the drops and show it prominently so Joseph understands the trade-off. Do not silently relax — every relaxation step should be labeled in the UI output.

---

## Item 7 — Session Persistence

**Status:** Deferred until after Joseph meeting
**Effort:** M
**Blocked by:** Joseph meeting (need to understand whether this is a single-operator tool or multi-user)

**Key decisions needed:**
- Single-machine JSON file vs. multi-user cloud persistence. The Joseph meeting will determine scope.
- Whether to persist the full session state (orders, assignments, driver names) or just the uploaded file reference + config overrides.

**Implementation approach:**
For single-machine use: serialize `st.session_state` orders and assignments to a JSON file in the project root on every solve and on manual reorder. On app start, offer to restore the last session via a "Resume last session" button. Use Python's built-in `json` module and `dataclasses.asdict`; no new dependencies. For multi-user: Streamlit Community Cloud + Google OAuth is the path of least resistance — defer until the team decides on deployment.

---

## Item 8 — Driver Name Assignment

**Status:** DONE — sidebar input and rendered in route cards
**Effort:** —
**Blocked by:** —

No further work needed.

---

## Item 9 — MapKit JS (Apple Maps Multi-Stop Routes)

**Status:** Deferred until drivers actively complain about the per-stop workaround
**Effort:** L
**Blocked by:** Apple Developer account ($99/yr) + MapKit JS API key. Not worth pursuing until per-stop links prove insufficient in the field.

**Key decisions needed:**
- Who owns the Apple Developer account (Lindsay IT vs. personal)?
- Whether drivers use iPhones exclusively or a mix of Android (MapKit JS is iOS/macOS only).

**Implementation approach (when unblocked):**
Replace the per-stop Apple Maps links in `src/export_html.py` with an embedded MapKit JS map that constructs a multi-waypoint route from the stop sequence and generates a shareable deep link. Keep the per-stop links as a fallback. Requires a MapKit JS token served from a backend endpoint (cannot embed the raw key in the HTML file). This implies a small server-side component — reconsider scope at that point.

---

## Item 10 — FeneVision Live Feed

**Status:** Getting its own spec doc (parallel track, not decomposed here)
**Effort:** XL
**Blocked by:** Several weeks of manual validation of optimizer output against Joseph's routes; VPN access to Lindsay's network; stored procedure access confirmed by Geoff's IT contact

**Key decisions needed:** Covered in the parallel FeneVision feed spec.

---

## New Items Added 2026-06-25

### Item 11 — Driver Employment Type (Contract vs. Full-Time)

**Status:** New — not yet started
**Effort:** S
**Blocked by:** Nothing

**Key decisions needed:**
- Whether employment type affects the optimizer objective (e.g. prefer full-time drivers before opening contract drivers, similar to the straight-before-trailer preference)
- Whether this is a per-truck config field or a per-route input

**Implementation approach:**
Add an `employment_type: str` field (`"full_time"` | `"contract"`) to each truck entry in `config.yaml`. In `src/optimizer.py`, use `SetFixedCostOfVehicle` to apply a higher activation penalty to contract trucks so the solver fills full-time drivers first before opening contract capacity. Expose the employment type in the sidebar truck editor (already present for driver name) as a dropdown. Surface it on the route card so Joseph can see at a glance which trucks are contract.

---

### Item 12 — Route Sheet: Condensed View + Confirmation Checkbox

**Status:** New — not yet started
**Effort:** S
**Blocked by:** Nothing

**Key decisions needed:**
- What "condensed" means in practice: fewer columns? no LIFO section? one line per stop?
- Whether the confirmation checkbox is a pre-departure driver sign-off (for record-keeping) or a planning tool for the planner

**Implementation approach:**
In `src/export_html.py`, add a `condensed=True` rendering path that strips the LIFO load diagram and QR code, keeps only stop number / customer / address / sq ft / notes, and fits on fewer printed pages. Add a checkbox at the bottom of each route section labeled "Driver confirmed — [driver name] [date]" that prints as an empty box and, if the HTML is served interactively, POSTs a confirmation timestamp to a simple endpoint (or just writes to a local file). Start with the print-only checkbox; the interactive confirmation can be wired later.

---

## Priority Order (confirmed 2026-06-25)

1. Customer restriction lookup table (Item 1)
2. FeneVision live feed (Item 10 — parallel spec)
3. 2D packing completion / frame depth (Item 4)
4. Delivery time windows (Item 5)
5. Constraint relaxation (Item 6)
6. Driver employment type (Item 11)
7. Route sheet condensed + confirmation checkbox (Item 12)
8. Session persistence (Item 7) — after Joseph meeting
9. MapKit JS (Item 9) — after drivers complain
