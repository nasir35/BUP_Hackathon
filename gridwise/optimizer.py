"""
GridWise — Stage 3: LP Optimizer

Builds a 24-hour linear program (PuLP + bundled CBC) that produces the cheapest
valid battery / grid / solar schedule satisfying:
  - base energy rules (Section 1.5 of the build plan)
  - directive-derived per-hour overrides (Section 1.4)

The directive_application layer is exposed so it can be unit-tested in isolation.
The main `optimize_schedule(...)` entry point returns a fully-populated
HourlyPlanEntry list plus totals, ready for Stage 4 (self-validator) and the
API response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence

try:
    import pulp  # type: ignore
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "PuLP is required. Install with `pip install pulp==2.8.0`."
    ) from e

# Local imports (these modules are pure Python and side-effect free).
from schemas import (
    BatteryConfig,
    HourlyDemand,
    HourlyPlanEntry,
)
from guardrails import ALLOWED_DIRECTIVE_TYPES


# ---------------------------------------------------------------------------
# Public dataclasses (stage boundary objects — easy to unit-test)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptimizationResult:
    """Outputs from Stage 3, ready for Stage 4 and the API response layer."""

    plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    solver_status: str  # e.g. "Optimal"


class InfeasibleSchedule(Exception):
    """Raised when the LP has no feasible solution (should not happen on
    organizer-valid inputs, but protects against malformed edge cases)."""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def optimize_schedule(
    hours: Sequence[HourlyDemand],
    battery: BatteryConfig,
    sanitized_directives: Sequence[dict],
) -> OptimizationResult:
    """Stage 3 entry point. Caller must hand in the already-Stage-2-sanitized
    directive list (length = number of operator notes). Returns a deterministic
    optimal schedule (CBC always returns the same LP optimum) plus totals."""

    # Normalise inputs to lists (defensive against tuple / generator inputs).
    hour_list: List[HourlyDemand] = list(hours)
    sanitized: List[dict] = list(sanitized_directives)
    capacity = float(battery.capacity_kwh)
    min_energy = float(battery.minimum_energy_kwh)
    initial = float(battery.initial_energy_kwh)
    max_charge = float(battery.max_charge_kwh_per_hour)
    max_discharge = float(battery.max_discharge_kwh_per_hour)

    # ---------------- Directive application layer ----------------
    # Per-hour override maps derived from the (sanitized) directive list.
    solar_factor: List[float] = [1.0] * 24      # multiplicative factor on raw solar
    reserve_floor: List[float] = [min_energy] * 24  # minimum_energy_kwh override
    no_charge_hours: set = set()                  # hours where charge is forced 0
    no_discharge_hours: set = set()               # hours where discharge is forced 0
    grid_cap: List[float] = [float("inf")] * 24  # kWh cap on grid per hour

    for d in sanitized:
        dtype = d.get("directive_type")
        if dtype not in ALLOWED_DIRECTIVE_TYPES:
            continue  # guardrails should never allow this; defensive only
        adj = d.get("structured_adjustment") or {}
        hours_set: List[int] = list(adj.get("hours", []) or [])

        if dtype == "solar_reduction":
            try:
                factor = float(adj.get("factor", 1.0))
            except (TypeError, ValueError):
                continue
            factor = max(0.0, min(1.0, factor))
            for h in hours_set:
                if 0 <= h <= 23:
                    solar_factor[h] = min(solar_factor[h], factor)
        elif dtype == "minimum_battery_reserve":
            try:
                min_kwh = float(adj.get("minimum_energy_kwh", min_energy))
            except (TypeError, ValueError):
                continue
            min_kwh = max(0.0, min(capacity, min_kwh))
            for h in hours_set:
                if 0 <= h <= 23:
                    reserve_floor[h] = max(reserve_floor[h], min_kwh)
        elif dtype == "no_charge_window":
            for h in hours_set:
                if 0 <= h <= 23:
                    no_charge_hours.add(h)
        elif dtype == "no_discharge_window":
            for h in hours_set:
                if 0 <= h <= 23:
                    no_discharge_hours.add(h)
        elif dtype == "max_grid_window":
            try:
                cap = float(adj.get("max_grid_kwh", float("inf")))
            except (TypeError, ValueError):
                continue
            cap = max(0.0, cap)
            for h in hours_set:
                if 0 <= h <= 23:
                    grid_cap[h] = min(grid_cap[h], cap)
        # no_op: nothing to apply

    # Effective per-hour solar availability (post any reduction directives).
    effective_solar: List[float] = [
        max(0.0, float(hd.solar_kwh) * solar_factor[hd.hour])
        for hd in hour_list
    ]

    # -------------------- Build the LP model --------------------
    H = list(range(24))  # canonical hour ordering

    prob = pulp.LpProblem("gridwise_24h", pulp.LpMinimize)

    # Decision variables (continuous, lower bound 0 by default for energy terms).
    grid = [pulp.LpVariable(f"grid_{h}", lowBound=0) for h in H]
    solar_used = [pulp.LpVariable(f"solar_used_{h}", lowBound=0) for h in H]
    charge = [pulp.LpVariable(f"charge_{h}", lowBound=0) for h in H]
    discharge = [pulp.LpVariable(f"discharge_{h}", lowBound=0) for h in H]
    battery_energy = [
        pulp.LpVariable(f"battery_energy_{h}", lowBound=min_energy, upBound=capacity)
        for h in H
    ]
    peak_grid_var = pulp.LpVariable("peak_grid_var", lowBound=0)

    # ---------- Objective: minimise Σ grid[h] * tariff[h] + tiny peak tie-breaker ----------
    # The 1e-4 tie-breaker prefers lower peak grid when cost is identical (multiple optima).
    prob += (
        pulp.lpSum(grid[h] * float(hour_list[h].tariff_bdt_per_kwh) for h in H)
        + 1e-4 * peak_grid_var
    )

    # ---------- Per-hour constraints ----------
    for h in H:
        hd = hour_list[h]
        demand = float(hd.demand_kwh)
        # Bound peak grid variable
        prob += peak_grid_var >= grid[h], f"peak_bound_{h}"
        # Energy balance: grid + solar_used + discharge == demand + charge
        prob += (
            grid[h] + solar_used[h] + discharge[h]
            == demand + charge[h],
            f"balance_{h}",
        )
        # Solar upper bound (post-directive).
        prob += solar_used[h] <= effective_solar[h], f"solar_cap_{h}"
        # Charge / discharge rate limits.
        prob += charge[h] <= max_charge, f"charge_rate_{h}"
        prob += discharge[h] <= max_discharge, f"discharge_rate_{h}"
        # Directive-driven charge / discharge locks.
        if h in no_charge_hours:
            prob += charge[h] == 0, f"no_charge_{h}"
        if h in no_discharge_hours:
            prob += discharge[h] == 0, f"no_discharge_{h}"
        # Directive-driven battery floor override.
        prob += (
            battery_energy[h] >= reserve_floor[h],
            f"reserve_floor_{h}",
        )
        # Directive-driven grid cap.
        if grid_cap[h] != float("inf"):
            prob += grid[h] <= grid_cap[h], f"grid_cap_{h}"

    # ---------- Battery dynamics ----------
    # battery_energy[h] = battery_energy[h-1] + charge[h] - discharge[h]
    # Using initial energy for h=0's predecessor.
    prob += (
        battery_energy[0] == initial + charge[0] - discharge[0],
        "battery_dyn_0",
    )
    for h in H[1:]:
        prob += (
            battery_energy[h] == battery_energy[h - 1] + charge[h] - discharge[h],
            f"battery_dyn_{h}",
        )

    # End-of-day battery neutrality: battery_energy[23] must return to initial
    prob += (
        battery_energy[23] == initial,
        "battery_neutrality_end_of_day",
    )

    # -------------------- Solve --------------------
    # Use the bundled CBC solver. Silence solver chatter; we only need status.
    solver = pulp.PULP_CBC_CMD(msg=False, warmStart=False)
    status = prob.solve(solver)

    if pulp.LpStatus[status] != "Optimal":
        raise InfeasibleSchedule(
            f"Solver returned non-optimal status: {pulp.LpStatus[status]}"
        )

    # -------------------- Build plan entries --------------------
    plan: List[HourlyPlanEntry] = []

    for h in H:
        g = float(grid[h].value() or 0.0)
        su = float(solar_used[h].value() or 0.0)
        ch = float(charge[h].value() or 0.0)
        dis = float(discharge[h].value() or 0.0)
        b_after = float(battery_energy[h].value() or 0.0)

        # Round-tiny-noise (CBC returns values like 1e-12 for idle terms).
        g = _zero_small(g)
        su = _zero_small(su)
        ch = _zero_small(ch)
        dis = _zero_small(dis)
        b_after = _zero_small(b_after)

        # LP-relaxation artifact: if simultaneous charge and discharge occur at
        # boundary hours, cancel them into a single net flow.
        # Note: net = ch - dis preserves both energy balance and battery dynamics identically.
        if ch > 1e-6 and dis > 1e-6:
            net = ch - dis
            if net > 0:
                ch = net
                dis = 0.0
            elif net < 0:
                ch = 0.0
                dis = -net
            else:
                ch = 0.0
                dis = 0.0

        if ch > 1e-6:
            action = "charge"
            bat_kwh = ch
        elif dis > 1e-6:
            action = "discharge"
            bat_kwh = dis
        else:
            action = "idle"
            bat_kwh = 0.0

        plan.append(
            HourlyPlanEntry(
                hour=h,
                grid_kwh=round(g, 4),
                solar_used_kwh=round(su, 4),
                battery_action=action,
                battery_kwh=round(bat_kwh, 4),
                battery_energy_after_kwh=round(b_after, 4),
            )
        )

    total_grid = sum(p.grid_kwh for p in plan)
    total_cost = sum(
        p.grid_kwh * float(hour_list[p.hour].tariff_bdt_per_kwh) for p in plan
    )
    peak_grid = max((p.grid_kwh for p in plan), default=0.0)

    return OptimizationResult(
        plan=plan,
        total_grid_kwh=round(total_grid, 4),
        total_cost_bdt=round(total_cost, 4),
        peak_grid_kwh=round(peak_grid, 4),
        solver_status="Optimal",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _zero_small(value: float, tol: float = 1e-6) -> float:
    """Treat near-zero values as exactly zero to keep plan output clean."""
    return 0.0 if abs(value) < tol else value


def build_directive_overrides(
    sanitized_directives: Iterable[dict],
    capacity_kwh: float,
    base_minimum_energy_kwh: float,
) -> dict:
    """
    Pure helper exposed for unit-testing the directive layer independently
    from the LP solver. Returns a dict of per-hour override maps.
    """
    capacity = float(capacity_kwh)
    base_min = float(base_minimum_energy_kwh)

    solar_factor = [1.0] * 24
    reserve_floor = [base_min] * 24
    no_charge_hours: set = set()
    no_discharge_hours: set = set()
    grid_cap = [float("inf")] * 24

    for d in sanitized_directives:
        dtype = d.get("directive_type")
        adj = d.get("structured_adjustment") or {}
        for h in list(adj.get("hours", []) or []):
            if not isinstance(h, int) or not (0 <= h <= 23):
                continue
            if dtype == "solar_reduction":
                try:
                    f = float(adj.get("factor", 1.0))
                except (TypeError, ValueError):
                    continue
                f = max(0.0, min(1.0, f))
                solar_factor[h] = min(solar_factor[h], f)
            elif dtype == "minimum_battery_reserve":
                try:
                    m = float(adj.get("minimum_energy_kwh", base_min))
                except (TypeError, ValueError):
                    continue
                m = max(0.0, min(capacity, m))
                reserve_floor[h] = max(reserve_floor[h], m)
            elif dtype == "no_charge_window":
                no_charge_hours.add(h)
            elif dtype == "no_discharge_window":
                no_discharge_hours.add(h)
            elif dtype == "max_grid_window":
                try:
                    c = float(adj.get("max_grid_kwh", float("inf")))
                except (TypeError, ValueError):
                    continue
                c = max(0.0, c)
                grid_cap[h] = min(grid_cap[h], c)

    return {
        "solar_factor": solar_factor,
        "reserve_floor": reserve_floor,
        "no_charge_hours": no_charge_hours,
        "no_discharge_hours": no_discharge_hours,
        "grid_cap": grid_cap,
    }
