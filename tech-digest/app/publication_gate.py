"""Every daily draft must pass this pure gate before snapshot or publication."""
from __future__ import annotations

import math

from app.reader_article import ARTICLE_TYPES, RUBRIC_VERSION, draft_hash, markdown_issues, publication_markdown_issues, source_metadata_hash, validate_draft

WEIGHTS = {"utility": .30, "depth": .30, "credibility": .25, "audience": .10, "novelty": .05}


def weighted_score(scores: dict) -> float:
    if not isinstance(scores, dict):
        raise ValueError("scores must be a complete numeric mapping")
    total = 0.0
    for key, weight in WEIGHTS.items():
        value = scores.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or not 0 <= value <= 10:
            raise ValueError("invalid score dimension: " + key)
        total += value * weight
    return round(total, 6)


def decide_publication(title: str, markdown: str, draft: dict,
                       source_packet: dict, review: dict | None) -> dict:
    def result(status, reasons, **extra):
        reasons = reasons if isinstance(reasons, list) else ["invalid_review_reasons"]
        return {"status": status, "reasons": list(dict.fromkeys(r for r in reasons if isinstance(r, str))), **extra}

    errors = validate_draft(draft, source_packet)
    errors.extend(markdown_issues(markdown))
    errors.extend(publication_markdown_issues(markdown))
    if not isinstance(title, str) or not title.strip() or len(title) > 30:
        errors.append("invalid_final_title")
    if errors:
        integrity = {"source_hash_mismatch", "draft_source_mismatch", "invalid_schema_version", "publication_body_not_normalized"}
        return result("generation_error" if integrity.intersection(errors) else "skip_quality", errors)
    if not isinstance(review, dict) or review.get("status") == "error":
        return result("generation_error", ["review_unavailable"])
    if not isinstance(review.get("status"), str) or review["status"] not in {"pass", "revise", "reject"}:
        return result("generation_error", ["invalid_review_status"])
    if review.get("rubric_version") != RUBRIC_VERSION \
            or review.get("source_url") != source_packet["url"] \
            or review.get("source_hash") != source_packet["source_hash"] \
            or review.get("source_metadata_hash") != source_metadata_hash(source_packet) \
            or review.get("draft_hash") != draft_hash(title, markdown):
        return result("generation_error", ["stale_or_unbound_review"])
    if title != draft.get("title"):
        return result("generation_error", ["final_title_mismatch"])
    if not isinstance(review.get("article_type"), str) or review["article_type"] not in ARTICLE_TYPES \
            or review.get("content_purpose") != "knowledge":
        return result("skip_quality", ["excluded_content"])
    if review.get("source_complete") is not True or review.get("evidence_supported") is not True:
        return result("skip_quality", ["incomplete_or_unsupported_research"])
    try:
        score = weighted_score(review.get("scores"))
    except ValueError:
        return result("generation_error", ["invalid_review_scores"])
    if review["status"] != "pass":
        return result("skip_quality", review.get("reasons") or ["review_not_passed"])
    dims = review["scores"]
    if score < 7 or any(dims[k] < 7 for k in ("utility", "depth", "credibility")) or dims["audience"] < 6:
        return result("skip_quality", ["below_quality_threshold"], score=score)
    return result("ready", [], score=score)
