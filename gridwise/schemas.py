"""Pydantic schemas matching the GridWise API contract exactly (Section 1.2/1.3)."""
from __future__ import annotations
from typing import Any, List, Optional
from pydantic import BaseModel, Field, ConfigDict


class HourlyDemand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0)
    solar_kwh: float = Field(..., ge=0)
    tariff_bdt_per_kwh: float = Field(..., ge=0)


class BatteryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capacity_kwh: float = Field(..., gt=0)
    initial_energy_kwh: float = Field(..., ge=0)
    minimum_energy_kwh: float = Field(..., ge=0)
    max_charge_kwh_per_hour: float = Field(..., ge=0)
    max_discharge_kwh_per_hour: float = Field(..., ge=0)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(..., min_length=1)
    operator_notes: List[str] = Field(..., min_length=1, max_length=3)
    hours: List[HourlyDemand] = Field(..., min_length=24, max_length=24)
    battery: BatteryConfig

    def model_post_init(self, __context: Any) -> None:
        # ensure exactly hours 0..23 with no duplicates and all present
        seen = set()
        for h in self.hours:
            if h.hour in seen:
                raise ValueError(f"duplicate hour {h.hour}")
            seen.add(h.hour)
        if seen != set(range(24)):
            raise ValueError("hours must contain exactly one entry per hour 0..23")
        if self.battery.minimum_energy_kwh > self.battery.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if self.battery.initial_energy_kwh > self.battery.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        # all notes must be non-empty
        for i, n in enumerate(self.operator_notes):
            if not n or not n.strip():
                raise ValueError(f"operator_notes[{i}] is empty")


class DirectiveInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: Optional[dict] = None
    explanation: str


class HourlyPlanEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: str  # charge|discharge|idle
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
