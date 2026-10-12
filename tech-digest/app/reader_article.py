"""Pure research-article contracts, evidence checks and Markdown validation."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

SCHEMA_VERSION = 2
RUBRIC_VERSION = "research-v1"
MAX_PUBLICATION_CHARS = 9000
ARTICLE_TYPES = frozenset({"research", "mechanism", "experiment"})
EVIDENCE_KINDS = frozenset({"empirical", "theoretical", "engineering_measurement"})
PUBLICATION_STATUSES = frozenset({"peer_reviewed", "preprint", "technical_report", "engineering_blog", "unknown"})
FATAL_SOURCE_ISSUES = frozenset({
    "abstract_only", "fulltext_required", "pdf_fulltext_required", "login_required",
    "empty_content", "truncated", "extract_truncated", "missing_method", "missing_support",
    "critical_figure_missing", "missing_critical_figure", "unsupported_content_type",
})


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def draft_hash(title: str, markdown: str) -> str:
    """Bind the exact final title and body, including whitespace and attribution."""
    return source_hash(title + "\n" + markdown)


def publication_markdown_issues(markdown: str) -> list[str]:
    """The reviewed daily string must survive the legacy publisher unchanged."""
    if not isinstance(markdown, str):
        return ["empty_markdown"]
    errors = []
    if markdown != markdown.strip():
        errors.append("publication_body_not_normalized")
    if len(markdown) > MAX_PUBLICATION_CHARS:
        errors.append("content_too_long_for_publication")
    return errors


def source_metadata_hash(packet: dict) -> str:
    """Bind reviewed provenance/structure; crawl time is deliberately excluded."""
    fields = ("url", "author", "title", "published_at", "publication_status", "source_version",
              "original_paper_url", "secondary_url", "method", "issues", "blocks")
    return source_hash(json.dumps({k: packet.get(k) for k in fields}, ensure_ascii=False, sort_keys=True))


def public_url(value) -> bool:
    if not isinstance(value, str) or any(c in value for c in "\r\n\t"):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                    and not parsed.username and not parsed.password)
    except ValueError:
        return False


def _without_code(text: str) -> tuple[str, bool]:
    visible, fence = [], None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is None:
            if match:
                fence = match.group(1)
            else:
                visible.append(line)
        elif match and match.group(1)[0] == fence[0] \
                and len(match.group(1)) >= len(fence) and not match.group(2).strip():
            fence = None
    return re.sub(r"(`+).*?\1", "", "\n".join(visible)), fence is not None


def markdown_issues(markdown: str) -> list[str]:
    if not isinstance(markdown, str) or not markdown.strip():
        return ["empty_markdown"]
    visible, unclosed = _without_code(markdown)
    issues = []
    if unclosed:
        issues.append("unclosed_code_fence")
    if re.search(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>", visible) \
            or re.search(r"(?i)\]\(\s*(?:javascript|data|file):", visible):
        issues.append("unsupported_markup")
    if re.search(r"\b(?:TODO|TBD)\b|\[待填[^\]]*\]|\[占位[^\]]*\]", visible, re.I):
        issues.append("editorial_placeholder")
    return issues


def source_issues(packet: dict) -> list[str]:
    if not isinstance(packet, dict):
        return ["missing_source"]
    errors = []
    text = packet.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append("empty_content")
    elif packet.get("source_hash") != source_hash(text):
        errors.append("source_hash_mismatch")
    if not public_url(packet.get("url")):
        errors.append("invalid_source_url")
    if not isinstance(packet.get("evidence_kind"), str) or packet["evidence_kind"] not in EVIDENCE_KINDS:
        errors.append("invalid_evidence_kind")
    raw_issues = packet.get("issues", [])
    if not isinstance(raw_issues, list):
        errors.append("invalid_source_issues")
    else:
        errors.extend(i for i in raw_issues if isinstance(i, str) and i in FATAL_SOURCE_ISSUES)
    if not isinstance(packet.get("publication_status", "unknown"), str) or packet.get("publication_status", "unknown") not in PUBLICATION_STATUSES:
        errors.append("invalid_publication_status")
    blocks = packet.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        errors.append("missing_source_blocks")
    else:
        ids = []
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("id"), str) \
                    or not block.get("id") or not isinstance(block.get("text"), str) \
                    or not block.get("text", "").strip():
                errors.append("invalid_source_block")
                continue
            ids.append(block["id"])
            if isinstance(text, str) and block["text"] not in text:
                errors.append("unbound_source_block")
        if len(ids) != len(set(ids)):
            errors.append("duplicate_source_block")
    return list(dict.fromkeys(errors))


def validate_draft(draft: dict, source_packet: dict) -> list[str]:
    """Validate provenance and structure; a semantic final review remains necessary."""
    errors = source_issues(source_packet)
    if not isinstance(source_packet, dict):
        return errors
    if not isinstance(draft, dict):
        return errors + ["missing_draft"]
    if draft.get("schema_version") != SCHEMA_VERSION:
        errors.append("invalid_schema_version")
    if not isinstance(draft.get("article_type"), str) or draft["article_type"] not in ARTICLE_TYPES:
        errors.append("excluded_article_type")
    for field in ("title", "learning_question", "opening", "takeaway", "limitation"):
        if not isinstance(draft.get(field), str) or not draft[field].strip():
            errors.append("missing_" + field)
    if not isinstance(draft.get("prerequisites"), list) \
            or any(not isinstance(p, str) for p in draft.get("prerequisites", [])):
        errors.append("invalid_prerequisites")
    if draft.get("source_url") != source_packet.get("url") \
            or draft.get("source_hash") != source_packet.get("source_hash"):
        errors.append("draft_source_mismatch")
    checks = draft.get("research_checks")
    if not isinstance(checks, dict) or any(not isinstance(checks.get(k), str) or not checks[k].strip()
                                          for k in ("problem", "method", "support", "conditions", "limits")):
        errors.append("missing_research_checks")

    raw_blocks = source_packet.get("blocks")
    blocks = {b["id"]: b for b in (raw_blocks if isinstance(raw_blocks, list) else [])
              if isinstance(b, dict) and isinstance(b.get("id"), str)}
    evidence = source_packet.get("evidence")
    valid_ids = set()
    if not isinstance(evidence, list):
        evidence = []
    for e in evidence:
        if not isinstance(e, dict):
            errors.append("missing_evidence")
            continue
        block_id = e.get("block_id")
        block = blocks.get(block_id) if isinstance(block_id, str) else None
        eid = e.get("id")
        if not isinstance(eid, str) or not eid or eid in valid_ids or not block \
                or not isinstance(e.get("statement"), str) or not e["statement"].strip():
            errors.append("missing_evidence")
            continue
        if e.get("quote") and (not isinstance(e["quote"], str) or e["quote"] not in block.get("text", "")):
            errors.append("unbound_evidence_quote")
            continue
        valid_ids.add(eid)
    refs = draft.get("evidence_ids")
    if not isinstance(refs, list) or not refs or any(not isinstance(e, str) or e not in valid_ids for e in refs):
        errors.append("missing_evidence")
    sections = draft.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("missing_sections")
    else:
        section_refs = set()
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("heading"), str) \
                    or not section.get("heading", "").strip() \
                    or not isinstance(section.get("markdown"), str) or not section.get("markdown", "").strip():
                errors.append("invalid_section")
                continue
            ids = section.get("evidence_ids", [])
            if not isinstance(ids, list) or any(not isinstance(e, str) or e not in valid_ids for e in ids):
                errors.append("missing_evidence")
            else:
                section_refs.update(ids)
            errors.extend(markdown_issues(section["markdown"]))
        if not section_refs:
            errors.append("missing_evidence")
    status = draft.get("test_status")
    if not isinstance(status, str) or status not in {"not_independently_tested", "independently_tested"}:
        errors.append("invalid_test_status")
    elif status == "independently_tested" and not draft.get("test_record"):
        errors.append("missing_test_record")
    for field in ("opening", "limitation", "takeaway", "title"):
        if isinstance(draft.get(field), str):
            errors.extend(markdown_issues(draft[field]))
    return list(dict.fromkeys(errors))
