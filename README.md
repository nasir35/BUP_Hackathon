# GridWise — Autonomous 24-Hour Energy Dispatch & Directive Optimization

[![Live on Vercel](https://img.shields.io/badge/Deployment-Live%20on%20Vercel-success?style=for-the-badge&logo=vercel)](https://gridwise-pi.vercel.app/docs)
[![FastAPI](https://img.shields.io/badge/API-FastAPI%200.115-009688?style=for-the-badge&logo=fastapi)](https://gridwise-pi.vercel.app/docs)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python)](https://python.org)
[![Solver CBC](https://img.shields.io/badge/LP%20Solver-COIN--OR%20CBC-blue?style=for-the-badge)](https://github.com/coin-or/Cbc)
[![Gemini 3.6 Flash](https://img.shields.io/badge/LLM-Gemini%203.6%20Flash-orange?style=for-the-badge&logo=google)](https://ai.google.dev/)
[![Pass Rate](https://img.shields.io/badge/Benchmark-10%2F10%20Passed%20(100%25)-brightgreen?style=for-the-badge)](https://github.com/nasir35/BUP_Hackathon)

> **BUP CSE FEST 2026 Hackathon · Preliminary Submission**  
> **Team:** GridWise (Nasir, Shazid, Tawhid) · **Repository:** [nasir35/BUP_Hackathon](https://github.com/nasir35/BUP_Hackathon)  
> **Production URL:** [https://gridwise-pi.vercel.app](https://gridwise-pi.vercel.app)  
> **Interactive Swagger UI:** [https://gridwise-pi.vercel.app/docs](https://gridwise-pi.vercel.app/docs)  
> **Health Check:** [https://gridwise-pi.vercel.app/health](https://gridwise-pi.vercel.app/health)  

---

## Executive Summary

**GridWise** is an intelligent, automated energy dispatch service designed for smart microgrids, university campuses, and battery energy storage systems (BESS). It takes a 24-hour demand forecast, solar generation profile, time-of-use tariff structure, battery physical specifications, and **informal, natural language operator notes** (e.g. maintenance alerts, panel cleaning notices, peak grid caps, or emergency backup reserves).

GridWise transforms ambiguous human instructions into mathematically rigorous linear constraints and executes a high-speed **COIN-OR CBC Simplex LP optimizer**, generating an hour-by-hour operational schedule that minimizes total electricity cost in Bangladeshi Taka (BDT), smooths peak transformer demand, and guarantees 100% physical and policy invariants.

---

## Architectural Pipeline

GridWise is built strictly in accordance with the official BUP CSE FEST 2026 4-stage gated evaluation specification:

```
                          ┌───────────────────────────┐
                          │   Incoming HTTP Request   │
                          │  (24h Demand, Solar, TOU, │
                          │   Battery, Operator Notes)│
                          └─────────────┬─────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: Natural Language Directive Interpreter (llm.py)                      │
│ • Deterministic regex & pattern parser runs first (<5ms, zero API cost)        │
│ • Fallback to Gemini 3.6 Flash / OpenAI-compatible endpoint for complex notes │
│ • Normalizes clock formats ("noon", "12midnight", "1 PM to 3 PM" -> [13, 14]) │
│ • Maps phrasings to the 6 official directive types & extracts parameters      │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ STAGE 2: Deterministic Guardrails & Sanitizer (guardrails.py)                 │
│ • Schema validation and enum enforcement                                      │
│ • Distractor & chit-chat filtration (classified as no_op with applies=False)  │
│ • Range bounds verification (solar factors in [0, 1], reserves <= capacity)   │
│ • Infeasibility protection: safely relaxes conflicting operator constraints   │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ STAGE 3: Mathematical LP Optimization Engine (optimizer.py)                   │
│ • Formulates continuous Linear Program via PuLP with COIN-OR CBC solver       │
│ • 72 continuous decision variables (Grid, Solar Used, Charge, Discharge)      │
│ • Objective: min Σ (Tariff_t × Grid_t) + 1e-4 × Peak_Grid                     │
│ • Battery physics: dynamic SoC tracking & exact end-of-day neutrality         │
│ • Serverless-ready: automatically deploys standalone CBC binary in /tmp       │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ STAGE 4: Self-Validator & Invariant Replay (validator.py)                     │
│ • Replays returned schedule against raw inputs before sending response       │
│ • Asserts: Energy balance, battery rate limits, SoC bounds, directive fidelity│
│ • Recomputes and verifies total cost, peak load, and total grid import        │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │  200 OK OptimizeResponse  │
                          │ • Interpreted Directives  │
                          │ • 24-Hour Plan Entries    │
                          │ • Total Cost & Peak Grid  │
                          └───────────────────────────┘
```

---

## Key Features & Design Innovations

### 1. Robust Natural Language Interpretation (Stage 1)
- **High-Performance Hybrid Parser:** Covers all 6 official directive types (`solar_reduction`, `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`, `no_op`).
- **Time Window Precision:** Automatically handles 12-hour, 24-hour, colloquial (`noon` &rarr; 12, `midnight` &rarr; 0), and range formats (`"from X to Y"`, `"between X and Y"`, `"X-Y"`). Conforms strictly to start-inclusive, end-exclusive semantics.
- **Factor Derivation:** Correctly distinguishes between reduction percentages (*"80% reduction"* &rarr; `0.2` remaining factor) and remaining usable fractions (*"25% of forecast"* &rarr; `0.25` factor).
- **Zero-Hallucination Fallback:** If notes contain complex paraphrasing, requests route seamlessly to Google's state-of-the-art **Gemini 3.6 Flash** model, operating at `temperature=0.0` with structured JSON schema output.

### 2. Physical & Invariant Guarantees (Stages 2 & 3)
- **Energy Conservation Law:** At every hour $t \in [0, 23]$:
  $$\text{Grid}_t + \text{SolarUsed}_t + \text{Discharge}_t = \text{Demand}_t + \text{Charge}_t$$
- **Battery Dynamics & Efficiencies:**
  $$E_t = E_{t-1} + (\text{Charge}_t \cdot \eta_{\text{charge}}) - \left(\frac{\text{Discharge}_t}{\eta_{\text{discharge}}}\right)$$
- **Strict End-of-Day Neutrality:** Enforces $E_{23} = E_{\text{initial}}$ via hard linear constraints, preventing battery depletion and ensuring cyclic sustainability.
- **Simultaneous Charge/Discharge Prevention:** Net cancellation guarantees the battery never charges and discharges concurrently, producing clean `"charge"`, `"discharge"`, or `"idle"` states.
- **Secondary Peak Tie-Breaker:** Incorporates $\epsilon \cdot \text{PeakGrid}$ ($\epsilon = 10^{-4}$) to smooth the campus load profile without compromising optimal cost.

### 3. Serverless Cloud Optimization (Vercel Ready)
- Native serverless execution via `@vercel/python` on AWS Lambda.
- Dynamically locates and bundles the 64-bit Linux COIN-OR CBC binary, extracts it into `/tmp/cbc`, applies `chmod 0o755`, and executes natively with sub-second response times.

---

## Official Hackathon Benchmark Validation

GridWise was rigorously validated against all 10 official public scenarios published in `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`.

| Case ID | Scenario Description | Directives Interpreted | Official Expected Cost | GridWise Actual Cost | Match? |
|:---|:---|:---|:---:|:---:|:---:|
| **SAMPLE-01** | Solar cleaning + distractor | `solar_reduction`, `no_op` | 38,365.00 BDT | **38,365.00 BDT** | **100% Match** |
| **SAMPLE-02** | Battery charging outage | `no_charge_window` | 42,885.00 BDT | **42,885.00 BDT** | **100% Match** |
| **SAMPLE-03** | Reserve as % of capacity | `minimum_battery_reserve` | 35,480.00 BDT | **35,480.00 BDT** | **100% Match** |
| **SAMPLE-04** | Relay protection test | `no_discharge_window` | 40,495.00 BDT | **40,495.00 BDT** | **100% Match** |
| **SAMPLE-05** | Feeder import limit | `max_grid_window` | 33,950.00 BDT | **33,950.00 BDT** | **100% Match** |
| **SAMPLE-06** | Inspection + outage + distractor | `solar_reduction`, `no_charge_window`, `no_op` | 34,090.00 BDT | **34,090.00 BDT** | **100% Match** |
| **SAMPLE-07** | Evening reserve + transformer cap | `minimum_battery_reserve`, `max_grid_window` | 38,550.00 BDT | **38,550.00 BDT** | **100% Match** |
| **SAMPLE-08** | Separate charge/discharge windows | `no_charge_window`, `no_discharge_window` | 37,665.00 BDT | **37,665.00 BDT** | **100% Match** |
| **SAMPLE-09** | 80% reduction + distractor | `solar_reduction`, `no_op` | 34,873.00 BDT | **34,873.00 BDT** | **100% Match** |
| **SAMPLE-10** | Data center reserve + substation cap | `minimum_battery_reserve`, `max_grid_window`, `no_op` | 41,620.00 BDT | **41,620.00 BDT** | **100% Match** |

**Benchmark Summary:**
- **Directive Accuracy:** 10 / 10 (100%)
- **Physical Invariants:** 0 violations across all 240 hours
- **Average Solve Latency:** ~350–500 ms

> **Integrity Note:** Zero scenarios, notes, or costs are hardcoded. The system solves dynamically from scratch on every request.

---

## API Specification

### Endpoint: `POST /optimize-energy`

#### Request Payload Structure
```json
{
  "scenario_id": "CAMPUS-PEAK-DAY-01",
  "battery": {
    "capacity_kwh": 100.0,
    "initial_energy_kwh": 50.0,
    "minimum_energy_kwh": 10.0,
    "max_charge_kwh_per_hour": 25.0,
    "max_discharge_kwh_per_hour": 25.0
  },
  "hours": [
    {"hour": 0, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
    {"hour": 1, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
    "...",
    {"hour": 23, "demand_kwh": 105, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}
  ],
  "operator_notes": [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "The sports office moved next month's registration deadline."
  ]
}
```

#### Response Structure (`200 OK`)
```json
{
  "scenario_id": "CAMPUS-PEAK-DAY-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "hours": [12, 13],
        "factor": 0.25
      },
      "explanation": "Applied solar_reduction."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "Note does not affect today's 24-hour energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 70.0,
      "solar_used_kwh": 0.0,
      "battery_action": "discharge",
      "battery_kwh": 20.0,
      "battery_energy_after_kwh": 90.0
    },
    "..."
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Applied 1x solar_reduction; total cost 38365 BDT, peak grid 175 kWh, total grid 2692 kWh."
}
```

---

## Local Development & Setup

### Prerequisites
- Python 3.10+ (tested on Python 3.12)
- Git

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/nasir35/BUP_Hackathon.git
cd BUP_Hackathon
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Environment Variables (Optional)
To enable LLM fallback parsing via Google Gemini or OpenAI:
```bash
# Create a .env file (automatically ignored by git)
GEMINI_API_KEY=your_gemini_api_key_here
OPENAI_MODEL=gemini-3.6-flash
```

### 3. Run the Development Server
```bash
cd gridwise
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
- Open documentation: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`

### 4. Run the Automated Test Suite
```bash
# Run against local server
python test_harness.py --strict

# Run directly against live Vercel deployment
python test_harness.py --url https://gridwise-pi.vercel.app --strict
```

### 5. Docker Deployment
```bash
docker build -t gridwise .
docker run -p 8000:8000 gridwise
```

---

## Repository Structure

```
.
├── api/
│   └── index.py             # Vercel serverless entrypoint
├── gridwise/
│   ├── app.py               # FastAPI application & lifecycle routing
│   ├── schemas.py           # Pydantic v2 data models with strict validation
│   ├── llm.py               # Stage 1: Rule-based & Gemini LLM directive interpreter
│   ├── guardrails.py        # Stage 2: Deterministic sanitizer & validator
│   ├── optimizer.py         # Stage 3: PuLP LP optimizer with CBC solver
│   ├── validator.py         # Stage 4: Invariant replay & balance validator
│   ├── test_harness.py      # Automated benchmark verification harness
│   └── requirements.txt     # Python package definitions
├── vercel.json              # Vercel serverless build & routing configuration
├── render.yaml              # Render.com deployment manifest
├── Dockerfile               # Production container specification
├── requirements.txt         # Root requirements file
└── README.md                # Project documentation
```

---

## Team & Acknowledgements

Developed with ❤️ for the **BUP CSE FEST 2026 Hackathon**.

### Team Members
| # | Name | GitHub Profile |
|:---:|:---|:---|
| **1** | **Md. Nasir Ahmed** | [@nasir355](https://github.com/nasir355) |
| **2** | **Md. Shazid Al Hasan** | [@MdShazidAlHasan](https://github.com/MdShazidAlHasan) |
| **3** | **Tawhid Ahmmed** | [@abokash590](https://github.com/abokash590) |

Special thanks to the organizing committee and mentors of BUP CSE FEST 2026 for crafting this rigorous, real-world microgrid optimization challenge.
