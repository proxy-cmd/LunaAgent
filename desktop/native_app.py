"""Native Qt front end for the shared Luna Desk FastAPI agent engine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from PySide6.QtCore import QDir, QLockFile, QSettings, QThread, Qt, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFileSystemModel,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QStatusBar, QTreeView, QVBoxLayout,
    QWidget, QDockWidget, QInputDialog,
)

BASE_URL = "http://127.0.0.1:8765"
ROOT = Path(__file__).resolve().parents[1]


class Backend:
    """Reuse a local Luna service when present, otherwise own one child process."""
    def __init__(self):
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        try:
            if httpx.get(BASE_URL + "/api/setup-status", timeout=0.7).is_success:
                return
        except httpx.HTTPError:
            pass
        log_dir = ROOT / ".luna"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "desktop-backend.log").open("ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app", "--host",
             "127.0.0.1", "--port", "8765"], cwd=ROOT, stdout=log,
            stderr=subprocess.STDOUT, creationflags=flags,
        )
        for _ in range(80):
            if self.process.poll() is not None:
                raise RuntimeError("Luna backend stopped during startup. See .luna/desktop-backend.log.")
            try:
                if httpx.get(BASE_URL + "/api/setup-status", timeout=0.5).is_success:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError("Luna backend did not become ready. See .luna/desktop-backend.log.")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = None


def request(method: str, path: str, **kwargs):
    try:
        response = httpx.request(method, BASE_URL + path, timeout=10, **kwargs)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Luna Desk: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not response.is_success:
        detail = data.get("detail", data.get("message", response.reason_phrase)) if isinstance(data, dict) else data
        if isinstance(detail, list):
            detail = "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("msg") or json.dumps(detail, ensure_ascii=False)
        raise RuntimeError(str(detail))
    return data


class TaskWorker(QThread):
    event = Signal(str, str)
    approval_requested = Signal(dict)
    finished_task = Signal(dict)
    failed = Signal(str)

    def __init__(self, message: str, mode: str):
        super().__init__()
        self.message = message
        self.mode = mode
        self.task_id: int | None = None

    def run(self):
        try:
            started = request("POST", "/api/tasks", json={"message": self.message, "mode": self.mode})
            self.task_id = started["id"]
            cursor = 0
            # The existing SSE endpoint replays persisted events and closes when the task ends.
            with httpx.stream("GET", f"{BASE_URL}/api/tasks/{self.task_id}/events", timeout=None) as response:
                response.raise_for_status()
                data_lines = []
                for line in response.iter_lines():
                    if self.isInterruptionRequested():
                        break
                    if line.startswith("id:"):
                        try:
                            cursor = int(line[3:].strip())
                        except ValueError:
                            pass
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line and data_lines:
                        try:
                            item = json.loads("\n".join(data_lines))
                            kind = item.get("kind", "event")
                            self.event.emit(kind, item.get("detail", ""))
                            if kind == "approval":
                                state = request("GET", f"/api/tasks/{self.task_id}")
                                if state.get("approval"):
                                    self.approval_requested.emit(state["approval"])
                        except (ValueError, TypeError):
                            self.event.emit("error", "Received an unreadable task event.")
                        data_lines.clear()
            state = request("GET", f"/api/tasks/{self.task_id}")
            self.finished_task.emit(state)
        except Exception as exc:
            self.failed.emit(str(exc))


class TaskComposer(QPlainTextEdit):
    submitRequested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
            self.submitRequested.emit()
            return
        super().keyPressEvent(event)


class LunaWindow(QMainWindow):
    def __init__(self, backend: Backend):
        super().__init__()
        self.backend = backend
        self.settings = QSettings("LunaDesk", "LunaDesk")
        self.worker: TaskWorker | None = None
        self.active_task: int | None = None
        self.project: Path | None = None
        self.setWindowTitle("Luna Desk")
        self.resize(1420, 900)
        self.setMinimumSize(900, 620)
        self._build()
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.refresh_projects()
        self.refresh_provider()

    def _build(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#17181e; color:#e7e7ed; }
            QLineEdit, QPlainTextEdit, QTreeView, QComboBox { background:#20212a; border:1px solid #343641; border-radius:6px; padding:7px; selection-background-color:#665bd6; }
            QPushButton { background:#282a34; border:1px solid #3a3c47; border-radius:6px; padding:8px 13px; }
            QPushButton:hover { border-color:#8e83fa; } QPushButton:disabled { color:#777; }
            QDockWidget::title { background:#20212a; padding:7px; }
            QStatusBar { background:#20212a; }
        """)
        bar = self.addToolBar("Main")
        bar.setMovable(False)
        open_action = QAction("Open Project", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.choose_project)
        bar.addAction(open_action)
        history_action = QAction("Task history", self)
        history_action.setShortcut("Ctrl+H")
        history_action.triggered.connect(self.show_history)
        bar.addAction(history_action)
        settings_action = QAction("Providers", self)
        settings_action.triggered.connect(self.show_provider_settings)
        bar.addAction(settings_action)
        new_action = QAction("New Task", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(lambda: self.composer.setFocus())
        bar.addAction(new_action)
        bar.addSeparator()
        bar.addWidget(QLabel("  Project: "))
        self.project_label = QLabel("No project selected")
        bar.addWidget(self.project_label)
        bar.addSeparator()
        bar.addWidget(QLabel("  Permission: "))
        self.mode = QComboBox()
        self.mode.addItem("Restricted · read only", "read_only")
        self.mode.addItem("Workspace · edits allowed", "workspace")
        self.mode.currentIndexChanged.connect(self.persist_mode)
        bar.addWidget(self.mode)
        saved_mode = self.settings.value("permission/mode", "read_only")
        self.mode.setCurrentIndex(max(0, self.mode.findData(saved_mode)))

        split = QSplitter(Qt.Horizontal)
        self.files = QTreeView()
        self.fs_model = QFileSystemModel(self)
        self.fs_model.setReadOnly(True)
        self.fs_model.setResolveSymlinks(False)
        self.fs_model.setFilter(QDir.Dirs | QDir.Files | QDir.NoDotAndDotDot)
        self.files.setModel(self.fs_model)
        self.files.setColumnWidth(0, 240)
        self.files.doubleClicked.connect(self.preview_file)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(QLabel("PROJECT FILES"))
        self.file_search = QLineEdit()
        self.file_search.setPlaceholderText("Filter visible file names…")
        self.file_search.textChanged.connect(self.filter_tree)
        left_layout.addWidget(self.file_search)
        left_layout.addWidget(self.files)
        self.recent = QComboBox()
        self.recent.currentIndexChanged.connect(self.open_recent)
        left_layout.addWidget(self.recent)
        split.addWidget(left)

        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(14, 12, 14, 12)
        self.project_path = QLabel("Choose a project folder to begin")
        self.project_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.project_path)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFont(QFont("Cascadia Code", 10))
        self.transcript.setPlaceholderText("Task messages and live agent events appear here.")
        layout.addWidget(self.transcript, 1)
        self.composer = TaskComposer()
        self.composer.setMaximumHeight(125)
        self.composer.setPlaceholderText("Describe the coding task…  (Ctrl+Enter to run)")
        self.composer.submitRequested.connect(self.run_task)
        layout.addWidget(self.composer)
        row = QHBoxLayout()
        self.provider_label = QLabel("Checking provider…")
        self.run_button = QPushButton("Run task")
        self.run_button.clicked.connect(self.run_task)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_task)
        row.addWidget(self.provider_label, 1)
        row.addWidget(self.run_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        split.addWidget(center)
        split.setSizes([310, 900])
        self.setCentralWidget(split)

        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(2500)
        dock = QDockWidget("Tool activity · task events", self)
        dock.setWidget(self.activity)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setMaximumBlockCount(3000)
        terminal_dock = QDockWidget("Command output", self)
        terminal_dock.setWidget(self.terminal)
        self.addDockWidget(Qt.BottomDockWidgetArea, terminal_dock)
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Local Luna agent ready")

    def persist_mode(self):
        self.settings.setValue("permission/mode", self.mode.currentData())

    def choose_project(self):
        start = str(self.project or Path.home())
        path = QFileDialog.getExistingDirectory(self, "Open Project", start, QFileDialog.ShowDirsOnly)
        if path:
            self.set_project(path)

    def set_project(self, path: str):
        try:
            info = request("POST", "/api/projects/open", json={"path": path})
            self.project = Path(info["active_project"])
            self.project_label.setText(self.project.name)
            self.project_label.setToolTip(str(self.project))
            self.project_path.setText(str(self.project))
            self.fs_model.setRootPath(str(self.project))
            self.files.setRootIndex(self.fs_model.index(str(self.project)))
            self.settings.setValue("project/last", str(self.project))
            self.refresh_projects()
            self.status.showMessage(f"Opened {self.project}", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "Could not open project", str(exc))

    def refresh_projects(self):
        try:
            info = request("GET", "/api/projects")
            self.recent.blockSignals(True)
            self.recent.clear()
            self.recent.addItem("Recent projects…", None)
            for item in info.get("recent_projects", []):
                self.recent.addItem(item.get("name", item["path"]), item["path"])
                self.recent.setItemData(self.recent.count() - 1, item["path"], Qt.ToolTipRole)
            self.recent.blockSignals(False)
            active = info.get("active_project") or self.settings.value("project/last")
            if active and Path(active).is_dir() and Path(active) != self.project:
                self.set_project(active)
        except Exception as exc:
            self.status.showMessage(str(exc))

    def open_recent(self, index: int):
        path = self.recent.itemData(index)
        if path:
            self.set_project(path)

    def refresh_provider(self):
        try:
            data = request("GET", "/api/providers")
            active = next((p for p in data["providers"] if p.get("is_default")), None)
            self.provider_label.setText((f"{active['name']} · {active['model']} · key configured" if active and active.get("key_configured") else "Provider setup needed · open browser settings"))
        except Exception as exc:
            self.provider_label.setText(str(exc))

    def show_provider_settings(self):
        dialog = ProviderDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.refresh_provider()

    def show_history(self):
        try:
            data = request("GET", "/api/history")
        except Exception as exc:
            QMessageBox.critical(self, "Task history", str(exc))
            return
        items = data.get("tasks", [])
        if not items:
            QMessageBox.information(self, "Task history", "No saved tasks yet.")
            return
        labels = [f"#{item['id']} · {item['status']} · {item['message'][:100]}" for item in items]
        label, ok = QInputDialog.getItem(self, "Task history", "Select a saved task:", labels, 0, False)
        if not ok:
            return
        selected = items[labels.index(label)]
        self.transcript.appendPlainText(f"\nHistory · {selected['status']}\n{selected.get('answer') or selected.get('error') or selected['message']}\n")
        self.review_changes(selected["id"])

    def review_changes(self, task_id: int):
        try:
            changes = request("GET", f"/api/tasks/{task_id}/changes").get("changes", [])
        except Exception as exc:
            QMessageBox.warning(self, "Change review", str(exc))
            return
        if not changes:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Changes from task #{task_id}")
        dialog.resize(900, 650)
        layout = QVBoxLayout(dialog)
        chooser = QComboBox()
        for change in changes:
            chooser.addItem(f"{change['path']} · {change['status']}", change)
        diff = QPlainTextEdit()
        diff.setReadOnly(True)
        diff.setFont(QFont("Cascadia Code", 10))
        restore = QPushButton("Restore this change")
        def show_change(index: int):
            item = chooser.itemData(index)
            diff.setPlainText(item.get("diff") or "No textual diff is available.")
            restore.setEnabled(item.get("status") == "applied")
        def restore_change():
            item = chooser.currentData()
            answer = QMessageBox.question(dialog, "Restore change", f"Restore {item['path']} to its pre-task contents?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            try:
                request("POST", f"/api/changes/{item['id']}/restore")
                item["status"] = "restored"
                chooser.setItemText(chooser.currentIndex(), f"{item['path']} · restored")
                restore.setEnabled(False)
            except Exception as exc:
                QMessageBox.warning(dialog, "Restore failed", str(exc))
        chooser.currentIndexChanged.connect(show_change)
        restore.clicked.connect(restore_change)
        layout.addWidget(chooser)
        layout.addWidget(diff, 1)
        layout.addWidget(restore)
        show_change(0)
        dialog.exec()

    def filter_tree(self, value: str):
        # Hide rows by name while leaving the selected project's filesystem model intact.
        root = self.files.rootIndex()
        for row in range(self.fs_model.rowCount(root)):
            idx = self.fs_model.index(row, 0, root)
            hidden = bool(value) and value.casefold() not in self.fs_model.fileName(idx).casefold()
            self.files.setRowHidden(row, root, hidden)

    def preview_file(self, index):
        path = Path(self.fs_model.filePath(index))
        if not path.is_file() or path.stat().st_size > 1_000_000:
            return
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            self.status.showMessage("This file is not readable UTF-8 text.", 5000)
            return
        preview = QMainWindow(self)
        preview.setWindowTitle(str(path))
        preview.resize(800, 600)
        view = QPlainTextEdit(preview)
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setFont(QFont("Cascadia Code", 10))
        preview.setCentralWidget(view)
        preview.show()
        self._preview = preview

    def run_task(self):
        message = self.composer.toPlainText().strip()
        if not self.project:
            QMessageBox.information(self, "Open a project", "Choose a project folder before starting a task.")
            return
        if not message:
            return
        self.transcript.appendPlainText(f"\nYou\n{message}\n\nLuna\n")
        self.composer.clear()
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker = TaskWorker(message, self.mode.currentData())
        self.worker.event.connect(self.on_event)
        self.worker.approval_requested.connect(self.confirm_approval)
        self.worker.finished_task.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()
        self.status.showMessage("Task running…")

    def on_event(self, kind: str, detail: str):
        if kind == "assistant_delta":
            cursor = self.transcript.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(detail)
            self.transcript.setTextCursor(cursor)
        else:
            self.activity.appendPlainText(f"[{kind}] {detail}")
            if kind == "command":
                self.terminal.appendPlainText(detail)
            if kind in ("tool", "command", "approval", "error", "status"):
                self.transcript.appendPlainText(f"[{kind}] {detail}")

    def confirm_approval(self, approval: dict):
        command = str(approval.get("command", "Sensitive action"))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Action approval required")
        box.setText("The shared agent backend paused before this sensitive action.")
        box.setInformativeText("Review the target below. Approved PowerShell commands run with your Windows account permissions.")
        box.setDetailedText(command)
        approve = box.addButton("Approve", QMessageBox.AcceptRole)
        deny = box.addButton("Deny", QMessageBox.RejectRole)
        box.setDefaultButton(deny)
        box.exec()
        task_id = self.worker.task_id if self.worker else None
        if not task_id:
            return
        try:
            request("POST", f"/api/tasks/{task_id}/approval", json={"approved": box.clickedButton() is approve})
        except Exception as exc:
            QMessageBox.warning(self, "Approval failed", str(exc))

    def on_finished(self, state: dict):
        self.transcript.appendPlainText(f"\nTask {state.get('status', 'finished')}.")
        self.status.showMessage(f"Task {state.get('status', 'finished')}", 7000)
        if state.get("id"):
            self.review_changes(state["id"])
        self._task_done()

    def on_failed(self, error: str):
        self.activity.appendPlainText(f"[error] {error}")
        QMessageBox.critical(self, "Task error", error)
        self._task_done()

    def _task_done(self):
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.worker = None

    def stop_task(self):
        if self.worker and self.worker.task_id:
            try:
                request("DELETE", f"/api/tasks/{self.worker.task_id}")
                self.status.showMessage("Cancellation requested…")
            except Exception as exc:
                QMessageBox.warning(self, "Could not stop task", str(exc))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            choice = QMessageBox.question(self, "Task is running", "A task is still running. Request cancellation and close Luna Desk?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if choice != QMessageBox.Yes:
                event.ignore()
                return
            self.stop_task()
            self.worker.requestInterruption()
            self.worker.wait(2500)
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.backend.stop()
        event.accept()


class ProviderDialog(QDialog):
    """Small native provider editor backed by the same provider API as the browser."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Provider settings")
        self.resize(620, 540)
        self.providers = []
        layout = QVBoxLayout(self)
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(self.load_selected)
        layout.addWidget(QLabel("Configured providers"))
        layout.addWidget(self.selector)
        form = QFormLayout()
        self.fields = {}
        for key, title in (("name", "Display name"), ("provider", "Provider"), ("base_url", "Base URL"), ("model", "Model")):
            field = QLineEdit()
            self.fields[key] = field
            form.addRow(title, field)
        self.fields["api_key"] = QLineEdit()
        self.fields["api_key"].setEchoMode(QLineEdit.Password)
        self.fields["api_key"].setPlaceholderText("Enter key; saved keys stay masked")
        form.addRow("API key", self.fields["api_key"])
        self.protocol = QComboBox()
        self.protocol.addItem("Chat Completions", "chat_completions")
        self.protocol.addItem("Responses", "responses")
        form.addRow("Protocol", self.protocol)
        layout.addLayout(form)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        add = QPushButton("New Apinex provider")
        add.clicked.connect(self.new_provider)
        test = QPushButton("Test connection")
        test.clicked.connect(self.test_provider)
        save = QPushButton("Save provider")
        save.clicked.connect(self.save_provider)
        default = QPushButton("Make default")
        default.clicked.connect(self.make_default)
        finish = QPushButton("Finish first-run setup")
        finish.clicked.connect(self.finish_setup)
        actions.addWidget(add)
        actions.addWidget(test)
        actions.addWidget(save)
        actions.addWidget(default)
        actions.addWidget(finish)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.reload()

    def reload(self):
        try:
            self.providers = request("GET", "/api/providers").get("providers", [])
            self.selector.blockSignals(True)
            self.selector.clear()
            for item in self.providers:
                self.selector.addItem(item["name"] + (" · Default" if item.get("is_default") else ""), item["id"])
            self.selector.blockSignals(False)
            if self.providers:
                self.load_selected(0)
            else:
                self.new_provider()
        except Exception as exc:
            self.message.setText(str(exc))

    def load_selected(self, index):
        if index < 0 or index >= len(self.providers):
            return
        item = self.providers[index]
        for key in ("name", "provider", "base_url", "model"):
            self.fields[key].setText(item.get(key, ""))
        self.fields["api_key"].clear()
        self.fields["api_key"].setPlaceholderText("Leave blank to keep saved key" if item.get("key_configured") else "Enter API key")
        self.protocol.setCurrentIndex(max(0, self.protocol.findData(item.get("protocol"))))
        self.message.setText("Key configured" if item.get("key_configured") else "No key configured")

    def new_provider(self):
        self.selector.setCurrentIndex(-1)
        for key, value in (("name", "Apinex Luna"), ("provider", "Apinex"), ("base_url", "https://api.apinex.bond/v1"), ("model", "free/gpt-6-luna")):
            self.fields[key].setText(value)
        self.fields["api_key"].clear()
        self.fields["api_key"].setPlaceholderText("Enter API key")
        self.protocol.setCurrentIndex(0)

    def payload(self):
        selected = self.selector.currentData()
        item = next((p for p in self.providers if p["id"] == selected), None)
        data = {"id": selected, **{key: self.fields[key].text().strip() for key in ("name", "provider", "base_url", "model")},
                "protocol": self.protocol.currentData(), "api_key": self.fields["api_key"].text() or None,
                "timeout": (item or {}).get("timeout", 60), "max_iterations": (item or {}).get("max_iterations", 12),
                "streaming": (item or {}).get("streaming", False),
                "max_requests_per_window": (item or {}).get("max_requests_per_window", 5),
                "request_window_seconds": (item or {}).get("request_window_seconds", 60),
                "min_request_interval_seconds": (item or {}).get("min_request_interval_seconds", 12),
                "retry_limit": (item or {}).get("retry_limit", 2),
                "max_backoff_seconds": (item or {}).get("max_backoff_seconds", 30),
                "task_timeout_seconds": (item or {}).get("task_timeout_seconds", 900)}
        return data

    def test_provider(self):
        try:
            result = request("POST", "/api/providers/test", json=self.payload())
            self.message.setText(("Connection succeeded" if result.get("ok") else "Connection failed") + ": " + result.get("message", "No details"))
        except Exception as exc:
            self.message.setText(str(exc))

    def save_provider(self):
        try:
            data = self.payload()
            selected = self.selector.currentData()
            if selected:
                request("PUT", f"/api/providers/{selected}", json=data)
            else:
                result = request("POST", "/api/providers", json=data)
                data["id"] = result["id"]
            self.fields["api_key"].clear()
            self.reload()
            self.message.setText("Provider saved. The API key remains masked and stored by Luna.")
        except Exception as exc:
            self.message.setText(str(exc))

    def make_default(self):
        selected = self.selector.currentData()
        if not selected:
            self.message.setText("Save the provider before making it default.")
            return
        try:
            request("POST", f"/api/providers/{selected}/default")
            self.reload()
            self.accept()
        except Exception as exc:
            self.message.setText(str(exc))

    def finish_setup(self):
        try:
            request("POST", "/api/setup/finish")
            self.message.setText("First-run setup is complete.")
            self.accept()
        except Exception as exc:
            self.message.setText(str(exc))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Luna Desk")
    app.setOrganizationName("LunaDesk")
    (ROOT / ".luna").mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(ROOT / ".luna" / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "Luna Desk is already open", "Close the existing Luna Desk window before starting another desktop session.")
        return 0
    backend = Backend()
    try:
        backend.start()
    except Exception as exc:
        QMessageBox.critical(None, "Luna Desk could not start", str(exc))
        lock.unlock()
        return 1
    window = LunaWindow(backend)
    window.show()
    result = app.exec()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
