# -*- coding: utf-8 -*-
"""日报人工倾向：preference.json 读取与容错（任何异常 → 默认模式，不炸主流程）。"""
import json
import tempfile
import unittest
from pathlib import Path

from app.preference import load_preference


class TestLoadPreference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preference.json"

    def _write(self, content: str) -> None:
        self.path.write_text(content, encoding="utf-8")

    def test_missing_file_returns_default(self):
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "default")
        self.assertEqual(pref.text, "")

    def test_valid_prefer_roundtrip(self):
        self._write(json.dumps({"mode": "prefer", "text": "jev 相关内容优先",
                                "updated_at": "2026-09-22T10:00:00",
                                "updated_by": "Rimuru"}, ensure_ascii=False))
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "prefer")
        self.assertEqual(pref.text, "jev 相关内容优先")
        self.assertEqual(pref.updated_by, "Rimuru")
        self.assertTrue(pref.active)

    def test_broken_json_falls_back_to_default(self):
        self._write("{不是 JSON")
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "default")
        self.assertFalse(pref.active)

    def test_illegal_mode_falls_back_to_default(self):
        self._write(json.dumps({"mode": "always-jev", "text": "x"}))
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_prefer_with_empty_text_falls_back_to_default(self):
        self._write(json.dumps({"mode": "prefer", "text": "   "}))
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_prefer_with_overlong_text_falls_back_to_default(self):
        self._write(json.dumps({"mode": "prefer", "text": "好" * 201}))
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_default_mode_ignores_text(self):
        self._write(json.dumps({"mode": "default", "text": "残留文本"}))
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "default")
        self.assertFalse(pref.active)

    def test_json_array_falls_back_to_default(self):
        self._write("[1, 2]")
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_fallback_logs_warning(self):
        self._write("{坏")
        with self.assertLogs("tech-digest.preference", level="WARNING") as cm:
            load_preference(self.path)
        self.assertTrue(any("回退默认模式" in m for m in cm.output))


class TestResolveMember(unittest.TestCase):
    def _resolve(self, fetch, token="tok", base="http://127.0.0.1:8080"):
        from app.preference import resolve_member
        return resolve_member(token, base, fetch_json=fetch)

    def test_resolves_name_from_data_user(self):
        def fetch(url, token, timeout):
            self.assertEqual(url, "http://127.0.0.1:8080/api/auth/info")
            self.assertEqual(token, "tok")
            return {"code": 0, "data": {"user": {"name": "Rimuru"}}}
        self.assertEqual(self._resolve(fetch), "Rimuru")

    def test_falls_back_to_alt_path_on_404(self):
        def fetch(url, token, timeout):
            if url.endswith("/api/auth/info"):
                return None          # 404
            return {"data": {"user": {"name": "Rimuru"}}}
        self.assertEqual(self._resolve(fetch), "Rimuru")

    def test_empty_token_returns_none_without_calling(self):
        calls = []
        def fetch(url, token, timeout):
            calls.append(url)
            return {"data": {"user": {"name": "Rimuru"}}}
        self.assertIsNone(self._resolve(fetch, token=""))
        self.assertEqual(calls, [])

    def test_network_error_returns_none(self):
        def fetch(url, token, timeout):
            raise OSError("connection refused")
        self.assertIsNone(self._resolve(fetch))

    def test_http_403_returns_none(self):
        # 非 404 的 HTTPError 由 _urllib_get_json 抛出——这里模拟其效果
        def fetch(url, token, timeout):
            raise OSError("HTTP 403")
        self.assertIsNone(self._resolve(fetch))

    def test_empty_name_returns_none(self):
        def fetch(url, token, timeout):
            return {"data": {"user": {"name": ""}}}
        self.assertIsNone(self._resolve(fetch))


class TestSavePreference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preference.json"

    def test_save_prefer_roundtrip_atomic(self):
        from app.preference import save_preference
        pref = save_preference("prefer", "jev 相关内容优先", "Rimuru", path=self.path)
        self.assertEqual(pref.mode, "prefer")
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["text"], "jev 相关内容优先")
        self.assertEqual(on_disk["updated_by"], "Rimuru")
        self.assertTrue(on_disk["updated_at"])
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_save_default_clears_text(self):
        from app.preference import save_preference
        save_preference("default", "残留", "Rimuru", path=self.path)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["mode"], "default")
        self.assertEqual(on_disk["text"], "")

    def test_save_rejects_bad_params(self):
        from app.preference import save_preference
        with self.assertRaises(ValueError):
            save_preference("always", "x", "Rimuru", path=self.path)
        with self.assertRaises(ValueError):
            save_preference("prefer", "  ", "Rimuru", path=self.path)
        with self.assertRaises(ValueError):
            save_preference("prefer", "好" * 201, "Rimuru", path=self.path)
