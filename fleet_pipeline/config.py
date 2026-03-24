import os

# Load .env from project root (if python-dotenv is installed)
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except ImportError:
    pass  # dotenv optional — set env vars manually or export them

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("FLEET_DB_PATH", os.path.join(BASE_DIR, "data", "fleet.db"))
PROMPT_TEMPLATE_PATH = os.path.join(BASE_DIR, "prompts", "level3_prompt_template.txt")

# ── LLM ──────────────────────────────────────────────────────────────────────
# Three modes, controlled by env vars:
#
#   FLEET_LLM_MOCK=true          → mock mode (no LLM, rule-based, dev/testing)
#   FLEET_LLM_BASE_URL=<url>     → OpenAI-compatible API (vllm serve, Ollama, llama.cpp…)
#   (neither)                    → vLLM in-process SDK (legacy, requires GPU + vllm package)
#
# FLEET_LLM_BASE_URL examples:
#   http://localhost:8001/v1     (vllm serve)
#   http://localhost:11434/v1    (Ollama)
#   http://localhost:8080/v1     (llama.cpp server)

LLM_MOCK        = os.environ.get("FLEET_LLM_MOCK", "false").lower() in ("1", "true", "yes")
LLM_BASE_URL    = os.environ.get("FLEET_LLM_BASE_URL", "")       # empty → in-process vLLM
LLM_API_KEY     = os.environ.get("FLEET_LLM_API_KEY", "EMPTY")

# Model name — used both for OpenAI-compat and in-process vLLM
MODEL_NAME      = os.environ.get("FLEET_MODEL", "Qwen/Qwen2.5-7B-Instruct-AWQ")

LLM_TEMPERATURE = float(os.environ.get("FLEET_LLM_TEMPERATURE", "0.0"))
LLM_MAX_TOKENS  = int(os.environ.get("FLEET_LLM_MAX_TOKENS", "2048"))

# Rolling L3 history window
L3_MAX_HISTORY = 12

# Confidence thresholds
CONFIDENCE_THRESHOLDS = {
    "AUTO_COMMIT": 0.85,   # >= 0.85 → COMMITTED silently
    "COMMIT_FLAG": 0.60,   # 0.60–0.85 → FLAGGED + HITL question
    "HOLD": 0.60,          # < 0.60 → HELD + HITL question
    "UNKNOWN_ENTITY": 0.0, # truck_id=null or required site_id=null → always HOLD
}

# API
API_HOST = os.environ.get("FLEET_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("FLEET_API_PORT", 8000))
