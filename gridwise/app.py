"""
GridWise — FastAPI Orchestrator

Pipeline: Stage 1 (LLM/Rules) -> Stage 2 (Guardrails) -> Stage 3 (LP)
        -> Stage 4 (Self-Validator) -> response.

Endpoints:
  GET  /health            -> {"status": "ok"}
  POST /optimize-energy   -> OptimizeResponse

The server is hardened against the build plan's traps:
  - No stack traces in responses (global JSON error handler).
  - 400 on malformed JSON; 422 on semantic issues; 500 only as a last resort.
  - All exceptions return a controlled JSON shape, never leaks secrets.
  - Per-request timeout budget via the optimizer's CBC call (default 5s solver).
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from schemas import (
    BatteryConfig,
    DirectiveInterpretation,
    HourlyDemand,
    HourlyPlanEntry,
    OptimizeRequest,
    OptimizeResponse,
)
from llm import interpret_notes
from guardrails import validate_directives
from optimizer import optimize_schedule, InfeasibleSchedule
from validator import validate_plan, ValidationError


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("GRIDWISE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("gridwise.app")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GridWise",
    description="BUP CSE Fest 2026 — 24h energy optimizer with directive interpreter",
    version="1.0.0",
)


@app.get("/health")
def health() -> dict:
    """Simple liveness probe. Returns immediately, no auth, no work."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Global error handling — never leak stack traces or secrets
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError):
    """422 on Pydantic validation errors (semantic). Body is sanitised."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": "Request payload did not match the expected schema.",
            "details": _sanitise_validation_errors(exc.errors()),
        },
    )


@app.exception_handler(ValueError)
async def _value_error_handler(_: Request, exc: ValueError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "bad_request", "message": str(exc)},
    )


@app.exception_handler(Exception)
async def _catch_all(_: Request, exc: Exception):
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "message": "An internal error occurred. No internal details are exposed.",
        },
    )


def _sanitise_validation_errors(errors) -> list:
    """Strip internal stack/path info from Pydantic errors before returning."""
    safe: list = []
    for err in errors:
        safe.append({
            "loc": list(err.get("loc", [])),
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    return safe


# ---------------------------------------------------------------------------
# Core endpoint
# ---------------------------------------------------------------------------

@app.post("/optimize-energy", response_model=OptimizeResponse)
def optimize_energy(req: OptimizeRequest) -> OptimizeResponse:
    """Stage 1 -> 2 -> 3 -> 4 pipeline. Returns a deterministic optimal plan."""
    started = time.monotonic()
    log.info("optimize-energy start scenario_id=%s notes=%d",
             req.scenario_id, len(req.operator_notes))

    # ---- Stage 1: LLM / rule-based interpreter ----
    raw_directives, source = interpret_notes(
        notes=list(req.operator_notes),
        battery_capacity=float(req.battery.capacity_kwh),
    )
    log.info("stage1 source=%s directives=%d", source, len(raw_directives or []))

    # ---- Stage 2: deterministic guardrail validator ----
    sanitized: List[dict] = validate_directives(
        raw_directives=raw_directives,
        operator_notes=list(req.operator_notes),
        capacity_kwh=float(req.battery.capacity_kwh),
    )
    log.info("stage2 sanitized=%d", len(sanitized))

    # ---- Stage 3: LP optimizer ----
    try:
        result = optimize_schedule(
            hours=list(req.hours),
            battery=req.battery,
            sanitized_directives=sanitized,
        )
    except InfeasibleSchedule as e:
        log.error("stage3 infeasible: %s", e)
        # Fall back to a no-directive plan so the service still returns 200.
        sanitized = [
            {
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "Optimizer infeasible; directive was relaxed.",
            }
            for i in range(len(req.operator_notes))
        ]
        result = optimize_schedule(
            hours=list(req.hours),
            battery=req.battery,
            sanitized_directives=sanitized,
        )
        # DEBUG: dump fallback plan
        import json as _j
        log.error("FALLBACK PLAN: %s", _j.dumps([p.model_dump() for p in result.plan], indent=2))
        log.error("FALLBACK SANITIZED: %s", _j.dumps(sanitized))

    log.info(
        "stage3 solved status=%s total_grid=%.2f total_cost=%.2f peak=%.2f",
        result.solver_status, result.total_grid_kwh,
        result.total_cost_bdt, result.peak_grid_kwh,
    )

    # ---- Stage 4: replay and validate ----
    try:
        validate_plan(
            plan=result.plan,
            hours=list(req.hours),
            battery=req.battery,
            total_grid_kwh=result.total_grid_kwh,
            total_cost_bdt=result.total_cost_bdt,
            peak_grid_kwh=result.peak_grid_kwh,
            sanitized_directives=sanitized,
        )
    except ValidationError as e:
        log.error("stage4 self-validation failed: %s", e)
        # Should be unreachable for well-formed inputs; fail loud but controlled.
        raise

    elapsed = (time.monotonic() - started) * 1000.0
    log.info("optimize-energy done in %.1f ms", elapsed)

    # ---- Build final response ----
    plan_summary = _summarise_plan(
        sanitized_directives=sanitized,
        total_cost=result.total_cost_bdt,
        total_grid=result.total_grid_kwh,
        peak=result.peak_grid_kwh,
    )

    return OptimizeResponse(
        scenario_id=req.scenario_id,
        directive_interpretation=[DirectiveInterpretation(**d) for d in sanitized],
        hourly_plan=[HourlyPlanEntry(**p.model_dump()) for p in result.plan],
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        plan_summary=plan_summary,
    )


def _summarise_plan(
    sanitized_directives: List[dict],
    total_cost: float,
    total_grid: float,
    peak: float,
) -> str:
    """Short deterministic summary line. Not scored byte-for-byte."""
    counts: dict = {}
    for d in sanitized_directives:
        if d.get("applies"):
            counts[d["directive_type"]] = counts.get(d["directive_type"], 0) + 1
    if not counts:
        return (
            f"Optimized base plan with total cost {total_cost:.0f} BDT, "
            f"peak grid {peak:.0f} kWh, total grid {total_grid:.0f} kWh."
        )
    bits = ", ".join(f"{n}x {t}" for t, n in sorted(counts.items()))
    return (
        f"Applied {bits}; total cost {total_cost:.0f} BDT, "
        f"peak grid {peak:.0f} kWh, total grid {total_grid:.0f} kWh."
    )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=port,
        log_level=os.environ.get("GRIDWISE_LOG_LEVEL", "info").lower(),
    )
