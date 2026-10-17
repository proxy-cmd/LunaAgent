"""OpenAI-compatible provider settings and connection tests."""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
_default_data = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LunaDesk"
                 if getattr(sys, "frozen", False) else ROOT / ".luna")
DATA_DIR = Path(os.getenv("LUNA_DATA_DIR", _default_data))
PROVIDERS_FILE = DATA_DIR / "providers.json"
_default_env = DATA_DIR / ".env" if getattr(sys, "frozen", False) else ROOT / ".env"
ENV_FILE = Path(os.getenv("LUNA_ENV_FILE", _default_env))


class ProviderInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    protocol: str = "chat_completions"
    api_key: str | None = Field(default=None, max_length=4096)
    timeout: int = Field(default=60, ge=5, le=300)
    max_iterations: int = Field(default=12, ge=1, le=100)
    streaming: bool = False
    max_requests_per_window: int | None = Field(default=None, ge=1, le=600)
    request_window_seconds: int = Field(default=60, ge=1, le=3600)
    min_request_interval_seconds: float | None = Field(default=None, ge=0, le=3600)
    retry_limit: int = Field(default=2, ge=0, le=5)
    max_backoff_seconds: int = Field(default=30, ge=1, le=300)
    task_timeout_seconds: int = Field(default=900, ge=30, le=7200)


_pace_lock = threading.RLock()


class ProviderRequestError(Exception):
    def __init__(self, category: str, message: str, status: int | None = None):
        super().__init__(category + ": " + message)
        self.category = category
        self.status = status


def rate_settings(provider: dict) -> tuple[int, int, float]:
    is_free_apinex = (provider.get("provider", "").casefold() == "apinex"
                      and provider.get("model", "").casefold().startswith("free/"))
    limit = provider.get("max_requests_per_window")
    if limit is None:
        limit = 5 if is_free_apinex else 60
    interval = provider.get("min_request_interval_seconds")
    if interval is None:
        interval = 12 if is_free_apinex else 0
    return int(limit), int(provider.get("request_window_seconds", 60)), float(interval)


def _scope(provider: dict, key: str) -> str:
    parts = (provider.get("base_url", "").casefold().rstrip("/"),
             provider.get("model", ""), hashlib.sha256(key.encode()).hexdigest())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _pace_file() -> Path:
    return DATA_DIR / "request_pacing.json"


def _read_pacing() -> dict:
    try:
        data = json.loads(_pace_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("requests", {})
            data.setdefault("cooldowns", {})
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"requests": {}, "cooldowns": {}}


def _write_pacing(data: dict) -> None:
    path = _pace_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _reserve(scope: str, limit: int, window: int, min_interval: float) -> float:
    now = time.time()
    with _pace_lock:
        data = _read_pacing()
        requests = data["requests"].get(scope, [])
        recent = [stamp for stamp in requests if stamp > now - window]
        cooldown = float(data["cooldowns"].get(scope, 0))
        delay = max(0.0, cooldown - now)
        if len(recent) >= limit:
            delay = max(delay, recent[0] + window - now)
        if recent and min_interval:
            delay = max(delay, recent[-1] + min_interval - now)
        if delay <= 0:
            recent.append(now)
            data["requests"][scope] = recent
            data["cooldowns"].pop(scope, None)
        else:
            data["requests"][scope] = recent
        _write_pacing(data)
        return delay


def _set_cooldown(scope: str, until: float) -> None:
    with _pace_lock:
        data = _read_pacing()
        data["cooldowns"][scope] = max(float(data["cooldowns"].get(scope, 0)), until)
        _write_pacing(data)


def _retry_after(headers) -> float | None:
    value = (headers.get("retry-after") or headers.get("Retry-After")) if headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (ValueError, AttributeError):
        try:
            date = email.utils.parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, date.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _retry_delay(attempt: int, headers, maximum: int) -> tuple[float, bool]:
    retry_after = _retry_after(headers)
    if retry_after is not None:
        return min(retry_after, maximum), retry_after <= maximum
    delay = min(float(maximum), 2 ** attempt)
    return min(float(maximum), random.uniform(delay * 0.8, delay * 1.2)), True


def _stat(stats: dict | None, name: str, amount=1) -> None:
    if stats is not None:
        stats[name] = round(stats.get(name, 0) + amount, 2) if name == "wait_seconds" else stats.get(name, 0) + amount


async def _wait(seconds: float, cancel_event: asyncio.Event | None,
                event=None, stats: dict | None = None,
                label: str = "Waiting") -> None:
    if seconds <= 0:
        return
    if event:
        event(f"{label}; retrying in {max(1, int(seconds + 0.99))} seconds.")
    started = time.monotonic()
    if cancel_event is None:
        await asyncio.sleep(seconds)
    else:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
            raise asyncio.CancelledError
        except asyncio.TimeoutError:
            pass
    _stat(stats, "wait_seconds", time.monotonic() - started)


async def _slot(provider: dict, key: str, cancel_event: asyncio.Event | None,
                event=None, stats: dict | None = None) -> str:
    limit, window, interval = rate_settings(provider)
    scope = _scope(provider, key)
    while True:
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        delay = _reserve(scope, limit, window, interval)
        if delay <= 0:
            _stat(stats, "attempted")
            return scope
        _stat(stats, "waits")
        await _wait(delay, cancel_event, event, stats, "Request pacing")


async def _post_once(client, url: str, headers: dict, body: dict,
                     cancel_event: asyncio.Event | None):
    if cancel_event is None:
        return await client.post(url, headers=headers, json=body)
    if cancel_event.is_set():
        raise asyncio.CancelledError
    request = asyncio.create_task(client.post(url, headers=headers, json=body))
    cancelled = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait({request, cancelled}, return_when=asyncio.FIRST_COMPLETED)
    if cancelled in done and cancel_event.is_set():
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        raise asyncio.CancelledError
    cancelled.cancel()
    return request.result()


async def model_post(client, provider: dict, key: str, url: str, headers: dict,
                     body: dict, cancel_event=None, event=None,
                     stats: dict | None = None):
    """Send a model request through the shared quota and retry policy."""
    retries = 0
    while True:
        scope = await _slot(provider, key, cancel_event, event, stats)
        try:
            response = await _post_once(client, url, headers, body, cancel_event)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            if retries >= provider.get("retry_limit", 2):
                raise
            _stat(stats, "retries")
            delay, _ = _retry_delay(retries, {}, provider.get("max_backoff_seconds", 30))
            retries += 1
            await _wait(delay, cancel_event, event, stats, "Provider connection failed")
            continue
        if response.status_code < 400:
            _stat(stats, "successful")
            return response
        category, _ = _error(response.status_code, response.text)
        if category == "rate_limit":
            _stat(stats, "rate_limits")
            delay, can_wait = _retry_delay(retries, response.headers,
                                            provider.get("max_backoff_seconds", 30))
            indicated = _retry_after(response.headers)
            _set_cooldown(scope, time.time() + (indicated if indicated is not None else delay))
            if retries < provider.get("retry_limit", 2) and can_wait:
                retries += 1
                _stat(stats, "retries")
                await _wait(delay, cancel_event, event, stats, "Rate limited")
                continue
            if event:
                event("Rate limit retry skipped: the retry budget is exhausted or the delay exceeds the configured maximum.")
            return response
        if category in {"network", "provider_error"} and response.status_code >= 500:
            if retries < provider.get("retry_limit", 2):
                delay, _ = _retry_delay(retries, {}, provider.get("max_backoff_seconds", 30))
                retries += 1
                _stat(stats, "retries")
                await _wait(delay, cancel_event, event, stats, "Provider temporarily unavailable")
                continue
        return response


@asynccontextmanager
async def model_stream(client, provider: dict, key: str, url: str, headers: dict,
                       body: dict, cancel_event=None, event=None,
                       stats: dict | None = None):
    """Retry only before a stream is accepted; never replay partial output."""
    retries = 0
    while True:
        scope = await _slot(provider, key, cancel_event, event, stats)
        stream = client.stream("POST", url, headers=headers, json=body)
        try:
            response = await stream.__aenter__()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            if retries >= provider.get("retry_limit", 2):
                raise
            retries += 1
            _stat(stats, "retries")
            delay, _ = _retry_delay(retries - 1, {}, provider.get("max_backoff_seconds", 30))
            await _wait(delay, cancel_event, event, stats, "Provider connection failed")
            continue
        if response.status_code == 429:
            raw = await response.aread()
            category, _ = _error(response.status_code, raw.decode(errors="replace"))
            if category == "rate_limit":
                _stat(stats, "rate_limits")
                delay, can_wait = _retry_delay(retries, response.headers,
                                                provider.get("max_backoff_seconds", 30))
                indicated = _retry_after(response.headers)
                _set_cooldown(scope, time.time() + (indicated if indicated is not None else delay))
                await stream.__aexit__(None, None, None)
                if retries < provider.get("retry_limit", 2) and can_wait:
                    retries += 1
                    _stat(stats, "retries")
                    await _wait(delay, cancel_event, event, stats, "Rate limited")
                    continue
                if event:
                    event("Rate limit retry skipped: the retry budget is exhausted or the delay exceeds the configured maximum.")
                raise ProviderRequestError("rate_limit", "The provider is rate limiting requests.", 429)
            await stream.__aexit__(None, None, None)
            _, message = _error(response.status_code, raw.decode(errors="replace"))
            raise ProviderRequestError(category, message, 429)
        if response.status_code >= 500 and retries < provider.get("retry_limit", 2):
            await response.aread()
            await stream.__aexit__(None, None, None)
            delay, _ = _retry_delay(retries, {}, provider.get("max_backoff_seconds", 30))
            retries += 1
            _stat(stats, "retries")
            await _wait(delay, cancel_event, event, stats, "Provider temporarily unavailable")
            continue
        try:
            yield response
            if response.status_code < 400:
                _stat(stats, "successful")
        finally:
            await stream.__aexit__(None, None, None)
        return


def validate(item: ProviderInput) -> str:
    if item.protocol not in {"chat_completions", "responses"}:
        raise HTTPException(422, "Choose Chat Completions or Responses.")
    parsed = urlparse(item.base_url.strip().rstrip("/"))
    if (parsed.scheme not in {"https", "http"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise HTTPException(422, "Enter a valid HTTP(S) API URL without credentials or query parameters.")
    if not item.name.strip() or not item.provider.strip() or not item.model.strip():
        raise HTTPException(422, "Provider name, display name, and model ID are required.")
    return item.base_url.strip().rstrip("/")


def _read() -> dict:
    if not PROVIDERS_FILE.exists():
        return {"providers": [], "default_id": None, "setup_done": False, "pending_test": None}
    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        data.setdefault("providers", [])
        data.setdefault("default_id", None)
        data.setdefault("setup_done", False)
        data.setdefault("pending_test", None)
        return data
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "Could not read local provider settings.") from exc


def _write(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROVIDERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PROVIDERS_FILE)


def _key_name(provider_id: str) -> str:
    return "LUNA_PROVIDER_KEY_" + provider_id.upper()


def get_key(provider_id: str) -> str | None:
    name = _key_name(provider_id)
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*" + re.escape(name) + r"\s*=\s*(.*)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'") or None
    return None


def _save_key(provider_id: str, value: str) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    name = _key_name(provider_id)
    lines = [line for line in lines if not line.startswith(name + "=")]
    lines.append(name + "=" + value.replace("\r", "").replace("\n", ""))
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_key(provider_id: str) -> None:
    if ENV_FILE.exists():
        name = _key_name(provider_id)
        lines = [line for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if not line.startswith(name + "=")]
        ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _fingerprint(item: ProviderInput, key: str) -> str:
    parts = (item.provider.strip(), item.name.strip(), validate(item), item.model.strip(),
             item.protocol, str(item.timeout), str(item.max_iterations), str(item.streaming),
             str(rate_settings(item.model_dump() if hasattr(item, "model_dump") else item.dict())),
             str(item.request_window_seconds), str(item.retry_limit), str(item.max_backoff_seconds),
             str(item.task_timeout_seconds),
             hashlib.sha256(key.encode()).hexdigest())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _public(record: dict, default_id: str | None) -> dict:
    fields = ("id", "name", "provider", "base_url", "model", "protocol", "timeout",
              "max_iterations", "streaming", "max_requests_per_window",
              "request_window_seconds", "min_request_interval_seconds", "retry_limit",
              "max_backoff_seconds", "task_timeout_seconds", "last_test")
    result = {field: record.get(field) for field in fields}
    result["key_configured"] = bool(get_key(record["id"]))
    result["is_default"] = record["id"] == default_id
    return result


def list_providers() -> dict:
    data = _read()
    return {"providers": [_public(p, data["default_id"]) for p in data["providers"]],
            "default_id": data["default_id"]}


def get_default() -> tuple[dict, str]:
    data = _read()
    record = next((p for p in data["providers"] if p["id"] == data["default_id"]), None)
    key = get_key(record["id"]) if record else None
    if not record or not key:
        raise HTTPException(409, "No provider is configured. Open Settings to add one.")
    return record, key


def _error(status: int, body: str) -> tuple[str, str]:
    try:
        error = json.loads(body).get("error", {})
        detail = " ".join(str(error.get(k, "")) for k in ("code", "type", "message")).lower()
    except (ValueError, AttributeError):
        detail = body.lower()[:400]
    if status == 401:
        return "authentication", "Authentication failed. Check the API key."
    if status == 402 or any(word in detail for word in ("billing", "insufficient balance", "payment required")):
        return "balance", "The provider reports insufficient balance or a billing issue."
    if status == 429:
        if any(word in detail for word in ("quota", "exhausted", "insufficient_quota",
                                           "limit reached", "daily limit", "daily_limit",
                                           "daily allowance", "token allowance")):
            return "quota", "The provider quota is exhausted. Check the plan or quota."
        return "rate_limit", "The provider is rate limiting requests. Wait and retry."
    if status in (404, 405, 501):
        return "unsupported_protocol", "This endpoint or API protocol is not supported at this URL."
    if status in (400, 422):
        return "request", "The provider rejected the request. Check the model ID and protocol."
    if status == 403:
        return "permission", "The provider denied access. Check key permissions and model access."
    if status in (408, 504) or status >= 500:
        return "network", "The provider is unavailable or timed out. Try again later."
    return "provider_error", "The provider returned HTTP " + str(status) + "."


def _hide_key(value, key: str):
    if isinstance(value, str):
        return value.replace(key, "[redacted]") if key else value
    if isinstance(value, list):
        return [_hide_key(item, key) for item in value]
    if isinstance(value, dict):
        return {str(name): _hide_key(item, key) for name, item in value.items()}
    return value


async def test_connection(item: ProviderInput, key: str) -> dict:
    base_url = validate(item)
    endpoint = "/responses" if item.protocol == "responses" else "/chat/completions"
    if item.protocol == "responses":
        body = {"model": item.model, "input": "Reply with OK.", "max_output_tokens": 16}
    else:
        body = {"model": item.model, "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 16, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=item.timeout) as client:
            config = item.model_dump() if hasattr(item, "model_dump") else item.dict()
            config["base_url"] = base_url
            response = await model_post(client, config, key, base_url + endpoint,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                body=body)
    except httpx.TimeoutException:
        return {"ok": False, "category": "network", "message": "The provider request timed out."}
    except httpx.RequestError:
        return {"ok": False, "category": "network", "message": "Could not reach provider. Check URL and network."}
    if response.status_code >= 400:
        category, message = _error(response.status_code, response.text)
        return {"ok": False, "category": category, "message": message}
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "category": "invalid_response", "message": "Provider returned invalid JSON."}
    if not isinstance(data, dict):
        return {"ok": False, "category": "invalid_response", "message": "Provider returned an invalid response."}
    valid = (isinstance(data.get("choices"), list) and bool(data["choices"])) if item.protocol == "chat_completions" else (isinstance(data.get("output"), list) or isinstance(data.get("output_text"), str))
    if not valid:
        return {"ok": False, "category": "invalid_response", "message": "Provider response did not contain a completion result."}
    actual_model = data.get("model")
    if not isinstance(actual_model, str) or not actual_model:
        actual_model = item.model
    return {"ok": True, "category": "success", "message": "Connection test succeeded.",
            "model": _hide_key(actual_model, key), "usage": _hide_key(data.get("usage"), key)}


def save_provider(item: ProviderInput, provider_id: str | None = None) -> dict:
    validate(item)
    data = _read()
    existing = next((p for p in data["providers"] if p["id"] == provider_id), None) if provider_id else None
    if provider_id and not existing:
        raise HTTPException(404, "Provider not found.")
    pid = provider_id or uuid.uuid4().hex
    key = item.api_key or get_key(pid)
    if not key:
        raise HTTPException(422, "Enter an API key before saving this provider.")
    last_test = None
    if (existing and existing.get("last_test", {}).get("fingerprint")
            == _fingerprint(item, key)):
        last_test = existing["last_test"]
    record = {"id": pid, "name": item.name.strip(), "provider": item.provider.strip(),
              "base_url": validate(item), "model": item.model.strip(), "protocol": item.protocol,
              "timeout": item.timeout, "max_iterations": item.max_iterations,
              "streaming": item.streaming,
              "max_requests_per_window": rate_settings(
                  item.model_dump() if hasattr(item, "model_dump") else item.dict())[0],
              "request_window_seconds": item.request_window_seconds,
              "min_request_interval_seconds": rate_settings(
                  item.model_dump() if hasattr(item, "model_dump") else item.dict())[2],
              "retry_limit": item.retry_limit,
              "max_backoff_seconds": item.max_backoff_seconds,
              "task_timeout_seconds": item.task_timeout_seconds,
              "last_test": last_test}
    pending = data.get("pending_test")
    if pending and pending.get("fingerprint") == _fingerprint(item, key):
        record["last_test"] = pending
    _save_key(pid, key)
    if existing:
        data["providers"] = [record if p["id"] == pid else p for p in data["providers"]]
    else:
        data["providers"].append(record)
        if not data["default_id"]:
            data["default_id"] = pid
    _write(data)
    return _public(record, data["default_id"])


async def run_test(item: ProviderInput) -> dict:
    key = item.api_key or (get_key(item.id) if item.id else None)
    if not key:
        raise HTTPException(422, "Enter an API key before testing this provider.")
    result = await test_connection(item, key)
    data = _read()
    checked = {"fingerprint": _fingerprint(item, key), "result": result,
               "tested_at": datetime.now(timezone.utc).isoformat()}
    data["pending_test"] = checked
    record = next((p for p in data["providers"] if p["id"] == item.id), None) if item.id else None
    if record:
        stored = ProviderInput(**record)
        if _fingerprint(stored, key) == checked["fingerprint"]:
            record["last_test"] = checked
    _write(data)
    return result


def set_default(provider_id: str) -> None:
    data = _read()
    if not any(p["id"] == provider_id for p in data["providers"]):
        raise HTTPException(404, "Provider not found.")
    data["default_id"] = provider_id
    _write(data)


def delete_provider(provider_id: str) -> None:
    data = _read()
    if not any(p["id"] == provider_id for p in data["providers"]):
        raise HTTPException(404, "Provider not found.")
    data["providers"] = [p for p in data["providers"] if p["id"] != provider_id]
    remove_key(provider_id)
    if data["default_id"] == provider_id:
        data["default_id"] = None
    if not data["providers"]:
        data["setup_done"] = False
    _write(data)


def delete_provider_key(provider_id: str) -> None:
    data = _read()
    record = next((p for p in data["providers"] if p["id"] == provider_id), None)
    if not record:
        raise HTTPException(404, "Provider not found.")
    remove_key(provider_id)
    record["last_test"] = None
    if data["default_id"] == provider_id:
        data["setup_done"] = False
    _write(data)


def finish_setup() -> None:
    data = _read()
    provider = next((p for p in data["providers"] if p["id"] == data["default_id"]), None)
    result = provider.get("last_test", {}).get("result", {}) if provider else {}
    if not result.get("ok"):
        raise HTTPException(409, "Test the default provider successfully before finishing setup.")
    data["setup_done"] = True
    _write(data)


def setup_status() -> dict:
    data = _read()
    return {"setup_done": data["setup_done"], "default_id": data["default_id"],
            "has_provider": bool(data["providers"])}
