# -*- coding: utf-8 -*-
"""渲染单测：主题行要能一眼看出好坏；HTML 必须包含全部板块与建议。"""
import unittest
from datetime import datetime
from pathlib import Path
import tempfile

from ops_report import model, render


def make_report(assist_status="critical"):
    rep = model.Report(generated_at="2026-09-20T21:30:00",
                       window_start="2026-09-18T21:30:00",
                       window_end="2026-09-20T21:30:00",
                       pending_alerts=["上次邮件发送失败：SMTP 认证错误"])
    d = model.Section(key="digest", name="自动发帖机器人", status="ok",
                      metrics={"应发档期": "2 次", "发帖成功率": "100%"})
    a = model.Section(key="assist", name="自动回复 AI", status=assist_status,
                      metrics={"请求成功率": "0%", "请求总数": "14577 次"})
    a.errors = [model.ErrorCluster(signature="upstream prematurely closed connection",
                                   count=13223, first_ts="t1", last_ts="t2",
                                   sample="upstream prematurely closed connection",
                                   source="nginx")]
    a.advice = [model.Advice(title="进程不在", action="1) 进 pod 2) 起服务",
                              origin="knowledge")]
    rep.sections = [d, a]
    return rep


class TestSubject(unittest.TestCase):
    def test_subject_has_headline_metrics_and_flag(self):
        s = render.subject(make_report())
        self.assertIn("发帖成功率 100%", s)
        self.assertIn("请求成功率 0%", s)
        self.assertIn("🔴", s)

    def test_ok_report_has_no_flag(self):
        s = render.subject(make_report(assist_status="ok"))
        self.assertNotIn("🔴", s)
        self.assertNotIn("⚠️", s)


class TestRenderHtml(unittest.TestCase):
    def test_contains_sections_metrics_errors_advice(self):
        html = render.render_html(make_report())
        self.assertIn("自动发帖机器人", html)
        self.assertIn("自动回复 AI", html)
        self.assertIn("发帖成功率", html)
        self.assertIn("×13223", html)
        self.assertIn("1) 进 pod 2) 起服务", html)

    def test_pending_alert_is_highlighted(self):
        html = render.render_html(make_report())
        self.assertIn("上次邮件发送失败", html)

    def test_llm_advice_rendered_once_not_per_section(self):
        """LLM 叙事全局一份（T8 单次调用设计）——板块下重复 3 次会淹没真正的信息。"""
        rep = make_report()
        for s in rep.sections:
            s.advice.append(model.Advice(title="LLM 分析与建议",
                                         action="同一份 LLM 文本", origin="llm"))
        html = render.render_html(rep)
        self.assertEqual(html.count("同一份 LLM 文本"), 1)

    def test_expected_errors_are_not_listed_as_problems(self):
        rep = make_report()
        rep.sections[1].errors.append(model.ErrorCluster(
            signature="周末只抓取入库、不发帖", count=5, first_ts="t", last_ts="t",
            sample="周末只抓取入库、不发帖", source="digest_log", expected=True))
        html = render.render_html(rep)
        self.assertNotIn("周末只抓取入库、不发帖", html)

    def test_html_is_escaped(self):
        rep = make_report()
        rep.sections[0].metrics["标题"] = "<script>alert(1)</script>"
        html = render.render_html(rep)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestAdviceMarkdownToEmail(unittest.TestCase):
    """建议文本（LLM/知识库）里的 markdown 痕迹必须转成邮件友好 HTML——
    邮箱里出现裸 **、#、反引号就是渲染事故（2026-09-28 实际发生的 case）。"""

    def llm_report(self, action):
        rep = make_report()
        rep.sections[0].advice.append(model.Advice(
            title="LLM 分析与建议", action=action, origin="llm"))
        return rep

    def test_bold_markdown_converted_no_asterisks(self):
        html = render.render_html(self.llm_report("**结论：需立即处理。** 磁盘 91%。"))
        self.assertIn("<b>结论：需立即处理。</b>", html)
        self.assertNotIn("**", html)

    def test_inline_code_converted_no_backticks(self):
        html = render.render_html(self.llm_report("执行 `du -sh /var/log` 定位大文件。"))
        self.assertIn("du -sh /var/log", html)
        self.assertNotIn("`", html)

    def test_heading_hashes_stripped_text_kept(self):
        html = render.render_html(self.llm_report("## 运维建议\n先清磁盘。"))
        self.assertNotIn("##", html)
        self.assertIn("运维建议", html)
        self.assertIn("先清磁盘。", html)

    def test_dash_and_star_bullets_become_bullet_dots(self):
        html = render.render_html(self.llm_report(
            "*   **清理旧日志**\n- 检查备用通道"))
        self.assertIn("•", html)
        self.assertNotIn("*   ", html)
        self.assertNotIn("- 检查", html)
        self.assertIn("<b>清理旧日志</b>", html)

    def test_numbered_items_keep_number_as_text(self):
        html = render.render_html(self.llm_report("1.  **P0 清理磁盘**\n2) 监控通道"))
        self.assertIn(">1. <b>P0 清理磁盘</b>", html)
        self.assertIn("2) 监控通道", html)

    def test_conversion_still_escapes_html(self):
        html = render.render_html(self.llm_report("**x** <script>alert(1)</script>"))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_blank_lines_make_paragraph_gaps(self):
        html = render.render_html(self.llm_report("第一段。\n\n第二段。"))
        self.assertIn("第一段。", html)
        self.assertIn("第二段。", html)
        self.assertNotIn("<pre", html.split("综合分析与建议", 1)[1].split("</div></div>", 1)[0])


class TestNotesValidList(unittest.TestCase):
    def test_notes_li_wrapped_in_ul(self):
        rep = make_report()
        rep.sections[0].notes = ["磁盘 91% 已达告警线"]
        html = render.render_html(rep)
        self.assertIn('<ul style="margin:6px 0 0 0;padding-left:18px;">'
                      "<li>磁盘 91% 已达告警线</li></ul>", html)


class TestArchive(unittest.TestCase):
    def test_writes_html_and_json(self):
        with tempfile.TemporaryDirectory() as td:
            rep = make_report()
            h, j = render.write_archive(rep, Path(td), datetime(2026, 9, 22, 21, 30))
            self.assertTrue(h.exists() and j.exists())
            self.assertEqual(h.name, "2026-09-22.html")
            self.assertEqual(j.name, "2026-09-22.json")
            self.assertIn("自动回复 AI", h.read_text(encoding="utf-8"))
            self.assertIn("assist", j.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
