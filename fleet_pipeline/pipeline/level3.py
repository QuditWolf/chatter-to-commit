"""
Level 3 — LLM inference wrapper.
Refactored from shift_sim2.py.

Responsibilities:
- Build the prompt from a Level 2 message + registries + rolling history
- Call the LLM (Qwen2.5-7B-Instruct-AWQ via vLLM)
- Strip markdown fences from output (Qwen wraps JSON in ```json``` blocks)
- Return parsed JSON dict

The LLM object is created once and reused across calls (pass it in).
"""
import json
import re
import sys
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fleet_pipeline.config import (
    MODEL_NAME, LLM_TEMPERATURE, LLM_MAX_TOKENS, PROMPT_TEMPLATE_PATH, L3_MAX_HISTORY,
    LLM_MOCK, LLM_BASE_URL, LLM_API_KEY,
)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def convert_level2_to_prompt_obj(
    level2_msg: Dict[str, Any],
    truck_registry: Dict[str, list],
    site_registry: Dict[str, list],
    l3_history: Optional[List[Dict]] = None,
    operator_clarification: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a Level 2 message dict into the prompt input object."""
    if l3_history is None:
        l3_history = []

    raw_block = level2_msg.get("raw", {})
    l2_meta_block = {
        "time_since_last_msg": level2_msg.get("cursor", {}).get("inactivity_window"),
        "time_since_last_sender_msg": level2_msg.get("cursor", {}).get("time_since_last_sender_msg"),
        "rough_shift_id": level2_msg.get("rough_shift_id"),
        "lang": level2_msg.get("lang"),
        "global_inactivity_window": level2_msg.get("cursor", {}).get("inactivity_window", 0),
        "candidate_msg_type": level2_msg.get("candidate_msg_type"),
        "rough_trucks": level2_msg.get("rough_trucks", []),
        "rough_sites": level2_msg.get("rough_sites", []),
        "rough_status_keywords": level2_msg.get("rough_status_keywords", []),
    }

    return {
        "raw": {
            "msg_id": raw_block.get("msg_id"),
            "timestamp_iso": raw_block.get("timestamp_iso"),
            "sender_id": raw_block.get("sender_id"),
            "sender_name": raw_block.get("sender_name"),
            "raw_text": raw_block.get("raw_text"),
            "is_edited": raw_block.get("is_edited", False),
            "is_deleted": raw_block.get("is_deleted", False),
        },
        "l2_meta": l2_meta_block,
        "l3_context_summary": {
            "last_status_events": deepcopy(l3_history),
            **({"operator_clarification": operator_clarification} if operator_clarification else {}),
        },
        "truck_registry": deepcopy(truck_registry),
        "site_registry": deepcopy(site_registry),
    }


def build_prompt(template_text: str, prompt_obj: Dict[str, Any]) -> str:
    """Replace {RAW}, {L2_META}, {L3_CONTEXT}, {TRUCK_REGISTRY}, {SITE_REGISTRY} in template."""
    replacements = {
        "{RAW}": json.dumps(prompt_obj["raw"], ensure_ascii=False),
        "{L2_META}": json.dumps(prompt_obj["l2_meta"], ensure_ascii=False),
        "{L3_CONTEXT}": json.dumps(prompt_obj["l3_context_summary"], ensure_ascii=False),
        "{TRUCK_REGISTRY}": json.dumps(prompt_obj["truck_registry"], ensure_ascii=False),
        "{SITE_REGISTRY}": json.dumps(prompt_obj["site_registry"], ensure_ascii=False),
    }
    out = template_text
    for k, v in replacements.items():
        out = out.replace(k, v)
    return out


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def strip_markdown_fences(text: str) -> str:
    """Strip ```json ... ``` wrapper and <think>...</think> blocks from LLM output."""
    text = text.strip()
    # Remove <think>...</think> blocks (GLM thinking model sometimes leaks these into content)
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()


def parse_llm_output(raw_text: str) -> Dict[str, Any]:
    """
    Parse the raw LLM output string into a dict.
    Handles markdown fences, <think> blocks, and embedded JSON.
    Returns an error dict on failure.
    """
    if not raw_text:
        return {"msg_type": "ERROR", "events": [], "error": "No output from model", "raw_llm_output": ""}

    cleaned = strip_markdown_fences(raw_text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: try to find the first complete {...} JSON object in the text.
        # Handles cases where the model wraps the JSON in prose.
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {
            "msg_type": "ERROR",
            "events": [],
            "error": f"JSON parse failed — could not extract JSON from model output",
            "raw_llm_output": raw_text,
        }


# ---------------------------------------------------------------------------
# Level 3 Processor
# ---------------------------------------------------------------------------

class Level3Processor:
    """
    Wraps LLM calls for Level 3 inference.
    Create once; call process_message() per message.

    Modes (checked in order):
      mock=True or FLEET_LLM_MOCK=true  → rule-based mock, no LLM
      FLEET_LLM_BASE_URL set            → OpenAI-compatible HTTP API
      (fallback)                        → vLLM in-process SDK
    """

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        prompt_template_path: str = PROMPT_TEMPLATE_PATH,
        mock: bool = False,
    ):
        # mock=True arg OR env var both activate mock mode
        self.mock = mock or LLM_MOCK
        self.model_name = model_name
        self.llm = None          # vLLM in-process handle
        self._openai = None      # OpenAI-compat client

        with open(prompt_template_path, "r", encoding="utf-8") as f:
            self.template_text = f.read()

        if self.mock:
            return  # nothing to initialise

        if LLM_BASE_URL:
            # OpenAI-compatible endpoint (vllm serve, Ollama, llama.cpp, etc.)
            try:
                from openai import OpenAI
                import os as _os
                _llm_timeout = float(_os.environ.get("FLEET_LLM_TIMEOUT", "600"))
                self._openai = OpenAI(
                    base_url=LLM_BASE_URL,
                    api_key=LLM_API_KEY,
                    timeout=_llm_timeout,
                )
                print(f"[Level3] Using OpenAI-compat endpoint: {LLM_BASE_URL} / model: {model_name}")
            except ImportError:
                raise RuntimeError(
                    "openai package not installed. Run: pip install openai"
                )
        else:
            # Legacy in-process vLLM (requires GPU + vllm package)
            try:
                from vllm import LLM, SamplingParams
                self.llm = LLM(model_name)
                self.params = SamplingParams(
                    temperature=LLM_TEMPERATURE,
                    max_tokens=LLM_MAX_TOKENS,
                    top_p=1.0,
                )
                print(f"[Level3] Using in-process vLLM: {model_name}")
            except ImportError:
                raise RuntimeError(
                    "vLLM not installed and FLEET_LLM_BASE_URL is not set.\n"
                    "Either:\n"
                    "  • Set FLEET_LLM_MOCK=true  (for testing without LLM)\n"
                    "  • Set FLEET_LLM_BASE_URL   (for vllm serve / Ollama / llama.cpp)\n"
                    "  • pip install vllm          (in-process, requires GPU)"
                )

    def process_message(
        self,
        level2_msg: Dict[str, Any],
        truck_registry: Dict[str, list],
        site_registry: Dict[str, list],
        l3_history: Optional[List[Dict]] = None,
        operator_clarification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run one Level 2 message through the LLM.
        Returns the parsed Level 3 result dict.
        """
        prompt_obj = convert_level2_to_prompt_obj(
            level2_msg, truck_registry, site_registry, l3_history or [],
            operator_clarification=operator_clarification,
        )
        prompt_text = build_prompt(self.template_text, prompt_obj)

        if self.mock:
            raw_output = self._mock_response(level2_msg)
        elif self._openai is not None:
            # GLM-4.7-Flash cold-cache issue: model may stop after </think> without
            # generating JSON content on the very first call after server restart.
            # The startup warmup in main.py handles the common case.
            # This 1-retry is a safety net for any residual empty-content responses.
            raw_output = None
            messages = [
                {"role": "user", "content": prompt_text},
            ]
            for attempt in range(2):
                retry_temp = min(LLM_TEMPERATURE + attempt * 0.3, 1.0)
                try:
                    resp = self._openai.chat.completions.create(
                        model=self.model_name,
                        messages=messages,
                        temperature=retry_temp,
                        max_tokens=LLM_MAX_TOKENS,
                        extra_body={
                            "chat_template_kwargs": {"enable_thinking": False},
                        },
                    )
                    msg = resp.choices[0].message
                    raw_output = (msg.content or "").strip()
                    if raw_output:
                        if attempt > 0:
                            print(f"[Level3] Retry {attempt} succeeded (temp={retry_temp:.1f})", file=sys.stderr)
                        break
                    print(
                        f"[Level3] Attempt {attempt+1}: content empty"
                        + (f" — retrying temp={retry_temp+0.3:.1f}" if attempt == 0 else ""),
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(f"[Level3] OpenAI-compat error (attempt {attempt+1}): {e}", file=sys.stderr)
                    break
        else:
            try:
                resp = self.llm.generate(prompt_text, self.params)
                raw_output = resp[0].outputs[0].text.strip()
            except Exception as e:
                raw_output = None
                print(f"[Level3] vLLM error: {e}", file=sys.stderr)

        parsed = parse_llm_output(raw_output)

        # Inject pipeline metadata (not from LLM)
        parsed["raw_message"] = prompt_obj["raw"]
        parsed["level2_meta"] = prompt_obj["l2_meta"]
        parsed["processing_id"] = str(uuid4())
        # Debug fields for audit log — pipeline_service pops these before committing
        parsed["_l3_prompt"] = prompt_text
        parsed["_l3_raw_output"] = raw_output or ""

        return parsed

    def _mock_response(self, level2_msg: Dict[str, Any]) -> str:
        """
        Generate a realistic mock response using L2 metadata.
        Uses rough_trucks, rough_sites, rough_status_keywords to build actual events.
        """
        candidate = level2_msg.get("candidate_msg_type", "NOISE_LIKE")
        rough_trucks = level2_msg.get("rough_trucks", [])
        rough_sites = level2_msg.get("rough_sites", [])
        rough_kws = level2_msg.get("rough_status_keywords", [])
        raw = level2_msg.get("raw", {})
        ts = raw.get("timestamp_iso", "")
        shift_id = level2_msg.get("rough_shift_id")

        _base = {"tally": None, "query": None, "notes": "mock", "shift_id": shift_id}

        if candidate == "DELETED":
            return json.dumps({**_base, "msg_type": "NOISE", "events": [],
                                "overall_confidence": 0.5, "commit_recommendation": "HOLD"})

        if candidate == "TALLY_LIKE":
            return json.dumps({**_base, "msg_type": "TALLY_UPDATE", "events": [],
                                "tally": {"note": "mock tally"},
                                "overall_confidence": 0.9, "commit_recommendation": "COMMIT"})

        if candidate in ("QUERY_LIKE",):
            return json.dumps({**_base, "msg_type": "QUERY", "events": [],
                                "query": {"intent": "STATUS_CHECK"},
                                "overall_confidence": 0.9, "commit_recommendation": "HOLD"})

        if candidate == "STATUS_LIKE" and rough_trucks and rough_kws:
            STATUS_MAP = {
                "ls": "LS", "lo": "LO", "us": "US", "uo": "UO",
                "enter": "ENTER", "left": "LEFT",
                "loading started": "LS", "loading over": "LO",
                "loading": "LS", "unloaded": "UO", "unloading": "US",
            }
            status = None
            for kw in rough_kws:
                status = STATUS_MAP.get(kw.lower())
                if status:
                    break
            if not status:
                status = "ENTER"

            events = []
            for truck_alias in rough_trucks[:3]:
                truck_id = self._mock_resolve_truck(truck_alias)
                site_alias = rough_sites[0] if rough_sites else None
                site_id = self._mock_resolve_site(site_alias) if site_alias else None
                conf = 0.88 if (truck_id and site_id) else (0.55 if truck_id else 0.35)
                events.append({
                    "event_id": str(uuid4()),
                    "truck_alias": truck_alias,
                    "truck_id": truck_id,
                    "status": status,
                    "site_alias": site_alias,
                    "site_id": site_id,
                    "material": None,
                    "timestamp_effective": ts,
                    "inferred": not (truck_id and site_id),
                    "confidence": conf,
                    "reasoning": f"mock: {truck_alias} {status} at {site_alias or 'unknown'}",
                })

            if events:
                conf = sum(e["confidence"] for e in events) / len(events)
                commit = "COMMIT" if conf >= 0.85 else "COMMIT_FLAG" if conf >= 0.6 else "HOLD"
                return json.dumps({**_base, "msg_type": "STATUS_UPDATE", "events": events,
                                   "overall_confidence": round(conf, 2), "commit_recommendation": commit})

        return json.dumps({**_base, "msg_type": "NOISE", "events": [],
                           "overall_confidence": 0.9, "commit_recommendation": "HOLD"})

    def _mock_resolve_truck(self, alias: str) -> Optional[str]:
        """Best-effort truck_id lookup for mock mode."""
        if not hasattr(self, "_mock_truck_map"):
            try:
                from fleet_pipeline.pipeline.registries import load_truck_registry
                reg = load_truck_registry()
                self._mock_truck_map = {}
                for tid, aliases in reg.items():
                    for a in aliases:
                        self._mock_truck_map[a.lower()] = tid
                    self._mock_truck_map[tid.lower()] = tid
            except Exception:
                self._mock_truck_map = {}
        return self._mock_truck_map.get(alias.lower())

    def _mock_resolve_site(self, alias: str) -> Optional[str]:
        """Best-effort site_id lookup for mock mode."""
        if not hasattr(self, "_mock_site_map"):
            try:
                from fleet_pipeline.pipeline.registries import load_site_registry
                reg = load_site_registry()
                self._mock_site_map = {}
                for sid, aliases in reg.items():
                    for a in aliases:
                        self._mock_site_map[a.lower()] = sid
                    self._mock_site_map[sid.lower()] = sid
            except Exception:
                self._mock_site_map = {}
        return self._mock_site_map.get(alias.lower())


# ---------------------------------------------------------------------------
# Rolling history helper
# ---------------------------------------------------------------------------

def update_l3_history(history: List[Dict], parsed: Dict[str, Any], max_size: int = L3_MAX_HISTORY) -> List[Dict]:
    """Append STATUS_UPDATE events to rolling history; trim to max_size."""
    if parsed.get("msg_type") == "STATUS_UPDATE":
        for ev in parsed.get("events", []):
            history.append({
                "truck_id": ev.get("truck_id"),
                "truck_alias": ev.get("truck_alias"),
                "site_id": ev.get("site_id"),
                "status": ev.get("status"),
                "timestamp_effective": ev.get("timestamp_effective"),
            })
    return history[-max_size:]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        try:
            if ts.endswith("Z"):
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            pass
    return None
