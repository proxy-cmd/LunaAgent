import asyncio
import json
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from backend import (agent_protocols, browser_tools, desktop_tools, main,
                     mcp_client, projects, providers, skills)


def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(providers, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(providers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(main, "DB", tmp_path / "history.sqlite3")
    monkeypatch.setattr(projects, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(mcp_client, "MCP_FILE", tmp_path / "mcp.json")
    mcp_client.sessions.clear()


def provider(protocol="chat_completions", streaming=False):
    return providers.ProviderInput(
        name="Test provider", provider="Custom",
        base_url="https://provider.example/v1", model="test-model",
        protocol=protocol, api_key="test-key", streaming=streaming,
    )


def test_rate_defaults_and_retry_after_formats(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    assert providers.rate_settings({"provider": "Apinex", "model": "free/gpt-6-luna"}) == (5, 60, 12)
    assert providers.rate_settings({"provider": "Custom", "model": "model"}) == (60, 60, 0)
    monkeypatch.setattr(providers.time, "time", lambda: 1_800_000_000)
    assert providers._retry_after({"retry-after": "12"}) == 12
    stamp = providers.email.utils.format_datetime(
        providers.datetime.fromtimestamp(1_800_000_015, providers.timezone.utc))
    assert providers._retry_after({"Retry-After": stamp}) == 15
    assert providers._retry_after({"retry-after": "not a date"}) is None


def test_shared_rolling_limiter_coordinates_concurrent_requests(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    waits = []

    async def advance(seconds, cancel_event, event=None, stats=None, label="Waiting"):
        waits.append(seconds)
        now[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(providers, "_wait", advance)
    config = {"provider": "Custom", "base_url": "https://host/v1", "model": "m",
              "max_requests_per_window": 2, "request_window_seconds": 10,
              "min_request_interval_seconds": 0}
    key = "secret"

    async def run():
        scopes = await asyncio.gather(*[
            providers._slot(config, key, None) for _ in range(3)])
        return scopes

    scopes = asyncio.run(run())
    assert len(set(scopes)) == 1
    assert waits == [10]
    saved = json.loads((tmp_path / "request_pacing.json").read_text())
    stamps = next(iter(saved["requests"].values()))
    assert len(stamps) == 1  # The third reservation expired the first window entries.
    assert stamps[0] == 1010


def test_minimum_interval_is_enforced(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    now = [100.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    scope = "same provider and model"
    assert providers._reserve(scope, 5, 60, 5) == 0
    assert providers._reserve(scope, 5, 60, 5) == 5
    now[0] += 5
    assert providers._reserve(scope, 5, 60, 5) == 0


def test_limiter_scopes_are_independent_and_cancel_waits(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    a = {"provider": "Custom", "base_url": "https://a.example/v1", "model": "m",
         "max_requests_per_window": 1, "request_window_seconds": 30}
    b = {**a, "base_url": "https://b.example/v1"}
    assert asyncio.run(providers._slot(a, "key", None)) != asyncio.run(
        providers._slot(b, "key", None))
    alias = {**a, "provider": "Another display name"}
    assert providers._scope(a, "key") == providers._scope(alias, "key")
    assert providers._scope(a, "other-key") != providers._scope(a, "key")

    event = asyncio.Event()
    event.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(providers._wait(10, event))


def test_model_requests_retry_429_but_not_quota_or_auth(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    waits = []

    async def fake_wait(seconds, cancel_event, event=None, stats=None, label="Waiting"):
        waits.append(seconds)
        now[0] += seconds
        if stats is not None:
            stats["wait_seconds"] += seconds

    monkeypatch.setattr(providers, "_wait", fake_wait)

    class Response:
        def __init__(self, status, body="{}", headers=None):
            self.status_code = status
            self.text = body
            self.headers = headers or {}

    class Client:
        def __init__(self, responses): self.responses = list(responses); self.count = 0
        async def post(self, *args, **kwargs):
            self.count += 1
            return self.responses.pop(0)

    config = {"provider": "Custom", "base_url": "https://host/v1", "model": "m",
              "max_requests_per_window": 10, "request_window_seconds": 60,
              "retry_limit": 2, "max_backoff_seconds": 10}
    stats = {"attempted": 0, "successful": 0, "rate_limits": 0,
             "retries": 0, "waits": 0, "wait_seconds": 0.0}
    client = Client([Response(429, '{"error":{"code":"rate_limit_exceeded"}}',
                              {"Retry-After": "2"}), Response(200)])
    response = asyncio.run(providers.model_post(client, config, "key", "https://host/v1/chat",
        {}, {}, stats=stats))
    assert response.status_code == 200
    assert client.count == 2 and waits == [2]
    assert stats["attempted"] == 2 and stats["rate_limits"] == 1
    assert stats["retries"] == 1 and stats["successful"] == 1

    waits.clear()
    quota = Client([Response(429, '{"error":{"code":"insufficient_quota"}}')])
    failed = asyncio.run(providers.model_post(quota, config, "key", "https://host/v1/chat", {}, {}))
    assert failed.status_code == 429 and quota.count == 1 and not waits

    auth = Client([Response(401)])
    failed = asyncio.run(providers.model_post(auth, config, "key", "https://host/v1/chat", {}, {}))
    assert failed.status_code == 401 and auth.count == 1


def test_model_request_retries_connect_error_with_bounded_jitter(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    delays = []
    monkeypatch.setattr(providers.random, "uniform", lambda low, high: high)

    async def wait(seconds, *args, **kwargs): delays.append(seconds)
    monkeypatch.setattr(providers, "_wait", wait)

    class Response:
        status_code = 200
        text = ""
        headers = {}

    class Client:
        count = 0
        async def post(self, *args, **kwargs):
            self.count += 1
            if self.count == 1:
                raise providers.httpx.ConnectError("offline")
            return Response()

    config = {"provider": "Custom", "base_url": "https://host/v1", "model": "m",
              "max_requests_per_window": 5, "request_window_seconds": 60,
              "retry_limit": 1, "max_backoff_seconds": 1}
    client = Client()
    result = asyncio.run(providers.model_post(client, config, "key", "https://host/v1/chat", {}, {}))
    assert result.status_code == 200 and client.count == 2
    assert delays == [1]


def test_cancel_during_retry_wait_does_not_send_again(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    event = asyncio.Event()

    async def cancel_wait(seconds, cancel_event, *args, **kwargs):
        cancel_event.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(providers, "_wait", cancel_wait)

    class Response:
        status_code = 429
        text = '{"error":{"code":"rate_limit_exceeded"}}'
        headers = {"Retry-After": "1"}

    class Client:
        count = 0
        async def post(self, *args, **kwargs):
            self.count += 1
            return Response()

    config = {"provider": "Custom", "base_url": "https://host/v1", "model": "m",
              "max_requests_per_window": 5, "request_window_seconds": 60,
              "retry_limit": 2, "max_backoff_seconds": 10}
    client = Client()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(providers.model_post(client, config, "key", "https://host/v1/chat",
                                         {}, {}, event))
    assert client.count == 1


def test_rate_limit_longer_than_backoff_is_not_retried(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    class Response:
        status_code = 429
        text = '{"error":{"code":"rate_limit_exceeded"}}'
        headers = {"Retry-After": "60"}
    class Client:
        count = 0
        async def post(self, *args, **kwargs):
            self.count += 1
            return Response()
    config = {"provider": "Custom", "base_url": "https://host/v1", "model": "m",
              "max_requests_per_window": 5, "request_window_seconds": 60,
              "retry_limit": 2, "max_backoff_seconds": 5}
    client = Client()
    result = asyncio.run(providers.model_post(client, config, "key", "https://host/v1/chat", {}, {}))
    assert result.status_code == 429 and client.count == 1


def test_stream_retries_only_before_any_content_is_read(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    waits = []
    async def fake_wait(seconds, *args, **kwargs):
        waits.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(providers, "_wait", fake_wait)

    class Response:
        headers = {"Retry-After": "1"}
        def __init__(self, status): self.status_code = status
        async def aread(self): return b'{"error":{"code":"rate_limit_exceeded"}}'

    class Stream:
        def __init__(self, status): self.response = Response(status)
        async def __aenter__(self): return self.response
        async def __aexit__(self, *args): pass

    class Client:
        calls = 0
        def stream(self, *args, **kwargs):
            self.calls += 1
            return Stream(429 if self.calls == 1 else 200)

    config = {"provider": "Custom", "base_url": "https://host/v1", "model": "m",
              "max_requests_per_window": 5, "request_window_seconds": 60,
              "retry_limit": 1, "max_backoff_seconds": 5}
    client = Client()

    async def run():
        async with providers.model_stream(client, config, "key", "https://host/v1/chat", {}, {}):
            return True
    assert asyncio.run(run()) is True
    assert client.calls == 2 and waits == [1]


def test_stream_read_failure_is_not_replayed(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)

    class Response:
        status_code = 200
        headers = {}

    class Stream:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *args): pass

    class Client:
        calls = 0
        def stream(self, *args, **kwargs):
            self.calls += 1
            return Stream()

    config = {"provider": "Custom", "base_url": "https://host/v1", "model": "m",
              "max_requests_per_window": 5, "request_window_seconds": 60,
              "retry_limit": 3, "max_backoff_seconds": 5}
    client = Client()

    async def run():
        async with providers.model_stream(client, config, "key", "https://host/v1/chat", {}, {}):
            raise providers.httpx.ReadTimeout("stream ended partway through")

    with pytest.raises(providers.httpx.ReadTimeout):
        asyncio.run(run())
    assert client.calls == 1


def test_responses_adapter_converts_tools_and_tool_results():
    chat_tools = [{"type": "function", "function": {
        "name": "read_file", "description": "Read text", "parameters": {"type": "object"}}}]
    messages = [
        {"role": "system", "content": "Stay safe."},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "file text"},
    ]
    body = agent_protocols.response_body("test-model", messages, chat_tools)
    assert body["input"][-2]["type"] == "function_call"
    assert body["input"][-1] == {
        "type": "function_call_output", "call_id": "call-1", "output": "file text"}
    assert body["tools"][0]["name"] == "read_file"


def test_responses_parser_handles_calls_text_refusal_and_incomplete():
    result = agent_protocols.responses_result({
        "id": "resp-1", "model": "served-model", "status": "completed",
        "output_text": "Done", "usage": {"total_tokens": 7},
        "output": [{"type": "function_call", "call_id": "call-2",
                    "name": "list_files", "arguments": "{\"path\":\".\"}"}],
    })
    assert result["message"]["content"] == "Done"
    assert result["message"]["tool_calls"][0]["id"] == "call-2"
    assert result["usage"]["total_tokens"] == 7
    with pytest.raises(ValueError, match="incomplete"):
        agent_protocols.responses_result({"status": "incomplete", "output": []})
    with pytest.raises(PermissionError, match="declined"):
        agent_protocols.responses_result({"status": "completed", "output": [
            {"type": "refusal", "refusal": "No"}]})


def test_agent_completes_responses_tool_cycle(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    projects.open_project(str(project))
    providers.save_provider(provider(protocol="responses"))
    calls = []
    results = [
        {"id": "resp-1", "model": "test-model", "status": "completed", "output": [
            {"type": "function_call", "call_id": "c1", "name": "write_file",
             "arguments": json.dumps({"path": "note.txt", "content": "hello", "expected_sha256": None})}]},
        {"id": "resp-2", "model": "test-model", "status": "completed", "output": [
            {"type": "function_call", "call_id": "c2", "name": "read_file",
             "arguments": json.dumps({"path": "note.txt"})}]},
        {"id": "resp-3", "model": "test-model", "status": "completed",
         "output_text": "Created and checked note.txt.", "output": [
             {"type": "message", "content": [{"type": "output_text", "text": "Created and checked note.txt."}]}]},
    ]

    class Reply:
        status_code = 200
        def __init__(self, data): self.data = data
        def json(self): return self.data

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return Reply(results.pop(0))

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    task = main.TaskInput(message="Write and verify note.txt", mode="workspace")
    result = asyncio.run(main._agent(task))
    assert result["answer"] == "Created and checked note.txt."
    assert result["iterations"] == 2
    assert (project / "note.txt").read_text(encoding="utf-8") == "hello"
    assert all(url.endswith("/responses") for url, _ in calls)
    assert calls[1][1]["input"][-1]["type"] == "function_call_output"
    assert result["request_stats"]["attempted"] == 3
    assert result["request_stats"]["successful"] == 3


def test_streaming_chat_builds_tool_calls_and_emits_text(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="write a note"))

    class StreamReply:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            values = [
                {"model": "stream-model", "choices": [{"delta": {"content": "Hello"}}]},
                {"model": "stream-model", "choices": [{"delta": {"content": " there"}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
                    "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}}]}}]},
                "[DONE]",
            ]
            for value in values:
                await asyncio.sleep(0)
                yield "data: " + (value if isinstance(value, str) else json.dumps(value))

    def fake_stream(self, *args, **kwargs):
        return StreamReply()

    monkeypatch.setattr(main.httpx.AsyncClient, "stream", fake_stream)
    async def run():
        async with main.httpx.AsyncClient() as client:
            return await main._stream_response(client, "https://provider.example/v1/chat/completions",
                {}, {}, "chat_completions", "test-key", asyncio.Event(), task_id)
    data = asyncio.run(run())
    result = agent_protocols.chat_result(data)
    assert result["message"]["content"] == "Hello there"
    assert result["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    details = [event["detail"] for event in main._task_state(task_id)["events"]]
    assert "Hello there" in "".join(details)


def test_streaming_responses_parses_text_and_function_calls(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)

    class StreamReply:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            events = [
                {"type": "response.output_text.delta", "delta": "Done"},
                {"type": "response.output_item.done", "item": {
                    "type": "function_call", "call_id": "call-1", "name": "read_file",
                    "arguments": "{\"path\":\"a.txt\"}"}},
                {"type": "response.completed", "response": {
                    "id": "resp-1", "model": "served-model", "status": "completed",
                    "output_text": "Done", "output": [{"type": "function_call",
                        "call_id": "call-1", "name": "read_file",
                        "arguments": "{\"path\":\"a.txt\"}"}]}},
            ]
            for item in events:
                yield "data: " + json.dumps(item)

    monkeypatch.setattr(main.httpx.AsyncClient, "stream",
                        lambda *args, **kwargs: StreamReply())
    async def run():
        async with main.httpx.AsyncClient() as client:
            data = await main._stream_response(client, "https://provider.example/v1/responses",
                {}, {}, "responses", "test-key", None, None)
        return agent_protocols.responses_result(data)
    result = asyncio.run(run())
    assert result["message"]["content"] == "Done"
    assert result["message"]["tool_calls"][0]["id"] == "call-1"


def test_stream_provider_error_is_categorized_and_sse_disconnect_closes(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)

    class ErrorReply:
        status_code = 429
        headers = {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aread(self): return b'{"error":{"code":"rate_limit"}}'

    monkeypatch.setattr(main.httpx.AsyncClient, "stream",
                        lambda *args, **kwargs: ErrorReply())
    async def failing_request():
        async with main.httpx.AsyncClient() as client:
            await main._stream_response(client, "https://provider.example/v1/chat/completions",
                {}, {}, "chat_completions", "test-key", None, None)
    with pytest.raises(Exception, match="rate_limit"):
        asyncio.run(failing_request())

    project = tmp_path / "project"
    project.mkdir()
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="disconnect"))

    class Disconnected:
        headers = {}
        async def is_disconnected(self): return True

    response = asyncio.run(main.stream_task_events(task_id, Disconnected()))
    async def read_once():
        with pytest.raises(StopAsyncIteration):
            await response.body_iterator.__anext__()
    asyncio.run(read_once())


def test_streaming_response_can_be_cancelled(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)

    class SlowReply:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            await asyncio.sleep(10)
            yield "data: [DONE]"

    monkeypatch.setattr(main.httpx.AsyncClient, "stream", lambda *args, **kwargs: SlowReply())
    cancel = asyncio.Event()

    async def run():
        async with main.httpx.AsyncClient() as client:
            job = asyncio.create_task(main._stream_response(client, "https://example.test",
                {}, {}, "chat_completions", "test-key", cancel, None))
            await asyncio.sleep(0.02)
            cancel.set()
            with pytest.raises(asyncio.CancelledError):
                await job
    asyncio.run(run())


def test_task_event_stream_replays_once_and_finishes(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="show events"))
    main._task_event(task_id, "assistant_delta", "hello")
    main._set_task(task_id, "completed")
    response = TestClient(main.app).get(f"/api/tasks/{task_id}/events")
    assert response.status_code == 200
    assert response.text.count('"detail": "hello"') == 1
    assert "id: 1" in response.text


def test_file_move_and_recursive_delete_require_approval_and_restore(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "old").mkdir()
    (project / "old" / "one.txt").write_text("one", encoding="utf-8")
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="change files", mode="workspace"))

    async def approve(*args, **kwargs): return True
    monkeypatch.setattr(main, "_request_approval", approve)
    moved = asyncio.run(main._run_tool("move_path", {"source": "old", "destination": "new"},
        project, "workspace", task_id=task_id))
    assert "verified 1 files" in moved
    assert (project / "new" / "one.txt").read_text(encoding="utf-8") == "one"
    client = TestClient(main.app)
    changes = client.get(f"/api/tasks/{task_id}/changes").json()["changes"]
    assert changes[0]["operation"] == "move"
    assert client.post(f"/api/changes/{changes[0]['id']}/restore").status_code == 200
    assert (project / "old" / "one.txt").read_text(encoding="utf-8") == "one"

    removed = asyncio.run(main._run_tool("delete_path", {"path": "old"},
        project, "workspace", task_id=task_id))
    assert "recorded 1 files" in removed
    assert not (project / "old").exists()
    deletion = client.get(f"/api/tasks/{task_id}/changes").json()["changes"][-1]
    assert deletion["operation"] == "delete"
    assert client.post(f"/api/changes/{deletion['id']}/restore").status_code == 200
    assert (project / "old" / "one.txt").read_text(encoding="utf-8") == "one"

    (project / "empty").mkdir()
    moved_empty = asyncio.run(main._run_tool("move_path", {
        "source": "empty", "destination": "empty-moved"}, project, "workspace", task_id=task_id))
    assert "verified 1 files" in moved_empty
    empty_move = client.get(f"/api/tasks/{task_id}/changes").json()["changes"][-1]
    assert empty_move["entry_type"] == "directory"
    assert client.post(f"/api/changes/{empty_move['id']}/restore").status_code == 200
    assert (project / "empty").is_dir()
    removed_empty = asyncio.run(main._run_tool("delete_path", {"path": "empty"},
        project, "workspace", task_id=task_id))
    assert "recorded 1 files" in removed_empty
    empty_delete = client.get(f"/api/tasks/{task_id}/changes").json()["changes"][-1]
    assert client.post(f"/api/changes/{empty_delete['id']}/restore").status_code == 200
    assert (project / "empty").is_dir()


def test_file_operations_reject_conflicts_paths_and_unapproved_actions(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "exists.txt").write_text("data", encoding="utf-8")
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="move safely", mode="workspace"))
    async def deny(*args, **kwargs): return False
    monkeypatch.setattr(main, "_request_approval", deny)
    conflict = asyncio.run(main._run_tool("move_path", {
        "source": "exists.txt", "destination": "exists.txt"}, project, "workspace", task_id=task_id))
    assert "already exists" in conflict
    denied = asyncio.run(main._run_tool("delete_path", {"path": "exists.txt"},
        project, "workspace", task_id=task_id))
    assert "denied" in denied.lower()
    outside = asyncio.run(main._run_tool("delete_path", {"path": "../outside"},
        project, "workspace", task_id=task_id))
    assert "selected project" in outside.lower()
    assert (project / "exists.txt").exists()


def test_move_restore_removes_only_verified_duplicate(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.txt"
    moved = project / "moved.txt"
    source.write_text("original", encoding="utf-8")
    moved.write_text("original", encoding="utf-8")
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="move", mode="workspace"))
    change_id = main._save_operation(task_id, "source.txt", "move", b"original", "moved.txt")
    db = main._db()
    digest = main.hashlib.sha256(b"original").hexdigest()
    db.execute("UPDATE file_changes SET after_hash=?,status='applied' WHERE id=?",
               (digest, change_id))
    db.commit()
    db.close()

    client = TestClient(main.app)
    response = client.post(f"/api/changes/{change_id}/restore")
    assert response.status_code == 200
    assert source.read_text(encoding="utf-8") == "original"
    assert not moved.exists()

    second = project / "second.txt"
    duplicate = project / "duplicate.txt"
    second.write_text("changed", encoding="utf-8")
    duplicate.write_text("original", encoding="utf-8")
    other_id = main._save_operation(task_id, "second.txt", "move", b"original", "duplicate.txt")
    db = main._db()
    db.execute("UPDATE file_changes SET after_hash=?,status='applied' WHERE id=?",
               (digest, other_id))
    db.commit()
    db.close()

    blocked = client.post(f"/api/changes/{other_id}/restore")
    assert blocked.status_code == 409
    assert second.read_text(encoding="utf-8") == "changed"
    assert duplicate.read_text(encoding="utf-8") == "original"


def test_restore_rejects_paths_through_symlinks(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep me", encoding="utf-8")
    try:
        (project / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="restore", mode="workspace"))
    change_id = main._save_operation(task_id, "escape/secret.txt", "delete", b"before")
    db = main._db()
    db.execute("UPDATE file_changes SET status='applied' WHERE id=?", (change_id,))
    db.commit()
    db.close()

    response = TestClient(main.app).post(f"/api/changes/{change_id}/restore")

    assert response.status_code == 409
    assert secret.read_text(encoding="utf-8") == "keep me"


def test_task_diffs_are_scoped_to_the_active_project(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    projects.open_project(str(first))
    task_id = main._create_task(main.TaskInput(message="change", mode="workspace"))
    main._save_operation(task_id, "private.py", "write", b"private diff")
    projects.open_project(str(second))

    response = TestClient(main.app).get(f"/api/tasks/{task_id}/changes")

    assert response.status_code == 409


def test_interrupted_task_resumes_only_when_no_tool_started(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    projects.open_project(str(project))
    task_id = main._create_task(main.TaskInput(message="inspect", mode="workspace"))
    main._set_task(task_id, "interrupted", "server stopped")
    assert main._task_state(task_id)["can_resume"] is True
    outcomes = []

    async def run_job(task_id, task, cancel):
        outcomes.append((task_id, task.mode))
        main._set_task(task_id, "completed")

    monkeypatch.setattr(main, "_run_job", run_job)
    with TestClient(main.app) as client:
        response = client.post(f"/api/tasks/{task_id}/resume", json={"mode": "read_only"})
        assert response.status_code == 200
        assert response.json()["mode"] == "read_only"
        for _ in range(20):
            if outcomes:
                break
            __import__("time").sleep(0.01)
        assert outcomes == [(task_id, "read_only")]

    second = main._create_task(main.TaskInput(message="inspect"))
    main._task_event(second, "tool", "Started write_file")
    main._set_task(second, "interrupted", "server stopped")
    assert main._task_state(second)["can_resume"] is False
    with TestClient(main.app) as client:
        blocked = client.post(f"/api/tasks/{second}/resume", json={"mode": "workspace"})
    assert blocked.status_code == 409
    assert "cannot be replayed safely" in blocked.json()["detail"]


def test_skills_install_enable_inspect_and_remove_as_untrusted_data(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    source = tmp_path / "sample-skill"
    source.mkdir()
    (source / "skill.json").write_text(json.dumps({
        "name": "review-notes", "description": "Review small changes.", "version": "1.0.0",
        "tools": ["read_file"]}), encoding="utf-8")
    (source / "SKILL.md").write_text("Check correctness and tests.", encoding="utf-8")
    (source / "unused.py").write_text("raise RuntimeError('must not run')", encoding="utf-8")
    client = TestClient(main.app)
    installed = client.post("/api/skills/install", json={"source": str(source)})
    assert installed.status_code == 200
    skill_id = installed.json()["id"]
    assert installed.json()["enabled"] is False
    enabled = client.put(f"/api/skills/{skill_id}/enabled", json={"enabled": True})
    assert enabled.status_code == 200
    detail = client.get(f"/api/skills/{skill_id}").json()
    assert detail["instructions"] == "Check correctness and tests."
    assert "unused.py" in detail["files"]
    assert skills.enabled_instructions() == [(skill_id, "Check correctness and tests.")]
    assert client.delete(f"/api/skills/{skill_id}").status_code == 200
    assert client.get("/api/skills").json()["skills"] == []


def test_skills_reject_invalid_metadata_and_traversal(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    source = tmp_path / "bad-skill"
    source.mkdir()
    (source / "skill.json").write_text("{}", encoding="utf-8")
    (source / "SKILL.md").write_text("instructions", encoding="utf-8")
    with TestClient(main.app) as client:
        bad = client.post("/api/skills/install", json={"source": str(source)})
    assert bad.status_code == 422
    with pytest.raises(Exception, match="Skill ID"):
        skills.inspect_skill("../providers")


def test_mcp_http_connect_enable_and_tool_call(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    replies = [
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": mcp_client.MODERN}},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
            "name": "lookup", "description": "Look up a value", "inputSchema": {
                "type": "object", "properties": {
                    "query": {"type": "string"},
                    "region": {"type": "string", "x-mcp-header": "Region"}},
                "required": ["query"], "additionalProperties": False}}]}},
        {"jsonrpc": "2.0", "id": 99, "result": {"content": [
            {"type": "text", "text": "Found one safe result."}]}},
    ]
    calls = []

    class Reply:
        status_code = 200
        headers = {"content-type": "application/json"}
        def __init__(self, data): self.text = json.dumps(data)

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            calls.append((headers, json))
            return Reply(replies.pop(0))

    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", Client)
    server = mcp_client.add_server("Local tools", "http://127.0.0.1:8000/mcp")
    assert server["enabled"] is False
    connected = asyncio.run(mcp_client.connect_server(server["id"]))
    assert connected["connected"] is True
    assert connected["protocol"] == mcp_client.MODERN
    assert connected["tools"][0]["name"] == "lookup"
    pending = mcp_client.add_server("Not connected", "http://127.0.0.1:8001/mcp")
    with pytest.raises(Exception, match="before enabling"):
        mcp_client.set_enabled(pending["id"], True)
    enabled = mcp_client.set_enabled(server["id"], True)
    assert enabled["enabled"] is True
    spec = mcp_client.tool_specs()[0]["function"]
    assert spec["name"].startswith("mcp_")
    answer = asyncio.run(mcp_client.call_tool(spec["name"], {"query": "hello", "region": "ap-south-1"}))
    assert answer == "Found one safe result."
    assert calls[-1][0]["Mcp-Method"] == "tools/call"
    assert calls[-1][0]["Mcp-Name"] == "lookup"
    assert calls[-1][0]["Mcp-Param-Region"] == "ap-south-1"
    assert calls[-1][1]["params"]["arguments"] == {"query": "hello", "region": "ap-south-1"}
    with pytest.raises(ValueError, match="required fields"):
        asyncio.run(mcp_client.call_tool(spec["name"], {}))
    mcp_client.remove_server(server["id"])
    mcp_client.remove_server(pending["id"])
    assert mcp_client.list_servers() == []


def test_mcp_rejects_plain_remote_urls_and_bad_addresses(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    with pytest.raises(Exception, match="HTTPS"):
        mcp_client.add_server("Remote", "http://10.0.0.1/mcp")
    monkeypatch.setattr(mcp_client.socket, "getaddrinfo", lambda *args: [
        (None, None, None, None, ("10.0.0.5", 0))])
    with pytest.raises(Exception, match="public addresses"):
        mcp_client._check_address("https://mcp.example/mcp")


def test_mcp_modern_transport_against_local_http_fixture(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            method = request["method"]
            assert self.headers.get("MCP-Protocol-Version") == mcp_client.MODERN
            assert self.headers.get("Mcp-Method") == method
            if method == "server/discover":
                assert request["params"]["_meta"][
                    "io.modelcontextprotocol/protocolVersion"] == mcp_client.MODERN
                result = {"protocolVersion": mcp_client.MODERN}
            elif method == "tools/list":
                result = {"tools": [{"name": "ping", "inputSchema": {
                    "type": "object", "properties": {}, "additionalProperties": False}}]}
            elif method == "tools/call":
                assert self.headers.get("Mcp-Name") == "ping"
                result = {"content": [{"type": "text", "text": "pong"}]}
            else:
                self.send_error(404)
                return
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        item = mcp_client.add_server("Fixture", f"http://127.0.0.1:{server.server_port}/mcp")
        connected = asyncio.run(mcp_client.connect_server(item["id"]))
        mcp_client.set_enabled(item["id"], True)
        alias = mcp_client.tool_specs()[0]["function"]["name"]
        assert connected["protocol"] == mcp_client.MODERN
        assert asyncio.run(mcp_client.call_tool(alias, {})) == "pong"
    finally:
        mcp_client.sessions.clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_browser_network_filter_and_missing_runtime_status(monkeypatch):
    monkeypatch.setattr(browser_tools.socket, "getaddrinfo", lambda *args: [
        (None, None, None, None, ("8.8.8.8", 0))])
    assert asyncio.run(browser_tools._allowed("https://example.com", False)) is True
    assert asyncio.run(browser_tools._allowed("http://example.com", True)) is False
    assert asyncio.run(browser_tools._allowed("https://127.0.0.1:8000", False)) is False
    assert asyncio.run(browser_tools._allowed("http://127.0.0.1:8000", True)) is True
    assert asyncio.run(browser_tools._allowed("file:///C:/secret.txt", True)) is False
    assert browser_tools.status()["active"] is False


def test_browser_plays_local_fixture_and_cleans_up(tmp_path, monkeypatch):
    pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(browser_tools, "_screens", tmp_path / "screenshots")
    html = b"<!doctype html><title>Start</title><h1>Local test page</h1>"
    html += b"<input id='name'><button id='go' onclick=\"document.title='Clicked'\">Go</button>"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        def log_message(self, *args): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        try:
            state = await browser_tools.start(allow_local=True)
            assert state["active"] is True
            url = f"http://127.0.0.1:{server.server_port}/"
            await browser_tools.navigate(url)
            await browser_tools.wait_for("#name", "visible")
            await browser_tools.fill("#name", "Luna")
            page = await browser_tools.inspect_page()
            assert "Local test page" in page["text"]
            await browser_tools.click("#go")
            shot = await browser_tools.screenshot()
            assert browser_tools.screenshot_path(shot["name"]).is_file()
            state = await browser_tools.close()
            assert state["installed"] is True and state["active"] is False
        finally:
            if browser_tools.status()["active"]:
                await browser_tools.close()
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_desktop_automation_requires_enable_and_checks_selected_controls(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    class Control:
        element_info = SimpleNamespace(control_type="Edit", automation_id="search")
        def __init__(self): self.value = ""; self.clicked = False
        def window_text(self): return "Search box"
        def is_enabled(self): return True
        def click_input(self): self.clicked = True
        def set_edit_text(self, value): self.value = value

    control = Control()

    class Window:
        handle = 123
        def exists(self, timeout=1): return True
        def window_text(self): return "Harmless Test Window"
        def class_name(self): return "TestWindow"
        def descendants(self): return [control]

    window = Window()

    class Desktop:
        def __init__(self, backend): assert backend == "uia"
        def windows(self, top_level_only=True): return [window]
        def window(self, handle): assert handle == 123; return window

    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=Desktop))
    monkeypatch.setattr(desktop_tools, "SCREEN_DIR", tmp_path / "screens")
    assert desktop_tools.status()["available"] is True
    with pytest.raises(Exception, match="Enable desktop"):
        desktop_tools.windows()
    desktop_tools.set_enabled(True)
    assert desktop_tools.windows()[0]["title"] == "Harmless Test Window"
    desktop_tools.select("0x7b")
    assert desktop_tools.inspect("0x7b")["controls"][0]["automation_id"] == "search"
    assert desktop_tools.click("0x7b", 0)["ok"] is True
    assert control.clicked is True
    desktop_tools.type_text("0x7b", 0, "safe text")
    assert control.value == "safe text"
    with pytest.raises(Exception, match="Credential-like"):
        desktop_tools.type_text("0x7b", 0, "API_KEY=private-value-123")
    assert all(item["function"]["name"] != "desktop_windows"
               for item in desktop_tools.tool_specs())
    desktop_tools.set_enabled(False)


def test_terminal_event_contains_actual_command_output(monkeypatch, tmp_path):
    events = []
    approvals = []

    class Process:
        pid = 101
        returncode = 3

        async def communicate(self):
            return b"real command output\n", None

    async def create_process(*args, **kwargs):
        return Process()

    async def approve(task_id, description, cancel_event):
        approvals.append((task_id, description))
        return True

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(main, "_request_approval", approve)
    monkeypatch.setattr(main, "_task_event", lambda task_id, kind, detail: events.append((task_id, kind, detail)))
    result = asyncio.run(main._run_tool("run_command", {"command": "Get-ChildItem"},
                                        tmp_path, "workspace", task_id=99))
    assert json.loads(result) == {"exit_code": 3, "output": "real command output\n"}
    assert approvals[0][0] == 99
    assert "not sandboxed to the project" in approvals[0][1]
    assert "Get-ChildItem" in approvals[0][1]
    assert events == [(99, "command", "Command finished with exit code 3\nreal command output\n")]


@pytest.mark.parametrize("tool,args", [
    ("list_files", {"path": "../outside"}),
    ("read_file", {"path": "../outside/secret.txt"}),
    ("search_files", {"path": "../outside", "query": "secret"}),
    ("write_file", {"path": "../outside/secret.txt", "content": "changed", "expected_sha256": None}),
    ("move_path", {"source": "../outside/secret.txt", "destination": "inside.txt"}),
    ("move_path", {"source": "inside.txt", "destination": "../outside/moved.txt"}),
    ("delete_path", {"path": "../outside/secret.txt"}),
])
def test_workspace_tools_reject_parent_traversal(tool, args, tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep me", encoding="utf-8")
    (root / "inside.txt").write_text("inside", encoding="utf-8")

    result = asyncio.run(main._run_tool(tool, args, root, "workspace", task_id=1))

    assert "relative to the selected project" in result
    assert secret.read_text(encoding="utf-8") == "keep me"
    assert not (outside / "moved.txt").exists()


@pytest.mark.parametrize("tool,args", [
    ("list_files", None),
    ("read_file", None),
    ("search_files", {"query": "secret"}),
    ("write_file", {"content": "changed", "expected_sha256": None}),
    ("move_path", {"destination": "inside.txt"}),
    ("delete_path", None),
])
def test_workspace_tools_reject_absolute_paths(tool, args, tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep me", encoding="utf-8")
    arguments = dict(args or {})
    if tool == "move_path":
        arguments["source"] = str(secret)
    else:
        arguments["path"] = str(secret)
    if tool == "search_files":
        arguments["path"] = str(outside)
    if tool == "list_files":
        arguments["path"] = str(outside)

    result = asyncio.run(main._run_tool(tool, arguments, root, "workspace", task_id=1))

    assert "relative to the selected project" in result
    assert secret.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("tool,args", [
    ("list_files", {"path": "escape"}),
    ("read_file", {"path": "escape/secret.txt"}),
    ("search_files", {"path": "escape", "query": "secret"}),
    ("write_file", {"path": "escape/secret.txt", "content": "changed", "expected_sha256": None}),
    ("move_path", {"source": "escape/secret.txt", "destination": "inside.txt"}),
    ("delete_path", {"path": "escape/secret.txt"}),
])
def test_workspace_tools_reject_symlink_escapes(tool, args, tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep me", encoding="utf-8")
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    result = asyncio.run(main._run_tool(tool, args, root, "workspace", task_id=1))

    assert ("symlinks or junctions" in result
            or "outside the selected workspace" in result)
    assert secret.read_text(encoding="utf-8") == "keep me"


def test_workspace_search_skips_nested_symlinked_directories(tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("needle outside", encoding="utf-8")
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    result = asyncio.run(main._run_tool("search_files", {
        "path": ".", "query": "needle"}, root, "read_only"))

    assert json.loads(result) == []


def test_workspace_target_rejects_absolute_and_traversal_paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(ValueError, match="relative to the selected project"):
        main._target(root, str(root / "file.txt"))
    with pytest.raises(ValueError, match="relative to the selected project"):
        main._target(root, "child/../file.txt")
