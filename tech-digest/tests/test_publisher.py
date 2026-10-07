# -*- coding: utf-8 -*-
"""publisher 单测：容器 IP 漂移自愈（docker inspect 解析 + .env 回写）、refresh 轮换持久化。

全部 mock，不碰真实 docker / 网络 / .env。
"""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import publisher


def docker_ok(ip: str) -> mock.Mock:
    m = mock.Mock()
    m.stdout = ip + "\n"
    m.returncode = 0
    return m


class TestBaseSelfHeal(unittest.TestCase):
    """_base()：配置指向容器网段 → docker inspect 动态解析，漂移即回写。"""

    def test_ip_drift_rebases(self):
        """.env 指向 172.18.0.5（已漂移）→ 解析出 .6，更新内存 + 持久化。"""
        with mock.patch.object(publisher.settings, "market_base_url",
                               "http://172.18.0.5:8080"), \
             mock.patch.object(publisher.subprocess, "run",
                               return_value=docker_ok("172.18.0.6")), \
             mock.patch.object(publisher, "_persist_env_value") as persist:
            base = publisher._base()
        self.assertEqual(base, "http://172.18.0.6:8080")
        persist.assert_called_once_with("MARKET_BASE_URL", "http://172.18.0.6:8080")

    def test_no_drift_unchanged(self):
        """IP 未漂移：原样返回，不写 .env。"""
        with mock.patch.object(publisher.settings, "market_base_url",
                               "http://172.18.0.6:8080"), \
             mock.patch.object(publisher.subprocess, "run",
                               return_value=docker_ok("172.18.0.6")), \
             mock.patch.object(publisher, "_persist_env_value") as persist:
            self.assertEqual(publisher._base(), "http://172.18.0.6:8080")
        persist.assert_not_called()

    def test_explicit_host_base_untouched(self):
        """显式配置非容器网段（127.0.0.1/域名）：不查 docker、不改写。"""
        for base in ("http://127.0.0.1:8080", "https://api.example.com"):
            with mock.patch.object(publisher.settings, "market_base_url", base), \
                 mock.patch.object(publisher.subprocess, "run") as drun, \
                 mock.patch.object(publisher, "_persist_env_value") as persist:
                self.assertEqual(publisher._base(), base)
            drun.assert_not_called()
            persist.assert_not_called()

    def test_docker_fail_fallback(self):
        """docker 不可用（inspect 抛错/超时/空输出）→ 沿用配置值。"""
        for side in (FileNotFoundError("docker not found"),
                     subprocess.CalledProcessError(1, "docker"),
                     subprocess.TimeoutExpired("docker", 5)):
            with mock.patch.object(publisher.settings, "market_base_url",
                                   "http://172.18.0.5:8080"), \
                 mock.patch.object(publisher.subprocess, "run", side_effect=side):
                self.assertEqual(publisher._base(), "http://172.18.0.5:8080")
        # inspect 成功但容器无 IP（已停止）→ 沿用配置值
        with mock.patch.object(publisher.settings, "market_base_url",
                               "http://172.18.0.5:8080"), \
             mock.patch.object(publisher.subprocess, "run",
                               return_value=docker_ok("")), \
             mock.patch.object(publisher, "_persist_env_value") as persist:
            self.assertEqual(publisher._base(), "http://172.18.0.5:8080")
        persist.assert_not_called()

    def test_empty_config_resolves(self):
        """MARKET_BASE_URL 未配置：默认值死 IP 也会被动态解析救活。"""
        with mock.patch.object(publisher.settings, "market_base_url", ""), \
             mock.patch.object(publisher.subprocess, "run",
                               return_value=docker_ok("172.18.0.6")), \
             mock.patch.object(publisher, "_persist_env_value") as persist:
            self.assertEqual(publisher._base(), "http://172.18.0.6:8080")
        persist.assert_called_once()


class TestRefreshRotation(unittest.TestCase):
    """refresh 成功且返回新 refresh_token → 必须立即持久化（单次轮换，丢了就得重新登录）。"""

    def test_refresh_persists_rotation(self):
        with mock.patch.object(publisher, "_base",
                               return_value="http://172.18.0.6:8080"), \
             mock.patch.object(publisher.settings, "market_refresh_token", "old-rt"), \
             mock.patch.object(publisher.requests, "post") as post, \
             mock.patch.object(publisher, "_persist_env_value") as persist:
            post.return_value = mock.Mock(status_code=200)
            post.return_value.json.return_value = {
                "code": 200, "msg": "刷新token成功",
                "token": "at", "refresh_token": "new-rt"}
            result = publisher.refresh_access_token()
        self.assertEqual(result, ("at", "new-rt"))
        persist.assert_called_once_with("MARKET_REFRESH_TOKEN", "new-rt")

    def test_refresh_no_new_token_no_persist(self):
        with mock.patch.object(publisher, "_base",
                               return_value="http://172.18.0.6:8080"), \
             mock.patch.object(publisher.settings, "market_refresh_token", "old-rt"), \
             mock.patch.object(publisher.requests, "post") as post, \
             mock.patch.object(publisher, "_persist_env_value") as persist:
            post.return_value = mock.Mock(status_code=200)
            post.return_value.json.return_value = {"token": "at"}
            result = publisher.refresh_access_token()
        self.assertEqual(result, ("at", None))
        persist.assert_not_called()


@unittest.skipIf(os.name == "nt", "权限位只在 Linux 上有意义（Windows 无 chmod 语义）")
class TestPersistEnvPermissions(unittest.TestCase):
    """回写 .env 必须保持原文件权限：600 含 API key/token（2026-09-20 事故：tmp.replace 造新文件变 644）。"""

    def test_env_keeps_0600_after_persist(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("A=1\n", encoding="utf-8")
            env.chmod(0o600)
            with mock.patch.object(publisher, "BASE_DIR", Path(td)):
                publisher._persist_env_value("A", "2")
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)


class TestPostComment(unittest.TestCase):
    """补楼（1 楼评论）：日报正文只放星榜 Top5，完整 15 条作为作者自评补在第 1 楼。

    端点是 /api/auth/postPcomment {content, postID, userTelephone}（2026-09-13 从集市
    前端 bundle assets/CommentToolbar-DswEeIFj.js 反查确认），与发帖同为 Bearer 鉴权。
    """

    def _call(self, resp=None, post=None, refresh=("at", "new-rt"),
              refresh_token="rt", telephone="13800000000",
              text="星榜完整版", post_id=4321):
        with mock.patch.object(publisher, "_base",
                               return_value="http://172.18.0.6:8080"), \
             mock.patch.object(publisher.settings, "market_refresh_token", refresh_token), \
             mock.patch.object(publisher.settings, "market_user_telephone", telephone), \
             mock.patch.object(publisher, "refresh_access_token", return_value=refresh), \
             mock.patch.object(publisher.requests, "post") as rp:
            rp.return_value = resp
            if post is not None:
                rp.side_effect = post
            return publisher.post_comment(post_id, text), rp

    def test_success_payload_and_url(self):
        resp = mock.Mock(ok=True, status_code=200)
        (ok, err), rp = self._call(resp=resp, text="📈 星榜完整版")
        self.assertTrue(ok)
        self.assertIsNone(err)
        args, kwargs = rp.call_args
        self.assertEqual(args[0], "http://172.18.0.6:8080/api/auth/postPcomment")
        self.assertEqual(kwargs["json"], {"content": "📈 星榜完整版", "postID": 4321,
                                          "userTelephone": "13800000000"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer at")

    def test_http_error_reported(self):
        resp = mock.Mock(ok=False, status_code=401)
        (ok, err), _ = self._call(resp=resp)
        self.assertFalse(ok)
        self.assertIn("401", err)

    def test_http_200_is_success_even_without_body(self):
        """后端只回 .ok（前端亦只判 .ok）；200 后再按 body 判失败会导致重试 → 重复评论。"""
        resp = mock.Mock(ok=True, status_code=200)
        resp.json.side_effect = ValueError("no body")
        (ok, err), _ = self._call(resp=resp)
        self.assertTrue(ok)

    def test_missing_credentials(self):
        for kw in ({"refresh_token": ""}, {"telephone": ""}):
            (ok, err), rp = self._call(resp=mock.Mock(ok=True), **kw)
            self.assertFalse(ok)
            self.assertIn("缺少", err)
            rp.assert_not_called()

    def test_refresh_failure(self):
        (ok, err), rp = self._call(refresh=None)
        self.assertFalse(ok)
        self.assertIn("access token", err)
        rp.assert_not_called()

    def test_exception_is_caught(self):
        (ok, err), _ = self._call(post=RuntimeError("boom"))
        self.assertFalse(ok)
        self.assertIn("boom", err)

    def test_zero_post_id_not_sent(self):
        """后端发帖成功不返回 postID（记 0）→ 不能对着 postID=0 发评论。"""
        (ok, err), rp = self._call(post_id=0)
        self.assertFalse(ok)
        self.assertIn("postID", err)
        rp.assert_not_called()

    def test_long_text_truncated_to_column(self):
        resp = mock.Mock(ok=True, status_code=200)
        (ok, _), rp = self._call(resp=resp, text="星" * 5000)
        self.assertTrue(ok)
        sent = rp.call_args.kwargs["json"]["content"]
        self.assertLessEqual(len(sent), publisher.MAX_COMMENT_CHARS)
        self.assertLessEqual(publisher.MAX_COMMENT_CHARS, 3000)   # pcomments.pctext varchar(3000)


if __name__ == "__main__":
    unittest.main()
