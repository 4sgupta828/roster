"""The shared strict-JSON model call: DeepSeek first, OpenAI on an HTTP error, a cooldown after 402 / 401."""
import io
import json
import urllib.error

from api import model_json


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _opener(calls, fail_first_with=None):
    def urlopen(req, timeout=0):
        calls.append(req.full_url)
        if fail_first_with and "deepseek" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, fail_first_with, "err", {}, io.BytesIO(b""))
        return _Resp(json.dumps({"choices": [{"message": {"content": json.dumps({"ok": req.full_url.split("/")[2]})}}]}).encode())
    return urlopen


def test_falls_back_to_openai_on_a_402_and_cools_deepseek_down(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d"); monkeypatch.setenv("OPENAI_API_KEY", "o")
    model_json._skip_until.clear(); model_json._last_error.clear()
    calls = []
    monkeypatch.setattr(model_json.urllib.request, "urlopen", _opener(calls, fail_first_with=402))
    out = model_json.llm_json("s", "u")
    assert out == {"ok": "api.openai.com"} and [c.split("/")[2] for c in calls] == ["api.deepseek.com", "api.openai.com"]
    calls.clear()
    out2 = model_json.llm_json("s", "u")                       # the cooldown skips DeepSeek without a request
    assert out2 == {"ok": "api.openai.com"} and [c.split("/")[2] for c in calls] == ["api.openai.com"]
    assert model_json.last_error("deepseek") == "HTTP 402"


def test_deepseek_first_when_healthy_and_no_key_raises(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d"); monkeypatch.setenv("OPENAI_API_KEY", "o")
    model_json._skip_until.clear()
    calls = []
    monkeypatch.setattr(model_json.urllib.request, "urlopen", _opener(calls))
    assert model_json.llm_json("s", "u") == {"ok": "api.deepseek.com"}
    monkeypatch.delenv("DEEPSEEK_API_KEY"); monkeypatch.delenv("OPENAI_API_KEY")
    try:
        model_json.llm_json("s", "u"); raise AssertionError("should raise")
    except RuntimeError as e:
        assert "no model" in str(e)
