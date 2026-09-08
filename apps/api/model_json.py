"""ONE strict-JSON model call for the small extraction / compile / intake prompts (DeepSeek first, OpenAI
gpt-4o-mini as the fallback). Shared by the API and the ingest scripts so a provider failure is handled in one
place: an HTTP error from the first provider (402 balance exhausted, 401, 429, 5xx) falls through to the
second, and a hard provider failure (402 / 401) is remembered for `COOLDOWN` seconds so a bulk loop does not
hammer a dead endpoint — the 2026-09-05 lesson (100 of 100 batches failing per pass, silently)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

COOLDOWN = 600.0
_skip_until: dict[str, float] = {}
_last_error: dict[str, str] = {}


def providers() -> list[tuple[str, str, str, str]]:
    """(name, endpoint, key, model) in order of preference, keys present only."""
    out = []
    ds, oa = os.environ.get("DEEPSEEK_API_KEY"), os.environ.get("OPENAI_API_KEY")
    if ds:
        out.append(("deepseek", "https://api.deepseek.com/chat/completions", ds, "deepseek-chat"))
    if oa:
        out.append(("openai", "https://api.openai.com/v1/chat/completions", oa, os.environ.get("ROSTER_JSON_MODEL_OPENAI", "gpt-4o-mini")))
    return out


def last_error(name: str) -> str:
    return _last_error.get(name, "")


def llm_json(system: str, user: str, *, timeout: float = 90.0, prefer: str | None = None, model: str | None = None,
             reasoning_effort: str | None = None) -> dict:
    """Strict JSON from the first healthy provider (`prefer` = a provider name to try first — an eval judge that
    must differ from the in-product judge). `model` pins the model on the preferred provider (the planner runs on
    the best reasoning model, not the cheap extractor); `reasoning_effort` is passed to reasoning models, which take
    no temperature. Raises RuntimeError when none answers."""
    errs = []
    now = time.monotonic()
    order = providers()
    if prefer:
        order = [p for p in order if p[0] == prefer] + [p for p in order if p[0] != prefer]
    for name, endpoint, key, default_model in order:
        if _skip_until.get(name, 0.0) > now:
            errs.append(f"{name}: cooling down after {_last_error.get(name, 'an error')}")
            continue
        use_model = model if (model and (prefer is None or name == prefer)) else default_model
        payload = {"model": use_model, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if reasoning_effort and use_model == model:
            payload["reasoning_effort"] = reasoning_effort
        else:
            payload["temperature"] = 0
        body = json.dumps(payload).encode()
        req = urllib.request.Request(endpoint, data=body, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(json.load(r)["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read()[:200].decode("utf-8", "replace")
            except Exception:   # noqa: BLE001
                pass
            msg = f"HTTP {e.code}" + (": out of credit" if "insufficient_quota" in detail or "credit_balance" in detail else "")
            _last_error[name] = msg
            # dead key / no balance / exhausted quota: stop trying it for a while (a plain rate-limit 429 is retried)
            if e.code in (401, 402) or ("insufficient_quota" in detail or "credit_balance" in detail):
                _skip_until[name] = time.monotonic() + COOLDOWN
            errs.append(f"{name}: {msg}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:   # network: try the next provider, no cooldown
            _last_error[name] = str(e)[:80]
            errs.append(f"{name}: {str(e)[:80]}")
            continue
    raise RuntimeError("no model answered: " + "; ".join(errs) if errs else "no model key")
