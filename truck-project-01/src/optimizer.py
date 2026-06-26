"""
Capacitated Vehicle Routing Problem solver — Google OR-Tools.

Constraint model:
  - Capacity dimension: hard floor-space cap per truck
  - Time dimension: hard daily hour cap per driver (supervisor's 9-hour rule)
      straight trucks: haversine_miles / straight_speed_mph (47 mph)
      53-ft trailers:  haversine_miles / trailer_speed_mph  (40 mph)
      route_time = sum(drive_legs) + (stops × stop_time_minutes)
  - Fixed activation cost per truck type: fills 26-ft straight trucks before 53-ft trailers
  - Per-order truck type restrictions via VehicleVar constraints (homebuilder stops)
  - Soft disjunctions: dropped orders reported explicitly, never silently lost
  - Priority weighting in arc cost: urgent orders pulled to earlier stops
"""

import json
import math
import urllib.request
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
MAX_ROUTE_HOURS = 9.0       # supervisor's practical daily driver cap; 11 hrs is legal max
STOP_TIME_MINUTES = 45.0    # default unload time per stop (neighborhood delivery, no dock)
STRAIGHT_SPEED_MPH = 47.0   # 26ft straight trucks — nimbler on tight residential streets
TRAILER_SPEED_MPH = 40.0    # 53ft trailers — slower on all road types (confirmed)
AVG_SPEED_MPH = 45.0        # fallback when truck type unknown (used in check_route_cap)
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


def _osrm_table(coords: list[tuple[float, float]], server: str) -> Optional[list[list[float]]]:
    """
    Single OSRM /table call → N×N road distances in meters, or None on any failure.
    Falls back gracefully so haversine takes over when OSRM is unreachable.
    """
    lnglat = ";".join(f"{lon},{lat}" for lat, lon in coords)
    url = f"{server.rstrip('/')}/table/v1/driving/{lnglat}?annotations=distance"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read())
        return data["distances"] if data.get("code") == "Ok" else None
    except Exception:
        return None


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlng = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _build_distance_matrix(
    coords: list[tuple[float, float]],
    road_m: Optional[list[list[float]]] = None,
) -> list[list[int]]:
    """Distance in DISTANCE_SCALE units (thousandths of a mile). road_m = OSRM meters matrix."""
    n = len(coords)
    if road_m is not None:
        return [[round(road_m[i][j] / 1609.344 * DISTANCE_SCALE) for j in range(n)] for i in range(n)]
    return [
        [0 if i == j else round(_haversine_miles(*coords[i], *coords[j]) * DISTANCE_SCALE) for j in range(n)]
        for i in range(n)
    ]


def _build_time_matrix(
    coords: list[tuple[float, float]],
    avg_speed_mph: float = AVG_SPEED_MPH,
    road_m: Optional[list[list[float]]] = None,
) -> list[list[int]]:
    """Travel-only time in TIME_SCALE units (hundredths of a minute). road_m = OSRM meters matrix."""
    n = len(coords)
    if road_m is not None:
        return [
            [round((road_m[i][j] / 1609.344) / avg_speed_mph * 60 * TIME_SCALE) for j in range(n)]
            for i in range(n)
        ]
    return [
        [0 if i == j else round(_haversine_miles(*coords[i], *coords[j]) / avg_speed_mph * 60 * TIME_SCALE) for j in range(n)]
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
    straight_speed_mph: float = STRAIGHT_SPEED_MPH,
    trailer_speed_mph: float = TRAILER_SPEED_MPH,
) -> list[str]:
    """
    Returns warning strings for orders whose minimum round-trip drive time already
    exceeds max_route_hours. Uses the slower trailer speed for conservative flagging
    since we don't know truck assignment at pre-flight time.
    """
    warnings = []
    for o in orders:
        if o.lat is None or o.lon is None:
            continue
        drive_miles = 2 * _haversine_miles(depot_coords[0], depot_coords[1], o.lat, o.lon)
        drive_hours = drive_miles / trailer_speed_mph  # conservative: assume slower truck
        total_hours = drive_hours + stop_time_minutes / 60
        if total_hours > max_route_hours:
            warnings.append(
                f"Order {o.order_id} ({o.customer_name}) @ {o.address} — "
                f"min round-trip ~{drive_hours:.1f} hr drive + {stop_time_minutes:.0f} min stop "
                f"exceeds {max_route_hours:.0f}-hour cap. Likely a two-day run."
            )
    return warnings


def solve(
    orders: list[Order],
    trucks: list[Truck],
    depot_coords: tuple[float, float] = (33.749, -84.388),  # Atlanta default
    max_route_hours: float = MAX_ROUTE_HOURS,
    stop_time_minutes: float = STOP_TIME_MINUTES,
    straight_speed_mph: float = STRAIGHT_SPEED_MPH,
    trailer_speed_mph: float = TRAILER_SPEED_MPH,
    solver_time_limit: int = SOLVER_TIME_LIMIT_SECONDS,
    osrm_server: str = "",
) -> tuple[list[TruckAssignment], list[Order]]:
    """
    Returns (assignments, dropped_orders).
    dropped_orders is non-empty when fleet capacity is insufficient or a route
    cannot be completed within the max_route_hours time cap.

    Route time uses per-vehicle speed: straight trucks at straight_speed_mph,
    53-ft trailers at trailer_speed_mph. Plus stop_time_minutes per delivery node.
    """
    if not orders or not trucks:
        return [], list(orders)

    coords = [depot_coords] + [
        (o.lat, o.lon) if (o.lat is not None and o.lon is not None) else depot_coords
        for o in orders
    ]

    road_m = _osrm_table(coords, osrm_server) if osrm_server else None
    distance_matrix = _build_distance_matrix(coords, road_m)
    straight_time_matrix = _build_time_matrix(coords, straight_speed_mph, road_m)
    trailer_time_matrix = _build_time_matrix(coords, trailer_speed_mph, road_m)
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

    # Time dimension — per-vehicle speed: straight trucks faster than 53-ft trailers.
    # Each vehicle gets its own transit callback using the appropriate time matrix.
    # j == 0 is the depot return leg: no unload time added there.
    def _make_time_cb(matrix):
        def _cb(from_idx: int, to_idx: int) -> int:
            i = manager.IndexToNode(from_idx)
            j = manager.IndexToNode(to_idx)
            return matrix[i][j] + (0 if j == 0 else stop_time_scaled)
        return _cb

    time_transit_indices = []
    for truck in trucks:
        tm = trailer_time_matrix if truck.truck_type == "trailer" else straight_time_matrix
        time_transit_indices.append(routing.RegisterTransitCallback(_make_time_cb(tm)))

    max_time_scaled = round(max_route_hours * 60 * TIME_SCALE)
    routing.AddDimensionWithVehicleTransitAndCapacity(
        time_transit_indices,
        0,                              # no waiting time slack
        [max_time_scaled] * len(trucks),
        True,                           # cumulative from zero at depot
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
