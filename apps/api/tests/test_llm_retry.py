"""LLM 抖动重试策略：瞬时错误同模型重试，重试不济才降级；致命错误立刻降级。"""
import httpx
import pytest

from app.services import llm as llm_svc


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code))

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class _ScriptedClient:
    """按剧本抛错/成功。剧本耗尽后永远成功。"""

    script: list = []
    calls: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        self.calls.append("post")
        i = len(self.calls) - 1
        if i < len(self.script):
            exc = self.script[i]
            if exc is not None:
                raise exc
        return _FakeResponse()


@pytest.fixture()
def scripted(monkeypatch):
    _ScriptedClient.script = []
    _ScriptedClient.calls = []
    monkeypatch.setattr(llm_svc.httpx, "Client", _ScriptedClient)
    monkeypatch.setattr(llm_svc.time, "sleep", lambda s: None)
    return _ScriptedClient


def _chain(monkeypatch):
    monkeypatch.setattr(llm_svc, "model_chain", lambda: ["主模型", "备胎A"])


def test_transient_blip_retries_same_model(scripted, monkeypatch):
    """抖 1 次就恢复 → 不降级，仍用主模型。"""
    _chain(monkeypatch)
    scripted.script = [httpx.RemoteProtocolError("Server disconnected")]
    content, model = llm_svc.chat_with_fallback([{"role": "u", "content": "x"}])
    assert model == "主模型"
    assert len(scripted.calls) == 2  # 首发 + 1 次重试，全是主模型


def test_two_blips_still_recover(scripted, monkeypatch):
    """抖 2 次恢复 → 第 3 次同模型成功。"""
    _chain(monkeypatch)
    scripted.script = [httpx.ConnectError("refused"),
                       httpx.RemoteProtocolError("disconnected")]
    content, model = llm_svc.chat_with_fallback([{"role": "u", "content": "x"}])
    assert model == "主模型"
    assert len(scripted.calls) == 3


def test_all_attempts_fail_then_degrade(scripted, monkeypatch):
    """主模型连抖 3 次 → 降级备胎成功。"""
    _chain(monkeypatch)
    scripted.script = [httpx.ConnectError("x")] * 3
    content, model = llm_svc.chat_with_fallback([{"role": "u", "content": "x"}])
    assert model == "备胎A"
    assert len(scripted.calls) == 4  # 主模型 3 次 + 备胎 1 次


def test_fatal_error_skips_retry(scripted, monkeypatch):
    """致命错误（空内容）不重试，立刻降级。"""
    _chain(monkeypatch)

    class _EmptyResp(_FakeResponse):
        def json(self):
            return {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                    "usage": {}}

    class _EmptyClient(_ScriptedClient):
        def post(self, *a, **k):
            self.calls.append("post")
            i = len(self.calls) - 1
            if i < len(self.script) and self.script[i] == "EMPTY":
                return _EmptyResp()  # 剧本指定的错误行为
            return _FakeResponse()  # 正常返回

    _EmptyClient.script = ["EMPTY"]  # 首发空内容，其后（备胎）正常返回
    monkeypatch.setattr(llm_svc.httpx, "Client", _EmptyClient)
    content, model = llm_svc.chat_with_fallback([{"role": "u", "content": "x"}])
    assert model == "备胎A"
    assert len(_EmptyClient.calls) == 2  # 主模型 1 次（无重试）+ 备胎 1 次


def test_read_timeout_degrades_immediately(scripted, monkeypatch):
    """等满超时 = 该模型病重，不重试直接降级。"""
    _chain(monkeypatch)
    scripted.script = [httpx.ReadTimeout("300s"), None]
    content, model = llm_svc.chat_with_fallback([{"role": "u", "content": "x"}])
    assert model == "备胎A"
    assert len(scripted.calls) == 2  # 主模型 1 次（病重不重试）+ 备胎 1 次
