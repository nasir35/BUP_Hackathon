# GridWise — Energy Schedule Optimizer

BUP CSE FEST 2026 Preliminary · Team GridWise · 4-stage HTTP API service.

The service accepts a 24-hour energy scenario plus 1–3 free-form operator
notes, interprets the notes into structured `directive_interpretation`s,
deterministically validates them, runs an LP optimizer to produce the cheapest
valid battery / grid / solar schedule, and replays the schedule against the
problem's invariants before returning it. Every stage boundary is
auditable — no silent fallbacks and no leaked stack traces.

---

## 1. Problem Statement (Summary)

* **Goal:** minimise 24-hour grid energy cost while meeting hourly demand and
  obeying all operator directives and the physical battery / solar model.
* **Scoring (per build plan §6):** interpretation correctness (10 pts) →
  directive application (10 pts) → schedule validity (30 pts) →
  cost optimisation (10 pts).
* Hard **30 s** synchronous deadline; `POST /optimize-energy` returns a JSON
  body in the schema defined in §2 of the problem statement.

The **canonical source of truth** is the *Problem Statement PDF*. This README
documents the HTTP service and how to run it; it does not restate the model
physics — see the in-code docstrings on every stage.

---

## 2. Repository Layout

```
gridwise/
├── app.py               # FastAPI orchestrator + global error handlers
├── schemas.py           # Pydantic v2 models (request/response contracts)
├── llm.py               # Stage 1 — hybrid rule + LLM interpreter
├── guardrails.py        # Stage 2 — deterministic directive validator
├── optimizer.py         # Stage 3 — PuLP LP model
├── validator.py         # Stage 4 — replay self-validator (0.01 tolerance)
├── test_harness.py      # End-to-end runner over the 10 public samples
├── requirements.txt     # Pinned deps (FastAPI, PuLP, Pydantic, …)
├── Dockerfile           # python:3.11-slim image, uvicorn entrypoint
└── README.md
```

---

## 3. Architecture — 4 Stages With Explicit Boundaries

```
 ┌──────────┐    ┌─────────────┐    ┌────────────┐    ┌────────────┐
 │ Stage 1  │ →  │   Stage 2   │ →  │   Stage 3  │ →  │   Stage 4  │
 │ LLM +    │    │ Guardrails  │    │  PuLP LP   │    │ Replay     │
 │ Rule     │    │ (no_op      │    │ optimizer  │    │ validator  │
 │ engine   │    │ fallback)   │    │ (CBC)      │    │ (0.01 tol) │
 └──────────┘    └─────────────┘    └────────────┘    └────────────┘
  raw notes     sanitized only       plan entries      ✓ / exception
```

* **Stage 1 (`llm.py`).** Hybrid rule engine first (time-window heuristics for
  `_detect_solar_reduction`, `_detect_reserve`, etc.). When rule confidence is
  below threshold **or** the rule engine emits no directive, an OpenAI-compatible
  HTTP fallback (`OPENAI_BASE_URL`) is consulted at temperature 0.0 with
  defensive markdown fence stripping.
* **Stage 2 (`guardrails.py`).** Outputs are checked against
  `ALLOWED_DIRECTIVE_TYPES`, hours are de-duplicated/sorted in `[0, 23]`,
  `factor ∈ (0,1]`, `minimum_energy_kwh` ≤ capacity, etc. Anything that fails
  validation is replaced with `no_op` for that note. A completely-empty
  response is replaced with an all-`no_op` fallback.
* **Stage 3 (`optimizer.py`).** Continuous LP with **5 vars per hour** over 24
  hours: `grid[h]`, `solar_used[h]`, `charge[h]`, `discharge[h]`,
  `battery_energy[h]`. Constraints: per-hour balance, solar cap (post-directive
  reduction), per-hour reserve floor, no-charge/no-discharge locks, grid cap,
  charge/discharge rate limits, battery dynamics, **end-of-day neutrality**
  (`battery_energy[23] == initial_energy_kwh`). Solved with bundled
  `PULP_CBC_CMD(msg=False)`.
* **Stage 4 (`validator.py`).** Replays every Section 1.5 invariant on the
  returned plan, plus directive locks, grid caps, end-of-day neutrality, and
  the totals the orchestrator reported back. Tolerance: `DEFAULT_TOLERANCE =
  0.01`. Any violation raises `ValidationError`; the orchestrator catches it
  and re-runs the LP with all directives converted to `no_op` (the
  `InfeasibleSchedule` fallback).

---

## 4. HTTP Contract

```http
GET /health             → 200 {"status": "ok"}
POST /optimize-energy   → 200 OptimizeResponse (see schemas.py)
                          400 malformed directive after sanitisation
                          422 Pydantic validation failure
                          500 only for truly unexpected errors
```

Request payload: scenario_id, hours (24 entries), battery config, and 1–3
`operator_notes`. Response payload: scenario_id echo,
`directive_interpretation[]` (one entry per submitted note, indexed
by `note_index`), `hourly_plan[24]`, `total_grid_kwh`, `total_cost_bdt`,
`peak_grid_kwh`, `plan_summary`. Time windows in notes are
**start-inclusive, end-exclusive** (`"6 PM to 10 PM"` → hours `[18, 19, 20, 21]`).

---

## 5. Installation

Tested on **Python 3.11** (Docker image) and **Python 3.12** (local Windows).
PuLP ships the CBC solver binary so no system-level solver install is needed.

```powershell
git clone <this-repo>
cd gridwise
python -m pip install -r requirements.txt
```

Required env vars (all optional except one):

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | _none_ | If unset, Stage 1 falls back to the rule engine only. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model id sent to the chat completions endpoint. |
| `HOST` | `0.0.0.0` | uvicorn bind address. |
| `PORT` | `8000` | uvicorn port. |
| `GRIDWISE_LOG_LEVEL` | `INFO` | Standard uvicorn log level. |

---

## 6. Running the Service

```powershell
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

or via the bundled CLI entrypoint:

```powershell
python app.py
```

Smoke test:

```powershell
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

---

## 7. End-to-End Test Harness

With the service running on port 8000:

```powershell
python test_harness.py
python test_harness.py --url http://localhost:8000
python test_harness.py --strict    # also compare totals against expected_output
```

The harness (a) calls `/health`, (b) POSTs each of the 10 public samples in
`BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`, (c) re-imports
`validator.validate_plan` and replays the returned `hourly_plan` against the
same request the service was given, and (d) compares interpretation
semantics (directive_type, applies, hours set, factor / min / grid cap
numerics). Final exit code is non-zero if any case fails — useful as a CI gate.

---

## 8. Docker

```bash
docker build -t gridwise .
docker run --rm -p 8000:8000 -e OPENAI_API_KEY=... gridwise
```

Image is `python:3.11-slim`, ~150 MB, no system solver required.

---

## 9. Failure Modes & Design Notes

* **LLM returns malformed JSON.** Strict Pydantic validation in Stage 2 falls
  back to `no_op` for that note (or for *all* notes if the LLM returns
  nothing parseable). The service still returns a valid, constraint-satisfying
  schedule.
* **`optimizer → InfeasibleSchedule`.** Stage 4 catches it and re-runs the LP
  with all `directive_interpretation` entries forced to `no_op`. The response
  includes this fallback so a downstream consumer can log it.
* **Pydantic validation failure (422).** Handled with a sanitised error body:
  no stack trace, no internal file path.
* **Totals mismatch (validator says re-derived ≠ reported).** Same
  InfeasibleSchedule fallback path: re-optimise with `no_op`.
* **Validation >= plans return identical totals.** Re-derived totals are
  injected into the response for consistency, but the optimisation target is
  the same — no cosmetic rounding tricks.

---

## 10. License & Credits

MIT (or whatever your contest submission template asks for). Team GridWise —
BUP CSE FEST 2026 Preliminary.
