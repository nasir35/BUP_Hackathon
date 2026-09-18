# GridWise — Energy Schedule Optimizer

> **BUP CSE FEST 2026 Preliminary Hackathon Solution**  
> Team GridWise · 4-Stage Energy Schedule Optimization Service

GridWise is an automated energy schedule optimization HTTP API service. It accepts a 24-hour energy scenario alongside 1–3 natural language operator notes, interprets the notes into structured directives, deterministically validates them, runs a linear programming (LP) solver to produce the cost-minimized battery/grid/solar schedule, and replays all physical and directive invariants before returning the final response.

---

## Architecture Overview

```
 ┌──────────────┐      ┌─────────────┐      ┌─────────────┐      ┌─────────────┐
 │   Stage 1    │  ─►  │   Stage 2   │  ─►  │   Stage 3   │  ─►  │   Stage 4   │
 │ LLM / Rules  │      │ Guardrails  │      │   PuLP LP   │      │   Replay    │
 │ Interpreter  │      │ (Sanitizer) │      │  Optimizer  │      │  Validator  │
 └──────────────┘      └─────────────┘      └─────────────┘      └─────────────┘
    Raw Notes           Sanitized Only        Plan Entries         Validated JSON
```

- **Stage 1 (`llm.py`)**: Hybrid rule engine with deterministic regex/heuristics for time windows, fallback to OpenAI-compatible LLM endpoint when necessary.
- **Stage 2 (`guardrails.py`)**: Strict deterministic guardrails, validation against allowed directive types, range bounds, and automatic fallback to `no_op`.
- **Stage 3 (`optimizer.py`)**: Continuous Linear Programming (LP) model using PuLP and CBC solver, meeting demand, rate limits, solar utilization, and end-of-day neutrality.
- **Stage 4 (`validator.py`)**: Replay invariant verification (energy conservation, battery dynamics, directive compliance) with 0.01 tolerance.

---

## Project Structure

```
.
├── .gitignore
├── README.md
└── gridwise/
    ├── app.py               # FastAPI orchestrator + HTTP endpoints
    ├── schemas.py           # Pydantic v2 data models
    ├── llm.py               # Stage 1 — hybrid rule + LLM interpreter
    ├── guardrails.py        # Stage 2 — deterministic directive validator
    ├── optimizer.py         # Stage 3 — PuLP LP continuous solver
    ├── validator.py         # Stage 4 — invariant replay self-validator
    ├── test_harness.py      # End-to-end runner over sample cases
    ├── requirements.txt     # Python dependencies
    ├── Dockerfile           # Production container definition
    └── README.md            # Detailed service documentation
```

---

## Quick Start

### 1. Installation

```bash
cd gridwise
python -m pip install -r requirements.txt
```

### 2. Running the Service

```bash
cd gridwise
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Check health:
```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

### 3. Running with Docker

```bash
cd gridwise
docker build -t gridwise .
docker run --rm -p 8000:8000 gridwise
```

### 4. Running the Test Harness

```bash
cd gridwise
python test_harness.py
```

---

## API Endpoints

- `GET /health` — Health check endpoint (`200 {"status": "ok"}`).
- `POST /optimize-energy` — Primary optimization endpoint. Accepts scenario configuration, hourly demands, battery limits, and operator notes; returns the optimal 24-hour dispatch schedule.
