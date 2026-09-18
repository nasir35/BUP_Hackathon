"""Stage 1 — LLM interpreter for operator notes.

Design choice: a deterministic rule-based interpreter runs first.
It covers all 5 directive types and many phrasings (clock formats,
percentages, kWh, relative phrasing). If it is confident, the LLM call
is skipped — this keeps the service fast, free, and reproducible for
judges without API keys. If rules are not confident, the service falls
back to an OpenAI-compatible LLM call (env: OPENAI_API_KEY).

The LLM output is treated as untrusted: it always flows through
Stage 2 guardrails.
"""
from __future__ import annotations
import json
import os
import re
import logging
from typing import Any, List, Optional

import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from guardrails import no_op_fallback_all

log = logging.getLogger("gridwise.llm")

# ---------------------------------------------------------------------------
# Time-window parsing
# ---------------------------------------------------------------------------

_TIME_TOKEN = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|noon|midday|midnight)"

_HOUR_PATTERNS = [
    # 1 PM, 1pm, 1 p.m., 13:00, 13, 1 p.m, noon, midnight
    re.compile(r"\b(\d{1,2})\s*(?::\s*\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2}):\s*\d{2}\b"),
    re.compile(r"\b(\d{2})\b\s*(?=\s*(?:00|hrs|hours|o'clock))", re.IGNORECASE),
    re.compile(r"\b(noon|midday|midnight)\b", re.IGNORECASE),
]

_RANGE_PATTERNS = [
    # "from X to Y", "X to Y", "between X and Y", "X-Y", "X – Y", "X → Y"
    re.compile(
        rf"(?:from|between)\s+({_TIME_TOKEN})\s+(?:to|until|till|through|and|-)\s+({_TIME_TOKEN})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"({_TIME_TOKEN})\s*(?:-|–|—|→|to|until)\s*({_TIME_TOKEN})",
        re.IGNORECASE,
    ),
]

_SINGLE_HOUR_PATTERN = re.compile(rf"\b(?:at|@)\s+({_TIME_TOKEN})\b", re.IGNORECASE)


def _parse_clock_to_24(token: str) -> Optional[int]:
    """Parse a clock-time token like '1pm', '13:00', '9 p.m.', '7am', 'noon', 'midnight' to hour 0..23."""
    if token is None:
        return None
    t = token.strip().lower().replace(".", "")
    if not t:
        return None
    if t in ("noon", "midday", "12noon"):
        return 12
    if t in ("midnight", "12midnight"):
        return 0
    # detect am/pm
    is_pm = "pm" in t
    is_am = "am" in t
    # strip am/pm
    t2 = re.sub(r"[ap]m", "", t).strip()
    # colon handling
    h_str = t2.split(":")[0] if ":" in t2 else t2
    try:
        h = int(h_str)
    except ValueError:
        return None
    if is_pm and h < 12:
        h += 12
    if is_am and h == 12:
        h = 0
    if 0 <= h <= 23:
        return h
    # also handle 24-hour
    if h == 24:
        return 0
    return None


def _expand_range(start_h: int, end_h: int) -> List[int]:
    """Start-inclusive, end-exclusive. If start >= end, wrap around midnight."""
    if start_h == end_h:
        return []  # empty window per Section 1.4 rules
    if end_h > start_h:
        return list(range(start_h, end_h))
    # wrap
    return list(range(start_h, 24)) + list(range(0, end_h))


def _extract_window_hours(note: str) -> List[int]:
    """Heuristic window extractor. Returns deduped, sorted ascending hour list."""
    hours: set[int] = set()
    # try range patterns
    for pat in _RANGE_PATTERNS:
        for m in pat.finditer(note):
            s = _parse_clock_to_24(m.group(1))
            e = _parse_clock_to_24(m.group(2))
            if s is None or e is None:
                continue
            for h in _expand_range(s, e):
                hours.add(h)
    # try single hour ("at 6 PM")
    for m in _SINGLE_HOUR_PATTERN.finditer(note):
        h = _parse_clock_to_24(m.group(1))
        if h is not None:
            hours.add(h)
    # also "for the next N hours" relative windows
    m = re.search(r"for (?:the )?next (\d{1,2})\s*(?:hours|hrs|h)\b", note, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        for h in range(n):
            hours.add(h)
    return sorted(hours)


# ---------------------------------------------------------------------------
# Directive-specific keyword matchers
# ---------------------------------------------------------------------------


def _detect_solar_reduction(note: str, cap: float) -> Optional[dict]:
    """Detect solar reduction. Returns directive dict or None."""
    if not re.search(r"solar|panel|photovoltaic|pv|rooftop", note, re.IGNORECASE):
        return None
    if not re.search(r"reduc|degrad|limit|cut|drop|less|decrease|low|cloud|shad|wash|clean|inspect|down|half|quarter|frac|leave|remain|output", note, re.IGNORECASE):
        return None
    hours = _extract_window_hours(note)
    if not hours:
        return None

    # detect fraction (fraction remaining)
    factor: Optional[float] = None

    if re.search(r"\bno\s+(?:usable\s+)?solar\b|\bzero\s+solar\b", note, re.IGNORECASE):
        factor = 0.0
    else:
        # Check if note describes fraction/percentage REMAINING:
        # e.g., "treated as roughly 25% of the forecast", "leave about half of the forecast", "drops to 30%"
        is_remaining = bool(re.search(
            r"(?:treated as|roughly|about|leaves?|remain(?:ing|s)?|left at|reduced to|cut to|drops? to|down to)\s+(?:roughly\s+|about\s+)?(?:\d{1,3}\s*%|half|quarter)|(?:\d{1,3}\s*%|half|quarter)\s+of\s+(?:the\s+)?forecast",
            note,
            re.IGNORECASE,
        ))

        # Check explicit percentages: e.g. "25%", "80 percent"
        m_pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)", note, re.IGNORECASE)
        if m_pct:
            val = float(m_pct.group(1))
            if is_remaining or re.search(r"\b(?:to|at)\s+\d{1,3}\s*%", note, re.IGNORECASE):
                factor = val / 100.0
            else:
                # e.g. "80% reduction", "cut by 30%"
                factor = 1.0 - (val / 100.0)
        elif re.search(r"\bhalf\b", note, re.IGNORECASE):
            factor = 0.5
        elif re.search(r"\bquarter\b|\bone[- ]fourth\b", note, re.IGNORECASE):
            factor = 0.25 if is_remaining else 0.75
        elif re.search(r"\bthree[- ]quarters?\b", note, re.IGNORECASE):
            factor = 0.75 if is_remaining else 0.25

    if factor is None:
        # Generic reduction with no clear fraction: assume 50%
        factor = 0.5

    factor = max(0.0, min(1.0, factor))
    return {
        "directive_type": "solar_reduction",
        "applies": True,
        "structured_adjustment": {"hours": hours, "factor": round(factor, 6)},
    }


def _detect_reserve(note: str, cap: float) -> Optional[dict]:
    """Detect minimum_battery_reserve (absolute kWh OR percentage)."""
    if not re.search(r"battery|reserve|storage|emergency|standby", note, re.IGNORECASE):
        return None
    if not re.search(r"keep|maintain|reserve|hold|stor|remain|at least|minimum|≥|>=|not (?:to )?fall|drop below", note, re.IGNORECASE):
        return None
    hours = _extract_window_hours(note)
    if not hours:
        return None

    val: Optional[float] = None
    # 1. Percentage check: "50% of the battery capacity", "at least 50% capacity", "keep 40%"
    m_pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)", note, re.IGNORECASE)
    if m_pct:
        pct = float(m_pct.group(1)) / 100.0
        val = pct * cap
    elif re.search(r"\bhalf\s+(?:of\s+)?(?:the\s+)?(?:battery|capacity|charge)", note, re.IGNORECASE):
        val = 0.5 * cap
    else:
        # 2. Absolute kWh
        m = re.search(r"(?:at least|minimum|min(?:imum)?|≥|>=|not less than|hold|reserve|maintain|remain in the battery)\s*(\d+(?:\.\d+)?)\s*(?:kwh|kWh|KWH)?\b", note, re.IGNORECASE)
        if m:
            val = float(m.group(1))
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*kWh\b", note, re.IGNORECASE)
            if m:
                val = float(m.group(1))

    if val is None:
        return None
    val = max(0.0, min(cap, val))
    return {
        "directive_type": "minimum_battery_reserve",
        "applies": True,
        "structured_adjustment": {"hours": hours, "minimum_energy_kwh": round(val, 3)},
    }


def _detect_no_charge(note: str) -> Optional[dict]:
    """Detect a no-charge window."""
    if not re.search(r"charg(?:ing|e|er)", note, re.IGNORECASE):
        return None
    if not re.search(r"isolat|disabl|unavailab|offline|off|not\s+(?:be\s+)?(?:charg|able)|no\s+charg|cannot\s+charg|won't\s+charg|maintenance|repair|down|outage|inspect", note, re.IGNORECASE):
        return None
    hours = _extract_window_hours(note)
    if not hours:
        return None
    return {
        "directive_type": "no_charge_window",
        "applies": True,
        "structured_adjustment": {"hours": hours},
    }


def _detect_no_discharge(note: str) -> Optional[dict]:
    if not re.search(r"discharg|protection\s+test|relay\s+test", note, re.IGNORECASE):
        return None
    if not re.search(r"isolat|disabl|unavailab|offline|off|not\s+(?:be\s+)?(?:discharg|able)|no\s+discharg|cannot\s+discharg|won't\s+discharg|maintenance|repair|down|outage|inspect|test|protect", note, re.IGNORECASE):
        return None
    hours = _extract_window_hours(note)
    if not hours:
        return None
    return {
        "directive_type": "no_discharge_window",
        "applies": True,
        "structured_adjustment": {"hours": hours},
    }


def _detect_max_grid(note: str) -> Optional[dict]:
    if not re.search(r"grid|import|feeder|transformer|infeed|intake|substation|kWh\s*(?:of\s*)?grid", note, re.IGNORECASE):
        return None
    if not re.search(r"cap(?:ped)?|limit(?:ed)?|maximum|max|cannot\s+exceed|not\s+(?:(?:to|import)\s+)?exceed|≤|<=|at most|no\s+more\s+than|not\s+(?:import\s+)?more\s+than|at or below|stay below|stay at or below|under|ceiling|constrained", note, re.IGNORECASE):
        return None
    hours = _extract_window_hours(note)
    if not hours:
        return None
    # find cap value
    cap_val: Optional[float] = None
    m = re.search(r"(?:max(?:imum)?|cap(?:ped)?(?:\s+at)?|limit(?:ed)?(?:\s+at)?|of|≤|<=|at most|no\s+more\s+than|not\s+(?:import\s+)?more\s+than|at or below|stay at or below|stay below)\s*(\d+(?:\.\d+)?)\s*(?:kWh|kwh|KWh|kW|kw)?", note, re.IGNORECASE)
    if m:
        cap_val = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:kWh|kW)\b", note, re.IGNORECASE)
        if m:
            cap_val = float(m.group(1))
    if cap_val is None:
        m = re.search(r"(\d{1,3})\s*%\s*(?:of\s+)?(?:the\s+)?(?:demand|load)", note, re.IGNORECASE)
        if m:
            cap_val = float(m.group(1))
    if cap_val is None:
        return None
    return {
        "directive_type": "max_grid_window",
        "applies": True,
        "structured_adjustment": {"hours": hours, "max_grid_kwh": round(cap_val, 3)},
    }


def _looks_relevant(note: str) -> bool:
    """Does this note look at all related to a 5-directive type?"""
    n = note.lower()
    keywords = [
        "solar", "panel", "pv", "rooftop", "photovoltaic",
        "battery", "charge", "discharge", "reserve", "storage",
        "grid", "feeder", "transformer", "infeed", "import",
        "kw", "kwh",
        "emergency", "maintenance", "test", "protect",
    ]
    return any(k in n for k in keywords)


def rule_based_interpret(note: str, cap_kwh: float) -> Tuple[Optional[dict], float]:
    """Run rule-based classifier on a single note.

    Returns (directive_dict_or_None, confidence 0..1).
    Confidence >=0.5 means we trust the rule output and skip the LLM.
    """
    note = note.strip()
    if not note:
        return None, 1.0  # empty handled by schema as 400

    detectors = [
        _detect_solar_reduction,
        _detect_reserve,
        _detect_no_charge,
        _detect_no_discharge,
        _detect_max_grid,
    ]
    matches = []
    for fn in detectors:
        try:
            if fn in (_detect_solar_reduction, _detect_reserve):
                r = fn(note, cap_kwh)
            else:
                r = fn(note)
        except Exception:
            r = None
        if r:
            matches.append(r)

    if not matches:
        if _looks_relevant(note):
            # Looks relevant but could not classify; let LLM have a swing
            return None, 0.0
        # clearly irrelevant
        return {
            "directive_type": "no_op",
            "applies": False,
            "structured_adjustment": None,
            "explanation": "Note does not affect today's 24-hour energy schedule.",
        }, 0.95

    if len(matches) == 1:
        m = matches[0]
        # confidence bump when we found a window and a value/factor
        return m, 0.95
    # multiple matching directives — defer to LLM to disambiguate
    return None, 0.0


# ---------------------------------------------------------------------------
# LLM caller
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are a deterministic directive-extraction module for a campus energy optimizer.
You will receive a numbered list of operator notes (0-indexed) describing conditions
for a 24-hour energy schedule (hours 0-23).

For EACH note, output exactly one JSON object with these fields:
- note_index: integer, matching the note's position
- applies: boolean
- directive_type: one of
  ["solar_reduction","minimum_battery_reserve","no_charge_window",
   "no_discharge_window","max_grid_window","no_op"]
- structured_adjustment: object matching the shape below, or null if no_op
- explanation: one short sentence

Shapes:
- solar_reduction: {"hours":[...], "factor": <fraction of solar REMAINING, e.g. 80% drop = 0.2>}
- minimum_battery_reserve: {"hours":[...], "minimum_energy_kwh": <number>}
- no_charge_window: {"hours":[...]}
- no_discharge_window: {"hours":[...]}
- max_grid_window: {"hours":[...], "max_grid_kwh": <number>}
- no_op: structured_adjustment is null, applies is false

Rules:
- Time ranges are START-INCLUSIVE, END-EXCLUSIVE. "1 PM to 3 PM" -> hours [13,14].
  Convert any clock time to 24-hour integer hours in [0,23].
- hours must be unique integers, ascending, within 0-23.
- If a note does not affect the 24-hour energy schedule (e.g. unrelated campus
  announcements, menus, deadlines, generic remarks), mark it no_op with applies=false.
- If a note is ambiguous or does not clearly match one of the 5 real directive types
  above, default to no_op rather than guessing.
- Never invent a directive type not listed above.
- Never change base demand, tariff, or battery hardware limits — only the 5 directive
  types may adjust the schedule.
- Output ONLY a raw JSON array of these objects, one per note, in note_index order.
  No markdown fences, no prose, no extra keys.
"""


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # remove first fence line
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def call_llm(notes: List[str], battery_capacity: float, timeout: float = 15.0) -> Optional[List[Any]]:
    """Call an OpenAI or Gemini chat completions endpoint. Returns parsed list or None on any failure."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    api_key = gemini_key or openai_key
    if not api_key:
        return None

    # Auto-detect Google Gemini vs OpenAI
    is_gemini = bool(gemini_key) or api_key.startswith("AQ.") or "generativelanguage" in os.environ.get("OPENAI_BASE_URL", "")

    if is_gemini:
        default_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        default_model = "gemini-3.6-flash"
    else:
        default_base_url = "https://api.openai.com/v1"
        default_model = "gpt-4o-mini"

    base_url = os.environ.get("OPENAI_BASE_URL", default_base_url).rstrip("/")
    model = os.environ.get("OPENAI_MODEL", default_model)

    numbered = "\n".join(f"{i}: {n}" for i, n in enumerate(notes))
    user = (
        f"Battery capacity for reference: {battery_capacity} kWh.\n"
        f"Notes:\n{numbered}\n\n"
        f"Return ONLY the JSON array."
    )
    payload = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=timeout)
        if r.status_code != 200:
            log.warning("LLM non-200: %s %s", r.status_code, r.text[:200])
            return None
        body = r.json()
        content = body["choices"][0]["message"]["content"]
        cleaned = _strip_fences(content)
        return json.loads(cleaned)
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None


def interpret_notes(
    notes: List[str],
    battery_capacity: float,
    precomputed: Optional[List[dict]] = None,
) -> Tuple[List[dict], str]:
    """Returns (directive_list, source) where source in {'rules', 'llm', 'fallback'}.

    `precomputed` lets the caller pass rule results in for testing.
    """
    if precomputed is not None:
        return precomputed, "rules"

    rule_results: List[Optional[dict]] = []
    any_low_conf = False
    for n in notes:
        d, conf = rule_based_interpret(n, battery_capacity)
        rule_results.append(d)
        if conf < 0.5:
            any_low_conf = True

    if not any_low_conf:
        # build response
        out = []
        for i, r in enumerate(rule_results):
            if r is None:
                # Defensive: shouldn't happen when all conf>=0.5
                out.append({
                    "note_index": i,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": "No actionable directive.",
                })
            else:
                r2 = dict(r)
                r2["note_index"] = i
                r2.setdefault("applies", r2["directive_type"] != "no_op")
                if r2["directive_type"] == "no_op":
                    r2["applies"] = False
                    r2["structured_adjustment"] = None
                r2.setdefault("explanation", f"Applied {r2['directive_type']}.")
                out.append(r2)
        return out, "rules"

    # at least one low-confidence note: try the LLM
    try:
        raw = call_llm(notes, battery_capacity)
    except Exception:
        raw = None
    if raw is None:
        # build a best-effort response from rules + LLM fallback for low-conf notes
        out = []
        for i, r in enumerate(rule_results):
            if r is not None:
                r2 = dict(r)
                r2["note_index"] = i
                r2.setdefault("applies", r2["directive_type"] != "no_op")
                if r2["directive_type"] == "no_op":
                    r2["applies"] = False
                    r2["structured_adjustment"] = None
                r2.setdefault("explanation", f"Applied {r2['directive_type']}.")
                out.append(r2)
            else:
                out.append({
                    "note_index": i,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": "LLM unavailable; defaulted to no_op.",
                })
        return out, "fallback"
    return raw, "llm"
