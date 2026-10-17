import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

from desktop import native_app


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def test_native_window_uses_shared_provider_and_project_apis(app, monkeypatch, tmp_path):
    project = tmp_path / "NativeProject"
    project.mkdir()
    (project / "main.py").write_text("print('hello')", encoding="utf-8")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/projects":
            return {"active_project": None, "recent_projects": []}
        if path == "/api/providers":
            return {"providers": [{"id": "p1", "name": "Test", "model": "test-model", "is_default": True, "key_configured": True}]}
        if path == "/api/projects/open":
            return {"active_project": str(project), "recent_projects": [{"path": str(project), "name": project.name}]}
        raise AssertionError((method, path))

    monkeypatch.setattr(native_app, "request", fake_request)
    window = native_app.LunaWindow(native_app.Backend())
    window.set_project(str(project))
    assert window.project == project
    assert window.files.rootIndex().data() == project.name
    assert "test-model" in window.provider_label.text()
    assert any(path == "/api/projects/open" for _, path, _ in calls)
    assert any(path == "/api/providers" for _, path, _ in calls)
    window.settings = QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)
    window.close()


def test_native_provider_key_entry_is_masked(app, monkeypatch):
    monkeypatch.setattr(native_app, "request", lambda method, path, **kwargs: {"providers": []})
    dialog = native_app.ProviderDialog()
    assert dialog.fields["api_key"].echoMode() == dialog.fields["api_key"].EchoMode.Password
    dialog.close()


def test_backend_reuses_existing_local_service(monkeypatch):
    class Response:
        is_success = True

    monkeypatch.setattr(native_app.httpx, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(native_app.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not start a duplicate backend"))
    backend = native_app.Backend()
    backend.start()
    assert backend.process is None


def test_backend_stops_only_the_child_it_started(monkeypatch, tmp_path):
    class Response:
        def __init__(self, success):
            self.is_success = success

    replies = iter([Response(False), Response(True)])
    monkeypatch.setattr(native_app, "ROOT", tmp_path)
    monkeypatch.setattr(native_app.httpx, "get", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(native_app.time, "sleep", lambda _: None)

    class Child:
        def __init__(self):
            self.stopped = False

        def poll(self):
            return None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            return 0

    child = Child()
    monkeypatch.setattr(native_app.subprocess, "Popen", lambda *args, **kwargs: child)
    backend = native_app.Backend()
    backend.start()
    assert backend.process is child
    backend.stop()
    assert child.stopped
    assert backend.process is None
