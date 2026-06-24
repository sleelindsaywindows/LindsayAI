"""
Capacitated Vehicle Routing Problem solver — Google OR-Tools.

Constraint model:
  - Capacity dimension: hard floor-space cap per truck
  - Time dimension: hard daily hour cap per driver (Joseph's 9-hour rule)
      drive_time = haversine_miles / avg_speed_mph
      route_time = sum(drive_legs) + (stops × stop_time_minutes)
  - Fixed activation cost per truck type: fills 26-ft straight trucks before 53-ft trailers
  - Per-order truck type restrictions via VehicleVar constraints (homebuilder stops)
  - Soft disjunctions: dropped orders reported explicitly, never silently lost
  - Priority weighting in arc cost: urgent orders pulled to earlier stops
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

DISTANCE_SCALE = 1_000      # integer units per mile (thousandths of a mile) — cost callback only
TIME_SCALE = 100            # integer units per minute (hundredths of a minute) — time dimension
MAX_ROUTE_HOURS = 9.0       # Joseph's practical daily driver cap; 11 hrs is legal max
STOP_TIME_MINUTES = 45.0    # default unload time per stop (neighborhood delivery, no dock)
AVG_SPEED_MPH = 45.0        # haversine → drive time; mixed GA urban/rural estimate
SOLVER_TIME_LIMIT_SECONDS = 15

# Must far exceed the cost of any real route so the solver never prefers
# dropping an order over serving it. 10_000_000 ≈ 10,000 miles.
UNASSIGNED_ORDER_PENALTY = 10_000_000

# Priority penalty: 1 priority point = 2 equivalent miles.
# Full 10-point gap (normal → urgent) = 20-mile detour budget.
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
    """Distance in DISTANCE_SCALE units (thousandths of a mile). Used for arc cost only."""
    n = len(coords)
    return [
        [
            0 if i == j
            else round(_haversine_miles(*coords[i], *coords[j]) * DISTANCE_SCALE)
            for j in range(n)
        ]
        for i in range(n)
    ]


def _build_time_matrix(
    coords: list[tuple[float, float]],
    avg_speed_mph: float = AVG_SPEED_MPH,
) -> list[list[int]]:
    """Travel-only time in TIME_SCALE units (hundredths of a minute). Stop time added separately."""
    n = len(coords)
    return [
        [
            0 if i == j
            else round(_haversine_miles(*coords[i], *coords[j]) / avg_speed_mph * 60 * TIME_SCALE)
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
    max_route_hours: float = MAX_ROUTE_HOURS,
    stop_time_minutes: float = STOP_TIME_MINUTES,
    avg_speed_mph: float = AVG_SPEED_MPH,
) -> list[str]:
    """
    Returns warning strings for orders whose minimum round-trip drive time already
    exceeds max_route_hours. These will be dropped unless the cap is raised or the
    run is flagged as a multi-day route.
    """
    warnings = []
    for o in orders:
        if o.lat is None or o.lon is None:
            continue
        drive_miles = 2 * _haversine_miles(depot_coords[0], depot_coords[1], o.lat, o.lon)
        drive_hours = drive_miles / avg_speed_mph
        total_hours = drive_hours + stop_time_minutes / 60
        if total_hours > max_route_hours:
            warnings.append(
                f"Order {o.order_id} ({o.customer_name}) @ {o.address} — "
                f"min round-trip ~{drive_hours:.1f} hr drive + {stop_time_minutes:.0f} min stop "
                f"exceeds {max_route_hours:.0f}-hour cap. Needs a two-day run or raised cap."
            )
    return warnings


def solve(
    orders: list[Order],
    trucks: list[Truck],
    depot_coords: tuple[float, float] = (33.749, -84.388),  # Atlanta default
    max_route_hours: float = MAX_ROUTE_HOURS,
    stop_time_minutes: float = STOP_TIME_MINUTES,
    avg_speed_mph: float = AVG_SPEED_MPH,
    solver_time_limit: int = SOLVER_TIME_LIMIT_SECONDS,
) -> tuple[list[TruckAssignment], list[Order]]:
    """
    Returns (assignments, dropped_orders).
    dropped_orders is non-empty when fleet capacity is insufficient or a route
    cannot be completed within the max_route_hours time cap.

    Route time = sum of drive legs (haversine / avg_speed_mph) + stops × stop_time_minutes.
    """
    if not orders or not trucks:
        return [], list(orders)

    coords = [depot_coords] + [
        (o.lat, o.lon) if (o.lat is not None and o.lon is not None) else depot_coords
        for o in orders
    ]

    distance_matrix = _build_distance_matrix(coords)
    time_matrix = _build_time_matrix(coords, avg_speed_mph)
    stop_time_scaled = round(stop_time_minutes * TIME_SCALE)

    SCALE = 100
    demands = [0] + [int(o.capacity_units * SCALE) for o in orders]
    capacities = [int(t.max_capacity * SCALE) for t in trucks]
    priorities = [10] + [o.priority for o in orders]

    manager = pywrapcp.RoutingIndexManager(len(coords), len(trucks), 0)
    routing = pywrapcp.RoutingModel(manager)

    # Arc cost: distance with priority penalty (encourages serving urgent orders first).
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

    # Fixed activation cost: steers solver to fill straight trucks before trailers.
    for v_idx, truck in enumerate(trucks):
        routing.SetFixedCostOfVehicle(int(truck.fixed_cost * DISTANCE_SCALE), v_idx)

    # Capacity dimension.
    def _demand_cb(from_idx: int) -> int:
        return demands[manager.IndexToNode(from_idx)]

    demand_idx = routing.RegisterUnaryTransitCallback(_demand_cb)
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, capacities, True, "Capacity")

    # Time dimension — drive time + stop service time at each destination.
    # j == 0 is the depot return leg: no unload time there.
    def _time_cb(from_idx: int, to_idx: int) -> int:
        i = manager.IndexToNode(from_idx)
        j = manager.IndexToNode(to_idx)
        travel = time_matrix[i][j]
        service = 0 if j == 0 else stop_time_scaled
        return travel + service

    time_transit_idx = routing.RegisterTransitCallback(_time_cb)
    max_time_scaled = round(max_route_hours * 60 * TIME_SCALE)
    routing.AddDimension(
        time_transit_idx,
        0,                   # no waiting time slack
        max_time_scaled,     # hard cap: max_route_hours of total route time
        True,                # cumulative from zero at depot
        "Time",
    )

    # Truck type restrictions — homebuilder stops that can't accept a 53-ft trailer.
    cp_solver = routing.solver()
    for order_idx, order in enumerate(orders):
        if order.allowed_truck_types:
            node_index = manager.NodeToIndex(order_idx + 1)
            vehicle_var = routing.VehicleVar(node_index)
            for v_idx, truck in enumerate(trucks):
                if truck.truck_type not in order.allowed_truck_types:
                    cp_solver.Add(vehicle_var != v_idx)

    # Soft constraint — dropped orders reported rather than causing solver failure.
    for node in range(1, len(distance_matrix)):
        routing.AddDisjunction([manager.NodeToIndex(node)], UNASSIGNED_ORDER_PENALTY)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = solver_time_limit

    solution = routing.SolveWithParameters(params)
    if not solution:
        return [], list(orders)

    time_dim = routing.GetDimensionOrDie("Time")

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
            end_time_scaled = solution.Value(time_dim.CumulVar(routing.End(v_idx)))
            assignments.append(TruckAssignment(
                truck=truck,
                stops=stops,
                route_distance_miles=route_dist / DISTANCE_SCALE,
                route_time_hours=end_time_scaled / TIME_SCALE / 60,
            ))

    # Detect dropped orders: a dropped node's NextVar points to itself.
    dropped = []
    for node in range(1, len(orders) + 1):
        idx = manager.NodeToIndex(node)
        if solution.Value(routing.NextVar(idx)) == idx:
            dropped.append(orders[node - 1])

    return assignments, dropped
