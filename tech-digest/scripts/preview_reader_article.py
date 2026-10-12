"""Create a portable offline research preview from local JSON, never publish.

Usage: python scripts/preview_reader_article.py --input packet.json --output-dir preview
Only stdlib and the pure digest/reader_article helpers are imported. Assets ship
beside this script; the generated HTML embeds them and denies network requests.
"""
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).resolve().parent / "preview_assets"
LENGTH_BUDGET = 8000


def preview_html(markdown: str, title: str) -> str:
    template = (ASSETS / "preview-template.html").read_text(encoding="utf-8")
    values = {
        "TITLE": escape(title),
        "MARKDOWN_CSS": (ASSETS / "github-markdown-light.css").read_text(encoding="utf-8"),
        "PRISM_CSS": (ASSETS / "prism-tomorrow.css").read_text(encoding="utf-8"),
        "COMPONENT_CSS": (ASSETS / "component.css").read_text(encoding="utf-8"),
        "MARKDOWN_IT": (ASSETS / "markdown-it.min.js").read_text(encoding="utf-8"),
        "DOMPURIFY": (ASSETS / "purify.min.js").read_text(encoding="utf-8"),
        "PRISM": (ASSETS / "prism.js").read_text(encoding="utf-8"),
        "PRISM_BASH": (ASSETS / "prism-bash.min.js").read_text(encoding="utf-8"),
        "PRISM_PYTHON": (ASSETS / "prism-python.min.js").read_text(encoding="utf-8"),
        "PRISM_JSON": (ASSETS / "prism-json.min.js").read_text(encoding="utf-8"),
        "BODY_JSON": json.dumps(markdown, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"),
        "FENCE_RENDERER": (ASSETS / "fence-renderer.js").read_text(encoding="utf-8"),
        "SANITIZER": (ASSETS / "sanitizer.js").read_text(encoding="utf-8"),
    }
    # One substitution pass prevents article text resembling a placeholder from
    # being interpreted as a template directive on later replacements.
    for name in ("MARKDOWN_IT", "DOMPURIFY", "PRISM", "PRISM_BASH", "PRISM_PYTHON", "PRISM_JSON"):
        values[name] = re.sub(r"</script", r"<\\/script", values[name], flags=re.I)
    return re.sub(r"/\*__([A-Z_]+)__\*/", lambda m: values[m.group(1)], template)


def build_preview(input_path: Path, output_dir: Path) -> dict:
    """All state is caller-selected local files; no config/env/database access."""
    sys.path.insert(0, str(ROOT))
    from app.digest import render_reader_article
    from app.reader_article import SCHEMA_VERSION, RUBRIC_VERSION, draft_hash, markdown_issues, source_issues

    output_dir = output_dir.resolve()
    # Prevent accidental overwrite of the project's formal output/state trees.
    for protected in (ROOT / "output", ROOT / "data", ROOT / "app", ROOT / "archive"):
        if output_dir == protected or protected in output_dir.parents:
            raise ValueError("protected_output_directory")
    if output_dir == ROOT:
        raise ValueError("protected_output_directory")
    if input_path.resolve() in {output_dir / n for n in ("body.md", "preview.html", "check.json")}:
        raise ValueError("input_output_collision")
    output_dir.mkdir(parents=True, exist_ok=True)
    check = {"valid": False, "schema_version": SCHEMA_VERSION, "rubric_version": RUBRIC_VERSION,
             "validation_scope": "structure_and_reference_binding_only",
             "semantic_review_status": "not_performed",
             "errors": [], "warnings": [], "editorial_notes": [], "draft_hash": None,
             "rendered_characters": 0, "length_budget": LENGTH_BUDGET,
             "preview_widths": [920, 360, 320], "math_mode": "readable_text"}
    markdown, page = None, None
    try:
        try:
            packet = json.loads(input_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("invalid_input_json") from None
        if not isinstance(packet, dict) or not isinstance(packet.get("source_packet"), dict) \
                or not isinstance(packet.get("draft"), dict):
            raise ValueError("invalid_packet_structure")
        source, draft = packet["source_packet"], packet["draft"]
        if packet.get("editorial_sample_only") is True:
            check.update(editorial_sample_only=True, independently_tested=False)
        issues = source_issues(source)
        if issues:
            raise ValueError(",".join(issues))
        markdown = render_reader_article(draft, source)
        issues = markdown_issues(markdown)
        if issues:
            raise ValueError(",".join(issues))
        check["draft_hash"] = draft_hash(draft["title"], markdown)
        check["source_hash"] = source["source_hash"]
        check["rendered_characters"] = len(markdown)
        if len(markdown) > LENGTH_BUDGET:
            check["warnings"].append("over_length_budget")
            check["editorial_notes"].append("正文超出编辑预算；请精简重复解释，保留完整代码、比较条件、推理与结论。当前输出未截断。")
        if re.search(r"\$\$|\\\(|\\\[|\$[^\n$]+\$", markdown):
            check["warnings"].append("math_readability_review")
            check["editorial_notes"].append("离线预览未加载数学插件；请使用可读符号或代码块，并在正文中定义变量与假设。")
        page = preview_html(markdown, draft["title"])
        check["valid"] = True
    except ValueError as exc:
        check["errors"] = str(exc).split(",")
    except OSError:
        check["errors"] = ["local_file_error"]
    except (TypeError, KeyError, AttributeError):
        check["errors"] = ["invalid_packet_structure"]
    if check["valid"]:
        (output_dir / "body.md").write_text(markdown, encoding="utf-8")
        (output_dir / "preview.html").write_text(page, encoding="utf-8")
    else:
        # A failed rerun must not leave a stale, apparently acceptable article.
        for name in ("body.md", "preview.html"):
            (output_dir / name).unlink(missing_ok=True)
    (output_dir / "check.json").write_text(json.dumps(check, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return check


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        check = build_preview(args.input, args.output_dir)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not check["valid"]:
        print(",".join(check["errors"]), file=sys.stderr)
        return 2
    print("Created body.md, preview.html and check.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
