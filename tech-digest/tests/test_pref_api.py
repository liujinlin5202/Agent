# -*- coding: utf-8 -*-
"""偏好 API 单测：白名单 fail closed + 原子写 + 审计行。"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manual_server
from app import preference


class PrefApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pref_path = Path(self.tmp.name) / "preference.json"
        self.log_path = Path(self.tmp.name) / "tech-digest.log"
        p1 = mock.patch.object(manual_server, "LOG_FILE", self.log_path)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(preference, "preference_path",
                               return_value=self.pref_path)
        p2.start()
        self.addCleanup(p2.stop)
        # daily_post_id 查询默认打桩为 None，避免单测触碰真实 store
        p3 = mock.patch.object(manual_server, "_store_for_pref")
        m_store = p3.start()
        m_store.return_value.daily_post_id.return_value = None
        self.addCleanup(p3.stop)
        # 集市 base 固定为假值，单测不依赖真实环境（market_base 在服务器上可能 shell docker inspect）
        p4 = mock.patch.object(manual_server, "_pref_base_url",
                               return_value="http://127.0.0.1:8080")
        p4.start()
        self.addCleanup(p4.stop)

    def _allow(self, name="Rimuru"):
        p = mock.patch.object(preference, "resolve_member", return_value=name)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"DIGEST_PREF_ADMINS": name})
        e.start()
        self.addCleanup(e.stop)

    def _deny(self):
        p = mock.patch.object(preference, "resolve_member", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    # ---- GET ----

    def test_get_403_without_token(self):
        code, _ = manual_server.handle_pref_get("")
        self.assertEqual(code, 403)

    def test_get_403_when_resolve_fails(self):
        self._deny()
        code, _ = manual_server.handle_pref_get("Bearer bad")
        self.assertEqual(code, 403)

    def test_get_403_when_not_admin(self):
        p = mock.patch.object(preference, "resolve_member", return_value="路人")
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.dict(os.environ, {"DIGEST_PREF_ADMINS": "Rimuru"}):
            code, _ = manual_server.handle_pref_get("Bearer ok")
        self.assertEqual(code, 403)

    def test_get_returns_default_when_uninitialized(self):
        self._allow()
        code, body = manual_server.handle_pref_get("Bearer ok")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["mode"], "default")
        self.assertEqual(data["text"], "")

    def test_get_includes_latest_daily_post_id(self):
        self._allow()
        with mock.patch.object(manual_server, "_latest_daily_post_id",
                               return_value=12345):
            code, body = manual_server.handle_pref_get("Bearer ok")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["daily_post_id"], 12345)

    def test_latest_daily_post_id_falls_back_to_yesterday(self):
        from datetime import date, timedelta
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with mock.patch.object(manual_server, "_store_for_pref") as mf:
            store = mock.MagicMock()
            store.daily_post_id.side_effect = lambda d: 777 if d == yesterday else None
            mf.return_value = store
            self.assertEqual(manual_server._latest_daily_post_id(), 777)
            store.daily_post_id.assert_any_call(today)
            store.daily_post_id.assert_any_call(yesterday)

    def test_latest_daily_post_id_spans_weekend_gap(self):
        """周末 daily 不发帖：回看窗口须跨到更早的工作日（周五帖周日/周一早仍命中）。"""
        from datetime import date, timedelta
        target = (date.today() - timedelta(days=3)).isoformat()
        with mock.patch.object(manual_server, "_store_for_pref") as mf:
            store = mock.MagicMock()
            store.daily_post_id.side_effect = lambda d: 6475 if d == target else None
            mf.return_value = store
            self.assertEqual(manual_server._latest_daily_post_id(), 6475)

    def test_latest_daily_post_id_none_when_store_raises(self):
        with mock.patch.object(manual_server, "_store_for_pref",
                               side_effect=RuntimeError("db broken")):
            self.assertIsNone(manual_server._latest_daily_post_id())

    # ---- POST ----

    def test_post_roundtrip_and_audit(self):
        self._allow()
        code, body = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "prefer", "text": "jev 相关"}).encode("utf-8"))
        self.assertEqual(code, 200)
        on_disk = json.loads(self.pref_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["mode"], "prefer")
        self.assertEqual(on_disk["updated_by"], "Rimuru")
        audit = self.log_path.read_text(encoding="utf-8")
        self.assertIn("[pref-audit] Rimuru mode=prefer text=jev 相关", audit)

    def test_post_audit_escapes_newline_text_kept_raw(self):
        self._allow()
        code, _ = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "prefer", "text": "a\nb"}).encode("utf-8"))
        self.assertEqual(code, 200)
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)          # 审计恰好一行，换行不得裂行
        self.assertIn("[pref-audit] Rimuru mode=prefer text=a\\nb", lines[0])
        # 转义只在输出层：落盘的 text 保留真实换行
        self.assertEqual(
            json.loads(self.pref_path.read_text(encoding="utf-8"))["text"], "a\nb")

    def test_post_400_bad_mode(self):
        self._allow()
        code, msg = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "always", "text": "x"}).encode("utf-8"))
        self.assertEqual(code, 400)
        self.assertIn("mode", msg)

    def test_post_400_prefer_empty_text(self):
        self._allow()
        code, _ = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "prefer", "text": "  "}).encode("utf-8"))
        self.assertEqual(code, 400)

    def test_post_400_non_json_body(self):
        self._allow()
        code, _ = manual_server.handle_pref_post("Bearer ok", b"not json")
        self.assertEqual(code, 400)

    def _drive_post(self, length_header: str) -> tuple[int, str]:
        """无 socket 直接驱动 Handler.do_POST，返回 (状态码, 响应体)。"""
        h = manual_server.Handler.__new__(manual_server.Handler)
        h.path = manual_server.PREF_PATH
        h.request_version = "HTTP/1.0"
        h.requestline = "POST /api/v1/digest-pref HTTP/1.0"
        h.headers = {"Content-Length": length_header, "Authorization": "Bearer ok"}
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        h.do_POST()
        head, _, payload = h.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), payload.decode("utf-8")

    def test_post_400_non_numeric_content_length(self):
        # 非数字头（改前 int() 裸抛 ValueError，连接被断，客户端收不到任何响应）
        status, msg = self._drive_post("abc")
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", msg)
        # 负值维持原行为：不读 body → 空 body 走参数校验 → 400
        self._allow()
        status2, msg2 = self._drive_post("-5")
        self.assertEqual(status2, 400)
        self.assertIn("body", msg2)

    def test_post_403_when_resolve_raises(self):
        p = mock.patch.object(preference, "resolve_member",
                              side_effect=RuntimeError("boom"))
        p.start()
        self.addCleanup(p.stop)
        code, _ = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "prefer", "text": "x"}).encode("utf-8"))
        self.assertEqual(code, 403)

    def test_post_403_when_not_admin(self):
        p = mock.patch.object(preference, "resolve_member", return_value="路人")
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.dict(os.environ, {"DIGEST_PREF_ADMINS": "Rimuru"}):
            code, _ = manual_server.handle_pref_post(
                "Bearer ok", json.dumps({"mode": "prefer", "text": "x"}).encode("utf-8"))
        self.assertEqual(code, 403)
        self.assertFalse(self.pref_path.exists())   # 拒绝时绝不落盘
