import copy
import os
import urllib.parse
import yaml
import streamlit as st
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from src.models import Order, Truck
from src.parser import parse_and_verify
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
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


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
    max_miles = st.sidebar.number_input(
        "Max route miles / driver (HOS cap)",
        value=float(routing.get("max_route_miles", 400)),
        min_value=50.0, step=50.0,
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
            "routing": {"max_route_miles": max_miles, "solver_time_limit_seconds": routing.get("solver_time_limit_seconds", 15)},
        }
        save_config(new_cfg)
        st.rerun()

    if st.sidebar.button("Save Config", type="primary"):
        new_cfg = {
            "measurement": {"unit": unit, "label": label, "abbreviation": abbr},
            "trucks": updated_trucks,
            "depot": {"name": depot_name, "address": depot_addr},
            "routing": {"max_route_miles": max_miles, "solver_time_limit_seconds": routing.get("solver_time_limit_seconds", 15)},
        }
        save_config(new_cfg)
        st.sidebar.success("Saved.")
        st.rerun()

    return cfg


def render_add_orders(cfg: dict):
    abbr = cfg["measurement"]["abbreviation"]
    unit_label = cfg["measurement"]["label"]

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
    st.subheader("Add via Natural Language")
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


def render_load_plan(cfg: dict):
    abbr = cfg["measurement"]["abbreviation"]
    routing_cfg = cfg.get("routing", {})
    max_route_miles = float(routing_cfg.get("max_route_miles", 400))
    solver_time_limit = int(routing_cfg.get("solver_time_limit_seconds", 15))

    trucks = [
        Truck(
            name=t["name"],
            truck_type=t["type"],
            max_capacity=t["max_capacity"],
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

    geocode_on = st.checkbox(
        "Geocode addresses for distance-based routing (uses free Nominatim — may be slow)",
        value=False,
        disabled=not GEOCODING_AVAILABLE,
        help="Install geopy to enable: pip install geopy" if not GEOCODING_AVAILABLE else "",
    )

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

    if not st.session_state.assignments and not st.session_state.dropped:
        return

    # Build plan_text once; reused by both banner and bottom download button
    lines = ["LINDSAY WINDOWS — LOAD PLAN", "=" * 60]
    for a in st.session_state.assignments:
        dist_note = f"  Route: {a.route_distance_miles:.0f} mi" if a.route_distance_miles else ""
        lines += [
            f"\n{a.truck.name}",
            f"Capacity: {a.total_capacity_used:.0f}/{a.truck.max_capacity:.0f} {abbr} ({a.utilization_pct:.0f}%){dist_note}",
            "\nDELIVERY ORDER:",
        ]
        for stop in a.stops:
            maps_url = "https://maps.google.com/?q=" + urllib.parse.quote(stop.order.address)
            pri_tag = f"  [PRIORITY {stop.order.priority}]" if stop.order.priority > 0 else ""
            lines.append(f"  Stop {stop.stop_number}: {stop.order.customer_name} | {stop.order.address} | {stop.order.capacity_units:.0f} {abbr}{pri_tag}")
            lines.append(f"    Maps: {maps_url}")
            if stop.order.notes:
                lines.append(f"    Note: {stop.order.notes}")
        lines.append("\nLOAD ORDER (load #1 first — deepest in truck):")
        for i, stop in enumerate(a.load_sequence, 1):
            lines.append(f"  Load {i}: {stop.order.customer_name} | {stop.order.capacity_units:.0f} {abbr}")

    if st.session_state.dropped:
        lines.append("\nUNASSIGNED ORDERS:")
        for o in st.session_state.dropped:
            lines.append(f"  {o.order_id}: {o.customer_name} | {o.address} | {o.capacity_units:.0f} {abbr}")

    lines.append("\n" + "=" * 60)
    plan_text = "\n".join(lines)

    if st.session_state.assignments:
        col_banner, col_dl = st.columns([3, 1])
        col_banner.success("✅ Plan ready — would you like to export?")
        col_dl.download_button(
            "⬇ Export (.txt)",
            data=plan_text,
            file_name="load_plan.txt",
            mime="text/plain",
            key="banner_download",
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
        label = (
            f"🚛 {assignment.truck.name} — "
            f"{assignment.total_capacity_used:.0f}/{assignment.truck.max_capacity:.0f} {abbr} "
            f"({assignment.utilization_pct:.0f}% utilized){dist_str}"
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
                st.markdown("**Load Sequence** *(load #1 first — goes in deepest)*")
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
    st.download_button(
        "⬇ Download Load Plan (.txt)",
        data=plan_text,
        file_name="load_plan.txt",
        mime="text/plain",
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
        {"Metric": "Trucks Used", "Optimizer": len(assignments), "Joseph's Manual": 13},
        {"Metric": "Total Est. Miles", "Optimizer": round(total_miles, 1) if has_miles else "N/A", "Joseph's Manual": "unknown"},
        {"Metric": "Total Est. Cost ($)", "Optimizer": round(total_cost, 2) if has_miles else "N/A", "Joseph's Manual": "unknown"},
        {"Metric": "Avg Utilization %", "Optimizer": avg_util, "Joseph's Manual": "unknown"},
        {"Metric": "Dropped Orders", "Optimizer": len(dropped), "Joseph's Manual": "unknown"},
    ]
    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True)
    st.caption("Joseph's manual cost computable once his 6/17 route sheet is entered.")

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

    cfg = load_config()
    render_sidebar(cfg)
    cfg = load_config()  # reload in case sidebar saved

    st.title("🪟 Lindsay Windows — Load Planner")

    tab_plan, tab_orders, tab_analysis = st.tabs(["Load Plan", "Add Orders", "Analysis"])
    with tab_plan:
        render_load_plan(cfg)
    with tab_orders:
        render_add_orders(cfg)
    with tab_analysis:
        render_analysis(cfg)


if __name__ == "__main__":
    main()
