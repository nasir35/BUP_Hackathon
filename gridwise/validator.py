"""
GridWise — Stage 4: Final Self-Validator

Replays the optimized hourly_plan independently of the solver and verifies
every invariant from Section 1.5. If anything is off, returns a structured
error tuple so the API layer can downgrade to no_op/no_overrides and retry.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from schemas import (
    BatteryConfig,
    HourlyDemand,
    HourlyPlanEntry,
)


# Tolerance per Section 1.5: "Numeric tolerance for all comparisons: 0.01 kWh / 0.01 BDT".
DEFAULT_TOLERANCE = 0.01


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when a planner output fails self-validation (should be rare)."""

    def __init__(self, message: str, *, hour: int = -1):
        super().__init__(message)
        self.hour = hour


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def validate_plan(
    plan: Sequence[HourlyPlanEntry],
    hours: Sequence[HourlyDemand],
    battery: BatteryConfig,
    *,
    total_grid_kwh: float,
    total_cost_bdt: float,
    peak_grid_kwh: float,
    sanitized_directives: Sequence[dict],
    tolerance: float = DEFAULT_TOLERANCE,
) -> Tuple[float, float, float]:
    """Recompute everything from the plan and verify invariants.

    Returns `(recomputed_total_grid, recomputed_total_cost, recomputed_peak_grid)`
    so the API layer can re-inject them after the LP solve (small numerical drift).

    Raises :class:`ValidationError` on any invariant violation.
    """

    # ---- Defensive sanity: must be 24 entries ----
    if len(plan) != 24:
        raise ValidationError(f"plan must have 24 entries, got {len(plan)}")
    if len(hours) != 24:
        raise ValidationError(f"hours must have 24 entries, got {len(hours)}")

    # ---- Build hour -> (demand, solar, tariff) lookup ----
    by_hour = {h.hour: h for h in hours}

    # ---- Pre-compute directive-derived expectations ----
    overrides = _derive_overrides(sanitized_directives, battery)

    # ---- Replay walk ----
    prev_energy = float(battery.initial_energy_kwh)
    recomputed_grid_total = 0.0
    recomputed_cost_total = 0.0
    recomputed_peak = 0.0

    for entry in plan:
        h = entry.hour
        if h not in by_hour:
            raise ValidationError(f"plan hour {h} not in hours list", hour=h)
        hd = by_hour[h]

        demand = float(hd.demand_kwh)
        raw_solar = float(hd.solar_kwh)
        tariff = float(hd.tariff_bdt_per_kwh)

        grid = float(entry.grid_kwh)
        solar_used = float(entry.solar_used_kwh)
        action = entry.battery_action
        bat_kwh = float(entry.battery_kwh)
        b_after = float(entry.battery_energy_after_kwh)

        # Action / magnitude consistency.
        if action == "charge":
            charge = bat_kwh
            discharge = 0.0
        elif action == "discharge":
            charge = 0.0
            discharge = bat_kwh
        elif action == "idle":
            charge = 0.0
            discharge = 0.0
            if abs(bat_kwh) > tolerance:
                raise ValidationError(
                    f"hour {h}: action=idle but battery_kwh={bat_kwh}", hour=h
                )
        else:
            raise ValidationError(f"hour {h}: unknown action '{action}'", hour=h)

        # 1) Energy balance: grid + solar_used + discharge == demand + charge.
        lhs = grid + solar_used + discharge
        rhs = demand + charge
        if abs(lhs - rhs) > tolerance:
            raise ValidationError(
                f"hour {h}: energy balance violated "
                f"(grid+solar+dis={lhs:.4f} vs demand+charge={rhs:.4f})", hour=h
            )

        # 2) Solar upper bound (post-directive).
        eff_solar = raw_solar * overrides["solar_factor"][h]
        if solar_used < -tolerance or solar_used > eff_solar + tolerance:
            raise ValidationError(
                f"hour {h}: solar_used={solar_used:.4f} out of "
                f"[0, {eff_solar:.4f}]", hour=h
            )

        # 3) Battery dynamics: previous + charge - discharge == b_after.
        expected_after = prev_energy + charge - discharge
        if abs(expected_after - b_after) > tolerance:
            raise ValidationError(
                f"hour {h}: battery dynamics violated "
                f"(prev+ch-dis={expected_after:.4f} vs after={b_after:.4f})",
                hour=h,
            )

        # 4) Battery bounds (incl. per-hour reserve floor override).
        lo = overrides["reserve_floor"][h]
        hi = float(battery.capacity_kwh)
        if b_after < lo - tolerance or b_after > hi + tolerance:
            raise ValidationError(
                f"hour {h}: battery level {b_after:.4f} outside "
                f"[{lo:.4f}, {hi:.4f}]", hour=h
            )

        # 5) Charge/discharge rate limits.
        if charge > float(battery.max_charge_kwh_per_hour) + tolerance:
            raise ValidationError(
                f"hour {h}: charge {charge:.4f} exceeds max_charge "
                f"{float(battery.max_charge_kwh_per_hour):.4f}", hour=h
            )
        if discharge > float(battery.max_discharge_kwh_per_hour) + tolerance:
            raise ValidationError(
                f"hour {h}: discharge {discharge:.4f} exceeds max_discharge "
                f"{float(battery.max_discharge_kwh_per_hour):.4f}", hour=h
            )

        # 6) Directive locks.
        if h in overrides["no_charge"] and charge > tolerance:
            raise ValidationError(
                f"hour {h}: charge={charge:.4f} violated no_charge_window",
                hour=h,
            )
        if h in overrides["no_discharge"] and discharge > tolerance:
            raise ValidationError(
                f"hour {h}: discharge={discharge:.4f} violated no_discharge_window",
                hour=h,
            )

        # 7) Grid cap.
        cap = overrides["grid_cap"][h]
        if cap != float("inf") and grid > cap + tolerance:
            raise ValidationError(
                f"hour {h}: grid={grid:.4f} exceeds cap {cap:.4f}", hour=h
            )

        # 8) All terms >= 0.
        for name, val in (
            ("grid", grid), ("solar_used", solar_used),
            ("bat_kwh", bat_kwh), ("b_after", b_after),
        ):
            if val < -tolerance:
                raise ValidationError(
                    f"hour {h}: {name}={val:.4f} is negative", hour=h
                )

        recomputed_grid_total += grid
        recomputed_cost_total += grid * tariff
        if grid > recomputed_peak:
            recomputed_peak = grid
        prev_energy = b_after

    # End-of-day neutrality: prev_energy after hour 23 must equal initial.
    if abs(prev_energy - float(battery.initial_energy_kwh)) > tolerance:
        raise ValidationError(
            f"end-of-day neutrality violated: battery ends at {prev_energy:.4f}, "
            f"initial was {float(battery.initial_energy_kwh):.4f}"
        )

    # Totals must match (within tolerance) the reported ones.
    if abs(recomputed_grid_total - float(total_grid_kwh)) > tolerance:
        raise ValidationError(
            f"reported total_grid_kwh {float(total_grid_kwh):.4f} != "
            f"recomputed {recomputed_grid_total:.4f}"
        )
    if abs(recomputed_cost_total - float(total_cost_bdt)) > tolerance:
        raise ValidationError(
            f"reported total_cost_bdt {float(total_cost_bdt):.4f} != "
            f"recomputed {recomputed_cost_total:.4f}"
        )
    if abs(recomputed_peak - float(peak_grid_kwh)) > tolerance:
        raise ValidationError(
            f"reported peak_grid_kwh {float(peak_grid_kwh):.4f} != "
            f"recomputed {recomputed_peak:.4f}"
        )

    return (
        round(recomputed_grid_total, 4),
        round(recomputed_cost_total, 4),
        round(recomputed_peak, 4),
    )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _derive_overrides(
    sanitized_directives: Sequence[dict],
    battery: BatteryConfig,
) -> dict:
    """Mirror the optimizer's directive-application logic so the validator
    computes the same per-hour expectations."""
    capacity = float(battery.capacity_kwh)
    base_min = float(battery.minimum_energy_kwh)

    solar_factor = [1.0] * 24
    reserve_floor = [base_min] * 24
    no_charge: set = set()
    no_discharge: set = set()
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
                no_charge.add(h)
            elif dtype == "no_discharge_window":
                no_discharge.add(h)
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
        "no_charge": no_charge,
        "no_discharge": no_discharge,
        "grid_cap": grid_cap,
    }


# ---------------------------------------------------------------------------
# Standalone sanity harness (run with `python validator.py <path-to-json>`)
# Useful for quick smoke-tests without booting the API.
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python validator.py <sample_case_json>")
        sys.exit(2)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cases = json.load(f)

    for case in cases if isinstance(cases, list) else cases.get("cases", [cases]):
        print(f"Smoke-replay not wired in this snippet; case id={case.get('id')}")
