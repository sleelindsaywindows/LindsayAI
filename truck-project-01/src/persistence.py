"""
Save/load session state (orders, assignments, dropped) as a JSON blob.
Download as file from browser; upload to restore. No server storage required.
"""

import json
from datetime import datetime
from typing import List, Tuple

from .models import Order, Truck, RouteStop, TruckAssignment

_VERSION = 1


def _order_to_dict(o: Order) -> dict:
    return {
        "order_id": o.order_id,
        "customer_name": o.customer_name,
        "address": o.address,
        "capacity_units": o.capacity_units,
        "priority": o.priority,
        "notes": o.notes,
        "lat": o.lat,
        "lon": o.lon,
        "allowed_truck_types": o.allowed_truck_types,
        "max_window_width_inches": o.max_window_width_inches,
        "fenevision_ids": o.fenevision_ids,
        # line_items excluded — large, not needed for restore
    }


def _order_from_dict(d: dict) -> Order:
    return Order(
        order_id=d["order_id"],
        customer_name=d["customer_name"],
        address=d["address"],
        capacity_units=d["capacity_units"],
        priority=d.get("priority", 0),
        notes=d.get("notes", ""),
        lat=d.get("lat"),
        lon=d.get("lon"),
        allowed_truck_types=d.get("allowed_truck_types"),
        max_window_width_inches=d.get("max_window_width_inches"),
        fenevision_ids=d.get("fenevision_ids"),
    )


def _truck_to_dict(t: Truck) -> dict:
    return {
        "name": t.name,
        "truck_type": t.truck_type,
        "max_capacity": t.max_capacity,
        "fixed_cost": t.fixed_cost,
        "cost_per_mile": t.cost_per_mile,
        "driver": t.driver,
        "employment_type": t.employment_type,
    }


def _truck_from_dict(d: dict) -> Truck:
    return Truck(
        name=d["name"],
        truck_type=d["truck_type"],
        max_capacity=d["max_capacity"],
        fixed_cost=d.get("fixed_cost", 5.0),
        cost_per_mile=d.get("cost_per_mile", 0.0),
        driver=d.get("driver", ""),
        employment_type=d.get("employment_type", "fulltime"),
    )


def _assignment_to_dict(a: TruckAssignment) -> dict:
    return {
        "truck": _truck_to_dict(a.truck),
        "stops": [
            {"stop_number": s.stop_number, "order": _order_to_dict(s.order)}
            for s in a.stops
        ],
        "route_distance_miles": a.route_distance_miles,
        "route_time_hours": a.route_time_hours,
    }


def _assignment_from_dict(d: dict) -> TruckAssignment:
    truck = _truck_from_dict(d["truck"])
    stops = [
        RouteStop(order=_order_from_dict(s["order"]), stop_number=s["stop_number"])
        for s in d["stops"]
    ]
    return TruckAssignment(
        truck=truck,
        stops=stops,
        route_distance_miles=d.get("route_distance_miles", 0.0),
        route_time_hours=d.get("route_time_hours", 0.0),
    )


def serialize_plan(
    orders: List[Order],
    assignments: List[TruckAssignment],
    dropped: List[Order],
    supervisor_routes: List[dict],
    fv_filename: str = "",
) -> bytes:
    """Return JSON bytes suitable for st.download_button."""
    payload = {
        "version": _VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "fv_filename": fv_filename,
        "orders": [_order_to_dict(o) for o in orders],
        "assignments": [_assignment_to_dict(a) for a in assignments],
        "dropped": [_order_to_dict(o) for o in dropped],
        "supervisor_routes": supervisor_routes,
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def deserialize_plan(
    data: bytes,
) -> Tuple[List[Order], List[TruckAssignment], List[Order], List[dict], str]:
    """
    Parse JSON bytes from a saved plan file.
    Returns (orders, assignments, dropped, supervisor_routes, fv_filename).
    Raises ValueError on version mismatch or schema error.
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"Not a valid plan file: {e}") from e

    version = payload.get("version", 0)
    if version != _VERSION:
        raise ValueError(f"Plan file version {version} — expected {_VERSION}. Re-save from current app.")

    orders = [_order_from_dict(d) for d in payload.get("orders", [])]
    assignments = [_assignment_from_dict(d) for d in payload.get("assignments", [])]
    dropped = [_order_from_dict(d) for d in payload.get("dropped", [])]
    supervisor_routes = payload.get("supervisor_routes", [])
    fv_filename = payload.get("fv_filename", "")
    return orders, assignments, dropped, supervisor_routes, fv_filename
