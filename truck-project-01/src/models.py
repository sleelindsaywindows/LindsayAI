from dataclasses import dataclass
from typing import Optional


@dataclass
class Order:
    order_id: str
    customer_name: str
    address: str
    capacity_units: float   # floor sq ft; label defined by config.yaml
    priority: int = 0       # 0 = normal, 10 = urgent
    notes: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass
class Truck:
    name: str
    truck_type: str         # "straight" | "trailer"
    max_capacity: float     # floor sq ft (practical, not theoretical)
    fixed_cost: float = 5.0     # activation penalty in equivalent miles — fills small trucks first
    cost_per_mile: float = 0.0  # $/mile placeholder for future cost objective


@dataclass
class RouteStop:
    order: Order
    stop_number: int        # 1 = first delivery


@dataclass
class TruckAssignment:
    truck: Truck
    stops: list             # list[RouteStop] in delivery order
    route_distance_miles: float = 0.0

    @property
    def load_sequence(self) -> list:
        # LIFO: last delivery stop is loaded first (goes deepest in truck)
        return list(reversed(self.stops))

    @property
    def total_capacity_used(self) -> float:
        return sum(s.order.capacity_units for s in self.stops)

    @property
    def utilization_pct(self) -> float:
        return (self.total_capacity_used / self.truck.max_capacity) * 100
