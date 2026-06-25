---
date: 2026-06-25
title: FeneVision Live Feed Integration
status: Draft
---

# FeneVision Live Feed Integration

## Overview

Joseph currently exports an xlsx from FeneVision each morning and uploads it manually to the Streamlit app. This feature replaces that upload step with a direct SQL pull from FeneVision — one button click loads the day's route data from the source system. The app will run on Lindsay's internal IIS server, which sits on the same network as FeneVision, so no VPN is required at runtime.

---

## Goals

- Let Joseph load route data without leaving the app (no xlsx export, no file picker)
- Show a diff after each refresh so Joseph can see what changed since the last load (new stops, cancelled stops, priority changes)
- Keep the GA-only scope configurable — same `exclude_route_patterns` mechanism already in `config.yaml`
- Preserve the existing xlsx upload path as a fallback

---

## Non-Goals

- Scheduled / automatic pulls (Option C) — build the manual button (Option B) first; schedule is a follow-on
- Pushing data back to FeneVision (read-only)
- Replacing Joseph's judgment — the diff surfaces changes, the optimizer still needs a manual run
- Multi-plant support beyond GA (configurable but not a launch requirement)

---

## Architecture

```
FeneVision SQL Server
        │
        │  pyodbc / ODBC driver
        │  stored procedure: <TBD — see Open Questions>
        ▼
src/fenevision_db.py
  fetch_orders_from_db(cfg) → pd.DataFrame
        │
        │  same DataFrame shape as xlsx export
        ▼
import_fenevision_xlsx() (existing)
  aggregates line items → List[Order]
        │
        ▼
st.session_state["orders"]  (same as today)
```

The new module `src/fenevision_db.py` is the only new file. It owns the connection and returns a DataFrame in the same schema that `import_fenevision_xlsx()` already expects. Everything downstream (aggregation, truck-type mapping, optimizer) is unchanged.

The connection is established per-request (connect → query → close). No connection pooling for v1 — the call happens at most once per planning session.

---

## UI Changes

### "Refresh from FeneVision" button

Location: top of the **Add Orders** tab, replacing the current FeneVision xlsx upload section when a DB connection is configured. The xlsx uploader stays as a fallback (rendered below the button).

Button label: **"Refresh from FeneVision"**

On click:
1. Spinner: "Pulling route data from FeneVision…"
2. Call `fetch_orders_from_db()` → pass result through `import_fenevision_xlsx()` logic
3. If `st.session_state["orders"]` is non-empty, compute and display a diff (see below)
4. Replace `st.session_state["orders"]` with the new order list
5. Show a success banner: "Loaded N stops from FeneVision — {timestamp}"

If the connection fails, show an error banner (see Error Handling). Do not clear the existing order list.

### Diff view

Shown between the button and the success banner when orders were already loaded. Compact table with three sections:

| Section | What it shows |
|---|---|
| New stops | Stops in the fresh pull not present in the previous load (by order_id) |
| Cancelled stops | Stops in the previous load not present in the fresh pull |
| Changed stops | Same order_id, different sqft or truck type |

If there are no changes, show: "No changes since last load."

The diff is informational — Joseph confirms it and then runs the optimizer manually.

---

## Config Changes

Add a `fenevision_db` block to `config.yaml`:

```yaml
fenevision_db:
  enabled: false                        # set true on IIS server
  driver: "{ODBC Driver 17 for SQL Server}"   # TBD — confirm with IT
  server: "<TBD>"                       # FeneVision SQL Server hostname or IP
  database: "<TBD>"                     # FeneVision database name
  trusted_connection: true              # Windows auth (preferred on domain-joined IIS)
  # username and password only if trusted_connection: false
  username: ""
  password: ""
  stored_procedure: "<TBD>"            # proc name that returns Orders by Route rows
  route_date_param: "today"            # "today" = pull today's date at runtime; or a fixed YYYY-MM-DD
```

When `fenevision_db.enabled` is `false`, the UI shows the xlsx uploader only (current behavior). No DB import attempted.

`exclude_route_patterns` in the existing `routing` block already handles GA-only scoping — no new config key needed.

---

## Data Flow

1. `app.py` reads `fenevision_db` from `config.yaml` at render time
2. If `enabled: true`, the Refresh button is rendered
3. On click, `fenevision_db.fetch_orders_from_db(cfg)` executes the stored procedure with the route date as a parameter and returns a `pd.DataFrame`
4. The DataFrame is passed directly into the existing aggregation and type-mapping logic in `import_fenevision_xlsx()` (refactored to accept a DataFrame as well as a file path)
5. Exclusion patterns (`routing.exclude_route_patterns`) are applied at this step, same as today
6. The resulting `List[Order]` replaces `st.session_state["orders"]`
7. The previous order list (snapshot before replacement) is used to compute the diff and then discarded

---

## Error Handling

| Condition | User-facing message | Behavior |
|---|---|---|
| DB not reachable (server down, wrong hostname) | "Cannot reach FeneVision database. Check that the IIS server has network access." | Error banner; existing orders unchanged |
| ODBC driver missing | "ODBC driver not installed. Contact IT." | Error banner; existing orders unchanged |
| Auth failure | "FeneVision login failed. Check credentials in config.yaml." | Error banner; existing orders unchanged |
| Stored procedure returns 0 rows | "FeneVision returned no routes for {date}. Is {date} a shipping day?" | Warning banner; existing orders unchanged |
| Stored procedure returns unexpected columns | Raise `ValueError` with column list — same guard already in `import_fenevision_xlsx()` | Error banner with column list; existing orders unchanged |
| Partial data (some rows malformed) | Log skipped rows; surface count in the success banner | Proceed with valid rows; show "N stops loaded, M rows skipped" |

In all error cases, the existing order list in session state is preserved so Joseph can still run the optimizer against the last good load.

---

## Open Questions

1. **SQL connection string** — hostname, database name, and ODBC driver version. Needs IT.
2. **Stored procedure name** — what proc returns the "Orders by Route" rows, and what parameters does it take (date, plant, route filter)? Needs FeneVision admin or Geoff.
3. **Auth method** — Windows trusted connection (preferred, no stored password) vs. SQL login. Depends on whether the IIS app pool identity has SQL access. Needs IT.
4. **IT approval for IIS deployment** — is Lindsay IT aware and on board? Any change control process?
5. **Route date logic** — does Joseph always pull for today's date, or does he sometimes pull for tomorrow (pre-planning the day before)? If the latter, add a date picker to the Refresh button UI.
6. **`import_fenevision_xlsx()` refactor scope** — the function currently only accepts a file path or file-like object. We can either (a) refactor to also accept a DataFrame, or (b) have `fetch_orders_from_db()` write a temporary in-memory BytesIO xlsx and pass it in. Option (a) is cleaner; confirm before touching the function signature.

---

## Out of Scope

- Scheduled automatic pulls
- Writing data back to FeneVision
- Multi-plant support at launch (configurable but untested)
- Driver roster / name assignment (separate roadmap item)
- Real driving distances (separate roadmap item — OSRM or Google Maps)
