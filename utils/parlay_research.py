# utils/parlay_research.py
import os
import json
import time
import datetime as dt
import re
from typing import List, Optional, Dict, Any, Tuple

from pydantic import BaseModel, Field, ValidationError, field_validator
from openai import OpenAI

# ----- Config / debug ---------------------------------------------------------
DEEP_MODEL = os.getenv("DEEP_MODEL", "o4-mini-deep-research-2025-06-26")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

# Set PARLAY_DEBUG_RAW=1 to print a trimmed copy of the raw model output
DEBUG_RAW = os.getenv("PARLAY_DEBUG_RAW", "0") == "1"

# ↑ Important: allow long-running deep research calls (your 14-min asyncio timeout still applies)
client = OpenAI(api_key=OPENAI_API_KEY, timeout=3600)

# ----- Strict output schema (Pydantic) ---------------------------------------
class ParlayLeg(BaseModel):
    market: str = Field(description="moneyline|spread|total|player-prop")
    selection: str = Field(description="e.g., BOS -3.5 or Shohei Ohtani O1.5 TB")
    book_examples: List[str] = Field(default_factory=list, description="Example books where widely available")
    confidence: str = Field(description="low|medium|high")

    @field_validator("confidence")
    @classmethod
    def _conf_ok(cls, v: str) -> str:
        v2 = v.strip().lower()
        if v2 not in {"low", "medium", "high"}:
            raise ValueError("confidence must be one of: low|medium|high")
        return v2

class ParlayResult(BaseModel):
    parlay: List[ParlayLeg] = Field(default_factory=list)
    rationales: List[str] = Field(default_factory=list)
    risks: str = Field(default="", description="Key caveats")
    sources: List[str] = Field(default_factory=list)

# ----- Prompting --------------------------------------------------------------
SYSTEM_RULES = """You are a cautious, evidence-based sports research assistant.
- Use current, reputable sources (prefer last 48 hours).
- Cite sources with URLs for every factual claim (injuries, starting lineups, weather, odds movement).
- Never promise certainty or guaranteed profit; note material uncertainties (rest, travel, lineup changes).
- Optimize for expected value: identify mispricings, matchup edges, and widely available lines.
- Consider: injuries & status, projected starters, travel & rest, pace/tempo, matchup stats, weather (outdoor),
  line movement & market consensus, and book availability by region.
- Output STRICT JSON per the target shape. No extra prose.
"""

def _build_user_prompt(
    *,
    user_query: str,
    sport: str,
    legs: int,
    date_iso: Optional[str],
    region: Optional[str],
    constraints: Optional[str],
) -> str:
    when = date_iso or dt.date.today().isoformat()
    region_line = f"Region: {region}" if region else "Region: unspecified"
    constraints_line = f"Constraints: {constraints}" if constraints else "Constraints: none"
    return f"""Task: Research a {legs}-leg {sport.upper()} parlay for games on {when} only.
User request (exact focus): {user_query}
{region_line}
{constraints_line}

Strict requirements:
- Only include legs from games scheduled on {when}.
- Prefer lines that are widely available in the user's region when specified.
- Provide citations/links for lineup/injury/weather/odds information and any key matchup or market claims.
- Prioritize edges that stem from real, recent data (last 48h), including odds movement and confirmed starters.
- If data is stale or uncertain, flag it clearly.
- If no edge exists for a leg, propose a safer alternative or omit the leg and explain why.
- Use at most 4 high-quality sources; stop early if two independent sources agree.
- Do not explore unrelated games/markets; ignore generic previews without concrete data.

Return ONLY a minified JSON object with exactly the keys: parlay, rationales, risks, sources.
Shape:
{{
  "parlay": [
    {{
      "market": "moneyline|spread|total|player-prop",
      "selection": "e.g., BOS -3.5 or Shohei Ohtani O1.5 TB",
      "book_examples": ["DraftKings","FanDuel"],
      "confidence": "low|medium|high"
    }}
  ],
  "rationales": ["why leg 1", "why leg 2", "..."],
  "risks": "key caveats & what could invalidate edge",
  "sources": ["https://...", "https://..."]
}}"""

# ----- JSON parsing helpers (robust) -----------------------------------------
def _extract_json_from_text(text: str) -> Dict[str, Any]:
    """
    Parse JSON even if wrapped in fences, contains trailing commas,
    uses single quotes, or the top-level is an array.
    """
    s = (text or "").strip()

    # Strip code fences ```json ... ```
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL | re.IGNORECASE)
    if m:
        s = m.group(1).strip()

    # If there's prose, isolate the largest {...} or [...] block
    start_obj, end_obj = s.find("{"), s.rfind("}")
    start_arr, end_arr = s.find("["), s.rfind("]")

    if start_obj != -1 and end_obj != -1 and end_obj > start_obj:
        candidate = s[start_obj:end_obj + 1]
    elif start_arr != -1 and end_arr != -1 and end_arr > start_arr:
        candidate = s[start_arr:end_arr + 1]
    else:
        candidate = s  # last resort

    t = candidate.strip()

    # Repairs:
    # 1) Remove trailing commas before } or ]
    t = re.sub(r",\s*([}\]])", r"\1", t)

    # 2) If it looks like single-quoted JSON, best-effort conversion
    if "'" in t and '"' not in t[:80]:
        t = re.sub(r'(?<!\\)\'', '"', t)

    # Try parse
    try:
        data = json.loads(t)
    except Exception:
        # last-ditch: strip odd backticks/spaces and retry
        t2 = t.strip("` \n\r\t")
        data = json.loads(t2)

    # If top-level is array, wrap to expected shape
    if isinstance(data, list):
        data = {
            "parlay": data,
            "rationales": [],
            "risks": "",
            "sources": []
        }
    return data

# ----- Calling Deep Research with retries & compatibility ---------------------
def _call_deep_research(user_prompt: str):
    """
    Try strict JSON output first (response_format). If the SDK rejects it (TypeError),
    retry without response_format and parse manually.
    Uses web access via 'web_search_preview_2025_03_11' and caps total tool calls.
    """
    max_retries = 3
    last_err: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            # Strict JSON path (newer SDKs)
            return client.responses.create(
                model=DEEP_MODEL,
                reasoning={"effort": "medium"},  # speed-leaning; change to "medium" for deeper runs
                tools=[{"type": "web_search_preview_2025_03_11"}],
                max_tool_calls=15,  # cap web search / open / read cycles
                input=[
                    {"role": "system", "content": SYSTEM_RULES},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},  # may raise TypeError on older clients
            )
        except TypeError:
            # Compatibility path: older SDK without response_format
            try:
                return client.responses.create(
                    model=DEEP_MODEL,
                    reasoning={"effort": "medium"},
                    tools=[{"type": "web_search_preview_2025_03_11"}],
                    max_tool_calls=15,
                    input=[
                        {"role": "system", "content": SYSTEM_RULES},
                        {"role": "user", "content": user_prompt},
                    ],
                )
            except Exception as e2:
                last_err = e2
        except Exception as e:
            last_err = e

        # Exponential backoff before retry
        time.sleep((2 ** attempt) + (0.1 * attempt))

    raise last_err or RuntimeError("Deep Research request failed")

# ----- Public runner ----------------------------------------------------------
def run_deep_research(
    *,
    user_query: str,
    sport: str,
    legs: int,
    date_iso: Optional[str],
    region: Optional[str],
    constraints: Optional[str],
) -> Tuple[Optional[ParlayResult], Optional[str]]:
    """
    Returns (ParlayResult | None, error_message | None).
    """
    if not user_query.strip():
        return None, "Please specify what you want researched (teams, props, angles)."

    user_prompt = _build_user_prompt(
        user_query=user_query.strip(),
        sport=sport,
        legs=legs,
        date_iso=date_iso,
        region=region,
        constraints=constraints,
    )

    # Console logs for visibility
    print("[DeepResearch] API request -> model=", DEEP_MODEL)
    try:
        resp = _call_deep_research(user_prompt)
    except Exception as e:
        return None, f"Deep Research request failed: {e}"
    print("[DeepResearch] API response received")

    text = getattr(resp, "output_text", None)
    if text is None:
        return None, "Empty response from the research model."

    if DEBUG_RAW:
        preview = (text[:1200] + " …") if len(text) > 1200 else text
        print("[DeepResearch][RAW <=]", preview)

    # Parse & validate
    try:
        data = _extract_json_from_text(text)
    except Exception:
        print("[DeepResearch] JSON parse failed")
        preview = (text[:300] + " …") if text and len(text) > 300 else (text or "")
        return None, f"Model did not return valid JSON. Preview: ```{preview}```"

    # Normalize missing fields to avoid brittle failures
    if isinstance(data, dict):
        data.setdefault("parlay", [])
        data.setdefault("rationales", [])
        data.setdefault("risks", "")
        data.setdefault("sources", [])

    try:
        parsed = ParlayResult.model_validate(data)
    except ValidationError as ve:
        print("[DeepResearch] Schema validation failed:", ve)
        # ---- Salvage path (best-effort) ----
        try:
            rough = ParlayResult(
                parlay=[ParlayLeg(**leg) for leg in (data.get("parlay") or []) if isinstance(leg, dict)],
                rationales=[str(x) for x in (data.get("rationales") or [])],
                risks=str(data.get("risks") or ""),
                sources=[str(x) for x in (data.get("sources") or [])],
            )
            parsed = rough
        except Exception as e2:
            print("[DeepResearch] Salvage failed:", e2)
            preview = (text[:300] + " …") if text and len(text) > 300 else (text or "")
            return None, f"Model output failed validation: {ve}\nPreview: ```{preview}```"

    return parsed, None
