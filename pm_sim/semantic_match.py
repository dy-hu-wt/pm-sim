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
NEGATION_TOKENS = {"not", "no", "never", "without"}


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
    mode = str(criteria.get("matcher_mode") or os.environ.get("PM_SIM_SEMANTIC_MATCHER", "deterministic")).lower()
    if mode == "semantic":
        mode = "deterministic"
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
        "task": "Decide whether the message satisfies each authored required concept without satisfying any forbidden concept. Return strict JSON only.",
        "rules": [
            "Use only the supplied message and authored criteria.",
            "Do not infer missing project facts.",
            "A required concept matches only if the message clearly expresses it.",
            "A forbidden concept matches only if the message actually commits to it; negated warnings do not count.",
            "Fail closed when unsure.",
        ],
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
    result = {
        "matches": bool(parsed.get("matches")),
        "mode": "llm",
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
    exemplars = [str(value) for value in item.get("exemplars", []) if str(value).strip()]
    if exemplars:
        matched = any(_exemplar_matches(text, exemplar) for exemplar in exemplars)
    elif signals:
        matched = any(
            signal and signal in normalized_text and not _signal_is_negated(normalized_text, signal)
            for signal in signals
        )
    else:
        matched = _description_matches(text, str(item.get("description", "")))
    return {
        "id": item.get("id") or item.get("description") or "criterion",
        "matched": bool(matched),
        "rationale": "matched authored exemplar" if matched else "no authored exemplar matched",
    }


def _description_matches(text: str, description: str) -> bool:
    text_tokens = _content_tokens(text)
    description_tokens = _content_tokens(description)
    if not description_tokens:
        return True
    overlap = text_tokens & description_tokens
    threshold = max(2, int(len(description_tokens) * 0.75))
    return len(overlap) >= threshold


def _exemplar_matches(text: str, exemplar: str) -> bool:
    exemplar_normalized = _normalize(exemplar)
    if not exemplar_normalized:
        return False
    text_normalized = _normalize(text)
    if exemplar_normalized in text_normalized:
        return not _signal_is_negated(text_normalized, exemplar_normalized)

    exemplar_tokens = _content_token_list(exemplar)
    if len(exemplar_tokens) < 3:
        return False
    exemplar_bigrams = _bigrams(exemplar_tokens)
    for sentence in _sentences(text):
        sentence_tokens = _content_token_list(sentence)
        if not sentence_tokens:
            continue
        token_coverage = len(set(exemplar_tokens) & set(sentence_tokens)) / len(set(exemplar_tokens))
        bigrams = _bigrams(sentence_tokens)
        bigram_coverage = (
            len(exemplar_bigrams & bigrams) / len(exemplar_bigrams)
            if exemplar_bigrams
            else 0
        )
        if token_coverage >= 0.8 and bigram_coverage >= 0.4:
            return not _sentence_negates_exemplar(sentence, exemplar)
    return False


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"[.!?;\n]+", text) if part.strip()]


def _content_tokens(text: str) -> set[str]:
    return set(_content_token_list(text))


def _content_token_list(text: str) -> list[str]:
    return [
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token and (token not in STOPWORDS or token in NEGATION_TOKENS)
    ]


def _bigrams(tokens: list[str]) -> set[tuple[str, str]]:
    return set(zip(tokens, tokens[1:]))


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _signal_is_negated(normalized_text: str, signal: str) -> bool:
    signal_tokens = signal.split()
    if not signal_tokens or any(token in NEGATION_TOKENS for token in signal_tokens):
        return False
    text_tokens = normalized_text.split()
    signal_length = len(signal_tokens)
    for index in range(0, len(text_tokens) - signal_length + 1):
        if text_tokens[index : index + signal_length] != signal_tokens:
            continue
        preceding = text_tokens[max(0, index - 3) : index]
        if any(token in NEGATION_TOKENS for token in preceding):
            return True
    return False


def _sentence_negates_exemplar(sentence: str, exemplar: str) -> bool:
    exemplar_tokens = _content_token_list(exemplar)
    if any(token in NEGATION_TOKENS for token in exemplar_tokens):
        return False
    sentence_tokens = _content_token_list(sentence)
    shared = set(exemplar_tokens) & set(sentence_tokens)
    for index, token in enumerate(sentence_tokens):
        if token not in shared:
            continue
        preceding = sentence_tokens[max(0, index - 4) : index]
        if any(value in NEGATION_TOKENS for value in preceding):
            return True
    return False


def _validate_llm_result(result: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any]:
    required = _criteria_items(criteria.get("required", []))
    forbidden = _criteria_items(criteria.get("forbidden", []))
    expected_required_ids = [str(item.get("id") or item.get("description") or "criterion") for item in required]
    expected_forbidden_ids = [str(item.get("id") or item.get("description") or "criterion") for item in forbidden]
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
