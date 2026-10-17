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
    dialog.numeric["retry_limit"].setValue(4)
    assert dialog.payload()["retry_limit"] == 4
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


def test_task_submission_names_the_selected_workspace(monkeypatch):
    calls = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            return iter(())

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"id": 7} if path == "/api/tasks" else {"id": 7, "status": "completed"}

    monkeypatch.setattr(native_app, "request", fake_request)
    monkeypatch.setattr(native_app.httpx, "stream", lambda *args, **kwargs: Stream())
    worker = native_app.TaskWorker("Inspect this project", "read_only", r"D:\Projects\Native")
    worker.start()
    assert worker.wait(5000)
    post = next(call for call in calls if call[1] == "/api/tasks")
    assert post[2]["json"]["workspace"] == r"D:\Projects\Native"


def test_native_integrations_settings_share_existing_apis(app, monkeypatch):
    paths = []

    def fake_request(method, path, **kwargs):
        paths.append(path)
        if path == "/api/skills":
            return {"skills": []}
        if path == "/api/mcp":
            return {"servers": []}
        if path == "/api/browser/status":
            return {"installed": False, "active": False}
        if path == "/api/desktop/status":
            return {"available": False, "enabled": False}
        raise AssertionError(path)

    monkeypatch.setattr(native_app, "request", fake_request)
    dialog = native_app.IntegrationsDialog()
    assert paths == ["/api/skills", "/api/mcp", "/api/browser/status", "/api/desktop/status"]
    assert "browser automation" in dialog.browser_status.text()
    assert not dialog.desktop_toggle.isEnabled()
    dialog.close()


def test_terminal_uses_owned_persistent_process_and_streams_interactive_io(app, tmp_path):
    import shutil
    from PySide6.QtCore import QEventLoop, QTimer

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is not installed.")
    terminal = native_app.InteractiveTerminal()
    terminal.shell_program = shell
    terminal.set_directory(tmp_path)
    terminal.start()
    worker = terminal.worker
    assert worker is not None
    loop = QEventLoop()
    def send_line(value):
        terminal.input.setText(value)
        terminal.send_input()

    QTimer.singleShot(900, lambda: send_line(
        "$value = Read-Host 'Input'; Write-Output ('got:' + $value); exit 7"))
    QTimer.singleShot(1800, lambda: send_line("interactive-value"))
    worker.finished.connect(loop.quit)
    QTimer.singleShot(12_000, loop.quit)
    loop.exec()
    assert not worker.isRunning()
    assert "got:interactive-value" in terminal.output.toPlainText()
    assert "code 7" in terminal.output.toPlainText()
    terminal.shutdown()


def test_terminal_bounds_output_marks_stderr_and_has_a_runtime_timeout(app):
    terminal = native_app.InteractiveTerminal()
    terminal._append("x" * 1_000_010)
    assert terminal.output.document().characterCount() <= 1_000_001
    assert terminal.runtime_timer.interval() == 4 * 60 * 60 * 1000
    terminal.shutdown()


def test_changed_file_system_model_marks_paths(app, tmp_path):
    model = native_app.ChangedFileSystemModel()
    target = tmp_path / "changed.py"
    target.write_text("x", encoding="utf-8")
    index = model.index(str(target))
    model.set_changed_paths({str(target): "Modified"})
    assert "[Modified]" in model.data(index)


def test_native_preview_blocks_protected_project_files(app, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    secret = project / ".env"
    secret.write_text("API_KEY=secret-preview-value", encoding="utf-8")
    monkeypatch.setattr(native_app, "request", lambda method, path, **kwargs: (
        {"active_project": None, "recent_projects": []} if path == "/api/projects"
        else {"providers": []} if path == "/api/providers" else {"active_project": str(project)}))
    window = native_app.LunaWindow(native_app.Backend())
    window.set_project(str(project))
    window.preview_path(secret)
    assert not hasattr(window, "_preview")
    assert "protected" in window.status.currentMessage().lower()
    window.backend.stop = lambda: None
    window.close()
