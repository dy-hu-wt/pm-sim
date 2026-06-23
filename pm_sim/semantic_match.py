from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from typing import Any

from .jsonutil import dumps, loads
from .paths import REPO_ROOT
from .state import get_state_value, set_state_value


CACHE_KEY = "semantic_match_cache_json"
DEFAULT_MODEL = "gpt-4.1-mini"
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "before",
    "by",
    "can",
    "for",
    "from",
    "has",
    "have",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "so",
    "that",
    "the",
    "their",
    "this",
    "to",
    "we",
    "with",
}
SYNONYMS = {
    "approve": {"approval", "approved", "approves", "review", "reviewer", "human"},
    "audit": {"audit", "security"},
    "automatic": {"auto", "automatic", "automatically"},
    "comment": {"comment", "comments", "commenting"},
    "defer": {"defer", "deferred", "followup", "follow-up", "later"},
    "draft": {"draft", "queue", "queues", "queued"},
    "export": {"export", "csv"},
    "human": {"human", "reviewer", "approval", "approve"},
    "koopa": {"koopa"},
    "nimbus": {"nimbus", "beta", "pilot", "friday"},
    "private": {"private", "source", "repo", "security"},
    "ready": {"ready", "readiness", "go", "track"},
    "repo": {"repo", "sync", "webhook", "commit", "stale", "older"},
    "retain": {"retain", "retained", "retention", "store", "stored"},
    "security": {"security", "private", "source", "repo"},
    "source": {"source", "code", "raw", "private"},
    "stale": {"stale", "older", "old"},
    "transient": {"transient", "transiently", "temporary", "not", "no"},
}


def semantic_match(
    conn: sqlite3.Connection,
    *,
    text: str,
    criteria: dict[str, Any],
    rule_id: str,
) -> dict[str, Any]:
    if not criteria:
        return {"matches": True, "mode": "none", "required": [], "forbidden": []}

    _load_dotenv()
    mode = os.environ.get("PM_SIM_SEMANTIC_MATCHER", "deterministic").lower()
    model = _semantic_model() if mode == "llm" else None
    cache_key = _cache_key(text, criteria, rule_id, mode=mode, model=model)
    cache = _load_cache(conn)
    if cache_key in cache:
        return dict(cache[cache_key])

    if mode == "llm":
        result = _safe_llm_match(text, criteria, model=model)
    else:
        result = _deterministic_match(text, criteria)

    result["cache_key"] = cache_key
    cache[cache_key] = result
    set_state_value(conn, CACHE_KEY, dumps(cache))
    return result


def _deterministic_match(text: str, criteria: dict[str, Any]) -> dict[str, Any]:
    required = _criteria_items(criteria.get("required", []))
    forbidden = _criteria_items(criteria.get("forbidden", []))
    required_results = [_item_match(text, item) for item in required]
    forbidden_results = [_item_match(text, item) for item in forbidden]
    return {
        "matches": all(item["matched"] for item in required_results)
        and not any(item["matched"] for item in forbidden_results),
        "mode": "deterministic",
        "required": required_results,
        "forbidden": forbidden_results,
    }


def _llm_match(text: str, criteria: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("PM_SIM_SEMANTIC_MATCHER=llm requires the openai package.") from exc

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("PM_SIM_SEMANTIC_MATCHER=llm requires OPENAI_API_KEY.")

    client = OpenAI()
    model = model or _semantic_model()
    prompt = {
        "task": "Decide whether the message satisfies the required semantic criteria without satisfying forbidden criteria. Return strict JSON only.",
        "message": text,
        "criteria": criteria,
        "schema": {
            "matches": "boolean",
            "required": [{"id": "string", "matched": "boolean", "rationale": "string"}],
            "forbidden": [{"id": "string", "matched": "boolean", "rationale": "string"}],
        },
    }
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": "You are a conservative semantic matcher. Fail closed when unsure.",
            },
            {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
        ],
        temperature=0,
        text={"format": {"type": "json_object"}},
    )
    content = getattr(response, "output_text", "")
    parsed = json.loads(content)
    return {
        "matches": bool(parsed.get("matches")),
        "mode": "llm",
        "model": model,
        "required": parsed.get("required", []),
        "forbidden": parsed.get("forbidden", []),
    }


def _safe_llm_match(
    text: str,
    criteria: dict[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    try:
        return _llm_match(text, criteria, model=model)
    except Exception as exc:
        return {
            "matches": False,
            "mode": "llm",
            "model": model or _semantic_model(),
            "error": f"{type(exc).__name__}: {exc}",
            "required": [],
            "forbidden": [],
        }


def _criteria_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            normalized.append({"id": f"criterion_{index}", "description": item})
        elif isinstance(item, dict):
            normalized.append(item)
    return normalized


def _item_match(text: str, item: dict[str, Any]) -> dict[str, Any]:
    normalized_text = _normalize(text)
    signals = [_normalize(value) for value in item.get("signals", [])]
    if signals:
        matched = any(signal and signal in normalized_text for signal in signals)
    else:
        matched = _description_matches(text, str(item.get("description", "")))
    return {
        "id": item.get("id") or item.get("description") or "criterion",
        "matched": bool(matched),
    }


def _description_matches(text: str, description: str) -> bool:
    text_tokens = _expanded_tokens(text)
    description_tokens = _expanded_tokens(description)
    if not description_tokens:
        return True
    overlap = text_tokens & description_tokens
    threshold = max(2, int(len(description_tokens) * 0.45))
    return len(overlap) >= threshold


def _expanded_tokens(text: str) -> set[str]:
    tokens = {_stem(token) for token in re.findall(r"[a-z0-9]+", text.lower())}
    tokens = {token for token in tokens if token and token not in STOPWORDS}
    expanded = set(tokens)
    for token in tokens:
        expanded.update(SYNONYMS.get(token, set()))
    return expanded


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _cache_key(
    text: str,
    criteria: dict[str, Any],
    rule_id: str,
    *,
    mode: str,
    model: str | None,
) -> str:
    payload = json.dumps(
        {
            "rule_id": rule_id,
            "criteria": criteria,
            "text": text,
            "mode": mode,
            "model": model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _semantic_model() -> str:
    return os.environ.get("PM_SIM_SEMANTIC_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL


def _load_cache(conn: sqlite3.Connection) -> dict[str, Any]:
    value = get_state_value(conn, CACHE_KEY)
    cache = loads(value, {})
    return cache if isinstance(cache, dict) else {}


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
