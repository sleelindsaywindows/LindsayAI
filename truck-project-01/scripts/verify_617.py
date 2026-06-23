#!/usr/bin/env python3
"""
CLI verification script: runs the optimizer against Geoff's 6/17 historical data
and generates lindsay_analysis_617.xlsx.

Usage:
    python scripts/verify_617.py
    python scripts/verify_617.py --geocode
"""

import argparse
import sys
import os

# Allow running from project root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

from src.models import Truck
from src.import_fenevision import import_fenevision_xlsx
from src.optimizer import solve, validate_inputs, check_route_cap, geocode_address
from src.analysis import generate_report

XLSX_PATH = "sample_data/GA_Trucks_2026-06-17.xlsx"
EXCLUDE_PATTERNS = [" MO "]  # matches interplant routes like "6/17 Lindsay MO 53'"; space prevents matching driver names like "Raymond"
OUTPUT_PATH = "lindsay_analysis_617.xlsx"
JOSEPHS_TRUCK_COUNT = 13


def _preflight_checks(orders, trucks, xlsx_path, exclude_patterns):
    """Run pre-flight checks and print warnings. Returns list of error strings."""
    errors = []
    warnings = []

    # 1. Standard input validation (blank IDs, zero capacity, bad coords)
    errors.extend(validate_inputs(orders, trucks))

    # 2. allowed_truck_types values that don't match any truck type in fleet
    fleet_types = {t.truck_type for t in trucks}
    for o in orders:
        if o.allowed_truck_types:
            for tt in o.allowed_truck_types:
                if tt not in fleet_types:
                    warnings.append(
                        f"Order {o.order_id} ({o.customer_name}): "
                        f"allowed_truck_types includes '{tt}' which matches no truck in fleet "
                        f"(fleet types: {fleet_types}). Stop will be dropped by solver."
                    )

    # 3. Stops with sqft == 0 (shouldn't reach here after import, but double-check)
    zero_sqft = [o for o in orders if o.capacity_units == 0]
    if zero_sqft:
        warnings.append(
            f"{len(zero_sqft)} stop(s) have capacity_units == 0: "
            + ", ".join(o.order_id for o in zero_sqft)
        )

    # 4. Total order sq ft exceeds total fleet capacity
    total_order_sqft = sum(o.capacity_units for o in orders)
    total_fleet_sqft = sum(t.max_capacity for t in trucks)
    if total_order_sqft > total_fleet_sqft:
        warnings.append(
            f"Total order sq ft ({total_order_sqft:.1f}) exceeds total fleet capacity "
            f"({total_fleet_sqft:.1f}). Some orders will be dropped."
        )

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

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)

    return errors


def main():
    parser = argparse.ArgumentParser(description="Verify optimizer against 6/17 historical data.")
    parser.add_argument("--geocode", action="store_true", help="Geocode addresses for distance routing.")
    args = parser.parse_args()

    # Load fleet from config
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

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

    # Import orders
    orders, skipped, excluded = import_fenevision_xlsx(XLSX_PATH, exclude_route_patterns=EXCLUDE_PATTERNS)
    print(f"Loaded {len(orders)} stops ({len(skipped)} skipped). Excluded routes: {excluded}.")

    # Pre-flight checks
    errors = _preflight_checks(orders, trucks, XLSX_PATH, EXCLUDE_PATTERNS)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    depot_coords = (33.749, -84.388)
    depot_addr = cfg.get("depot", {}).get("address", "")

    if args.geocode:
        print("Geocoding addresses (this may take a few minutes)…")
        if depot_addr:
            result = geocode_address(depot_addr)
            if result:
                depot_coords = result
        for order in orders:
            coords = geocode_address(order.address)
            if coords:
                order.lat, order.lon = coords

        routing_cfg = cfg.get("routing", {})
        max_route_miles = float(routing_cfg.get("max_route_miles", 600))
        cap_warnings = check_route_cap(orders, depot_coords, max_route_miles)
        for w in cap_warnings:
            print(f"WARN (route cap): {w}", file=sys.stderr)

    solver_time_limit = int(cfg.get("routing", {}).get("solver_time_limit_seconds", 30))
    print(f"Running optimizer (time limit: {solver_time_limit}s)…")
    assignments, dropped = solve(
        orders, trucks, depot_coords,
        max_route_miles=float(cfg.get("routing", {}).get("max_route_miles", 600)),
        solver_time_limit=solver_time_limit,
    )

    # Generate report (also runs constraint audit internally)
    report_path = generate_report(
        assignments=assignments,
        dropped=dropped,
        orders=orders,
        output_path=OUTPUT_PATH,
        josephs_truck_count=JOSEPHS_TRUCK_COUNT,
    )

    # Constraint audit summary for terminal
    restricted = [
        (a, stop)
        for a in assignments
        for stop in a.stops
        if stop.order.allowed_truck_types
    ]
    fail_count = sum(
        1 for a, stop in restricted
        if a.truck.truck_type not in stop.order.allowed_truck_types
    )
    constraint_result = "PASS" if fail_count == 0 else f"FAIL ({fail_count} violations)"

    total_cost = sum(
        a.route_distance_miles * a.truck.cost_per_mile
        for a in assignments
        if a.route_distance_miles
    )
    total_miles = sum(a.route_distance_miles for a in assignments)

    print()
    print(f"Optimizer used {len(assignments)} trucks. Joseph used {JOSEPHS_TRUCK_COUNT}.")
    print(f"Homebuilder constraint: {constraint_result}")
    print(f"Dropped orders: {len(dropped)}")
    if args.geocode and total_miles:
        print(f"Est. cost: ${total_cost:.2f} (geocoding: on, ~{total_miles:.0f} mi total)")
    else:
        print(f"Est. cost: N/A (geocoding: {'on' if args.geocode else 'off'})")
    print(f"Report written: {report_path}")

    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
