import asyncio

from fastapi.testclient import TestClient

from backend import main, providers


def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(providers, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(providers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(main, "DB", tmp_path / "history.sqlite3")


def payload(**updates):
    data = {"name": "Apinex Luna", "provider": "Apinex",
            "base_url": "https://api.apinex.bond/v1", "model": "free/gpt-6-luna",
            "protocol": "chat_completions", "api_key": "secret-test-value",
            "timeout": 60, "max_iterations": 12, "streaming": False}
    data.update(updates)
    return data


def test_provider_key_stays_out_of_api_and_metadata(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/api/providers", json=payload())
    assert response.status_code == 200
    assert "secret-test-value" not in response.text
    assert "secret-test-value" not in (tmp_path / "providers.json").read_text()
    assert client.get("/api/providers").json()["providers"][0]["key_configured"] is True
    assert "secret-test-value" in (tmp_path / ".env").read_text()


def test_url_and_protocol_validation(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.post("/api/providers", json=payload(base_url="file:///tmp")).status_code == 422
    assert client.post("/api/providers", json=payload(protocol="unknown")).status_code == 422
    assert client.post("/api/providers", json=payload(base_url="https://user:pass@example.com/v1")).status_code == 422


def test_validation_errors_do_not_echo_api_keys():
    client = TestClient(main.app)
    marker = "top-secret-invalid-key"
    response = client.post("/api/providers", json=payload(api_key=marker * 300))
    assert response.status_code == 422
    assert marker not in response.text


def test_error_categories():
    assert providers._error(401, "{}")[0] == "authentication"
    assert providers._error(402, "{}")[0] == "balance"
    assert providers._error(429, '{"error":{"code":"insufficient_quota"}}')[0] == "quota"
    assert providers._error(429, "{}")[0] == "rate_limit"
    assert providers._error(404, "{}")[0] == "unsupported_protocol"
    assert providers._error(503, "{}")[0] == "network"


def test_connection_test_requires_a_completion(monkeypatch):
    class Response:
        status_code = 200
        text = ""
        def json(self):
            return {"model": "served-model", "choices": [{"message": {"content": "OK"}}], "usage": {"total_tokens": 3}}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    result = asyncio.run(providers.test_connection(providers.ProviderInput(**payload()), "secret-test-value"))
    assert result["ok"] is True
    assert result["model"] == "served-model"
    assert result["usage"]["total_tokens"] == 3


def test_successful_test_is_required_to_finish_setup(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.post("/api/setup/finish").status_code == 409
    record = providers.save_provider(providers.ProviderInput(**payload()))
    data = providers._read()
    data["providers"][0]["last_test"] = {"result": {"ok": True}}
    providers._write(data)
    assert client.post("/api/setup/finish").json() == {"ok": True}
    assert client.get("/api/setup-status").json()["setup_done"] is True
    assert record["key_configured"] is True


def test_saved_provider_records_its_connection_test(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)

    class Response:
        status_code = 200
        def json(self):
            return {"model": "actual-model", "choices": [{"message": {"content": "OK"}}]}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    record = providers.save_provider(providers.ProviderInput(**payload()))
    request = providers.ProviderInput(**payload(id=record["id"], api_key=None))
    result = asyncio.run(providers.run_test(request))
    saved = providers.list_providers()["providers"][0]
    assert result["ok"] is True
    assert saved["last_test"]["result"]["model"] == "actual-model"


def test_remove_default_does_not_silently_switch_provider(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    first = providers.save_provider(providers.ProviderInput(**payload()))
    second = providers.save_provider(providers.ProviderInput(**payload(name="Other", api_key="other-key")))
    assert providers.list_providers()["default_id"] == first["id"]
    providers.delete_provider(first["id"])
    assert providers.list_providers()["default_id"] is None
    assert not providers.get_key(first["id"])
    assert providers.list_providers()["providers"][0]["id"] == second["id"]


def test_edit_invalidates_a_stale_connection_result(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    original = providers.ProviderInput(**payload())
    saved = providers.save_provider(original)
    data = providers._read()
    data["providers"][0]["last_test"] = {
        "fingerprint": providers._fingerprint(original, "secret-test-value"),
        "result": {"ok": True},
    }
    providers._write(data)
    changed = providers.ProviderInput(**payload(model="different-model", api_key=None))
    updated = providers.save_provider(changed, saved["id"])
    assert updated["last_test"] is None


def test_workspace_paths_and_read_only_mode(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    assert main._target(root, "inside.txt") == root / "inside.txt"
    try:
        main._target(root, "../outside.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("workspace traversal was accepted")
    result = asyncio.run(main._run_tool("write_file", {"path": "x.txt", "content": "x"}, root, "read_only"))
    assert "Permission denied" in result
    assert not (root / "x.txt").exists()


def test_gitignore_excludes_secrets_and_local_database():
    text = (providers.ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in text and ".luna/" in text and "*.sqlite3" in text
