"""Offline reader packets and subprocess isolation checks (no publisher/store)."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preview_reader_article.py"


def sample_packet(kind="empirical"):
    """Synthetic local sources: numbers illustrate the contract, not real studies."""
    bodies = {
        "empirical": (
            "离线合成研究：检索位置与答案命中的关系。作者在固定的 100 个问题上，"
            "保持答案段与两个干扰段相同，仅交换位置。答案置前命中 80 次，置中命中 60 次。"
            "命中率为命中数除以问题数，未报告置信区间或跨模型复验。位置是受控变量，"
            "这些合成数值不构成真实模型能力的证据。方法通过固定内容隔离位置影响。"
            "只能对这组固定条件比较结果，不能推断所有长上下文任务都相同。",
            "为何相同材料换个位置会改变命中率",
            "问题：材料长度相同仍会答错。此离线合成样例演示如何读取受控位置比较；"
            "其数字只是格式示例，不能推广到任何真实模型。",
            "固定内容，仅交换答案段位置；避免同时改变长度和问题难度。"
            "[原文条件](https://example.org/article)\n\n"
            "简化例子（假设，非真实实验）：A 是答案段，B、C 是干扰段，比较 ABC 与 BAC。",
            "来源报告的合成实验：固定 100 个问题；置前 80/100，置中 60/100。"
            "分母和问题集一致；差值为 20 个百分点，不能据此宣称因果机制已得到证明。\n\n"
            "| 同一问题集中的位置 | 命中数 / 总数 |\n| --- | --- |\n| 前部 | 80 / 100 |\n| 中部 | 60 / 100 |",
            "未报告置信区间、跨模型对照或随机种子；本包数字是离线合成示例。",
            "受控比较需要固定内容与指标分母；位置差异本身还不能解释注意力机制。",
        ),
        "theoretical": (
            "离线算法分析：对 n 个元素的一次扫描求最大值。假设全序比较且每次比较常数成本。"
            "循环不变式：处理前 k 个元素后 best 为此前缀最大值。初始化 k=1 成立。"
            "下一步比较 best 与 x 并保留较大者，归纳成立；终止 k=n 时 best 为全局最大值。"
            "恰需 n-1 次比较；对任意输入，每个非最大值至少要输一次比较才能排除，故下界 n-1。"
            "空输入无最大值，偏序或非恒定比较成本不满足以上假设。没有实验吞吐量或硬件数据。",
            "一次扫描为什么足以求最大值",
            "问题：求最大值是否需要排序。全序且比较为常数成本时，一次扫描就足够；"
            "证明依赖循环不变式，不能从比较次数推断硬件吞吐量。",
            "排序得到额外顺序，求最大值只需排除候选。保留前缀最大值能避免完整排序。"
            "[算法假设](https://example.org/article)\n\n"
            "简化例子（假设）：依次读取 3、1、5，best 从 3 保持 3，再变为 5。",
            "假设与推导：n ≥ 1，全序比较为常数成本。k 表示已处理元素数，best 表示前缀最大值。"
            "初始化 k=1；比较下一个元素保持不变式；终止 k=n，因而结果正确。"
            "比较数 C(n)=n-1，因为每个非最大值都须至少输一次。这里是推导，非实测结果。\n\n"
            "```text\nC(n) = n - 1\nn：非空输入元素数；C：比较次数，非耗时。\n```",
            "空输入、偏序及非恒定比较成本超出假设；来源未报告运行速度。",
            "先说明不变式与成本模型，再讨论复杂度；比较次数不等于实际耗时。",
        ),
        "engineering_measurement": (
            "离线合成工程研究：批量写入与尾延迟。条件为同一设备、单线程、1000 条到达记录，"
            "每条 1 KiB，批次大小为 1 或 10。作者说明一次 flush 成本可由同批记录分摊，"
            "但凑批会引入等待。合成测量中批次 1 的吞吐为 100 条/s、p99 为 10 ms；"
            "批次 10 的吞吐为 500 条/s、p99 为 30 ms。未报告持久化保证、设备型号或热身。"
            "无法由吞吐改善推出实时业务适用，尾延迟和可靠性约束必须另行检查。",
            "批量写入为何增吞吐却增加尾延迟",
            "问题：提高吞吐是否意味着每条请求更快。批量把固定成本分摊，却增加等候；"
            "这里使用离线合成测量，未说明持久化保证，不能直接作部署依据。",
            "令 F 为一次 flush 的固定成本，B 为每批条数；F/B 是每条的分摊成本，"
            "W 是等待凑批的时间。总延迟仍须包含 W。"
            "[测量条件](https://example.org/article)\n\n"
            "```python\n# 简化例子（假设），解释分摊关系，未执行性能测试\ndef cost_per_record(flush_cost, batch_size):\n    return flush_cost / batch_size\n```",
            "来源报告的合成测量：同一设备、单线程、1000 条，每条 1 KiB。\n\n"
            "| 批次 / 同一负载条件 | 吞吐；p99 |\n| --- | --- |\n| 1 条 | 100 条/s；10 ms |\n| 10 条 | 500 条/s；30 ms |\n\n"
            "p99 为 99% 请求延迟不超过的阈值，不能把吞吐增益写成单请求延迟改善。",
            "来源未报告设备型号、热身或持久化保证；这些数值只用于离线格式对照。",
            "批处理同时改变吞吐与等待；判断收益必须把尾延迟和持久化条件一起读。",
        ),
    }
    text, title, opening, method, support, limitation, takeaway = bodies[kind]
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source = {
        "url": "https://example.org/article", "source_hash": source_hash,
        "published_at": None, "fetched_at": "2026-10-11T02:00:00+00:00",
        "method": "site_selector", "text": text, "author": "离线合成来源",
        "publication_status": "technical_report", "original_paper_url": None,
        "evidence_kind": kind,
        "blocks": [{"id": "b1", "kind": "paragraph", "text": text}], "issues": [],
        "evidence": [{"id": "e1", "block_id": "b1", "statement": text}],
    }
    draft = {
        "schema_version": 2, "article_type": "research", "source_url": source["url"],
        "source_hash": source_hash, "title": title, "learning_question": title,
        "prerequisites": ["比例与受控比较" if kind == "empirical" else "基本算法与成本模型"],
        "takeaway": takeaway, "opening": opening,
        "sections": [
            {"heading": "已有方法瓶颈", "markdown": "回答技术问题前，先区分结论所依赖的条件与方法代价。", "evidence_ids": ["e1"]},
            {"heading": "核心方法", "markdown": method, "evidence_ids": ["e1"]},
            {"heading": "假设与推导" if kind == "theoretical" else "证据与比较条件", "markdown": support, "evidence_ids": ["e1"]},
        ],
        "limitation": limitation, "evidence_ids": ["e1"],
        "test_status": "not_independently_tested", "test_record": None,
        "research_checks": {"problem": title, "method": method, "support": support,
                            "conditions": support, "limits": limitation},
    }
    return {"source_packet": source, "draft": draft}


class TestReaderPreview(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "offline preview script must be implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="reader-preview-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "packet.json"
        self.output = self.root / "preview"
        guard = self.root / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(
            "import sys, socket\n"
            "class Guard:\n"
            "    def find_spec(self, fullname, *args):\n"
            "        if fullname in {'main', 'app.publisher', 'app.store', 'app.config'}:\n"
            "            raise RuntimeError('forbidden production import: ' + fullname)\n"
            "sys.meta_path.insert(0, Guard())\n"
            "def blocked(*args, **kwargs):\n"
            "    raise RuntimeError('offline network guard')\n"
            "socket.create_connection = blocked\n"
            "socket.socket.connect = blocked\n"
            "try:\n"
            "    import requests\n"
            "    requests.sessions.Session.request = blocked\n"
            "except ImportError:\n"
            "    pass\n",
            encoding="utf-8",
        )
        self.env = {**os.environ, "PYTHONPATH": str(guard), "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    def run_preview(self, packet, cwd=None, output=None):
        self.fixture.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), "--input", str(self.fixture),
                               "--output-dir", str(output or self.output)],
                              cwd=cwd or self.root, env=self.env, text=True,
                              encoding="utf-8", capture_output=True, timeout=30)

    def test_editorial_sample_check_is_only_structure_and_binding_validation(self):
        packet = sample_packet()
        packet["editorial_sample_only"] = True
        result = self.run_preview(packet)
        self.assertEqual(result.returncode, 0, result.stderr)
        check = json.loads((self.output / "check.json").read_text(encoding="utf-8"))
        self.assertTrue(check["valid"])
        self.assertEqual(check.get("validation_scope"), "structure_and_reference_binding_only")
        self.assertEqual(check.get("semantic_review_status"), "not_performed")
        self.assertIs(check.get("editorial_sample_only"), True)
        self.assertIs(check.get("independently_tested"), False)

    def test_real_editorial_samples_keep_version_identity_and_question_first(self):
        examples = ROOT / "docs" / "examples" / "research-reader"
        for slug in ("lost-in-the-middle", "bisect-invariant", "sqlite-small-blobs"):
            with self.subTest(sample=slug):
                packet = json.loads((examples / (slug + ".json")).read_text(encoding="utf-8"))
                source, draft = packet["source_packet"], packet["draft"]
                self.assertIs(source["fetched_at"], None)
                self.assertEqual(source["method"], "manual_evidence_excerpt")
                self.assertTrue(all(b["kind"] == "evidence_excerpt" and b["origin_url"]
                                    and b["origin_locator"] for b in source["blocks"]))
                self.assertNotIn("人工编辑样稿", draft["opening"])
                self.assertNotIn("本次未", draft["opening"])
                self.assertIn("？", draft["opening"].split("。", 1)[0])
                self.assertEqual(draft["test_status"], "not_independently_tested")
                self.assertIs(draft["test_record"], None)
                if slug == "lost-in-the-middle":
                    self.assertEqual(source["publication_status"], "preprint")
                    self.assertEqual(source["published_at"], "2023-11-20")
                    self.assertIn("后来发表于 TACL 2024", source["source_notes"])
                else:
                    self.assertIs(source["published_at"], None)
                output = self.root / slug
                result = self.run_preview(packet, output=output)
                self.assertEqual(result.returncode, 0, result.stderr)
                body = (output / "body.md").read_text(encoding="utf-8")
                self.assertIn(draft["opening"], body)
                self.assertIn("未独立复现", body)
                self.assertNotIn("人工编辑样稿", body)
                check = json.loads((output / "check.json").read_text(encoding="utf-8"))
                self.assertTrue(check["valid"])
                self.assertEqual(check["semantic_review_status"], "not_performed")
                self.assertTrue(check["editorial_sample_only"])

    def test_subprocess_isolated_from_database_issue_and_formal_outputs(self):
        db = self.root / "data" / "tech-digest.db"
        db.parent.mkdir()
        with closing(sqlite3.connect(db)) as conn:
            with conn:
                conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO kv VALUES ('issue_next', '42')")
        formal = self.root / "output"
        formal.mkdir()
        (formal / "daily.md").write_text("正式输出哨兵", encoding="utf-8")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [db, formal / "daily.md"]}
        result = self.run_preview(sample_packet())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
        self.assertEqual([p.name for p in formal.iterdir()], ["daily.md"])
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM kv WHERE key='issue_next'").fetchone()[0], "42")
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["body.md", "check.json", "preview.html"])
        body = (self.output / "body.md").read_text(encoding="utf-8")
        check = json.loads((self.output / "check.json").read_text(encoding="utf-8"))
        self.assertTrue(check["valid"])
        self.assertEqual(check["errors"], [])
        expected = hashlib.sha256((sample_packet()["draft"]["title"] + "\n" + body).encode("utf-8")).hexdigest()
        self.assertEqual(check["draft_hash"], expected)

    def test_three_evidence_kinds_keep_methods_conditions_and_reasoning_visible(self):
        for kind in ("empirical", "theoretical", "engineering_measurement"):
            with self.subTest(kind=kind):
                packet = sample_packet(kind)
                output = self.root / kind
                result = self.run_preview(packet, output=output)
                self.assertEqual(result.returncode, 0, result.stderr)
                body = (output / "body.md").read_text(encoding="utf-8")
                for section in packet["draft"]["sections"]:
                    self.assertIn(section["markdown"], body)
                self.assertNotIn("<details", body)
                page = (output / "preview.html").read_text(encoding="utf-8")
                self.assertIn("DOMPurify", page)
                self.assertNotIn('src="https://', page)
                self.assertNotIn("QiuDock", page)
                self.assertNotIn("/*__", page)

    def test_invalid_resource_is_rejected_without_preview_body(self):
        packet = sample_packet()
        packet["draft"]["article_type"] = "resource"
        result = self.run_preview(packet)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.output / "body.md").exists())
        check = json.loads((self.output / "check.json").read_text(encoding="utf-8"))
        self.assertFalse(check["valid"])
        self.assertIn("excluded_article_type", check["errors"])

    def test_long_markdown_gets_editing_advice_without_losing_code_or_takeaway(self):
        packet = sample_packet()
        code = "```python\n" + "print('保留完整代码')\n" * 600 + "```"
        packet["draft"]["sections"][1]["markdown"] += "\n\n" + code
        result = self.run_preview(packet)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = (self.output / "body.md").read_text(encoding="utf-8")
        self.assertIn(code, body)
        self.assertIn(packet["draft"]["takeaway"], body)
        check = json.loads((self.output / "check.json").read_text(encoding="utf-8"))
        self.assertIn("over_length_budget", check["warnings"])

    def test_malformed_json_returns_check_instead_of_partial_article(self):
        self.fixture.write_text("{bad json", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), "--input", str(self.fixture),
                                 "--output-dir", str(self.output)], cwd=self.root,
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.output / "body.md").exists())
        check = json.loads((self.output / "check.json").read_text(encoding="utf-8"))
        self.assertIn("invalid_input_json", check["errors"])

    def test_clean_checkout_copy_has_no_developer_preview_dependency(self):
        checkout = self.root / "checkout"
        (checkout / "app").mkdir(parents=True)
        (checkout / "scripts").mkdir()
        for name in ("__init__.py", "digest.py", "reader_article.py", "titles.py", "emoji.py"):
            shutil.copyfile(ROOT / "app" / name, checkout / "app" / name)
        shutil.copyfile(SCRIPT, checkout / "scripts" / SCRIPT.name)
        shutil.copytree(ROOT / "scripts" / "preview_assets", checkout / "scripts" / "preview_assets")
        self.fixture.write_text(json.dumps(sample_packet()), encoding="utf-8")
        result = subprocess.run([sys.executable, str(checkout / "scripts" / SCRIPT.name),
                                 "--input", str(self.fixture), "--output-dir", str(self.output)],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("来源状态", (self.output / "body.md").read_text(encoding="utf-8"))
        self.assertTrue(json.loads((self.output / "check.json").read_text(encoding="utf-8"))["valid"])

    def test_bad_rerun_removes_stale_valid_article(self):
        packet = sample_packet()
        self.assertEqual(self.run_preview(packet).returncode, 0)
        packet["source_packet"]["issues"] = ["abstract_only"]
        result = self.run_preview(packet)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.output / "body.md").exists())
        self.assertFalse((self.output / "preview.html").exists())
        check = json.loads((self.output / "check.json").read_text(encoding="utf-8"))
        self.assertIn("abstract_only", check["errors"])

    def test_refuses_formal_output_tree_in_portable_checkout(self):
        checkout = self.root / "checkout"
        (checkout / "app").mkdir(parents=True)
        (checkout / "scripts").mkdir()
        for name in ("__init__.py", "digest.py", "reader_article.py", "titles.py", "emoji.py"):
            shutil.copyfile(ROOT / "app" / name, checkout / "app" / name)
        shutil.copyfile(SCRIPT, checkout / "scripts" / SCRIPT.name)
        self.fixture.write_text(json.dumps(sample_packet()), encoding="utf-8")
        formal = checkout / "output"
        formal.mkdir()
        sentinel = formal / "daily.md"
        sentinel.write_text("正式产物不应改动", encoding="utf-8")
        result = subprocess.run([sys.executable, str(checkout / "scripts" / SCRIPT.name),
                                 "--input", str(self.fixture), "--output-dir", str(formal)],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protected_output_directory", result.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "正式产物不应改动")
        self.assertEqual([p.name for p in formal.iterdir()], ["daily.md"])


if __name__ == "__main__":
    unittest.main()
