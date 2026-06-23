from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any

from .jsonutil import dumps, loads
from .paths import REPO_ROOT
from .state import get_state_value, set_state_value


CACHE_KEY = "concept_match_cache_json"
DEFAULT_MODEL = "gpt-4.1-mini"


def concept_match(
    conn: sqlite3.Connection,
    *,
    text: str,
    criteria: dict[str, Any],
    rule_id: str,
) -> dict[str, Any]:
    if not criteria:
        return {"matches": True, "mode": "none", "matcher": "none", "required": [], "forbidden": []}

    _load_dotenv()
    model = _concept_model()
    cache_key = _cache_key(text, criteria, rule_id, model=model)
    cache = _load_cache(conn)
    if cache_key in cache:
        return dict(cache[cache_key])

    result = _safe_llm_match(text, criteria, model=model)
    result["cache_key"] = cache_key
    cache[cache_key] = result
    set_state_value(conn, CACHE_KEY, dumps(cache))
    return result


def _llm_match(text: str, criteria: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Concept matching requires the openai package.") from exc

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Concept matching requires OPENAI_API_KEY.")

    client = OpenAI()
    model = model or _concept_model()
    prompt = {
        "task": "Decide whether the message satisfies each authored required concept without satisfying any forbidden concept. Return strict JSON only.",
        "rules": [
            "Use only the supplied message and authored criteria.",
            "Do not infer missing project facts.",
            "A required concept matches only if the message clearly expresses it.",
            "A forbidden concept matches only if the message actually commits to it; negated warnings do not count.",
            "Fail closed when unsure.",
        ],
        "message": text,
        "criteria": _llm_criteria(criteria),
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
                "content": "You are a conservative concept matcher. Fail closed when unsure.",
            },
            {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
        ],
        temperature=0,
        text={"format": {"type": "json_object"}},
    )
    parsed = json.loads(getattr(response, "output_text", ""))
    result = {
        "matches": bool(parsed.get("matches")),
        "mode": "concept_match",
        "matcher": "llm",
        "model": model,
        "required": parsed.get("required", []),
        "forbidden": parsed.get("forbidden", []),
    }
    return _validate_llm_result(result, criteria)


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
            "mode": "concept_match",
            "matcher": "llm",
            "model": model or _concept_model(),
            "error": f"{type(exc).__name__}: {exc}",
            "required": _unmatched_rows(criteria.get("required", [])),
            "forbidden": _unmatched_rows(criteria.get("forbidden", [])),
        }


def _llm_criteria(criteria: dict[str, Any]) -> dict[str, Any]:
    return {
        "required": _criteria_items(criteria.get("required", [])),
        "forbidden": _criteria_items(criteria.get("forbidden", [])),
    }


def _criteria_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            normalized.append({"id": f"criterion_{index}", "description": item})
        elif isinstance(item, dict):
            normalized.append(
                {
                    "id": item.get("id") or item.get("description") or f"criterion_{index}",
                    "description": item.get("description", ""),
                    "exemplars": item.get("exemplars", []),
                    "anchors": item.get("anchors", []),
                    "must_be_asserted": item.get("must_be_asserted", False),
                }
            )
    return normalized


def _unmatched_rows(items: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "matched": False,
            "rationale": "concept matcher failed closed before evaluating this concept.",
        }
        for item in _criteria_items(items)
    ]


def _validate_llm_result(result: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any]:
    required = _criteria_items(criteria.get("required", []))
    forbidden = _criteria_items(criteria.get("forbidden", []))
    expected_required_ids = [str(item["id"]) for item in required]
    expected_forbidden_ids = [str(item["id"]) for item in forbidden]
    actual_required = _llm_rows_by_id(result.get("required", []))
    actual_forbidden = _llm_rows_by_id(result.get("forbidden", []))

    if set(actual_required) != set(expected_required_ids) or set(actual_forbidden) != set(expected_forbidden_ids):
        return {
            **result,
            "matches": False,
            "error": "LLM response did not return exactly the authored concept ids.",
        }
    if not all(_valid_rationale(row) for row in [*actual_required.values(), *actual_forbidden.values()]):
        return {
            **result,
            "matches": False,
            "error": "LLM response omitted required rationales.",
        }

    computed_match = all(bool(actual_required[item_id].get("matched")) for item_id in expected_required_ids)
    computed_match = computed_match and not any(
        bool(actual_forbidden[item_id].get("matched")) for item_id in expected_forbidden_ids
    )
    if bool(result.get("matches")) != computed_match:
        return {
            **result,
            "matches": False,
            "error": "LLM response top-level matches flag contradicted per-concept results.",
        }
    return {**result, "matches": computed_match}


def _llm_rows_by_id(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    mapped = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            mapped[row_id] = row
    return mapped


def _valid_rationale(row: dict[str, Any]) -> bool:
    return isinstance(row.get("matched"), bool) and isinstance(row.get("rationale"), str) and bool(row["rationale"].strip())


def _cache_key(
    text: str,
    criteria: dict[str, Any],
    rule_id: str,
    *,
    model: str,
) -> str:
    payload = json.dumps(
        {
            "rule_id": rule_id,
            "criteria": _llm_criteria(criteria),
            "text": text,
            "matcher": "llm",
            "model": model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _concept_model() -> str:
    return os.environ.get("PM_SIM_CONCEPT_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL


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
