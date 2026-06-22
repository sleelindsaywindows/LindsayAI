"""
Capacitated Vehicle Routing Problem solver — Google OR-Tools.

Merges the best of two iterations:
  From LyndsayWindowsLLC (Streamlit build):
    - Priority weighting in the distance callback (urgent orders served first)
    - LIFO load sequence (enforced in TruckAssignment model, not here)
    - Graceful geocoding degradation (works without geopy)
    - Streamlit-compatible return type

  From LW-Storage-and-Shipping-Basic (AWLindsay prototype):
    - Fixed activation cost per vehicle type (fills 26-ft trucks before 53-ft trailers)
    - Hard distance cap per truck per day (HOS compliance)
    - Soft disjunctions — dropped orders reported, never silently lost
    - Pre-flight route cap check for multi-day orders
    - Input validation before solver
    - Distance in miles × 1000 (matches domain language; precise for dense stops)
    - Dropped order detection via NextVar self-reference

Node 0 = depot; nodes 1..N = orders.
"""

import math
from typing import Optional

from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from .models import Order, Truck, TruckAssignment, RouteStop

try:
    from geopy.geocoders import Nominatim
    from geopy.extra.rate_limiter import RateLimiter
    _geolocator = Nominatim(user_agent="lindsay-windows-optimizer")
    _geocode_fn = RateLimiter(_geolocator.geocode, min_delay_seconds=1.1)
    GEOCODING_AVAILABLE = True
except ImportError:
    GEOCODING_AVAILABLE = False

# --- Solver constants (overridable via solve() parameters) ----------------------

DISTANCE_SCALE = 1_000          # integer units per mile (thousandths of a mile)
MAX_ROUTE_MILES = 400           # default hard daily distance cap per driver (HOS)
SOLVER_TIME_LIMIT_SECONDS = 15

# Must far exceed the cost of any real route so the solver never prefers
# dropping an order over serving it. 10_000_000 ≈ 10,000 miles.
UNASSIGNED_ORDER_PENALTY = 10_000_000

# Priority penalty: 1 priority point = 2 equivalent miles.
# Full 10-point gap (normal → urgent) = 20-mile detour budget.
# Calibrated so urgent orders always lead the route in metro areas
# without enabling absurd cross-state detours.
PRIORITY_WEIGHT_MILES = 2


def geocode_address(address: str) -> Optional[tuple[float, float]]:
    if not GEOCODING_AVAILABLE:
        return None
    try:
        result = _geocode_fn(address)
        return (result.latitude, result.longitude) if result else None
    except Exception:
        return None


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlng = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _build_distance_matrix(coords: list[tuple[float, float]]) -> list[list[int]]:
    n = len(coords)
    return [
        [
            0 if i == j
            else round(_haversine_miles(*coords[i], *coords[j]) * DISTANCE_SCALE)
            for j in range(n)
        ]
        for i in range(n)
    ]


def validate_inputs(orders: list[Order], trucks: list[Truck]) -> list[str]:
    """Returns list of error strings. Empty list means inputs are valid."""
    errors = []
    for o in orders:
        if not str(o.order_id).strip():
            errors.append("An order has a blank order_id.")
        if o.capacity_units <= 0:
            errors.append(f"Order {o.order_id}: capacity_units must be > 0 (got {o.capacity_units}).")
        if o.lat is not None and not (-90 <= o.lat <= 90):
            errors.append(f"Order {o.order_id}: lat {o.lat} out of valid range.")
        if o.lon is not None and not (-180 <= o.lon <= 180):
            errors.append(f"Order {o.order_id}: lon {o.lon} out of valid range.")
    for t in trucks:
        if t.max_capacity <= 0:
            errors.append(f"Truck {t.name}: max_capacity must be > 0.")
    return errors


def check_route_cap(
    orders: list[Order],
    depot_coords: tuple[float, float],
    max_route_miles: float = MAX_ROUTE_MILES,
) -> list[str]:
    """
    Returns warning strings for orders whose minimum round-trip already exceeds
    max_route_miles. These will be dropped by the solver unless the cap is raised
    or the run is split into a multi-day route (NC/VA pattern).
    """
    warnings = []
    for o in orders:
        if o.lat is None or o.lon is None:
            continue
        min_rt = 2 * _haversine_miles(depot_coords[0], depot_coords[1], o.lat, o.lon)
        if min_rt > max_route_miles:
            warnings.append(
                f"Order {o.order_id} ({o.customer_name}) @ {o.address} — "
                f"min round-trip ~{min_rt:.0f} mi exceeds {max_route_miles}-mile cap. "
                f"Needs a two-day run or raised cap."
            )
    return warnings


def solve(
    orders: list[Order],
    trucks: list[Truck],
    depot_coords: tuple[float, float] = (33.749, -84.388),  # Atlanta default
    max_route_miles: float = MAX_ROUTE_MILES,
    solver_time_limit: int = SOLVER_TIME_LIMIT_SECONDS,
) -> tuple[list[TruckAssignment], list[Order]]:
    """
    Returns (assignments, dropped_orders).
    dropped_orders is non-empty when fleet capacity is insufficient or an order
    physically cannot be served within the route distance cap.
    """
    if not orders or not trucks:
        return [], list(orders)

    coords = [depot_coords] + [
        (o.lat, o.lon) if (o.lat is not None and o.lon is not None) else depot_coords
        for o in orders
    ]

    distance_matrix = _build_distance_matrix(coords)

    SCALE = 100
    demands = [0] + [int(o.capacity_units * SCALE) for o in orders]
    capacities = [int(t.max_capacity * SCALE) for t in trucks]
    priorities = [10] + [o.priority for o in orders]  # depot gets max priority

    manager = pywrapcp.RoutingIndexManager(len(coords), len(trucks), 0)
    routing = pywrapcp.RoutingModel(manager)

    # Cost callback includes priority penalty — encourages serving urgent orders first.
    # Guard j==0: never penalise return-to-depot arcs.
    def _dist_cb(from_idx: int, to_idx: int) -> int:
        i = manager.IndexToNode(from_idx)
        j = manager.IndexToNode(to_idx)
        dist = distance_matrix[i][j]
        if j == 0:
            return dist
        priority_penalty = max(0, priorities[j] - priorities[i]) * int(PRIORITY_WEIGHT_MILES * DISTANCE_SCALE)
        return dist + priority_penalty

    transit_idx = routing.RegisterTransitCallback(_dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    # Fixed activation cost per truck type — steers solver to fill 26-ft trucks
    # before activating 53-ft trailers (fixed_cost in config.yaml, equivalent miles).
    for v_idx, truck in enumerate(trucks):
        routing.SetFixedCostOfVehicle(int(truck.fixed_cost * DISTANCE_SCALE), v_idx)

    def _demand_cb(from_idx: int) -> int:
        return demands[manager.IndexToNode(from_idx)]

    demand_idx = routing.RegisterUnaryTransitCallback(_demand_cb)
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, capacities, True, "Capacity")

    # Raw distance callback (no priority penalty) — used for the HOS distance cap.
    def _raw_dist_cb(from_idx: int, to_idx: int) -> int:
        return distance_matrix[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    raw_transit_idx = routing.RegisterTransitCallback(_raw_dist_cb)
    routing.AddDimension(
        raw_transit_idx,
        0,
        int(max_route_miles * DISTANCE_SCALE),
        True,
        "Distance",
    )

    # Truck type restrictions — if an order specifies allowed_truck_types, forbid
    # all incompatible vehicles from serving that node via VehicleVar constraints.
    # OR-Tools 9.x SetAllowedVehiclesForIndex does not accept Python sequences;
    # using VehicleVar != forbidden_vehicle constraints achieves the same result.
    cp_solver = routing.solver()
    for order_idx, order in enumerate(orders):
        if order.allowed_truck_types:
            node_index = manager.NodeToIndex(order_idx + 1)  # node 0 = depot
            vehicle_var = routing.VehicleVar(node_index)
            for v_idx, truck in enumerate(trucks):
                if truck.truck_type not in order.allowed_truck_types:
                    cp_solver.Add(vehicle_var != v_idx)

    # Soft constraint — allows solver to drop an order and report it rather than
    # returning no solution when fleet capacity is insufficient.
    for node in range(1, len(distance_matrix)):
        routing.AddDisjunction([manager.NodeToIndex(node)], UNASSIGNED_ORDER_PENALTY)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = solver_time_limit

    solution = routing.SolveWithParameters(params)
    if not solution:
        return [], list(orders)

    assignments = []
    for v_idx, truck in enumerate(trucks):
        idx = routing.Start(v_idx)
        stops = []
        stop_num = 1
        route_dist = 0
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                stops.append(RouteStop(order=orders[node - 1], stop_number=stop_num))
                stop_num += 1
            next_idx = solution.Value(routing.NextVar(idx))
            route_dist += distance_matrix[manager.IndexToNode(idx)][manager.IndexToNode(next_idx)]
            idx = next_idx
        if stops:
            assignments.append(TruckAssignment(
                truck=truck,
                stops=stops,
                route_distance_miles=route_dist / DISTANCE_SCALE,
            ))

    # Detect dropped orders: a dropped node's NextVar points to itself.
    dropped = []
    for node in range(1, len(orders) + 1):
        idx = manager.NodeToIndex(node)
        if solution.Value(routing.NextVar(idx)) == idx:
            dropped.append(orders[node - 1])

    return assignments, dropped
