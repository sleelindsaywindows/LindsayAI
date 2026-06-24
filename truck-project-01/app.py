import copy
import os
import yaml
import streamlit as st
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

import datetime
from src.models import Order, Truck
from src.parser import parse_and_verify
from src.import_fenevision import import_fenevision_xlsx
from src.export_html import generate_html_routes
from src.optimizer import (
    solve,
    validate_inputs,
    check_route_cap,
    geocode_address,
    GEOCODING_AVAILABLE,
)

try:
    import folium
    from streamlit_folium import st_folium
    FOLIUM_AVAILABLE = True
except ImportError:
    FOLIUM_AVAILABLE = False

CONFIG_PATH = Path("config.yaml")

_SOLVE_ANIMATION_HTML = """
<div style="text-align:center;padding:2.5rem 0;font-family:Arial,sans-serif;">
  <svg width="72" height="72" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg"
       style="display:block;margin:0 auto 1.25rem;">
    <style>
      @keyframes _fp{0%,100%{opacity:.2}50%{opacity:.9}}
      .wp{fill:#c8e6f7;animation:_fp 2.5s ease-in-out infinite;}
      .wf{fill:none;stroke:#1a5fa8;stroke-width:3;}
      .wb{stroke:#1a5fa8;stroke-width:2;}
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
<div style="text-align:center;padding:1.5rem 0;font-family:Arial,sans-serif;
            color:#1a7a1a;font-size:1rem;">
  &#10003; Routes are ready &mdash; head to the <strong>Load Plan</strong> tab
  to see assignments and export for drivers.
</div>
"""


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def init_state():
    defaults = {
        "orders": [],
        "pending": None,
        "assignments": [],
        "dropped": [],
        "uploader_key": 0,
        "auto_run_pending": False,
        "first_visit": True,     # drives one-time onboarding modal
        "onboarding_slide": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


@st.dialog("🪟 Lindsay Windows Load Planner", width="large")
def _onboarding_modal():
    slides = [
        (
            "👋 Welcome!",
            "#1a5fa8",
            "This tool takes your FeneVision delivery data and builds an optimized load plan "
            "for each truck — automatically. It sequences stops, enforces truck restrictions "
            "for homebuilder customers, and prints route sheets drivers can scan with their phone.",
        ),
        (
            "📂 Upload your FeneVision file",
            "#2e7d32",
            "Go to **Add Orders** and upload the xlsx export from FeneVision (GA Trucks format). "
            "The tool reads the 'Orders by Route' sheet, groups line items into stops, and "
            "loads them automatically. Interplant routes are excluded based on your config.",
        ),
        (
            "🗺️ Review the Load Plan",
            "#6a1b9a",
            "Head to **Load Plan** — it runs automatically after upload. "
            "You'll see each truck's delivery sequence and LIFO loading order. "
            "Click **Regenerate Load Plan** anytime to re-run with updated settings.",
        ),
        (
            "📊 Check the Analysis tab",
            "#e65100",
            "**Analysis** shows utilization per truck, a homebuilder constraint audit "
            "(flags any stop assigned to the wrong truck type), and a cost comparison "
            "against the current manual routing process. Export an Excel report from here.",
        ),
        (
            "🖨️ Print for drivers",
            "#1565c0",
            "Click **Export Route Sheets (HTML)** in the Load Plan tab and open it in your browser. "
            "Hit **Cmd+P** to print — each truck gets its own page.\n\n"
            "**Morning dispatch workflow:**\n"
            "1. Open the app → upload today's FeneVision file\n"
            "2. Wait ~30 seconds for the optimizer to run\n"
            "3. Export route sheets → print one page per driver\n"
            "4. Drivers scan the QR code to open their full route in Google Maps\n"
            "5. Hand sheets to drivers before they leave the dock",
        ),
    ]

    idx = st.session_state.get("onboarding_slide", 0)
    title, accent, body = slides[idx]

    # Progress dots
    dots = ""
    for i in range(len(slides)):
        color = accent if i == idx else "#dde3ea"
        dots += (
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            f'background:{color};margin:0 5px;transition:background 0.3s;"></span>'
        )

    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{accent}18,{accent}08);
                    border-left:4px solid {accent};border-radius:8px;
                    padding:18px 20px;margin-bottom:16px;">
            <div style="font-size:1.35rem;font-weight:700;color:{accent};
                        margin-bottom:4px;">{title}</div>
            <div style="color:#444;margin-top:2px;font-size:0.8rem;letter-spacing:0.5px;">
                STEP {idx + 1} OF {len(slides)}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(body)

    st.markdown(
        f'<div style="text-align:center;margin-top:20px;margin-bottom:4px;">{dots}</div>',
        unsafe_allow_html=True,
    )

    col_back, col_spacer, col_next = st.columns([1, 3, 1])
    if idx > 0:
        if col_back.button("← Back", key="ob_back"):
            st.session_state.onboarding_slide = idx - 1
            st.rerun()
    if idx < len(slides) - 1:
        if col_next.button("Next →", key="ob_next", type="primary"):
            st.session_state.onboarding_slide = idx + 1
            st.rerun()
    else:
        if col_next.button("Got it!", type="primary", key="ob_done"):
            st.session_state.first_visit = False
            st.session_state.onboarding_slide = 0
            st.session_state._jump_orders = True
            st.rerun()


def render_sidebar(cfg: dict) -> dict:
    st.sidebar.title("⚙️ Configuration")

    st.sidebar.subheader("Measurement Unit")
    unit = st.sidebar.text_input("Unit (internal key)", value=cfg["measurement"]["unit"])
    label = st.sidebar.text_input("Display label", value=cfg["measurement"]["label"])
    abbr = st.sidebar.text_input("Abbreviation", value=cfg["measurement"]["abbreviation"])

    st.sidebar.subheader("Depot")
    depot_name = st.sidebar.text_input("Warehouse name", value=cfg["depot"].get("name", ""))
    depot_addr = st.sidebar.text_input("Warehouse address", value=cfg["depot"].get("address", ""))

    st.sidebar.subheader("Routing")
    routing = cfg.get("routing", {})
    max_hours = st.sidebar.number_input(
        "Max route hours / driver",
        value=float(routing.get("max_route_hours", 9.0)),
        min_value=1.0, max_value=14.0, step=0.5,
        help="Joseph's 9-hour cap (11 hrs is legal max; 9 gives a buffer). Includes drive + unload time.",
    )
    stop_time = st.sidebar.number_input(
        "Unload time per stop (minutes)",
        value=float(routing.get("stop_time_minutes", 45)),
        min_value=5.0, max_value=180.0, step=5.0,
        help="Average time to unload at each stop. 30–60 min for neighborhood deliveries without a dock.",
    )
    max_fill_pct = st.sidebar.slider(
        "Max truck fill %",
        min_value=50, max_value=100, step=5,
        value=int(routing.get("max_fill_pct", 90)),
        help="Optimizer won't fill trucks past this percentage. 90% leaves room for real-world loading variance.",
    )
    manual_truck_count = st.sidebar.number_input(
        "Manual route count (for comparison)",
        value=int(routing.get("manual_truck_count", 13)),
        min_value=1, step=1,
        help="How many trucks were used in the current manual process. Used to compute trucks saved.",
    )

    st.sidebar.subheader("Truck Fleet")
    h1, h2 = st.sidebar.columns([3, 2])
    h1.caption("Truck Name")
    h2.caption(f"Max ({abbr})")

    updated_trucks = []
    for i, truck in enumerate(cfg["trucks"]):
        c1, c2 = st.sidebar.columns([3, 2])
        name = c1.text_input("Name", value=truck["name"], key=f"t_name_{i}", label_visibility="collapsed")
        cap = c2.number_input(f"Max", value=float(truck["max_capacity"]), key=f"t_cap_{i}", min_value=0.1, label_visibility="collapsed")
        updated_trucks.append({
            "name": name,
            "type": truck["type"],
            "max_capacity": cap,
            "fixed_cost": truck.get("fixed_cost", 5.0),
            "cost_per_mile": truck.get("cost_per_mile", 0.0),
        })

    if st.sidebar.button("＋ Add Truck"):
        new_cfg = {
            "measurement": {"unit": unit, "label": label, "abbreviation": abbr},
            "trucks": updated_trucks + [{"name": "New Truck", "type": "straight", "max_capacity": 176.0, "fixed_cost": 5.0, "cost_per_mile": 0.0}],
            "depot": {"name": depot_name, "address": depot_addr},
            "routing": {
                "max_route_hours": max_hours,
                "stop_time_minutes": stop_time,
                "max_fill_pct": max_fill_pct,
                "manual_truck_count": manual_truck_count,
                "straight_speed_mph": routing.get("straight_speed_mph", 47),
                "trailer_speed_mph": routing.get("trailer_speed_mph", 40),
                "solver_time_limit_seconds": routing.get("solver_time_limit_seconds", 15),
                "exclude_route_patterns": routing.get("exclude_route_patterns", []),
            },
        }
        save_config(new_cfg)
        st.rerun()

    if st.sidebar.button("Save Config", type="primary"):
        new_cfg = {
            "measurement": {"unit": unit, "label": label, "abbreviation": abbr},
            "trucks": updated_trucks,
            "depot": {"name": depot_name, "address": depot_addr},
            "routing": {
                "max_route_hours": max_hours,
                "stop_time_minutes": stop_time,
                "max_fill_pct": max_fill_pct,
                "manual_truck_count": manual_truck_count,
                "straight_speed_mph": routing.get("straight_speed_mph", 47),
                "trailer_speed_mph": routing.get("trailer_speed_mph", 40),
                "solver_time_limit_seconds": routing.get("solver_time_limit_seconds", 15),
                "exclude_route_patterns": routing.get("exclude_route_patterns", []),
            },
        }
        save_config(new_cfg)
        st.sidebar.success("Saved.")
        st.rerun()

    st.sidebar.divider()
    if st.sidebar.button("Need Help?", key="sidebar_help", use_container_width=True):
        st.session_state.first_visit = True
        st.session_state.onboarding_slide = 0
        st.rerun()

    return cfg


def render_add_orders(cfg: dict):
    abbr = cfg["measurement"]["abbreviation"]
    unit_label = cfg["measurement"]["label"]

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
            st.session_state._jump_plan = True
            msg = f"✅ {len(new_orders)} stops loaded"
            if excluded:
                msg += f" · {len(excluded)} interplant route(s) excluded"
            if skipped:
                msg += f" · {len(skipped)} placeholder stop(s) skipped"
            msg += " — heading to Load Plan to optimize…"
            st.success(msg)
            st.rerun()

    st.divider()
    st.subheader("Upload CSV")
    st.caption(
        f"Required columns: `order_id`, `customer_name`, `address`, `capacity_units` ({abbr})  "
        f"— optional: `priority`, `notes`"
    )
    uploaded = st.file_uploader(
        "Choose file", type=["csv"],
        key=f"uploader_{st.session_state.uploader_key}",
        label_visibility="collapsed",
    )
    if uploaded:
        df = pd.read_csv(uploaded)
        required = {"order_id", "customer_name", "address", "capacity_units"}
        missing = required - set(df.columns)
        if missing:
            st.error(f"CSV missing columns: {missing}")
        else:
            new_orders = [
                Order(
                    order_id=str(row["order_id"]),
                    customer_name=str(row["customer_name"]),
                    address=str(row["address"]),
                    capacity_units=float(row["capacity_units"]),
                    priority=0 if pd.isna(row.get("priority", 0)) else int(row.get("priority", 0)),
                    notes="" if pd.isna(row.get("notes", "")) else str(row.get("notes", "")),
                )
                for _, row in df.iterrows()
            ]
            st.session_state.orders.extend(new_orders)
            st.session_state.assignments = []
            st.session_state.dropped = []
            st.session_state.uploader_key += 1
            st.session_state.auto_run_pending = True
            st.rerun()

    st.divider()
    st.subheader("Add in Plain English")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_key:
        st.warning("ANTHROPIC_API_KEY not set — add it to your .env file to enable NL parsing. "
                   "CSV upload and optimization still work without it.")

    col_input, col_btn = st.columns([5, 1])
    nl_text = col_input.text_input(
        "Order description",
        placeholder=f'e.g. "Order 2241, Riverside Homes, 450 River Rd Macon GA, 48 {abbr}, rush"',
        label_visibility="collapsed",
        disabled=not has_key,
    )
    parse_clicked = col_btn.button("Parse", disabled=not has_key, use_container_width=True)

    if parse_clicked and nl_text:
        with st.spinner("Parsing + verifying…"):
            try:
                parsed, verif = parse_and_verify(nl_text, unit_label)
                st.session_state.pending = {"raw": nl_text, "parsed": parsed, "verif": verif}
            except Exception as e:
                st.error(f"Parse error: {e}")

    if st.session_state.pending:
        p = st.session_state.pending
        verif = p["verif"]
        parsed = p["parsed"]

        st.divider()
        st.markdown("**Verification Agent**")
        if verif.get("confident", False):
            st.success(verif.get("summary", "Looks good."))
        else:
            st.warning(verif.get("summary", "Review required."))
            for issue in verif.get("issues", []):
                st.caption(f"⚠ {issue}")

        c1, c2 = st.columns(2)
        oid   = c1.text_input("Order ID",              value=str(parsed.get("order_id", "")),      key="p_id")
        cname = c2.text_input("Customer Name",          value=str(parsed.get("customer_name", "")), key="p_name")
        addr  = c1.text_input("Ship-To Address",        value=str(parsed.get("address", "")),        key="p_addr")
        cap   = c2.number_input(f"Floor Space ({abbr})", value=float(parsed.get("capacity_units", 0)), min_value=0.0, key="p_cap")
        pri   = c1.number_input("Priority (0=normal, 10=urgent)", value=int(parsed.get("priority", 0)), min_value=0, max_value=10, key="p_pri")
        notes = c2.text_input("Notes (dock info, gate codes…)", value=str(parsed.get("notes", "")), key="p_notes")

        btn_ok, btn_no = st.columns(2)
        if btn_ok.button("✓ Confirm & Add", type="primary", use_container_width=True):
            st.session_state.orders.append(Order(
                order_id=oid, customer_name=cname, address=addr,
                capacity_units=cap, priority=pri, notes=notes,
            ))
            st.session_state.pending = None
            st.session_state.assignments = []
            st.session_state.dropped = []
            st.rerun()
        if btn_no.button("✗ Discard", use_container_width=True):
            st.session_state.pending = None
            st.rerun()

    st.divider()
    st.subheader(f"Current Orders ({len(st.session_state.orders)})")
    if not st.session_state.orders:
        st.caption("No orders yet.")
        return

    rows = [
        {
            "Order ID": o.order_id,
            "Customer": o.customer_name,
            "Ship-To Address": o.address,
            f"Floor Space ({abbr})": o.capacity_units,
            "Priority": o.priority,
            "Notes": o.notes,
        }
        for o in st.session_state.orders
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True)

    if st.button("🗑 Clear All Orders"):
        st.session_state.orders = []
        st.session_state.assignments = []
        st.session_state.dropped = []
        st.rerun()


_ADDR_PLACEHOLDERS = {"none", "n/a", "tbd", "unknown", "na", ""}
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS",
    "KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV",
    "WI","WY","DC",
}
import re as _re
_WORD_RE = _re.compile(r'\b[A-Za-z]{2}\b')

def _has_state(addr: str) -> bool:
    return any(w.upper() in _US_STATES for w in _WORD_RE.findall(addr))

def _flag_address_issues(assignments):
    """Return list of (truck_name, stop_num, customer, address, issue) for suspect addresses."""
    issues = []
    for a in assignments:
        for stop in a.stops:
            addr = (stop.order.address or "").strip()
            low = addr.lower()
            if not addr or low in _ADDR_PLACEHOLDERS:
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "Blank or placeholder address"))
            elif len(addr) < 15:
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "Address too short — may be incomplete"))
            elif not any(ch.isdigit() for ch in addr):
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "No house number — may be a street-level or new construction address, verify before printing"))
            elif not _has_state(addr):
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "No US state detected (e.g. GA, ga)"))
    return issues


def render_load_plan(cfg: dict):
    abbr = cfg["measurement"]["abbreviation"]
    routing_cfg = cfg.get("routing", {})
    max_route_hours = float(routing_cfg.get("max_route_hours", 9.0))
    stop_time_minutes = float(routing_cfg.get("stop_time_minutes", 45.0))
    straight_speed_mph = float(routing_cfg.get("straight_speed_mph", 47.0))
    trailer_speed_mph = float(routing_cfg.get("trailer_speed_mph", 40.0))
    max_fill_pct = float(routing_cfg.get("max_fill_pct", 90)) / 100.0
    manual_truck_count = int(routing_cfg.get("manual_truck_count", 13))
    solver_time_limit = int(routing_cfg.get("solver_time_limit_seconds", 15))

    trucks = [
        Truck(
            name=t["name"],
            truck_type=t["type"],
            max_capacity=t["max_capacity"] * max_fill_pct,
            fixed_cost=t.get("fixed_cost", 5.0),
            cost_per_mile=t.get("cost_per_mile", 0.0),
        )
        for t in cfg["trucks"]
    ]

    if not st.session_state.orders:
        st.info("Add orders in the 'Add Orders' tab first.")
        return

    depot_addr = cfg["depot"].get("address", "")
    depot_coords = (33.749, -84.388)

    total_needed = sum(o.capacity_units for o in st.session_state.orders)
    fleet_cap = sum(t.max_capacity for t in trucks)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Orders", len(st.session_state.orders))
    m2.metric(f"Space Needed ({abbr})", f"{total_needed:.0f}")
    m3.metric(f"Fleet Capacity ({abbr})", f"{fleet_cap:.0f}")
    m4.metric("Fleet Utilization", f"{(total_needed / fleet_cap * 100) if fleet_cap else 0:.0f}%")

    if total_needed > fleet_cap:
        st.error(f"Orders exceed fleet capacity by {total_needed - fleet_cap:.0f} {abbr}. Add trucks or remove orders.")

    with st.expander("⚙️ Distance routing (optional — off by default)", expanded=False):
        st.caption(
            "By default the optimizer assigns stops to trucks without real driving distances — "
            "it still produces valid load plans, just with unoptimized stop order within each truck. "
            "Enable geocoding to add real distance-based sequencing (Haversine straight-line, ~5 min for 50 stops). "
            "Swap for OSRM or Google Maps Distance Matrix API when you're ready for true road distances."
        )
        geocode_on = st.checkbox(
            "Enable geocoding (uses free Nominatim — slow on large loads)",
            value=False,
            disabled=not GEOCODING_AVAILABLE,
            help="Install geopy to enable: pip install geopy" if not GEOCODING_AVAILABLE else "",
        )

    if st.session_state.get("auto_run_pending") and st.session_state.orders:
        orders_copy = copy.deepcopy(st.session_state.orders)
        errors = validate_inputs(orders_copy, trucks)
        if not errors:
            st.session_state.auto_run_pending = False
            anim = st.empty()
            anim.markdown(_SOLVE_ANIMATION_HTML, unsafe_allow_html=True)
            assignments, dropped = solve(
                orders_copy, trucks, depot_coords,
                max_route_hours=max_route_hours,
                stop_time_minutes=stop_time_minutes,
                straight_speed_mph=straight_speed_mph,
                trailer_speed_mph=trailer_speed_mph,
                solver_time_limit=solver_time_limit,
            )
            st.session_state.assignments = assignments
            st.session_state.dropped = dropped
            anim.markdown(_SOLVE_DONE_HTML, unsafe_allow_html=True)
        else:
            st.session_state.auto_run_pending = False
            for e in errors:
                st.error(e)

    btn_label = "🔄 Regenerate Load Plan" if st.session_state.assignments else "Generate Load Plan"
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

            warnings = check_route_cap(
                orders_copy, depot_coords,
                max_route_hours=max_route_hours,
                stop_time_minutes=stop_time_minutes,
                straight_speed_mph=straight_speed_mph,
                trailer_speed_mph=trailer_speed_mph,
            )
            for w in warnings:
                st.warning(f"Multi-day route flag: {w}")

        anim = st.empty()
        anim.markdown(_SOLVE_ANIMATION_HTML, unsafe_allow_html=True)
        assignments, dropped = solve(
            orders_copy, trucks, depot_coords,
            max_route_hours=max_route_hours,
            stop_time_minutes=stop_time_minutes,
            avg_speed_mph=avg_speed_mph,
            solver_time_limit=solver_time_limit,
        )
        st.session_state.assignments = assignments
        st.session_state.dropped = dropped
        anim.markdown(_SOLVE_DONE_HTML, unsafe_allow_html=True)

    if not st.session_state.assignments and not st.session_state.dropped:
        return

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

        _asgn = st.session_state.assignments
        _total_stops = sum(len(a.stops) for a in _asgn)
        _avg_util = sum(a.utilization_pct for a in _asgn) / len(_asgn) if _asgn else 0
        _total_miles = sum(a.route_distance_miles for a in _asgn if a.route_distance_miles)
        _trucks_saved = manual_truck_count - len(_asgn)
        _rm1, _rm2, _rm3, _rm4 = st.columns(4)
        _rm1.metric("Trucks Used", len(_asgn), delta=f"{_trucks_saved} saved vs manual" if _trucks_saved > 0 else None)
        _rm2.metric("Total Stops", _total_stops)
        _rm3.metric("Avg Utilization", f"{_avg_util:.0f}%")
        _rm4.metric("Est. Total Miles", f"{_total_miles:.0f}" if _total_miles else "—")

        if _trucks_saved > 0:
            st.info(
                f"**{_trucks_saved} truck{'s' if _trucks_saved != 1 else ''} saved on this run.** "
                f"Optimizer used {len(_asgn)} trucks. "
                f"Current process used {manual_truck_count}."
            )

        # Bay width limits per truck type (inches). Windows wider than this can't physically load.
        _BAY_WIDTH = {"straight": 96.0, "trailer": 99.0}
        packing_issues = []
        for a in st.session_state.assignments:
            bay = _BAY_WIDTH.get(a.truck.truck_type, 96.0)
            for stop in a.stops:
                w = getattr(stop.order, 'max_window_width_inches', None)
                if w is not None and w > bay:
                    packing_issues.append((
                        a.truck.name, stop.stop_number,
                        stop.order.customer_name, w, bay,
                    ))
        if packing_issues:
            with st.expander(
                f"🚨 {len(packing_issues)} stop(s) have windows wider than the truck bay — WILL NOT FIT",
                expanded=True,
            ):
                st.caption(
                    "These windows exceed the physical bay width of their assigned truck. "
                    "Re-assign to a wider truck or verify the window width in FeneVision."
                )
                for truck_name, stop_num, customer, w, bay in packing_issues:
                    st.error(
                        f"**{truck_name} → Stop {stop_num} — {customer}**  \n"
                        f"Max window width {w:.0f}\" exceeds truck bay {bay:.0f}\""
                    )

        addr_issues = _flag_address_issues(st.session_state.assignments)
        if addr_issues:
            with st.expander(
                f"⚠️ {len(addr_issues)} address(es) flagged for review — verify before printing",
                expanded=True,
            ):
                st.caption(
                    "These stops have addresses that may not route correctly in Google/Apple Maps. "
                    "Check with the route coordinator before handing sheets to drivers."
                )
                for truck_name, stop_num, customer, addr, issue in addr_issues:
                    st.warning(
                        f"**{truck_name} → Stop {stop_num} — {customer}**  \n"
                        f"`{addr or '(blank)'}` — {issue}"
                    )

    if st.session_state.dropped:
        dropped_ids = ", ".join(o.order_id for o in st.session_state.dropped)
        st.error(
            f"⚠ {len(st.session_state.dropped)} order(s) could not be assigned "
            f"(fleet capacity exceeded or route cap hit): {dropped_ids}"
        )

    if not st.session_state.assignments:
        st.error("Optimizer found no solution. Check that fleet capacity covers total order space.")
        return

    st.divider()
    for assignment in st.session_state.assignments:
        dist_str = f" · {assignment.route_distance_miles:.0f} mi" if assignment.route_distance_miles else ""
        _rth = getattr(assignment, 'route_time_hours', 0.0)
        time_str = f" · ~{_rth:.1f} hr" if _rth else ""
        label = (
            f"🚛 {assignment.truck.name} — "
            f"{assignment.total_capacity_used:.0f}/{assignment.truck.max_capacity:.0f} {abbr} "
            f"({assignment.utilization_pct:.0f}% utilized){dist_str}{time_str}"
        )
        with st.expander(label, expanded=True):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Delivery Sequence**")
                for stop in assignment.stops:
                    st.markdown(
                        f"**{stop.stop_number}.** {stop.order.customer_name}  \n"
                        f"<span style='color:gray;font-size:0.85em'>"
                        f"{stop.order.address} · {stop.order.capacity_units:.0f} {abbr}"
                        f"{'  · P' + str(stop.order.priority) if stop.order.priority > 0 else ''}"
                        f"</span>",
                        unsafe_allow_html=True,
                    )
                    if stop.order.notes:
                        st.caption(f"  Note: {stop.order.notes}")
            with c2:
                st.markdown("**Load Sequence** *(load #1 first — loads deepest into truck)*")
                for i, stop in enumerate(assignment.load_sequence, 1):
                    st.markdown(f"**Load {i}.** {stop.order.customer_name} — {stop.order.capacity_units:.0f} {abbr}")

            if FOLIUM_AVAILABLE:
                stops_with_coords = [s for s in assignment.stops if s.order.lat is not None]
                if stops_with_coords:
                    all_lats = [depot_coords[0]] + [s.order.lat for s in stops_with_coords]
                    all_lons = [depot_coords[1]] + [s.order.lon for s in stops_with_coords]
                    center = [sum(all_lats) / len(all_lats), sum(all_lons) / len(all_lons)]
                    m = folium.Map(location=center, zoom_start=8)
                    folium.Marker(
                        list(depot_coords),
                        popup="Depot (Lindsay Windows)",
                        icon=folium.Icon(color="red", icon="star", prefix="fa"),
                    ).add_to(m)
                    for stop in assignment.stops:
                        if stop.order.lat is not None:
                            folium.Marker(
                                [stop.order.lat, stop.order.lon],
                                popup=f"Stop {stop.stop_number}: {stop.order.customer_name}",
                                icon=folium.DivIcon(
                                    html=(
                                        f'<div style="font-size:13px;font-weight:bold;color:white;'
                                        f'background:#1f77b4;border-radius:50%;width:26px;height:26px;'
                                        f'text-align:center;line-height:26px;border:2px solid white;">'
                                        f'{stop.stop_number}</div>'
                                    ),
                                    icon_size=(26, 26),
                                    icon_anchor=(13, 13),
                                ),
                            ).add_to(m)
                    route_pts = (
                        [list(depot_coords)]
                        + [[s.order.lat, s.order.lon] for s in assignment.stops if s.order.lat is not None]
                        + [list(depot_coords)]
                    )
                    folium.PolyLine(route_pts, color="#1f77b4", weight=2.5, opacity=0.8).add_to(m)
                    st_folium(m, width="100%", height=350, returned_objects=[])

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



def render_analysis(cfg: dict):
    if not st.session_state.assignments:
        st.info("Run the optimizer in the Load Plan tab first.")
        return

    assignments = st.session_state.assignments
    dropped = st.session_state.dropped
    orders = st.session_state.orders

    abbr = cfg["measurement"]["abbreviation"]
    has_miles = any(a.route_distance_miles for a in assignments)

    # --- Summary table ---
    st.subheader("Summary")
    summary_rows = []
    for a in assignments:
        miles = a.route_distance_miles if a.route_distance_miles else 0.0
        cost = miles * a.truck.cost_per_mile if miles else None
        summary_rows.append({
            "Truck": a.truck.name,
            "Type": a.truck.truck_type,
            "Stops": len(a.stops),
            f"Sq Ft Used": round(a.total_capacity_used, 1),
            f"Capacity ({abbr})": round(a.truck.max_capacity, 1),
            "Utilization %": round(a.utilization_pct, 1),
            "Est. Miles": round(miles, 1) if miles else "N/A",
            "Est. Cost ($)": round(cost, 2) if cost is not None else "N/A",
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True)

    # --- Utilization bar chart ---
    st.subheader("Utilization % per Truck")
    st.caption(
        "Utilization = sq ft used ÷ truck capacity. "
        "**85–95% is the target range** — high enough to avoid wasting truck space, "
        "low enough to handle real-world variation (odd-sized windows, last-minute adds). "
        "100% means zero buffer; anything that doesn't fit gets dropped. "
        "Under 70% usually means a truck could be consolidated."
    )
    chart_data = pd.DataFrame(
        {"Utilization %": [a.utilization_pct for a in assignments]},
        index=[a.truck.name for a in assignments],
    )
    st.bar_chart(chart_data)

    if not has_miles:
        st.caption("Mileage estimates require geocoding — enable in Load Plan tab.")

    st.divider()

    # --- Constraint audit ---
    st.subheader("Homebuilder Truck-Type Constraint Audit")
    st.caption(
        "Some customers — typically homebuilders in residential subdivisions — physically cannot "
        "receive a 53-ft trailer (tight driveways, low-clearance streets). These stops are flagged "
        "in FeneVision with a 26-ft truck type. The optimizer enforces this constraint so they are "
        "never assigned to a trailer. **PASS = all restricted stops landed on 26-ft straight trucks.** "
        "A FAIL here means a driver would show up with the wrong truck and couldn't deliver."
    )
    restricted = [
        (a, stop)
        for a in assignments
        for stop in a.stops
        if stop.order.allowed_truck_types
    ]
    if not restricted:
        st.caption("No homebuilder (truck-restricted) stops in current plan.")
    else:
        audit_rows = []
        fail_count = 0
        for a, stop in restricted:
            o = stop.order
            passed = a.truck.truck_type in o.allowed_truck_types
            if not passed:
                fail_count += 1
            audit_rows.append({
                "Order ID": o.order_id,
                "Customer": o.customer_name,
                "Assigned Truck": a.truck.name,
                "Truck Type": a.truck.truck_type,
                "Restriction": ", ".join(o.allowed_truck_types),
                "Result": "PASS" if passed else "FAIL",
            })
        audit_df = pd.DataFrame(audit_rows)

        def _highlight_result(row):
            color = "background-color: #ccffcc" if row["Result"] == "PASS" else "background-color: #ff9999"
            return [color] * len(row)

        st.dataframe(
            audit_df.style.apply(_highlight_result, axis=1),
            use_container_width=True,
        )
        if fail_count:
            st.error(f"{fail_count} homebuilder constraint violation(s) — check assignments above.")
        else:
            st.success(f"All {len(restricted)} homebuilder stops passed the truck-type constraint.")

    st.divider()

    # --- Cost comparison ---
    st.subheader("Cost Comparison")
    total_miles = sum(a.route_distance_miles for a in assignments if a.route_distance_miles)
    total_cost = sum(
        a.route_distance_miles * a.truck.cost_per_mile
        for a in assignments
        if a.route_distance_miles
    )
    util_values = [a.utilization_pct for a in assignments]
    avg_util = round(sum(util_values) / len(util_values), 1) if util_values else 0.0

    comp_rows = [
        {"Metric": "Trucks Used", "Optimizer": len(assignments), "Current Process": 13},
        {"Metric": "Total Est. Miles", "Optimizer": round(total_miles, 1) if has_miles else "N/A", "Current Process": "unknown"},
        {"Metric": "Total Est. Cost ($)", "Optimizer": round(total_cost, 2) if has_miles else "N/A", "Current Process": "unknown"},
        {"Metric": "Avg Utilization %", "Optimizer": avg_util, "Current Process": "unknown"},
        {"Metric": "Dropped Orders", "Optimizer": len(dropped), "Current Process": "unknown"},
    ]
    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True)
    st.caption("Current process cost computable once the 6/17 manual route sheet is entered.")

    st.divider()

    # --- Excel export ---
    st.subheader("Export to Excel")
    import tempfile, os
    from src.analysis import generate_report

    if st.button("Generate Excel Report"):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            generate_report(
                assignments=assignments,
                dropped=dropped,
                orders=orders,
                output_path=tmp_path,
            )
            with open(tmp_path, "rb") as f:
                st.session_state["analysis_xlsx_bytes"] = f.read()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if st.session_state.get("analysis_xlsx_bytes"):
        st.download_button(
            "⬇ Download lindsay_analysis.xlsx",
            data=st.session_state["analysis_xlsx_bytes"],
            file_name="lindsay_analysis.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="analysis_download",
        )


def main():
    st.set_page_config(page_title="Lindsay Windows — Load Planner", page_icon="🪟", layout="wide")
    init_state()

    if st.session_state.first_visit:
        _onboarding_modal()

    cfg = load_config()
    render_sidebar(cfg)
    cfg = load_config()  # reload in case sidebar saved

    st.title("🪟 Lindsay Windows — Load Planner")

    if st.session_state.get("_jump_orders"):
        st.session_state._jump_orders = False
        st.components.v1.html(
            "<script>setTimeout(function(){try{"
            "var t=window.parent.document.querySelectorAll('[data-baseweb=tab]');"
            "if(t&&t.length>0)t[0].click();"
            "}catch(e){}},400);</script>",
            height=0,
        )
    if st.session_state.get("_jump_plan"):
        st.session_state._jump_plan = False
        st.components.v1.html(
            "<script>setTimeout(function(){try{"
            "var t=window.parent.document.querySelectorAll('[data-baseweb=tab]');"
            "if(t&&t.length>1)t[1].click();"
            "}catch(e){}},400);</script>",
            height=0,
        )

    st.components.v1.html("""
<script>
(function(){
  var doc = window.parent.document;
  var existing = doc.getElementById('lw-help-fab');
  if (existing) existing.remove();
  var btn = doc.createElement('button');
  btn.id = 'lw-help-fab';
  btn.innerHTML = '&#10067; Need Help?';
  btn.style.cssText = [
    'position:fixed','bottom:24px','right:24px','z-index:999999',
    'background:#1a5fa8','color:#fff','border:none','border-radius:20px',
    'padding:10px 20px','font-size:14px','font-weight:600','cursor:pointer',
    'box-shadow:0 4px 16px rgba(26,95,168,0.4)','font-family:Arial,sans-serif',
    'transition:transform 0.15s,box-shadow 0.15s','letter-spacing:0.2px'
  ].join(';');
  btn.onmouseover = function(){ this.style.transform='scale(1.05)'; this.style.boxShadow='0 6px 20px rgba(26,95,168,0.55)'; };
  btn.onmouseout  = function(){ this.style.transform='scale(1)';    this.style.boxShadow='0 4px 16px rgba(26,95,168,0.4)'; };
  btn.onclick = function(){
    var all = doc.querySelectorAll('button');
    for(var i=0;i<all.length;i++){
      if(all[i].innerText.trim()==='Need Help?'){ all[i].click(); return; }
    }
  };
  doc.body.appendChild(btn);
})();
</script>
""", height=0)

    plan_label = "Load Plan ✓" if st.session_state.assignments else "Load Plan"
    tab_orders, tab_plan, tab_analysis = st.tabs(["Add Orders", plan_label, "Analysis"])
    with tab_orders:
        render_add_orders(cfg)
    with tab_plan:
        render_load_plan(cfg)
    with tab_analysis:
        render_analysis(cfg)


if __name__ == "__main__":
    main()
