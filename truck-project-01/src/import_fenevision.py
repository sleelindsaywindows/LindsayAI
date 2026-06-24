import pandas as pd
from typing import List, Tuple, Optional
from .models import Order


# Maps FeneVision TruckTypeDesc to Truck.truck_type values in config.yaml.
# Add entries here when Lindsay adds new truck types.
_TRUCK_TYPE_MAP = {
    "53' Trailer": "trailer",
    "53' trailer": "trailer",
    "GA-26' ST": "straight",
    "ga-26' st": "straight",
    "26' ST": "straight",
}


def _fenevision_truck_type(desc: Optional[str]) -> Optional[str]:
    """Return Truck.truck_type string for a FeneVision TruckTypeDesc, or None if unknown."""
    if not desc or (isinstance(desc, float)):
        return None
    return _TRUCK_TYPE_MAP.get(str(desc).strip())


def import_fenevision_xlsx(
    xlsx_path,
    sheet_name: str = "Orders by Route",
    exclude_route_patterns: Optional[List[str]] = None,
    min_sqft: float = 0.01,
) -> Tuple[List[Order], List[dict], List[str]]:
    """
    Import a FeneVision xlsx export (Orders by Route format) and return
    one Order per delivery stop (aggregated from line-item rows).

    Each stop = unique (RouteID, Stop, shpaddr_companyname) combination.
    sqftShippedQty (pre-calculated by FeneVision) is summed across all line items.
    Truck type restriction is read from TruckTypeDesc and stored in allowed_truck_types.

    Args:
        xlsx_path: Path string or file-like object for the xlsx file.
        sheet_name: Sheet with order line items (default "Orders by Route").
        exclude_route_patterns: Substrings matched case-insensitively against RouteName.
            Any route whose name contains a pattern is excluded. Empty list = no exclusions.
        min_sqft: Stops with total sqft below this are skipped (placeholder stops).

    Returns:
        (orders, skipped, excluded_route_names)
        - orders: one Order per stop
        - skipped: list of dicts describing stops excluded for low sqft
        - excluded_route_names: unique RouteName values removed by pattern matching
    """
    df = pd.read_excel(xlsx_path, sheet_name=sheet_name, header=0)

    required = {
        "RouteID", "RouteName", "Stop",
        "shpaddr_companyname", "ShpAddr_Address1", "ShpAddr_City",
        "ShpAddr_State", "ShpAddr_ZipCode",
        "sqftShippedQty",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"xlsx sheet '{sheet_name}' missing columns: {missing}")

    excluded_route_names: List[str] = []
    if exclude_route_patterns:
        patterns_lower = [p.lower() for p in exclude_route_patterns]
        mask = df["RouteName"].apply(
            lambda name: any(p in str(name).lower() for p in patterns_lower)
        )
        excluded_route_names = list(df.loc[mask, "RouteName"].dropna().unique())
        df = df[~mask]

    # Aggregate line items → one row per delivery stop.
    # TruckType/TruckTypeDesc come from the route, so first() is stable within a stop.
    stop_df = (
        df.groupby(
            ["RouteID", "RouteName", "Stop",
             "shpaddr_companyname", "ShpAddr_Address1",
             "ShpAddr_City", "ShpAddr_State", "ShpAddr_ZipCode"],
            dropna=False,
        )
        .agg(
            sqft_total=("sqftShippedQty", "sum"),
            truck_type_desc=("TruckTypeDesc", "first"),
        )
        .reset_index()
    )

    orders = []
    skipped = []

    for _, row in stop_df.iterrows():
        sqft = float(row["sqft_total"]) if pd.notna(row["sqft_total"]) else 0.0

        if sqft < min_sqft:
            skipped.append({
                "route": row["RouteName"],
                "stop": row["Stop"],
                "customer": row["shpaddr_companyname"],
                "sqft": sqft,
                "reason": "sqft below threshold",
            })
            continue

        # Build address string from components
        # ZipCode comes in as float (e.g. 30507.0) — cast to int to strip the decimal
        zip_raw = row["ShpAddr_ZipCode"]
        try:
            zip_str = str(int(float(zip_raw))) if zip_raw and str(zip_raw).lower() != "nan" else ""
        except (ValueError, TypeError):
            zip_str = str(zip_raw).strip()
        parts = [
            str(row["ShpAddr_Address1"]).strip(),
            str(row["ShpAddr_City"]).strip(),
            str(row["ShpAddr_State"]).strip(),
            zip_str,
        ]
        address = " ".join(p for p in parts if p and p.lower() != "nan")

        # Unique order ID: RouteID-Stop (padded) — readable and stable
        order_id = f"R{int(row['RouteID'])}-S{int(row['Stop']):02d}"
        # If the same (route, stop) has multiple companies, append a suffix
        if any(o.order_id == order_id for o in orders):
            suffix = sum(1 for o in orders if o.order_id.startswith(order_id))
            order_id = f"{order_id}-{suffix}"

        truck_type = _fenevision_truck_type(row.get("truck_type_desc"))
        allowed = [truck_type] if truck_type else None

        orders.append(Order(
            order_id=order_id,
            customer_name=str(row["shpaddr_companyname"]).strip(),
            address=address,
            capacity_units=round(sqft, 3),
            priority=0,
            notes=str(row["RouteName"]),
            allowed_truck_types=allowed,
        ))

    return orders, skipped, excluded_route_names


def import_fenevision(
    csv_path: str,
) -> Tuple[List[Order], List[dict]]:
    """
    Convert FeneVision generic CSV export to Order objects (one Order per CSV row).
    For FeneVision xlsx data use import_fenevision_xlsx() instead.

    FeneVision fields expected:
      order_number, window_width, window_height, ship_qty,
      ship_to_name, ship_to_street, ship_to_city, ship_to_state, ship_to_zip
      (optional: route_description, target_ship_date)

    Returns:
      (orders, errors) where errors is a list of dicts with 'row' and 'reason'
    """
    df = pd.read_csv(csv_path)
    orders = []
    errors = []

    required = {
        "order_number", "window_width", "window_height", "ship_qty",
        "ship_to_name", "ship_to_street", "ship_to_city", "ship_to_state", "ship_to_zip"
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"FeneVision CSV missing required columns: {missing}")

    for idx, row in df.iterrows():
        try:
            address_parts = [
                str(row["ship_to_street"]).strip(),
                str(row["ship_to_city"]).strip(),
                str(row["ship_to_state"]).strip(),
                str(row["ship_to_zip"]).strip(),
            ]
            address = " ".join(p for p in address_parts if p and p.lower() != "nan")

            try:
                if pd.isna(row["window_width"]) or pd.isna(row["window_height"]) or pd.isna(row["ship_qty"]):
                    raise ValueError("Missing dimension or qty")
                width = float(row["window_width"])
                height = float(row["window_height"])
                qty = float(row["ship_qty"])
            except (ValueError, TypeError) as e:
                errors.append({
                    "row": idx + 2,
                    "order_number": str(row.get("order_number", "?")),
                    "reason": f"Invalid dimension or qty: {e}",
                })
                continue

            if width <= 0 or height <= 0 or qty < 0:
                errors.append({
                    "row": idx + 2,
                    "order_number": str(row.get("order_number", "?")),
                    "reason": f"Invalid dimension (width={width}, height={height}, qty={qty})",
                })
                continue

            capacity_units = width * height * qty

            notes_parts = []
            if pd.notna(row.get("route_description")) and str(row["route_description"]).strip().lower() != "nan":
                notes_parts.append(f"Route: {row['route_description']}")
            if pd.notna(row.get("target_ship_date")) and str(row["target_ship_date"]).strip().lower() != "nan":
                notes_parts.append(f"Target ship: {row['target_ship_date']}")

            truck_type = _fenevision_truck_type(row.get("TruckTypeDesc"))
            orders.append(Order(
                order_id=str(row["order_number"]).strip(),
                customer_name=str(row["ship_to_name"]).strip(),
                address=address,
                capacity_units=capacity_units,
                priority=0,
                notes=" | ".join(notes_parts),
                allowed_truck_types=[truck_type] if truck_type else None,
            ))

        except Exception as e:
            errors.append({
                "row": idx + 2,
                "order_number": str(row.get("order_number", "?")),
                "reason": str(e),
            })

    return orders, errors


def export_app_csv(
    orders: List[Order],
    output_path: str,
) -> None:
    """Export Order list to app CSV format."""
    rows = [
        {
            "order_id": o.order_id,
            "customer_name": o.customer_name,
            "address": o.address,
            "capacity_units": o.capacity_units,
            "priority": o.priority,
            "notes": o.notes,
        }
        for o in orders
    ]
    df = pd.DataFrame(rows)
    df.to_csv(output_path, index=False)
    print(f"Exported {len(orders)} orders to {output_path}")
