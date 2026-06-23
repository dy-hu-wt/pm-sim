from __future__ import annotations

import os
import re


os.environ.setdefault("OPENAI_API_KEY", "test-key")


def _install_fake_concept_matcher() -> None:
    from pm_sim import concept_match

    def fake_llm_match(text, criteria, *, model=None):
        required = []
        for item in concept_match._criteria_items(criteria.get("required", [])):
            required.append(
                {
                    "id": item["id"],
                    "matched": _matches_item(text, item),
                    "rationale": "test fake matcher",
                }
            )
        forbidden = []
        for item in concept_match._criteria_items(criteria.get("forbidden", [])):
            forbidden.append(
                {
                    "id": item["id"],
                    "matched": _matches_forbidden_item(text, item),
                    "rationale": "test fake matcher",
                }
            )
        result = {
            "matches": all(row["matched"] for row in required) and not any(row["matched"] for row in forbidden),
            "mode": "concept_match",
            "matcher": "llm",
            "model": model or "test-concept-model",
            "required": required,
            "forbidden": forbidden,
        }
        return concept_match._validate_llm_result(result, criteria)

    concept_match._llm_match = fake_llm_match


def _matches_item(text, item) -> bool:
    normalized_text = _normalize(text)
    exemplars = item.get("exemplars") if isinstance(item.get("exemplars"), list) else []
    if not exemplars:
        return False
    for exemplar in exemplars:
        normalized_exemplar = _normalize(exemplar)
        if not _exemplar_matches(normalized_text, exemplar, required=True):
            continue
        if _requires_assertion(item) and _has_defer_or_negation(normalized_text, normalized_exemplar):
            continue
        return True
    return False


def _matches_forbidden_item(text, item) -> bool:
    normalized_text = _normalize(text)
    exemplars = item.get("exemplars") if isinstance(item.get("exemplars"), list) else []
    if not exemplars:
        return False
    anchors = item.get("anchors") if isinstance(item.get("anchors"), list) else []
    if anchors and all(_normalize(anchor) in normalized_text for anchor in anchors):
        return True
    return any(_forbidden_exemplar_matches(normalized_text, exemplar) for exemplar in exemplars)


def _requires_assertion(item) -> bool:
    item_id = str(item.get("id", ""))
    description = str(item.get("description", ""))
    return item.get("must_be_asserted") and any(
        word in f"{item_id} {description}"
        for word in ("commitment", "unsafe", "promise", "overcommit")
    )


def _exemplar_matches(normalized_text: str, exemplar: str, *, required: bool) -> bool:
    normalized_exemplar = _normalize(exemplar)
    if not normalized_exemplar:
        return False
    if normalized_exemplar in normalized_text:
        return True
    text_tokens = set(normalized_text.split())
    exemplar_tokens = [token for token in normalized_exemplar.split() if token not in _STOP_WORDS]
    if not exemplar_tokens:
        return False
    matched = sum(1 for token in exemplar_tokens if token in text_tokens)
    coverage = matched / len(exemplar_tokens)
    threshold = 0.75 if required else 0.9
    return coverage >= threshold


def _forbidden_exemplar_matches(normalized_text: str, exemplar: str) -> bool:
    normalized_exemplar = _normalize(exemplar)
    if normalized_exemplar and normalized_exemplar in normalized_text:
        return (
            f"not {normalized_exemplar}" not in normalized_text
            and f"no {normalized_exemplar}" not in normalized_text
            and f"without {normalized_exemplar}" not in normalized_text
        )
    return _exemplar_matches(normalized_text, exemplar, required=False) and not _has_defer_or_negation(
        normalized_text,
        normalized_exemplar,
    )


def _has_defer_or_negation(normalized_text: str, normalized_exemplar: str) -> bool:
    exemplar_tokens = [token for token in normalized_exemplar.split() if token not in _STOP_WORDS]
    text_tokens = normalized_text.split()
    if not exemplar_tokens:
        return False
    positions = [
        index
        for index, token in enumerate(text_tokens)
        if token in exemplar_tokens
    ]
    if not positions:
        return False
    start = max(0, min(positions) - 4)
    end = min(len(text_tokens), max(positions) + 5)
    window = set(text_tokens[start:end])
    return bool(window.intersection(_DEFER_OR_NEGATION_WORDS))


def _normalize(value) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).lower()).split())


_STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "before",
    "for",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


_DEFER_OR_NEGATION_WORDS = {
    "defer",
    "deferred",
    "follow",
    "followup",
    "follow-up",
    "later",
    "no",
    "not",
    "should",
    "without",
}


_install_fake_concept_matcher()
