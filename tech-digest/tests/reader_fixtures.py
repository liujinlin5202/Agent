"""Small, original offline research fixtures; never use publication credentials."""
from copy import deepcopy
from hashlib import sha256


def article_packet():
    blocks = [
        {"id": "b1", "kind": "paragraph", "text": "缓存实验比较重复读取与缓存命中。"},
        {"id": "b2", "kind": "paragraph", "text": "固定输入，缓存命中减少重复读取，测量只覆盖这一输入。"},
        {"id": "b3", "kind": "paragraph", "text": "输入变化会使缓存失效，结果不能推广到所有工作负载。"},
    ]
    text = "\n\n".join(b["text"] for b in blocks)
    source = {
        "url": "https://example.org/research/cache",
        "source_hash": sha256(text.encode("utf-8")).hexdigest(),
        "text": text, "title": "缓存实验", "author": "离线样本作者",
        "published_at": None, "fetched_at": "2026-10-11T02:00:00+00:00",
        "method": "article", "publication_status": "technical_report",
        "evidence_kind": "engineering_measurement", "blocks": blocks,
        "issues": [], "evidence": [
            {"id": "e1", "block_id": "b1", "statement": blocks[0]["text"]},
            {"id": "e2", "block_id": "b2", "statement": blocks[1]["text"]},
            {"id": "e3", "block_id": "b3", "statement": blocks[2]["text"]},
        ],
    }
    draft = {
        "schema_version": 2, "article_type": "mechanism",
        "source_url": source["url"], "source_hash": source["source_hash"],
        "title": "为什么缓存能减少重复读取？",
        "learning_question": "缓存命中怎样避免重复读取？",
        "prerequisites": ["知道程序会重复读取数据"],
        "takeaway": "缓存收益依赖输入重复性和失效条件。",
        "opening": "重复读取会重复付出访问开销。这个固定输入实验解释缓存命中的收益及输入变化的边界。",
        "sections": [
            {"heading": "方法怎样比较", "markdown": "固定输入，对照缓存命中与重复读取。", "evidence_ids": ["e1"]},
            {"heading": "证据说明什么", "markdown": "测量覆盖固定输入，缓存命中减少重复读取。", "evidence_ids": ["e2"]},
        ],
        "limitation": blocks[2]["text"], "evidence_ids": ["e1", "e2", "e3"],
        "test_status": "not_independently_tested", "test_record": None,
        "research_checks": {
            "problem": blocks[0]["text"], "method": "固定输入的缓存对照",
            "support": blocks[1]["text"], "conditions": "固定输入",
            "limits": blocks[2]["text"],
        },
    }
    return deepcopy(source), deepcopy(draft)


def passing_review(title, markdown, source):
    import json
    metadata = {k: source.get(k) for k in ("url", "author", "title", "published_at", "publication_status",
                "source_version", "original_paper_url", "secondary_url", "method", "issues", "blocks")}
    return {
        "status": "pass", "scores": dict(utility=8, depth=8, credibility=8, audience=7, novelty=5),
        "reasons": [], "source_url": source["url"], "source_hash": source["source_hash"],
        "draft_hash": sha256((title + "\n" + markdown).encode("utf-8")).hexdigest(),
        "source_metadata_hash": sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "rubric_version": "research-v1", "article_type": "mechanism",
        "content_purpose": "knowledge", "source_complete": True, "evidence_supported": True,
    }
