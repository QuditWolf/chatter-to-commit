"""
Pipeline audit logger.

Writes one JSONL line per processed message to logs/pipeline_audit.jsonl.
Each line is a complete trace: raw WA text → L2 enrichment → L3 LLM I/O → commit result.

Designed to be tailed/grepped directly:
    tail -f logs/pipeline_audit.jsonl | python -m json.tool
    grep '"raw_text":"D left"' logs/pipeline_audit.jsonl | python -m json.tool
"""
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fleet_pipeline.config import LOGS_DIR

_audit_logger: logging.Logger | None = None


def _build_logger(logs_dir: str) -> logging.Logger:
    lg = logging.getLogger("fleet.pipeline.audit")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False  # keep audit entries out of api.log
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(
        os.path.join(logs_dir, "pipeline_audit.jsonl"),
        maxBytes=50 * 1024 * 1024,   # 50 MB per file
        backupCount=10,
        encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    return lg


def get_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = _build_logger(LOGS_DIR)
    return _audit_logger


def audit(entry: dict) -> None:
    """Write one audit entry as a single JSONL line. Never raises."""
    try:
        get_audit_logger().info(
            json.dumps(entry, ensure_ascii=False, default=str)
        )
    except Exception:
        pass
