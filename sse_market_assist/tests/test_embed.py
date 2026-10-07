# -*- coding: utf-8 -*-
"""embed.py 单测：texts/openai 两种协议、响应解析、重试与快速失败、维度告警。

全部 mock urllib，不发真实网络请求。
"""
from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.embed as embed_mod
from app.config import settings
from app.embed import EmbeddingUnavailable, embed_texts


class FakeResp:
    def __init__(self, data: dict):
        self._data = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


def _mock_urlopen(monkeypatch, responses):
    """responses: 函数或列表。函数按序被调用；列表元素为 (FakeResp | HTTPError)。"""
    calls: list = []

    def fake(req, timeout=30):
        calls.append(req)
        resp = responses(calls) if callable(responses) else responses[len(calls) - 1]
        if isinstance(resp, urllib.error.HTTPError):
            raise resp
        return resp

    monkeypatch.setattr(embed_mod.urllib.request, "urlopen", fake)
    return calls


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "err", {}, BytesIO(body.encode("utf-8")))


VEC = [0.1] * 1024


def test_texts_mode_request_and_parse(monkeypatch):
    # 明确切回 texts 模式（.env 当前为 openai 模式），测试不依赖配置文件
    monkeypatch.setattr(settings, "embed_mode", "texts")
    monkeypatch.setattr(embed_mod, "EMBED_URL", "http://x/embed")
    calls = _mock_urlopen(monkeypatch, [FakeResp({"embeddings": [VEC, VEC]})])
    vecs = embed_texts(["你好", "世界"])
    assert vecs == [VEC, VEC]
    req = calls[0]
    assert req.full_url.endswith("/embed")  # texts 模式路径为 /embed
    assert json.loads(req.data.decode("utf-8")) == {"texts": ["你好", "世界"]}
    assert "Authorization" not in req.headers  # 平台服务无鉴权


def test_openai_mode_request_and_parse(monkeypatch):
    monkeypatch.setattr(settings, "embed_mode", "openai")
    monkeypatch.setattr(settings, "embedding_api_key", "sk-test")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(embed_mod, "EMBED_URL", "http://x/embeddings")
    calls = _mock_urlopen(monkeypatch, [FakeResp({"data": [{"embedding": VEC}]})])
    vecs = embed_texts(["a"])
    assert vecs == [VEC]
    req = calls[0]
    assert json.loads(req.data.decode("utf-8")) == {"model": "bge-m3", "input": ["a"]}
    assert req.headers["Authorization"] == "Bearer sk-test"


def test_parse_raw_list_schema(monkeypatch):
    _mock_urlopen(monkeypatch, [FakeResp([VEC])])
    assert embed_texts(["a"]) == [VEC]


def test_parse_unparseable_schema_fails_fast(monkeypatch):
    calls = _mock_urlopen(monkeypatch, [FakeResp({"unexpected": 1})])
    with pytest.raises(EmbeddingUnavailable):
        embed_texts(["a"])
    assert len(calls) == 1  # 响应已收到但结构不对 → 不重试


def test_count_mismatch_fails_fast(monkeypatch):
    calls = _mock_urlopen(monkeypatch, [FakeResp({"embeddings": [VEC]})])
    with pytest.raises(EmbeddingUnavailable):
        embed_texts(["a", "b"])
    assert len(calls) == 1


def test_retry_on_5xx_then_success(monkeypatch):
    calls = _mock_urlopen(monkeypatch, [
        _http_error(500, "boom"), _http_error(503, "boom"), FakeResp({"embeddings": [VEC]}),
    ])
    assert embed_texts(["a"]) == [VEC]
    assert len(calls) == 3


def test_4xx_fails_fast_no_retry(monkeypatch):
    calls = _mock_urlopen(monkeypatch, [_http_error(400, '{"code":"Arrearage"}')])
    with pytest.raises(EmbeddingUnavailable):
        embed_texts(["a"])
    assert len(calls) == 1


def test_network_error_retries_then_raises(monkeypatch):
    def boom(req, timeout=30):
        calls.append(req)
        raise ConnectionError("空响应（隧道后端挂）")

    calls: list = []
    monkeypatch.setattr(embed_mod.urllib.request, "urlopen", boom)
    monkeypatch.setattr(embed_mod.time, "sleep", lambda s: None)  # 不真睡
    with pytest.raises(EmbeddingUnavailable):
        embed_texts(["a"])
    assert len(calls) == 3


def test_dimension_warning_once(capsys, monkeypatch):
    _mock_urlopen(monkeypatch, [FakeResp({"embeddings": [[0.1] * 768]})])
    embed_texts(["a"])
    out = capsys.readouterr().out
    assert "警告" in out and "768" in out
    # 第二次不再重复告警
    _mock_urlopen(monkeypatch, [FakeResp({"embeddings": [[0.1] * 768]})])
    embed_texts(["b"])
    assert "警告" not in capsys.readouterr().out
