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

CONFIG_PATH = Path("config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def init_state():
    defaults = {"orders": [], "pending": None, "assignments": [], "dropped": [], "uploader_key": 0}
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

    if st.button("Generate Load Plan", type="primary"):
        orders_copy = [Order(**o.__dict__) for o in st.session_state.orders]

        # Validate inputs before touching the solver
        errors = validate_inputs(orders_copy, trucks)
        if errors:
            for e in errors:
                st.error(e)
            return

        depot_addr = cfg["depot"].get("address", "")
        depot_coords = (33.749, -84.388)  # Atlanta default for Georgia plant

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

            # Pre-flight: warn about orders that may need multi-day runs
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

    # Dropped orders banner
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

    # Export
    st.divider()
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

    st.download_button(
        "⬇ Download Load Plan (.txt)",
        data="\n".join(lines),
        file_name="load_plan.txt",
        mime="text/plain",
    )


def main():
    st.set_page_config(page_title="Lindsay Windows — Load Planner", page_icon="🪟", layout="wide")
    init_state()

    cfg = load_config()
    render_sidebar(cfg)
    cfg = load_config()  # reload in case sidebar saved

    st.title("🪟 Lindsay Windows — Load Planner")

    tab_orders, tab_plan = st.tabs(["Add Orders", "Load Plan"])
    with tab_orders:
        render_add_orders(cfg)
    with tab_plan:
        render_load_plan(cfg)


if __name__ == "__main__":
    main()
