import asyncio
import hashlib

import pytest

from fastapi.testclient import TestClient

from backend import main, projects, providers


def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(providers, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(providers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(main, "DB", tmp_path / "history.sqlite3")
    monkeypatch.setattr(projects, "SETTINGS_FILE", tmp_path / "settings.json")


def payload(**updates):
    data = {"name": "Apinex Luna", "provider": "Apinex",
            "base_url": "https://api.apinex.bond/v1", "model": "free/gpt-6-luna",
            "protocol": "chat_completions", "api_key": "secret-test-value",
            "timeout": 60, "max_iterations": 12, "streaming": False,
            "max_requests_per_window": 600, "request_window_seconds": 60,
            "min_request_interval_seconds": 0, "retry_limit": 2,
            "max_backoff_seconds": 30, "task_timeout_seconds": 900}
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


def test_provider_pacing_settings_are_saved_and_validated(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    client = TestClient(main.app)
    saved = client.post("/api/providers", json=payload(
        max_requests_per_window=4, request_window_seconds=45,
        min_request_interval_seconds=8, retry_limit=1,
        max_backoff_seconds=20, task_timeout_seconds=600))
    assert saved.status_code == 200
    item = saved.json()
    assert item["max_requests_per_window"] == 4
    assert item["request_window_seconds"] == 45
    assert item["min_request_interval_seconds"] == 8
    assert item["retry_limit"] == 1
    assert item["max_backoff_seconds"] == 20
    assert item["task_timeout_seconds"] == 600
    bad = client.post("/api/providers", json=payload(max_requests_per_window=0))
    assert bad.status_code == 422


def test_validation_errors_do_not_echo_api_keys():
    client = TestClient(main.app)
    marker = "top-secret-invalid-key"
    response = client.post("/api/providers", json=payload(api_key=marker * 300))
    assert response.status_code == 422
    assert marker not in response.text


def test_command_environment_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("LUNA_TEST_API_KEY", "secret-value")
    monkeypatch.setenv("LUNA_TEST_TOKEN", "token-value")
    environment = main._command_environment()
    assert "LUNA_TEST_API_KEY" not in environment
    assert "LUNA_TEST_TOKEN" not in environment
    assert main._redact_text("API_KEY=secret-value-123") == "[redacted]"


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
            return {"model": "served-model", "choices": [{"message": {"content": "OK"}}],
                    "usage": {"total_tokens": 3, "note": "secret-test-value"}}

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
    assert "secret-test-value" not in str(result)


def test_responses_connection_uses_responses_endpoint(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        def json(self):
            return {"model": "responses-model", "output": [{"type": "message"}],
                    "usage": {"input_tokens": 2, "output_tokens": 1}}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return Response()

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    result = asyncio.run(providers.test_connection(
        providers.ProviderInput(**payload(protocol="responses")), "secret-test-value"))
    assert result["ok"] is True
    assert result["model"] == "responses-model"
    assert calls[0][0].endswith("/responses")
    assert calls[0][1]["input"] == "Reply with OK."


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


def test_file_tools_protect_secrets_and_detect_stale_edits(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("SECRET=do-not-read", encoding="utf-8")
    target = root / "code.py"
    target.write_text("before", encoding="utf-8")
    (root / "config.py").write_text('API_KEY="super-secret-value-123"', encoding="utf-8")
    secret_result = asyncio.run(main._run_tool("read_file", {"path": ".env"}, root, "read_only"))
    assert "Permission denied" in secret_result
    content_secret = asyncio.run(main._run_tool("read_file", {"path": "config.py"}, root, "read_only"))
    assert "super-secret-value-123" not in content_secret
    assert "Permission denied" in content_secret

    stale = asyncio.run(main._run_tool("write_file", {
        "path": "code.py", "content": "after", "expected_sha256": "wrong-hash",
    }, root, "workspace"))
    assert "conflict" in stale.lower()
    assert target.read_text(encoding="utf-8") == "before"

    digest = __import__("hashlib").sha256(b"before").hexdigest()
    updated = asyncio.run(main._run_tool("write_file", {
        "path": "code.py", "content": "after", "expected_sha256": digest,
    }, root, "workspace"))
    assert "updated" in updated
    assert target.read_text(encoding="utf-8") == "after"


def test_write_file_preserves_exact_utf8_content(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "literal.txt"
    target.write_bytes("old line\r\n".encode("utf-8"))
    content = "  café!?! \t\r\nsecond line.\n"
    expected = hashlib.sha256(target.read_bytes()).hexdigest()

    result = asyncio.run(main._run_tool("write_file", {
        "path": "literal.txt", "content": content,
        "expected_sha256": expected,
    }, root, "workspace"))

    written = target.read_bytes()
    assert "updated" in result
    assert written == content.encode("utf-8")
    assert hashlib.sha256(written).hexdigest() == hashlib.sha256(
        content.encode("utf-8")).hexdigest()


def test_file_tools_reject_symlink_paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "real.txt"
    target.write_text("keep me", encoding="utf-8")
    link = root / "alias.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symbolic links.")

    read = asyncio.run(main._run_tool("read_file", {
        "path": "alias.txt",
    }, root, "read_only"))
    write = asyncio.run(main._run_tool("write_file", {
        "path": "alias.txt", "content": "changed", "expected_sha256": None,
    }, root, "workspace"))

    assert "Permission denied" in read
    assert "Permission denied" in write
    assert target.read_text(encoding="utf-8") == "keep me"


def test_write_file_does_not_follow_a_staged_file_symlink(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    staged = root / "note.txt.luna-tmp"
    try:
        staged.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symbolic links.")

    result = asyncio.run(main._run_tool("write_file", {
        "path": "note.txt", "content": "new note", "expected_sha256": None,
    }, root, "workspace"))

    assert "created" in result
    assert (root / "note.txt").read_bytes() == b"new note"
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert staged.is_symlink()


def test_project_selection_persists_and_recent_removal_keeps_directory(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "my-project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='demo'", encoding="utf-8")
    (project / "AGENTS.md").write_text("untrusted instructions", encoding="utf-8")
    client = TestClient(main.app)
    opened = client.post("/api/projects/open", json={"path": str(project)})
    assert opened.status_code == 200
    assert opened.json()["active_project"] == str(project.resolve())
    inspected = client.post("/api/projects/inspect", json={"path": str(project)})
    assert "pyproject.toml" in inspected.json()["manifests"]
    assert inspected.json()["has_agent_instructions"] is True
    removed = client.request("DELETE", "/api/projects/recent", json={"path": str(project)})
    assert removed.status_code == 200
    assert project.is_dir()
    assert removed.json()["active_project"] == str(project.resolve())


def test_project_rejects_missing_folder(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    response = TestClient(main.app).post("/api/projects/open", json={"path": str(tmp_path / "missing")})
    assert response.status_code == 422


def test_project_summary_reports_framework_without_returning_script_body(tmp_path):
    project = tmp_path / "react-app"
    project.mkdir()
    (project / "package.json").write_text(
        '{"dependencies":{"react":"1"},"scripts":{"test":"echo private-token-should-not-leak"}}',
        encoding="utf-8",
    )
    summary = projects.inspect_project(str(project))
    assert summary["frameworks"] == ["react"]
    assert summary["package_script_names"] == ["test"]
    assert "private-token-should-not-leak" not in str(summary)


def test_mock_agent_reads_writes_and_verifies_inside_active_project(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "selected-project"
    project.mkdir()
    (project / "notes.txt").write_text("initial", encoding="utf-8")
    projects.open_project(str(project))
    providers.save_provider(providers.ProviderInput(**payload()))

    def answer(name=None, args=None, content=None):
        message = {"content": content, "tool_calls": []}
        if name:
            message["tool_calls"] = [{"id": "call-" + name, "type": "function",
                "function": {"name": name, "arguments": __import__("json").dumps(args)}}]
        return {"choices": [{"message": message}], "model": "mock-model"}

    responses = [
        answer("list_files", {"path": "."}),
        answer("read_file", {"path": "notes.txt"}),
        answer("write_file", {"path": "result.txt", "content": "verified", "expected_sha256": None}),
        answer("read_file", {"path": "result.txt"}),
        answer(content="Created result.txt and read it back successfully."),
    ]

    class Response:
        status_code = 200
        def __init__(self, value): self.value = value
        def json(self): return self.value

    class Client:
        def __init__(self, **kwargs): self.index = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            result = responses[self.index]
            self.index += 1
            return Response(result)

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    task = main.TaskInput(message="Create result.txt", mode="workspace")
    result = asyncio.run(main._agent(task))
    assert result["answer"].startswith("Created result.txt")
    assert (project / "result.txt").read_text(encoding="utf-8") == "verified"
    assert "secret-test-value" not in (tmp_path / "history.sqlite3").read_bytes().decode("latin1")
    assert main.history()["tasks"][0]["workspace"] == str(project.resolve())


def test_task_endpoint_can_cancel_and_records_status(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "active"
    project.mkdir()
    projects.open_project(str(project))
    providers.save_provider(providers.ProviderInput(**payload()))

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): await asyncio.sleep(30)

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    client = TestClient(main.app)
    started = client.post("/api/tasks", json={"message": "inspect the project"}).json()
    assert started["status"] == "running"
    cancelled = client.delete("/api/tasks/" + str(started["id"]))
    assert cancelled.status_code == 200
    for _ in range(30):
        state = client.get("/api/tasks/" + str(started["id"])).json()
        if state["status"] == "cancelled":
            break
        __import__("time").sleep(0.01)
    assert state["status"] == "cancelled"
    assert state["events"][-1]["detail"] == "Task cancelled"


def test_mocked_task_endpoint_persists_redacted_completed_task(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "task-project"
    project.mkdir()
    projects.open_project(str(project))
    providers.save_provider(providers.ProviderInput(**payload()))
    seen = []

    class Response:
        status_code = 200
        def json(self):
            return {"model": "mock-model", "choices": [{"message": {
                "content": "Task checked.", "tool_calls": []}}]}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            seen.extend(kwargs["json"]["messages"])
            return Response()

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    client = TestClient(main.app)
    secret = "API_KEY=superSecretValue123"
    started = client.post("/api/tasks", json={"message": "Inspect " + secret}).json()
    state = client.get(f"/api/tasks/{started['id']}").json()
    assert state["status"] == "completed"
    assert secret not in state["message"]
    assert secret not in str(seen)
    assert state["answer"] == "Task checked."
    assert state["request_stats"]["attempted"] == 1
    assert state["request_stats"]["successful"] == 1


def test_local_web_guard_rejects_foreign_origin_and_host():
    client = TestClient(main.app)
    foreign_origin = client.get("/api/projects", headers={"origin": "http://evil.example"})
    bad_host = client.get("/api/projects", headers={"host": "luna.evil.example"})
    assert foreign_origin.status_code == 403
    assert bad_host.status_code == 400


def test_risky_commands_wait_for_explicit_user_approval(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "approval-project"
    project.mkdir()
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="run a command"))
    assert main._command_needs_approval("python -m pytest") is False
    assert main._command_needs_approval("Remove-Item important.txt") is True

    async def check():
        waiting = asyncio.create_task(main._request_approval(
            task_id, "Remove-Item important.txt", asyncio.Event()))
        await asyncio.sleep(0)
        assert main._task_state(task_id)["status"] == "awaiting_approval"
        assert main._task_state(task_id)["approval"]["command"] == "Remove-Item important.txt"
        main.decide_command(task_id, main.ApprovalInput(approved=True))
        return await waiting

    assert asyncio.run(check()) is True
    assert main._task_state(task_id)["status"] == "running"


def test_task_diff_snapshot_and_safe_restore(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "change-project"
    project.mkdir()
    target = project / "source.py"
    target.write_text("old value\n", encoding="utf-8")
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="update source"))
    old_hash = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
    result = asyncio.run(main._run_tool("write_file", {
        "path": "source.py", "content": "new value\n", "expected_sha256": old_hash,
    }, project, "workspace", task_id=task_id))
    assert "updated" in result
    client = TestClient(main.app)
    change = client.get(f"/api/tasks/{task_id}/changes").json()["changes"][0]
    assert "-old value" in change["diff"]
    assert "+new value" in change["diff"]
    restored = client.post(f"/api/changes/{change['id']}/restore")
    assert restored.status_code == 200
    assert target.read_text(encoding="utf-8") == "old value\n"
    assert client.get(f"/api/tasks/{task_id}/changes").json()["changes"][0]["status"] == "restored"


def test_gitignore_excludes_secrets_and_local_database():
    text = (providers.ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in text and ".luna/" in text and "*.sqlite3" in text
