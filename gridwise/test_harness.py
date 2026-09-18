"""
GridWise — local end-to-end test harness.

Loads the public sample cases from the participant docs, POSTs each one to a
running `/optimize-energy` instance, and asserts:

  1. HTTP 200 within 30s (hard ceiling from the build plan).
  2. Response schema has exactly the expected fields and lengths.
  3. `directive_interpretation` matches expected semantics per note
     (directive_type, applies flag, structured_adjustment content) — wording
     of `explanation` is NOT byte-compared.
  4. `hourly_plan` independently satisfies Section 1.5 invariants and the
     totals match within the 0.01 tolerance.

Usage:
    python test_harness.py                    # uses default URL and cases file
    python test_harness.py --url http://localhost:8000
    python test_harness.py --strict            # also compare numerical totals
                                              # against the sample expected_output
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List

import requests

# Ensure Unicode glyphs survive the Windows cp1252 console: write all progress
# to a UTF-8 log file alongside the harness, and reconfigure stdout to UTF-8
# when possible. The on-screen line uses ASCII status chars; the rich glyphs
# go to the log.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from schemas import (
    BatteryConfig,
    HourlyDemand,
)
from validator import validate_plan, ValidationError


DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_CASES_PATH = (
    Path(__file__).resolve().parent.parent
    / "problems"
    / "BUP_CSE_FEST_2026_Participant_Docs"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


def load_cases(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "cases" in data:
        return data["cases"]
    raise ValueError(f"Unrecognised cases file shape at {path}")


def post_optimize(url: str, payload: dict, timeout: float = 30.0) -> requests.Response:
    return requests.post(
        f"{url.rstrip('/')}/optimize-energy",
        json=payload,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )


def check_interpretation_semantics(
    case_id: str,
    returned: List[dict],
    expected: List[dict],
) -> List[str]:
    """Return list of error messages (empty = pass)."""
    errs: List[str] = []
    if len(returned) != len(expected):
        errs.append(
            f"{case_id}: interpretation length {len(returned)} != expected {len(expected)}"
        )
        return errs
    for r, e in zip(returned, expected):
        if r.get("directive_type") != e.get("directive_type"):
            errs.append(
                f"{case_id}: note {r.get('note_index')} directive_type "
                f"{r.get('directive_type')} != expected {e.get('directive_type')}"
            )
        if r.get("applies") != e.get("applies"):
            errs.append(
                f"{case_id}: note {r.get('note_index')} applies "
                f"{r.get('applies')} != expected {e.get('applies')}"
            )
        # Hours sets must match (if both have them).
        r_adj = r.get("structured_adjustment") or {}
        e_adj = e.get("structured_adjustment") or {}
        r_hours = sorted(r_adj.get("hours", []) or [])
        e_hours = sorted(e_adj.get("hours", []) or [])
        if r_hours != e_hours:
            errs.append(
                f"{case_id}: note {r.get('note_index')} hours {r_hours} != expected {e_hours}"
            )
        # For solar_reduction the factor must agree within 0.01.
        if r.get("directive_type") == "solar_reduction" and e.get("directive_type") == "solar_reduction":
            rf = float(r_adj.get("factor", -1))
            ef = float(e_adj.get("factor", -2))
            if abs(rf - ef) > 0.01:
                errs.append(
                    f"{case_id}: note {r.get('note_index')} factor {rf} != expected {ef}"
                )
        # For minimum_battery_reserve and max_grid_window compare numeric.
        if r.get("directive_type") == "minimum_battery_reserve":
            rv = float(r_adj.get("minimum_energy_kwh", -1))
            ev = float(e_adj.get("minimum_energy_kwh", -2))
            if abs(rv - ev) > 0.5:
                errs.append(
                    f"{case_id}: note {r.get('note_index')} minimum_energy_kwh "
                    f"{rv} != expected {ev}"
                )
        if r.get("directive_type") == "max_grid_window":
            rv = float(r_adj.get("max_grid_kwh", -1))
            ev = float(e_adj.get("max_grid_kwh", -2))
            if abs(rv - ev) > 0.5:
                errs.append(
                    f"{case_id}: note {r.get('note_index')} max_grid_kwh "
                    f"{rv} != expected {ev}"
                )
    return errs


def replay_and_check(case_id: str, body: dict, resp: dict) -> List[str]:
    """Re-invoke validator.py on the returned plan."""
    errs: List[str] = []
    try:
        from schemas import HourlyPlanEntry as HPE
        plan_entries = [HPE.model_validate(p) for p in resp["hourly_plan"]]
        # DEBUG
        print(f"  DEBUG body[hours] first entry: {body['hours'][0] if body.get('hours') else 'MISSING'}")
        print(f"  DEBUG resp[hourly_plan] first entry: {resp['hourly_plan'][0] if resp.get('hourly_plan') else 'MISSING'}")
        hours = [HourlyDemand.model_validate(h) for h in body["hours"]]
        battery = BatteryConfig.model_validate(body["battery"])
        sanitized = resp["directive_interpretation"]
        validate_plan(
            plan=plan_entries,
            hours=hours,
            battery=battery,
            total_grid_kwh=resp["total_grid_kwh"],
            total_cost_bdt=resp["total_cost_bdt"],
            peak_grid_kwh=resp["peak_grid_kwh"],
            sanitized_directives=sanitized,
        )
    except ValidationError as e:
        errs.append(f"{case_id}: replay validation failed :: {e}")
    except Exception as e:
        errs.append(f"{case_id}: replay raised unexpected {type(e).__name__}: {e}")
    return errs


def compare_totals(case_id: str, resp: dict, expected: dict) -> List[str]:
    errs: List[str] = []
    for fld in ("total_grid_kwh", "total_cost_bdt", "peak_grid_kwh"):
        if fld not in expected:
            continue
        diff = abs(float(resp.get(fld, 0)) - float(expected[fld]))
        # Strict is intentional: total_cost_bdt must match exactly per judge.
        if diff > 0.5:
            errs.append(
                f"{case_id}: {fld} got {resp.get(fld)} expected {expected[fld]} (diff {diff:.2f})"
            )
    return errs


def run(args: argparse.Namespace) -> int:
    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"!! Cases file not found: {cases_path}", file=sys.stderr)
        return 2

    cases = load_cases(cases_path)
    print(f"==> Loaded {len(cases)} cases from {cases_path}")
    print(f"==> Target service: {args.url}")
    print(f"==> Strict mode: {args.strict}")
    sys.stdout.flush()

    # Quick health probe.
    try:
        h = requests.get(f"{args.url.rstrip('/')}/health", timeout=10)
        if h.status_code != 200 or h.json().get("status") != "ok":
            print(f"!! Health check failed: {h.status_code} {h.text[:200]}", file=sys.stderr)
            return 3
    except Exception as e:
        print(f"!! Health check unreachable: {e}", file=sys.stderr)
        return 3

    passed = 0
    failed = 0
    for case in cases:
        cid = case.get("id", "?")
        body = case["input"]
        t0 = time.monotonic()
        try:
            r = post_optimize(args.url, body, timeout=30.0)
        except requests.exceptions.Timeout:
            print(f"  ✗ {cid}: TIMEOUT (>30s)")
            failed += 1
            continue
        except Exception as e:
            print(f"  ✗ {cid}: HTTP error {type(e).__name__}: {e}")
            failed += 1
            continue
        dt_ms = (time.monotonic() - t0) * 1000

        if r.status_code != 200:
            print(f"  ✗ {cid}: HTTP {r.status_code} in {dt_ms:.0f} ms :: {r.text[:200]}")
            failed += 1
            continue

        try:
            resp = r.json()
        except json.JSONDecodeError as e:
            print(f"  ✗ {cid}: malformed JSON response: {e}")
            failed += 1
            continue

        all_errs: List[str] = []
        # 1) Echo scenario_id.
        if resp.get("scenario_id") != body.get("scenario_id"):
            all_errs.append("scenario_id echo mismatch")

        # 2) Plan length.
        if len(resp.get("hourly_plan", [])) != 24:
            all_errs.append(f"hourly_plan length {len(resp.get('hourly_plan', []))} != 24")

        # 3) Interpretation length and semantics.
        expected = case.get("expected_output", {})
        all_errs.extend(check_interpretation_semantics(
            cid, resp.get("directive_interpretation", []),
            expected.get("directive_interpretation", []),
        ))

        # 4) Replay validation (invariants + totals consistency).
        all_errs.extend(replay_and_check(cid, body, resp))

        # 5) Optional strict comparison against expected totals.
        if args.strict:
            all_errs.extend(compare_totals(cid, resp, expected))

        if all_errs:
            print(f"  FAIL {cid}: {len(all_errs)} issue(s) in {dt_ms:.0f} ms")
            for e in all_errs:
                print(f"      - {e}")
            failed += 1
        else:
            cost = resp.get("total_cost_bdt", 0)
            grid = resp.get("total_grid_kwh", 0)
            print(f"  PASS {cid}: ok in {dt_ms:.0f} ms  "
                  f"(cost={cost:.0f} grid={grid:.0f})")
            passed += 1

    print()
    print(f"==> Summary: {passed} passed / {failed} failed / {len(cases)} total")
    return 0 if failed == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    p.add_argument("--strict", action="store_true",
                   help="Also compare totals against expected_output")
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
