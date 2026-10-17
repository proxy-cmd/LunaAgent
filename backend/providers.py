"""OpenAI-compatible provider settings and connection tests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("LUNA_DATA_DIR", ROOT / ".luna"))
PROVIDERS_FILE = DATA_DIR / "providers.json"
ENV_FILE = Path(os.getenv("LUNA_ENV_FILE", ROOT / ".env"))


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
             hashlib.sha256(key.encode()).hexdigest())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _public(record: dict, default_id: str | None) -> dict:
    fields = ("id", "name", "provider", "base_url", "model", "protocol", "timeout",
              "max_iterations", "streaming", "last_test")
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
        if any(word in detail for word in ("quota", "exhausted", "insufficient_quota", "limit reached")):
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
            response = await client.post(base_url + endpoint,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, json=body)
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
              "streaming": item.streaming, "last_test": last_test}
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
