# -*- coding: utf-8 -*-
"""发帖凭据轮换的 state 盘持久化单测（2026-10-09 daily 首发翻车根治）。

集市 auth 是一次一换的轮换制：refresh 成功返回新 refresh_token、旧 token 立即
作废。秋坞 pod 模式下 BASE_DIR/.env 属于 clone、随 pod 蒸发——轮换值必须落到
state 盘（data/runtime_env.json），启动时读回且优先级高于 env/.env。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config, publisher


class TestApplyStateOverrides(unittest.TestCase):
    def _settings(self, data_dir: Path, **kw) -> config.Settings:
        return config.Settings(data_dir=data_dir, **kw)

    def test_state_json_wins_over_seed(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._settings(Path(td), market_refresh_token="seed-old")
            (Path(td) / "runtime_env.json").write_text(
                json.dumps({"market_refresh_token": "rotated-new",
                            "market_base_url": "http://172.18.0.6:8080"}),
                encoding="utf-8")
            config._apply_state_overrides(s)
            self.assertEqual(s.market_refresh_token, "rotated-new")
            self.assertEqual(s.market_base_url, "http://172.18.0.6:8080")

    def test_missing_or_broken_file_keeps_seed(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._settings(Path(td), market_refresh_token="seed-old")
            config._apply_state_overrides(s)  # 文件不存在
            self.assertEqual(s.market_refresh_token, "seed-old")
            (Path(td) / "runtime_env.json").write_text("{broken", encoding="utf-8")
            config._apply_state_overrides(s)  # 文件损坏
            self.assertEqual(s.market_refresh_token, "seed-old")

    def test_unknown_keys_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            s = self._settings(Path(td), market_refresh_token="seed-old")
            (Path(td) / "runtime_env.json").write_text(
                json.dumps({"MARKET_REFRESH_TOKEN": "should-not-map",
                            "market_user_telephone": "hijack",
                            "top_n": "999"}), encoding="utf-8")
            config._apply_state_overrides(s)
            self.assertEqual(s.market_refresh_token, "seed-old")  # 大写键不映射
            self.assertEqual(s.market_user_telephone, "")          # 白名单外不动
            self.assertEqual(s.top_n, 15)


class TestPersistStateValue(unittest.TestCase):
    def test_rotation_lands_in_state_json(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(publisher.settings, "data_dir", Path(td)):
                publisher._persist_state_value("MARKET_REFRESH_TOKEN", "gen-B")
                publisher._persist_state_value("MARKET_REFRESH_TOKEN", "gen-C")
                publisher._persist_state_value("DEEPSEEK_API_KEY", "ignored")
                data = json.loads((Path(td) / "runtime_env.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"MARKET_REFRESH_TOKEN": "gen-C"})

    def test_whitelisted_base_url(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(publisher.settings, "data_dir", Path(td)):
                publisher._persist_state_value("MARKET_BASE_URL", "http://172.18.0.6:8080")
                data = json.loads((Path(td) / "runtime_env.json").read_text(encoding="utf-8"))
        self.assertEqual(data["MARKET_BASE_URL"], "http://172.18.0.6:8080")


if __name__ == "__main__":
    unittest.main()
