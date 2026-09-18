"""Stage 2 — deterministic guardrail validator for LLM output."""
from __future__ import annotations
from typing import Any, List, Tuple

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

TOL = 1e-6


def _clean_hours(values: Any) -> List[int]:
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            iv = v
        elif isinstance(v, float):
            if not v.is_integer():
                continue
            iv = int(v)
        else:
            continue
        if 0 <= iv <= 23:
            out.append(iv)
    out = sorted(set(out))
    return out


def _clean_adjustment(directive_type: str, raw: Any, capacity_kwh: float) -> Tuple[dict | None, bool]:
    """Return (cleaned_adjustment, valid_flag). valid_flag=False means downgrade to no_op."""
    if directive_type == "no_op":
        return None, True
    if not isinstance(raw, dict):
        return None, False
    hours = _clean_hours(raw.get("hours"))

    if directive_type == "solar_reduction":
        factor = raw.get("factor")
        if not isinstance(factor, (int, float)) or factor < 0 or factor > 1:
            return None, False
        if not hours:
            return None, False
        return {"hours": hours, "factor": float(factor)}, True

    if directive_type == "minimum_battery_reserve":
        v = raw.get("minimum_energy_kwh")
        if not isinstance(v, (int, float)) or v < 0:
            return None, False
        v = float(v)
        if v > capacity_kwh + TOL:
            return None, False
        if not hours:
            return None, False
        return {"hours": hours, "minimum_energy_kwh": v}, True

    if directive_type == "no_charge_window":
        if not hours:
            return None, False
        return {"hours": hours}, True

    if directive_type == "no_discharge_window":
        if not hours:
            return None, False
        return {"hours": hours}, True

    if directive_type == "max_grid_window":
        cap = raw.get("max_grid_kwh")
        if not isinstance(cap, (int, float)) or cap < 0:
            return None, False
        cap = float(cap)
        if not hours:
            return None, False
        return {"hours": hours, "max_grid_kwh": cap}, True

    return None, False


def validate_directives(raw_directives: Any, operator_notes: List[str], capacity_kwh: float) -> List[dict]:
    """Return a sanitized directive list with exactly one entry per note, in note_index order."""
    n_notes = len(operator_notes)
    placeholder: List[dict | None] = [None] * n_notes

    # accept list or non-list (treat as total failure)
    if not isinstance(raw_directives, list):
        raw_directives = []

    # First pass: assign by note_index if valid; otherwise discard
    used_indices: set[int] = set()
    for entry in raw_directives:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("note_index")
        if not isinstance(idx, int) or idx < 0 or idx >= n_notes or idx in used_indices:
            continue
        used_indices.add(idx)
        dtype = entry.get("directive_type")
        if not isinstance(dtype, str) or dtype not in ALLOWED_DIRECTIVE_TYPES:
            dtype = "no_op"
        adj_raw = entry.get("structured_adjustment")
        explanation = entry.get("explanation")
        if not isinstance(explanation, str):
            explanation = ""
        adj, valid = _clean_adjustment(dtype, adj_raw, capacity_kwh)
        if dtype == "no_op":
            placeholder[idx] = {
                "note_index": idx,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": explanation or "No actionable directive.",
            }
        elif valid:
            placeholder[idx] = {
                "note_index": idx,
                "applies": True,
                "directive_type": dtype,
                "structured_adjustment": adj,
                "explanation": explanation or f"Applied {dtype} directive.",
            }
        else:
            placeholder[idx] = {
                "note_index": idx,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": f"Downgraded {dtype} due to invalid structured_adjustment.",
            }

    # Fill any missing indices with no_op
    for i in range(n_notes):
        if placeholder[i] is None:
            placeholder[i] = {
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "No interpretation returned; defaulted to no_op.",
            }

    return placeholder  # type: ignore[return-value]


def no_op_fallback_all(operator_notes: List[str], reason: str) -> List[dict]:
    """Used when the LLM completely failed (timeout/garbage/refusal)."""
    return [
        {
            "note_index": i,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": f"Interpreter unavailable ({reason}); defaulted to no_op.",
        }
        for i in range(len(operator_notes))
    ]
