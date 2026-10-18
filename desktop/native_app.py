"""Native Qt front end for the shared Luna Desk FastAPI agent engine."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import re

import httpx
from PySide6.QtCore import QDir, QLockFile, QSettings, QSortFilterProxyModel, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPixmap, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFileSystemModel,
    QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QStatusBar,
    QSpinBox, QTreeView, QVBoxLayout, QWidget, QDockWidget, QInputDialog, QTabWidget,
    QToolButton, QStyle,
    QTreeWidget, QTreeWidgetItem,
)
from backend import projects, providers
from backend.workspace_paths import is_link_or_junction

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
        log_dir = providers.DATA_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "desktop-backend.log").open("ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if getattr(sys, "frozen", False):
            backend_exe = Path(sys.executable).with_name("LunaBackend.exe")
            if not backend_exe.is_file():
                raise RuntimeError("LunaBackend.exe is missing. Rebuild the packaged application.")
            command, cwd = [str(backend_exe)], str(backend_exe.parent)
        else:
            command = [sys.executable, "-m", "uvicorn", "backend.main:app", "--host",
                       "127.0.0.1", "--port", "8765"]
            cwd = str(ROOT)
        self.process = subprocess.Popen(command, cwd=cwd, stdout=log,
                                        stderr=subprocess.STDOUT, creationflags=flags)
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
        timeout = kwargs.pop("timeout", 10)
        response = httpx.request(method, BASE_URL + path, timeout=timeout, **kwargs)
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

    def __init__(self, message: str, mode: str, workspace: str, task_id: int | None = None):
        super().__init__()
        self.message = message
        self.mode = mode
        self.workspace = workspace
        self.task_id: int | None = task_id

    def run(self):
        exit_code = -1
        try:
            if self.task_id is None:
                started = request("POST", "/api/tasks", json={"message": self.message,
                                                                  "mode": self.mode,
                                                                  "workspace": self.workspace})
                self.task_id = started["id"]
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


class CodeHighlighter(QSyntaxHighlighter):
    """Small language-aware highlighter for the built-in Qt code editor."""
    def __init__(self, document):
        super().__init__(document)
        self.rules = []
        keyword = QTextCharFormat()
        keyword.setForeground(QColor("#b9a6ff"))
        keyword.setFontWeight(QFont.DemiBold)
        self.rules.append((re.compile(r"\b(class|def|return|if|else|elif|for|while|in|import|from|as|try|except|finally|with|async|await|pass|raise|yield|const|let|var|function|export|default|new|public|private|static|void|int|string|using|namespace)\b"), keyword))
        string = QTextCharFormat()
        string.setForeground(QColor("#a8c88a"))
        self.rules.append((re.compile(r"(['\"].*?['\"]|`.*?`)"), string))
        comment = QTextCharFormat()
        comment.setForeground(QColor("#777e88"))
        self.rules.append((re.compile(r"(#.*$|//.*$)"), comment))
        number = QTextCharFormat()
        number.setForeground(QColor("#d7a879"))
        self.rules.append((re.compile(r"\b\d+(?:\.\d+)?\b"), number))

    def highlightBlock(self, text):
        for pattern, style in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), style)


class CodeEditor(QPlainTextEdit):
    """Lightweight Qt Widgets editor with line numbers and syntax colors."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Cascadia Code", 10))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet("QPlainTextEdit { background:#17191f; border:0; padding:8px; selection-background-color:#453d72; }")
        self.highlighter = CodeHighlighter(self.document())
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_numbers)
        self._update_margin()

    def _margin_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self, *_):
        self.setViewportMargins(self._margin_width(), 0, 0, 0)

    def _update_numbers(self, rect, dy):
        if dy:
            self.viewport().scroll(0, dy)
        else:
            self.viewport().update(0, rect.y(), self._margin_width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_margin()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.fillRect(0, event.rect().y(), self._margin_width(), event.rect().height(), QColor("#1b1d23"))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#737985"))
                painter.drawText(0, top, self._margin_width() - 6, self.fontMetrics().height(),
                                 Qt.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1


class EditorTabWidget(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.file_paths: dict[QWidget, Path] = {}
        self.workspace: Path | None = None
        self.tabCloseRequested.connect(self.close_tab)

    def close_tab(self, index):
        widget = self.widget(index)
        if widget.document().isModified():
            answer = QMessageBox.question(self, "Unsaved file", "Save changes before closing this file?",
                                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                          QMessageBox.Save)
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Save and not self.save_editor(widget):
                return
        self.removeTab(index)
        self.file_paths.pop(widget, None)
        widget.deleteLater()

    def save_editor(self, editor):
        path = self.file_paths.get(editor)
        if not path or not self.workspace:
            return False
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.workspace.resolve(strict=True))
            if _protected_preview_path(path, self.workspace):
                raise ValueError("The file is outside the active workspace or is protected.")
            path.write_text(editor.toPlainText(), encoding="utf-8", newline="")
            editor.document().setModified(False)
            self._update_title(editor)
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Could not save file", str(exc))
            return False

    def _update_title(self, editor):
        index = self.indexOf(editor)
        if index >= 0:
            title = self.file_paths[editor].name
            self.setTabText(index, ("● " if editor.document().isModified() else "") + title)


class TerminalWorker(QThread):
    output = Signal(str)
    terminal_started = Signal()
    failed = Signal(str)
    exited = Signal(int)

    ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
    ENVIRONMENT = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                   "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "COMSPEC", "APPDATA",
                   "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PSMODULEPATH"}

    def __init__(self, program: str, directory: str, parent=None):
        super().__init__(parent)
        self.program = program
        self.directory = directory
        self.process = None
        self._closing = threading.Event()

    def run(self):
        try:
            from winpty import PtyProcess
            if self._closing.is_set():
                return
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in self.ENVIRONMENT}
            self.process = PtyProcess.spawn([self.program, "-NoLogo", "-NoProfile"],
                                            cwd=self.directory, env=env, dimensions=(32, 120))
            if self._closing.is_set():
                return
            self.terminal_started.emit()
            while not self._closing.is_set():
                try:
                    output = self.process.read(4096)
                except EOFError:
                    break
                output = self.ANSI.sub("", output).replace("\r\n", "\n").replace("\r", "\n")
                output = "".join(char for char in output if char in "\n\t" or ord(char) >= 32)
                if output:
                    self.output.emit(output)
            code = self.process.exitstatus
            exit_code = int(code) if code is not None else -1
        except Exception as exc:
            if not self._closing.is_set():
                self.failed.emit(f"Could not start or read the Windows ConPTY terminal: {exc}")
        finally:
            if self.process is not None:
                try:
                    self.process.close(force=True)
                except Exception:
                    pass
                code = self.process.exitstatus
                if code is not None:
                    exit_code = int(code)
            self.exited.emit(exit_code)

    def write(self, text: str):
        process = self.process
        if process is not None and not self._closing.is_set():
            try:
                process.write(text)
            except (EOFError, OSError):
                pass

    def terminate(self):
        if self._closing.is_set():
            return
        self._closing.set()
        process = self.process
        if process is not None:
            # Closing the ConPTY stops only the process spawned by this worker.
            threading.Thread(target=lambda: self._close_process(process), daemon=True).start()

    @staticmethod
    def _close_process(process):
        try:
            process.close(force=True)
        except Exception:
            pass


class InteractiveTerminal(QWidget):
    """Persistent Windows ConPTY terminal; starts only on explicit user action."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.shell_program: str | None = None
        self.worker: TerminalWorker | None = None
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setSingleShot(True)
        self.runtime_timer.setInterval(4 * 60 * 60 * 1000)
        self.runtime_timer.timeout.connect(self.terminate)
        self._output_char_count = 0
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(3000)
        self.output.setFont(QFont("Cascadia Code", 10))
        self.cwd = QLineEdit()
        self.cwd.setPlaceholderText("Working directory")
        self.cwd.setReadOnly(True)
        self.browse_button = QPushButton("Choose folder")
        self.start_button = QPushButton("Start PowerShell")
        self.stop_button = QPushButton("Terminate")
        self.stop_button.setEnabled(False)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Start the shell, then enter a command or redirected-stdin input")
        self.input.setEnabled(False)
        top = QHBoxLayout()
        top.addWidget(self.cwd, 1)
        top.addWidget(self.browse_button)
        top.addWidget(self.start_button)
        top.addWidget(self.stop_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.output, 1)
        layout.addWidget(self.input)
        self.start_button.clicked.connect(self.start)
        self.browse_button.clicked.connect(self.choose_directory)
        self.stop_button.clicked.connect(self.terminate)
        self.input.returnPressed.connect(self.send_input)

    def set_directory(self, path: str | Path | None):
        self.cwd.setText(str(path or ""))

    def choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Terminal working directory", self.cwd.text() or str(Path.home()))
        if directory:
            self.set_directory(directory)

    def start(self):
        if self.worker and self.worker.isRunning():
            return
        directory = self.cwd.text()
        if not directory or not Path(directory).is_dir():
            self._append("Open a project before starting the terminal.\n")
            return
        program = self.shell_program or shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        self.output.appendPlainText(f"Starting {program} in {directory}\nConPTY combines stdout and stderr. Manual commands use your Windows account permissions; they are not OS-sandboxed.\n")
        self.worker = TerminalWorker(program, directory, self)
        self.worker.output.connect(self._append)
        self.worker.terminal_started.connect(self._started)
        self.worker.failed.connect(self._append_error)
        self.worker.exited.connect(self._finished)
        self.worker.start()

    def _started(self):
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.input.setEnabled(True)
        self.runtime_timer.start()
        self.input.setFocus()
        self._append("[shell started]\n")

    def send_input(self):
        if not self.worker or not self.worker.isRunning():
            return
        command = self.input.text()
        self.input.clear()
        self._append(f"> {command}\n")
        self.worker.write(command + "\r")

    def terminate(self):
        if not self.worker or not self.worker.isRunning():
            return
        self.worker.terminate()
        self._append("[termination requested for Luna's terminal process]\n")

    def _finished(self, code):
        self.runtime_timer.stop()
        self._append(f"[shell exited: code {code}]\n")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.input.setEnabled(False)

    def _append_error(self, error: str):
        self._append("[terminal error] " + error + "\n")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.input.setEnabled(False)

    def _append(self, text: str):
        if text:
            cursor = self.output.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(text)
            excess = self.output.document().characterCount() - 1 - 1_000_000
            if excess > 0:
                cursor.movePosition(cursor.MoveOperation.Start)
                cursor.movePosition(cursor.MoveOperation.Right, cursor.MoveMode.KeepAnchor, excess)
                cursor.removeSelectedText()
            self.output.setTextCursor(cursor)

    def shutdown(self):
        self.runtime_timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(5000)


class ApiWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, method: str, path: str, payload: dict | None = None,
                 timeout: float = 10, parent=None):
        super().__init__(parent)
        self.method = method
        self.path = path
        self.payload = payload
        self.timeout = timeout

    def run(self):
        try:
            kwargs = {"json": self.payload} if self.payload is not None else {}
            self.completed.emit(request(self.method, self.path, timeout=self.timeout, **kwargs))
        except Exception as exc:
            self.failed.emit(str(exc))


class SearchWorker(QThread):
    match = Signal(dict)
    progress = Signal(int)
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, query: str, limit: int = 100, parent=None):
        super().__init__(parent)
        self.query = query
        self.limit = limit

    def run(self):
        try:
            with httpx.stream("POST", BASE_URL + "/api/projects/search",
                              json={"query": self.query, "limit": self.limit},
                              timeout=httpx.Timeout(connect=10, read=5, write=10, pool=10)) as response:
                response.raise_for_status()
                final = {}
                for line in response.iter_lines():
                    if self.isInterruptionRequested():
                        return
                    if not line:
                        continue
                    item = json.loads(line)
                    if "match" in item:
                        self.match.emit(item["match"])
                    elif "progress" in item:
                        self.progress.emit(item["progress"])
                    elif item.get("done"):
                        final = item
                if final:
                    self.completed.emit(final)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class SearchDialog(QDialog):
    def __init__(self, query: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Search project contents")
        self.resize(850, 580)
        self.results = QListWidget()
        self.status = QLabel("Searching the active project…")
        self.cancel = QPushButton("Cancel search")
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.results, 1)
        layout.addWidget(self.cancel)
        self.worker = SearchWorker(query, parent=self)
        self.worker.match.connect(self.add_match)
        self.worker.progress.connect(lambda count: self.status.setText(f"Checked {count} files…"))
        self.worker.completed.connect(self.search_done)
        self.worker.failed.connect(self.search_failed)
        self.cancel.clicked.connect(self.stop_search)
        self.worker.start()

    def add_match(self, match: dict):
        row = QListWidgetItem(f"{match['path']}:{match['line']}  {match['text']}")
        row.setData(Qt.UserRole, match)
        self.results.addItem(row)

    def search_done(self, info: dict):
        suffix = " Results capped; refine the query." if info.get("truncated") else ""
        self.status.setText(f"Search complete. Checked {info.get('examined', 0)} files.{suffix}")
        self.cancel.setText("Close")
        self.cancel.clicked.disconnect()
        self.cancel.clicked.connect(self.accept)

    def search_failed(self, error: str):
        self.status.setText("Search failed: " + error)
        self.cancel.setText("Close")
        self.cancel.clicked.disconnect()
        self.cancel.clicked.connect(self.accept)

    def stop_search(self):
        self.worker.requestInterruption()
        self.status.setText("Cancelling search…")
        self.cancel.setEnabled(False)

    def closeEvent(self, event):
        if self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(1200)
        event.accept()


class ChangedFileSystemModel(QFileSystemModel):
    """Decorate files changed by the most recently reviewed task."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.changed_paths: dict[str, str] = {}

    def set_changed_paths(self, changed: dict[str, str]):
        self.changed_paths = {os.path.normcase(os.path.abspath(path)): label
                              for path, label in changed.items()}

    def data(self, index, role=Qt.DisplayRole):
        value = super().data(index, role)
        if role == Qt.DisplayRole and index.isValid() and index.column() == 0:
            label = self.changed_paths.get(os.path.normcase(os.path.abspath(self.filePath(index))))
            if label:
                return f"{value}  [{label}]"
        return value

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid():
            path = Path(self.filePath(index))
            if is_link_or_junction(path):
                return flags & ~Qt.ItemIsEnabled
        return flags


def _protected_preview_path(path: Path, root: Path) -> bool:
    try:
        lexical_root = root.absolute()
        lexical_path = path.absolute()
        lexical_relative = lexical_path.relative_to(lexical_root)
        current = lexical_root
        for part in lexical_relative.parts:
            current = current / part
            if is_link_or_junction(current):
                return True
        relative = path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return True
    parts = {part.casefold() for part in relative.parts}
    name = path.name.casefold()
    return (".git" in parts or ".luna" in parts or name == ".env"
            or name.startswith(".env.")
            or name in {"id_rsa", "id_ed25519", ".npmrc", ".pypirc"}
            or path.suffix.casefold() in {".pem", ".p12", ".pfx", ".key"})


class LunaWindow(QMainWindow):
    def __init__(self, backend: Backend):
        super().__init__()
        self.backend = backend
        self.settings = QSettings("LunaDesk", "LunaDesk")
        self.worker: TaskWorker | None = None
        self.active_task: int | None = None
        self.project: Path | None = None
        self.last_task_id: int | None = None
        self.setWindowTitle("Luna Desk")
        self.resize(1420, 900)
        self.setMinimumSize(900, 620)
        self._build()
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        state = self.settings.value("window/state")
        if state:
            self.restoreState(state)
        saved_split = self.settings.value("workspace/split")
        if saved_split:
            self.findChild(QSplitter, "workspace-splitter").setSizes([int(value) for value in saved_split])
        saved_chat_split = self.settings.value("workspace/chat_split")
        if saved_chat_split:
            self.findChild(QSplitter, "editor-agent-splitter").setSizes([int(value) for value in saved_chat_split])
        self.refresh_projects()
        self.refresh_provider()

    def _build(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#17191f; color:#dedee6; font-family:'Segoe UI'; font-size:9pt; }
            QLineEdit, QPlainTextEdit, QTreeView, QComboBox { background:#202229; border:1px solid #30323a; border-radius:4px; padding:6px; selection-background-color:#51458a; }
            QPushButton { background:#252730; border:1px solid #343640; border-radius:4px; padding:6px 11px; }
            QPushButton:hover { background:#2c2e38; } QPushButton:disabled { color:#777; }
            QToolBar { background:#1b1d23; border:0; spacing:8px; padding:4px 8px; }
            QToolBar QComboBox { padding:4px 7px; }
            QDockWidget::title { background:#1d1f26; padding:6px 9px; border-top:1px solid #30323a; }
            QTabWidget::pane { border:1px solid #30323a; }
            QTabBar::tab { background:#1d1f26; padding:7px 12px; border-right:1px solid #30323a; }
            QTabBar::tab:selected { background:#252730; border-top:2px solid #8878d5; }
            QStatusBar { background:#1d1f26; border-top:1px solid #30323a; }
        """)
        bar = self.addToolBar("Main")
        bar.setMovable(False)
        bar.setObjectName("main-toolbar")
        open_action = QAction("Open Project", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.choose_project)
        bar.addAction(open_action)
        history_action = QAction("History", self)
        history_action.setShortcut("Ctrl+H")
        history_action.triggered.connect(self.show_history)
        bar.addAction(history_action)
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.show_provider_settings)
        bar.addAction(settings_action)
        integrations_action = QAction("Integrations", self)
        integrations_action.triggered.connect(self.show_integrations_settings)
        bar.addAction(integrations_action)
        bar.addSeparator()
        self.project_label = QLabel("No project")
        bar.addWidget(self.project_label)
        bar.addWidget(QLabel("Permission"))
        self.mode = QComboBox()
        self.mode.addItem("Read only", "read_only")
        self.mode.addItem("Workspace", "workspace")
        self.mode.currentIndexChanged.connect(self.persist_mode)
        saved_mode = self.settings.value("permission/mode", "read_only")
        self.mode.setCurrentIndex(max(0, self.mode.findData(saved_mode)))
        self.provider_label = QLabel("Checking provider?")
        bar.addWidget(self.provider_label)

        split = QSplitter(Qt.Horizontal)
        split.setObjectName("workspace-splitter")
        self.files = QTreeView()
        self.fs_model = ChangedFileSystemModel(self)
        self.fs_model.setReadOnly(True)
        self.fs_model.setResolveSymlinks(False)
        self.fs_model.setFilter(QDir.Dirs | QDir.Files | QDir.NoDotAndDotDot)
        self.fs_proxy = QSortFilterProxyModel(self)
        self.fs_proxy.setSourceModel(self.fs_model)
        self.fs_proxy.setRecursiveFilteringEnabled(True)
        self.fs_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.files.setModel(self.fs_proxy)
        self.files.setColumnWidth(0, 240)
        self.files.setHeaderHidden(True)
        self.files.setIndentation(14)
        self.files.doubleClicked.connect(self.preview_file)
        left = QWidget()
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        activity_rail = QWidget()
        activity_rail.setFixedWidth(38)
        activity_layout = QVBoxLayout(activity_rail)
        activity_layout.setContentsMargins(3, 6, 3, 6)
        explorer_button = QToolButton()
        explorer_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        explorer_button.setToolTip("Explorer")
        explorer_button.clicked.connect(self.files.setFocus)
        search_button = QToolButton()
        search_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogContentsView))
        search_button.setToolTip("Search in project")
        search_button.clicked.connect(lambda: self.content_search.setFocus())
        changes_button = QToolButton()
        changes_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        changes_button.setToolTip("Changes")
        changes_button.clicked.connect(lambda: self.findChild(QDockWidget, "changes-dock").show())
        for button in (explorer_button, search_button, changes_button):
            button.setAutoRaise(True)
            activity_layout.addWidget(button)
        activity_layout.addStretch(1)
        left_layout.addWidget(activity_rail)
        explorer = QWidget()
        explorer.setMinimumWidth(170)
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(explorer, 1)
        left_layout = explorer_layout
        left_layout.addWidget(QLabel("EXPLORER"))
        self.file_search = QLineEdit()
        self.file_search.setPlaceholderText("Search files")
        self.file_search.textChanged.connect(self.filter_tree)
        left_layout.addWidget(self.file_search)
        self.content_search = QLineEdit()
        self.content_search.setPlaceholderText("Search in project")
        self.content_search.returnPressed.connect(self.search_contents)
        left_layout.addWidget(self.content_search)
        left_layout.addWidget(self.files)
        self.recent = QComboBox()
        self.recent.currentIndexChanged.connect(self.open_recent)
        self.recent.setMaximumHeight(30)
        self.recent.setMinimumWidth(170)
        bar.addWidget(self.recent)

        self.breadcrumbs = QLabel("Open a project to begin")
        self.breadcrumbs.setStyleSheet("padding:6px 12px; color:#9296a1; border-bottom:1px solid #30323a;")
        self.editor_tabs = EditorTabWidget(self)
        self.editor_tabs.workspace = None
        empty_editor = CodeEditor(self)
        empty_editor.setReadOnly(True)
        empty_editor.setPlaceholderText("Open a file from the project explorer to edit it here.")
        self.editor_tabs.addTab(empty_editor, "Welcome")
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFont(QFont("Cascadia Code", 10))
        self.transcript.setPlaceholderText("Luna Agent responses and task summaries appear here.")
        self.transcript.setStyleSheet("QPlainTextEdit { border:0; background:#191b21; padding:10px; }")
        chat_split = QSplitter(Qt.Horizontal)
        chat_split.setObjectName("editor-agent-splitter")
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(self.breadcrumbs)
        editor_layout.addWidget(self.editor_tabs)
        chat_split.addWidget(editor)
        chat = QWidget()
        chat_layout = QVBoxLayout(chat)
        chat_layout.setContentsMargins(0, 0, 0, 4)
        chat_layout.addWidget(QLabel("LUNA AGENT"))
        chat_layout.addWidget(self.transcript, 1)
        self.composer = TaskComposer()
        self.composer.setMinimumHeight(76)
        self.composer.setMaximumHeight(140)
        self.composer.setPlaceholderText("Ask Luna about this project?  (Ctrl+Enter to run)")
        self.composer.submitRequested.connect(self.run_task)
        chat_layout.addWidget(self.composer)
        row = QHBoxLayout()
        self.run_button = QPushButton("Send")
        self.run_button.clicked.connect(self.run_task)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_task)
        self.composer_mode = QComboBox()
        self.composer_mode.addItem("Read only", "read_only")
        self.composer_mode.addItem("Workspace", "workspace")
        self.composer_mode.setCurrentIndex(self.mode.currentIndex())
        self.composer_mode.currentIndexChanged.connect(self.mode.setCurrentIndex)
        self.mode.currentIndexChanged.connect(self.composer_mode.setCurrentIndex)
        row.addWidget(self.composer_mode, 1)
        row.addWidget(self.run_button)
        row.addWidget(self.stop_button)
        chat_layout.addLayout(row)
        chat_split.addWidget(chat)
        chat_split.setSizes([620, 320])
        split.addWidget(left)
        split.addWidget(chat_split)
        split.setSizes([250, 1170])
        self.setCentralWidget(split)

        self.activity = QTreeWidget()
        self.activity.setColumnCount(1)
        self.activity.setHeaderHidden(True)
        self.activity.setWordWrap(True)
        self.activity.setStyleSheet("QTreeWidget { border:0; background:#191b21; }")
        self.tool_activity_items = {}
        output_dock = QDockWidget("Output", self)
        output_dock.setObjectName("output-dock")
        output_dock.setWidget(self.activity)
        self.addDockWidget(Qt.BottomDockWidgetArea, output_dock)
        self.terminal = InteractiveTerminal(self)
        terminal_dock = QDockWidget("Terminal", self)
        terminal_dock.setObjectName("terminal-dock")
        terminal_dock.setWidget(self.terminal)
        self.addDockWidget(Qt.BottomDockWidgetArea, terminal_dock)
        self.changes_list = QListWidget()
        self.changes_list.itemDoubleClicked.connect(lambda _item: self.open_selected_change())
        changes_panel = QWidget()
        changes_layout = QVBoxLayout(changes_panel)
        changes_layout.setContentsMargins(6, 6, 6, 6)
        changes_layout.addWidget(self.changes_list, 1)
        changes_actions = QHBoxLayout()
        open_diff = QPushButton("Open diff")
        open_diff.clicked.connect(self.open_selected_change)
        restore_change = QPushButton("Restore")
        restore_change.clicked.connect(self.restore_selected_change)
        changes_actions.addWidget(open_diff)
        changes_actions.addWidget(restore_change)
        changes_layout.addLayout(changes_actions)
        changes_dock = QDockWidget("Changes", self)
        changes_dock.setObjectName("changes-dock")
        changes_dock.setWidget(changes_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, changes_dock)
        self.problems_list = QListWidget()
        problems_dock = QDockWidget("Problems", self)
        problems_dock.setObjectName("problems-dock")
        problems_dock.setWidget(self.problems_list)
        self.addDockWidget(Qt.BottomDockWidgetArea, problems_dock)
        self.tabifyDockWidget(output_dock, terminal_dock)
        self.tabifyDockWidget(output_dock, problems_dock)
        self.tabifyDockWidget(output_dock, changes_dock)
        for dock in (output_dock, terminal_dock, problems_dock, changes_dock):
            dock.hide()
        view_menu = self.menuBar().addMenu("View")
        for dock in (output_dock, terminal_dock, problems_dock, changes_dock):
            view_menu.addAction(dock.toggleViewAction())

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.path_status = QLabel("No project")
        self.branch_status = QLabel("")
        self.permission_status = QLabel("Read only")
        self.permission_status.setText("Workspace" if self.mode.currentData() == "workspace" else "Read only")
        self.agent_status = QLabel("Ready")
        self.provider_status = QLabel("Provider")
        for label in (self.path_status, self.branch_status, self.permission_status,
                      self.provider_status, self.agent_status):
            self.status.addPermanentWidget(label)
        self.status.showMessage("Local Luna agent ready")
        save_action = QAction("Save file", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.save_active_editor)
        self.addAction(save_action)

    def persist_mode(self):
        self.settings.setValue("permission/mode", self.mode.currentData())
        if hasattr(self, "permission_status"):
            self.permission_status.setText("Workspace" if self.mode.currentData() == "workspace" else "Read only")

    def choose_project(self):
        start = str(self.project or Path.home())
        path = QFileDialog.getExistingDirectory(self, "Open Project", start, QFileDialog.ShowDirsOnly)
        if path:
            self.set_project(path)

    def set_project(self, path: str):
        try:
            requested = Path(path).expanduser().resolve(strict=True)
            if self.project and requested != self.project.resolve():
                dirty = [editor for editor in self.editor_tabs.file_paths
                         if editor.document().isModified()]
                if dirty:
                    choice = QMessageBox.question(self, "Unsaved files",
                        "Save changes before switching projects?",
                        QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                        QMessageBox.Save)
                    if choice == QMessageBox.Cancel:
                        return
                    if choice == QMessageBox.Save and not all(
                            self.editor_tabs.save_editor(editor) for editor in dirty):
                        return
                while self.editor_tabs.count():
                    widget = self.editor_tabs.widget(0)
                    self.editor_tabs.removeTab(0)
                    self.editor_tabs.file_paths.pop(widget, None)
                    widget.deleteLater()
                welcome = CodeEditor(self.editor_tabs)
                welcome.setReadOnly(True)
                welcome.setPlaceholderText("Open a file from the project explorer to edit it here.")
                self.editor_tabs.addTab(welcome, "Welcome")
            info = request("POST", "/api/projects/open", json={"path": str(requested)})
            self.project = Path(info["active_project"])
            self.editor_tabs.workspace = self.project
            self.terminal.set_directory(self.project)
            self.fs_model.set_changed_paths({})
            self.changes_list.clear()
            self.project_label.setText(self.project.name)
            self.project_label.setToolTip(str(self.project))
            self.breadcrumbs.setText(self.project.name + "  /  ")
            self.path_status.setText(str(self.project))
            self.path_status.setToolTip(str(self.project))
            self.branch_status.setText(self._git_branch(self.project))
            self.fs_model.setRootPath(str(self.project))
            self.files.setRootIndex(self.fs_proxy.mapFromSource(self.fs_model.index(str(self.project))))
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
            self.provider_status.setText(f"{active['name']} · {active['model']}" if active else "Provider not set")
        except Exception as exc:
            self.provider_label.setText(str(exc))
            self.provider_status.setText("Provider unavailable")

    def save_active_editor(self):
        editor = self.editor_tabs.currentWidget()
        if isinstance(editor, CodeEditor) and editor in self.editor_tabs.file_paths:
            self.editor_tabs.save_editor(editor)

    @staticmethod
    def _git_branch(project: Path) -> str:
        return projects.git_branch(project)

    def show_provider_settings(self):
        dialog = ProviderDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.refresh_provider()

    def show_integrations_settings(self):
        IntegrationsDialog(self).exec()

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
        try:
            state = request("GET", f"/api/tasks/{selected['id']}")
        except Exception as exc:
            QMessageBox.warning(self, "Task history", str(exc))
            return
        events = state.get("events", [])
        event_text = "\n".join(
            f"{event['created']} [{event['kind']}] {event['detail'].splitlines()[0][:240]}"
            for event in events)
        try:
            used_skills = request("GET", f"/api/tasks/{selected['id']}/skills").get("skills", [])
        except Exception:
            used_skills = []
        reason = "Safe to resume: no tool had started before interruption." if state.get("can_resume") else "This task cannot be replayed safely." if state.get("status") == "interrupted" else "Resume is available only for interrupted tasks."
        details = f"{state.get('answer') or state.get('error') or state['message']}\n\n{reason}\nSkills used: {', '.join(used_skills) or '(none recorded)'}\n\nRecorded events:\n{event_text or '(none)'}"
        box = QMessageBox(self)
        box.setWindowTitle(f"Task #{selected['id']} · {selected['status']}")
        box.setText(f"{selected['message']}\n\n{reason}")
        box.setDetailedText(details)
        review = box.addButton("Review changes", QMessageBox.ActionRole)
        resume = box.addButton("Resume safely", QMessageBox.AcceptRole)
        new_task = box.addButton("New task from context", QMessageBox.ActionRole)
        close = box.addButton(QMessageBox.Close)
        resume.setEnabled(bool(state.get("can_resume")))
        box.setDefaultButton(close)
        box.exec()
        clicked = box.clickedButton()
        self.transcript.appendPlainText(f"\nHistory · {selected['status']}\n{details}\n")
        if clicked is review:
            self.last_task_id = selected["id"]
            self.review_changes(selected["id"])
            self.refresh_changes(selected["id"])
        elif clicked is resume:
            self.resume_saved_task(state)
        elif clicked is new_task:
            self.composer.setPlainText(
                f"Continue from task #{selected['id']} using this recorded context.\n"
                f"Original request: {selected['message']}\n"
                f"Recorded response: {selected.get('answer') or selected.get('error') or '(none)'}\n\n"
                "New request: ")
            self.composer.setFocus()
        return

    def resume_saved_task(self, state: dict):
        if not state.get("can_resume"):
            return
        if not self.project or Path(state.get("workspace", "")).resolve() != self.project.resolve():
            QMessageBox.information(self, "Open the original project", "Resume is allowed only after reopening the project that owns this task.")
            return
        mode, ok = QInputDialog.getItem(self, "Resume task", "Choose permissions for this retry:",
                                        ["Restricted · read only", "Workspace · edits and approved actions"], 0, False)
        if not ok:
            return
        permission = "workspace" if mode.startswith("Workspace") else "read_only"
        confirm = QMessageBox.question(self, "Resume from the beginning",
            "This task had not started a tool, so Luna can safely retry it. The original request will run again with the selected permission mode. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self.mode.setCurrentIndex(max(0, self.mode.findData(permission)))
        try:
            request("POST", f"/api/tasks/{state['id']}/resume", json={"mode": permission})
        except Exception as exc:
            QMessageBox.warning(self, "Could not resume task", str(exc))
            return
        self._start_task_worker(state["message"], permission, state["workspace"], state["id"])

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
        if self.project:
            self.refresh_changes(task_id)

    def refresh_changes(self, task_id: int):
        try:
            state = request("GET", f"/api/tasks/{task_id}")
            changes = request("GET", f"/api/tasks/{task_id}/changes").get("changes", [])
        except Exception as exc:
            self.status.showMessage(f"Could not refresh task changes: {exc}", 7000)
            return
        if not self.project or Path(state.get("workspace", "")).resolve() != self.project.resolve():
            return
        self.last_task_id = task_id
        marked = {}
        self.changes_list.clear()
        for change in changes:
            operation = change.get("operation", "write")
            if change.get("status") == "restored":
                label = "Restored"
            elif operation == "delete":
                label = "Deleted"
            elif operation == "move":
                label = "Moved"
            elif change.get("before_hash") is None:
                label = "Added"
            else:
                label = "Modified"
            if change.get("status") != "restored":
                marked[str(self.project / change["path"])] = label
            if operation == "move" and change.get("destination"):
                marked[str(self.project / change["destination"])] = "Moved here"
            target = change.get("destination") if operation == "move" else change["path"]
            item = QListWidgetItem(f"{target} · {label} · task #{task_id}")
            item.setData(Qt.UserRole, change)
            self.changes_list.addItem(item)
        if changes:
            self.findChild(QDockWidget, "changes-dock").show()
        self.fs_model.set_changed_paths(marked)
        self.files.viewport().update()

    def open_selected_change(self, *_):
        item = self.changes_list.currentItem()
        change = item.data(Qt.UserRole) if item else None
        if not change:
            return
        title = change.get("path", "Change") + " diff"
        for index in range(self.editor_tabs.count()):
            if self.editor_tabs.tabText(index).removeprefix("● ") == title:
                self.editor_tabs.setCurrentIndex(index)
                return
        diff = CodeEditor(self.editor_tabs)
        diff.setReadOnly(True)
        diff.setPlainText(change.get("diff") or "No textual diff is available.")
        self.editor_tabs.addTab(diff, title)
        self.editor_tabs.setCurrentWidget(diff)
        self.breadcrumbs.setText(title)

    def restore_selected_change(self):
        item = self.changes_list.currentItem()
        change = item.data(Qt.UserRole) if item else None
        if not change or change.get("status") != "applied":
            return
        answer = QMessageBox.question(self, "Restore change",
            f"Restore {change['path']} to its pre-task contents?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            request("POST", f"/api/changes/{change['id']}/restore")
            change["status"] = "restored"
            item.setText(item.text().replace("Added", "Restored").replace("Modified", "Restored")
                         .replace("Deleted", "Restored").replace("Moved", "Restored"))
            self.status.showMessage(f"Restored {change['path']}", 5000)
        except Exception as exc:
            QMessageBox.warning(self, "Restore failed", str(exc))

    def filter_tree(self, value: str):
        self.fs_proxy.setFilterFixedString(value)

    def search_contents(self):
        query = self.content_search.text().strip()
        if not self.project:
            QMessageBox.information(self, "Open a project", "Content search is limited to the active project.")
            return
        if not query:
            return
        dialog = SearchDialog(query, self)
        dialog.results.itemDoubleClicked.connect(
            lambda row: self.preview_path(self.project / row.data(Qt.UserRole)["path"]))
        dialog.exec()

    def preview_file(self, index):
        source_index = self.fs_proxy.mapToSource(index)
        path = Path(self.fs_model.filePath(source_index))
        self.preview_path(path)

    def preview_path(self, path: Path):
        if not self.project or _protected_preview_path(path, self.project):
            self.status.showMessage("Credential files and repository metadata are protected from preview.", 5000)
            return
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.project.resolve())
        except (OSError, ValueError, RuntimeError):
            self.status.showMessage("Preview is limited to files inside the active project.", 5000)
            return
        if is_link_or_junction(path) or not resolved.is_file() or resolved.stat().st_size > 1_000_000:
            return
        try:
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            self.status.showMessage("This file is not readable UTF-8 text.", 5000)
            return
        for index in range(self.editor_tabs.count()):
            editor = self.editor_tabs.widget(index)
            if self.editor_tabs.file_paths.get(editor) == resolved:
                self.editor_tabs.setCurrentIndex(index)
                return
        welcome = self.editor_tabs.widget(0)
        if self.editor_tabs.count() == 1 and welcome not in self.editor_tabs.file_paths:
            self.editor_tabs.removeTab(0)
            welcome.deleteLater()
        editor = CodeEditor(self.editor_tabs)
        editor.setPlainText(text)
        editor.document().setModified(False)
        self.editor_tabs.file_paths[editor] = resolved
        index = self.editor_tabs.addTab(editor, resolved.name)
        editor.document().modificationChanged.connect(lambda _dirty, item=editor: self.editor_tabs._update_title(item))
        editor.cursorPositionChanged.connect(lambda item=editor: self._update_breadcrumb(item))
        self.editor_tabs.setCurrentIndex(index)
        self._update_breadcrumb(editor)

    def _update_breadcrumb(self, editor):
        path = self.editor_tabs.file_paths.get(editor)
        if not path or not self.project:
            return
        try:
            relative = path.relative_to(self.project)
        except ValueError:
            return
        cursor = editor.textCursor()
        self.breadcrumbs.setText(f"{self.project.name}  /  {relative.as_posix()}   Ln {cursor.blockNumber() + 1}, Col {cursor.positionInBlock() + 1}")

    def run_task(self):
        message = self.composer.toPlainText().strip()
        if not self.project:
            QMessageBox.information(self, "Open a project", "Choose a project folder before starting a task.")
            return
        if not message:
            return
        self._start_task_worker(message, self.mode.currentData(), str(self.project))

    def _start_task_worker(self, message: str, mode: str, workspace: str, task_id: int | None = None):
        if self.worker and self.worker.isRunning():
            return
        self.transcript.appendPlainText(f"\nYou\n{message}\n\nLuna\n")
        self.composer.clear()
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker = TaskWorker(message, mode, workspace, task_id)
        self.worker.event.connect(self.on_event)
        self.worker.approval_requested.connect(self.confirm_approval)
        self.worker.finished_task.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()
        self.agent_status.setText("Working")
        self.status.showMessage("Task running…")

    def on_event(self, kind: str, detail: str):
        if kind == "assistant_delta":
            cursor = self.transcript.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(detail)
            self.transcript.setTextCursor(cursor)
        else:
            summary = (detail.splitlines()[0] if detail.splitlines() else kind)[:240]
            if kind == "tool":
                action, _, name = summary.partition(" ")
                if action == "Started":
                    item = QTreeWidgetItem([name or summary])
                    item.addChild(QTreeWidgetItem(["Running…"]))
                    self.activity.addTopLevelItem(item)
                    self.tool_activity_items[name] = item
                elif action == "Finished" and name in self.tool_activity_items:
                    parent = self.tool_activity_items.pop(name)
                    parent.child(0).setText(0, "Finished")
                else:
                    self.activity.addTopLevelItem(QTreeWidgetItem([summary]))
            elif kind == "command":
                item = QTreeWidgetItem([summary])
                output = detail.partition("\n")[2]
                if output:
                    item.addChild(QTreeWidgetItem([output[:20_000]]))
                self.activity.addTopLevelItem(item)
            else:
                self.activity.addTopLevelItem(QTreeWidgetItem([f"{kind}: {summary}"]))
            if self.activity.topLevelItemCount() > 2500:
                self.activity.takeTopLevelItem(0)
            if kind in ("tool", "command"):
                self.findChild(QDockWidget, "output-dock").show()
            if kind in ("approval", "error", "status"):
                self.transcript.appendPlainText(f"[{kind}] {summary}")
            elif kind == "tool":
                self.transcript.appendPlainText(f"Working: {summary}")

    def confirm_approval(self, approval: dict):
        command = str(approval.get("command", "Sensitive action"))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Action approval required")
        box.setText("Luna is waiting for approval.")
        box.setInformativeText("PowerShell commands use your Windows account and are not sandboxed to the project.")
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
        self.agent_status.setText(state.get("status", "Ready").title())
        if state.get("id"):
            self.refresh_changes(state["id"])
        self._task_done()

    def on_failed(self, error: str):
        self.activity.addTopLevelItem(QTreeWidgetItem(["Error: " + error[:240]]))
        QMessageBox.critical(self, "Task error", error)
        self._task_done()
        self.agent_status.setText("Error")

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
        elif self.composer.toPlainText().strip():
            choice = QMessageBox.question(self, "Unsent task", "Close Luna Desk and discard the unsent task text?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if choice != QMessageBox.Yes:
                event.ignore()
                return
        dirty = [editor for editor in self.editor_tabs.file_paths if editor.document().isModified()]
        if dirty:
            answer = QMessageBox.question(self, "Unsaved files", "Save changes to open files before closing?",
                                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                          QMessageBox.Save)
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save and not all(self.editor_tabs.save_editor(editor) for editor in dirty):
                event.ignore()
                return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        self.settings.setValue("workspace/split", self.findChild(QSplitter, "workspace-splitter").sizes())
        self.settings.setValue("workspace/chat_split", self.findChild(QSplitter, "editor-agent-splitter").sizes())
        self.terminal.shutdown()
        self.backend.stop()
        event.accept()


class IntegrationsDialog(QDialog):
    """Native management view for shared skills, MCP and automation settings."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Skills, MCP and automation")
        self.resize(800, 650)
        self.worker = None
        self.cancel_worker = None
        self.skills = {}
        self.servers = {}
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        skills_tab = QWidget()
        skills_layout = QVBoxLayout(skills_tab)
        self.skill_list = QListWidget()
        self.skill_details = QPlainTextEdit()
        self.skill_details.setReadOnly(True)
        skills_layout.addWidget(self.skill_list, 1)
        skills_layout.addWidget(self.skill_details, 1)
        skill_buttons = QHBoxLayout()
        for label, callback in (("Install from folder…", self.install_skill), ("Inspect", self.inspect_skill), ("Enable / disable", self.toggle_skill), ("Remove", self.remove_skill), ("Refresh", self.refresh_skills)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            skill_buttons.addWidget(button)
        skills_layout.addLayout(skill_buttons)
        tabs.addTab(skills_tab, "Skills")

        mcp_tab = QWidget()
        mcp_layout = QVBoxLayout(mcp_tab)
        mcp_form = QFormLayout()
        self.mcp_name = QLineEdit()
        self.mcp_url = QLineEdit()
        self.mcp_url.setPlaceholderText("https://host/mcp or configured loopback URL")
        mcp_form.addRow("Server name", self.mcp_name)
        mcp_form.addRow("Streamable HTTP URL", self.mcp_url)
        mcp_layout.addLayout(mcp_form)
        self.mcp_list = QListWidget()
        mcp_layout.addWidget(self.mcp_list, 1)
        self.mcp_details = QPlainTextEdit()
        self.mcp_details.setReadOnly(True)
        mcp_layout.addWidget(self.mcp_details, 1)
        self.mcp_list.itemDoubleClicked.connect(self.inspect_mcp_tools)
        mcp_buttons = QHBoxLayout()
        for label, callback in (("Add server", self.add_mcp), ("Connect / refresh tools", self.connect_mcp), ("Enable / disable tools", self.toggle_mcp), ("Disconnect", self.disconnect_mcp), ("Remove", self.remove_mcp), ("Refresh", self.refresh_mcp)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            mcp_buttons.addWidget(button)
        mcp_layout.addLayout(mcp_buttons)
        tabs.addTab(mcp_tab, "MCP")

        automation = QWidget()
        automation_layout = QVBoxLayout(automation)
        automation_layout.addWidget(QLabel("Browser automation"))
        self.browser_status = QLabel("")
        automation_layout.addWidget(self.browser_status)
        self.browser_url = QLineEdit()
        self.browser_url.setPlaceholderText("https://public-site.example (loopback requires explicit opt-in)")
        automation_layout.addWidget(self.browser_url)
        browser_buttons = QHBoxLayout()
        self.browser_toggle = QPushButton("Start browser session")
        self.browser_toggle.clicked.connect(self.toggle_browser)
        browser_buttons.addWidget(self.browser_toggle)
        self.browser_local = QCheckBox("Allow localhost for this session")
        browser_buttons.addWidget(self.browser_local)
        for label, callback in (("Navigate", self.browser_navigate), ("Inspect page", self.browser_inspect),
                                ("Screenshot", self.browser_capture)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            browser_buttons.addWidget(button)
        automation_layout.addLayout(browser_buttons)
        self.browser_selector = QLineEdit()
        self.browser_selector.setPlaceholderText("CSS selector for an explicitly approved click or fill")
        self.browser_value = QLineEdit()
        self.browser_value.setPlaceholderText("Non-secret text to fill (blank to click)")
        automation_layout.addWidget(self.browser_selector)
        automation_layout.addWidget(self.browser_value)
        browser_actions = QHBoxLayout()
        for label, callback in (("Inspect page text", self.browser_inspect), ("Click / fill with approval", self.browser_act),
                                ("Cancel / close session", self.close_browser)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            browser_actions.addWidget(button)
        automation_layout.addLayout(browser_actions)
        self.browser_page = QPlainTextEdit()
        self.browser_page.setReadOnly(True)
        self.browser_page.setMaximumHeight(140)
        automation_layout.addWidget(self.browser_page)
        automation_layout.addWidget(QLabel("Windows UI Automation"))
        self.desktop_status = QLabel("")
        automation_layout.addWidget(self.desktop_status)
        self.desktop_windows = QComboBox()
        automation_layout.addWidget(self.desktop_windows)
        desktop_controls = QHBoxLayout()
        for label, callback in (("Refresh windows", self.refresh_windows), ("Select window", self.select_window),
                                ("Inspect controls", self.inspect_window), ("Screenshot", self.desktop_capture)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            desktop_controls.addWidget(button)
        automation_layout.addLayout(desktop_controls)
        self.desktop_control = QComboBox()
        self.desktop_text = QLineEdit()
        self.desktop_text.setPlaceholderText("Optional non-secret text for the selected Edit control")
        self.desktop_details = QPlainTextEdit()
        self.desktop_details.setReadOnly(True)
        self.desktop_details.setMaximumHeight(130)
        automation_layout.addWidget(self.desktop_control)
        automation_layout.addWidget(self.desktop_text)
        desktop_actions = QHBoxLayout()
        for label, callback in (("Click selected control", self.desktop_click), ("Type into selected control", self.desktop_type)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            desktop_actions.addWidget(button)
        self.desktop_toggle = QPushButton("Enable desktop access")
        self.desktop_toggle.clicked.connect(self.toggle_desktop)
        desktop_actions.addWidget(self.desktop_toggle)
        automation_layout.addLayout(desktop_actions)
        automation_layout.addWidget(self.desktop_details)
        automation_layout.addStretch(1)
        tabs.addTab(automation, "Automation")
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        outer.addWidget(close)
        self.refresh_skills()
        self.refresh_mcp()
        self.refresh_automation()

    def _selected_id(self, listing):
        item = listing.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _action(self, method, path, payload, button, callback, timeout=10):
        if self.worker and self.worker.isRunning():
            return
        button = button or self.status
        button.setEnabled(False)
        self.worker = ApiWorker(method, path, payload, timeout, self)
        self.worker.completed.connect(lambda result: (button.setEnabled(True), callback(result)))
        self.worker.failed.connect(lambda error: (button.setEnabled(True), self.status.setText(error)))
        self.worker.finished.connect(lambda: setattr(self, "worker", None))
        self.worker.start()

    def refresh_skills(self):
        try:
            items = request("GET", "/api/skills").get("skills", [])
            self.skills = {item["id"]: item for item in items}
            self.skill_list.clear()
            for item in items:
                row = QListWidgetItem(f"{item['name']} · v{item['version']} · {'enabled' if item['enabled'] else 'disabled'}")
                row.setData(Qt.UserRole, item["id"])
                self.skill_list.addItem(row)
            self.status.setText(f"{len(items)} skill(s) configured. Skill instructions are untrusted task context.")
        except Exception as exc:
            self.status.setText(str(exc))

    def install_skill(self):
        source = QFileDialog.getExistingDirectory(self, "Install skill from folder", str(Path.home()))
        if not source:
            return
        try:
            request("POST", "/api/skills/install", json={"source": source})
            self.refresh_skills()
        except Exception as exc:
            self.status.setText(str(exc))

    def inspect_skill(self):
        skill_id = self._selected_id(self.skill_list)
        if not skill_id:
            return
        try:
            info = request("GET", f"/api/skills/{skill_id}")
            self.skill_details.setPlainText(info.get("instructions", "") + "\n\nFiles: " + ", ".join(info.get("files", [])))
        except Exception as exc:
            self.status.setText(str(exc))

    def toggle_skill(self):
        skill_id = self._selected_id(self.skill_list)
        if not skill_id:
            return
        enabled = not self.skills[skill_id]["enabled"]
        try:
            request("PUT", f"/api/skills/{skill_id}/enabled", json={"enabled": enabled})
            self.refresh_skills()
        except Exception as exc:
            self.status.setText(str(exc))

    def remove_skill(self):
        skill_id = self._selected_id(self.skill_list)
        if not skill_id:
            return
        item = self.skills[skill_id]
        if QMessageBox.question(self, "Remove skill", f"Remove {item['name']} and its Luna-managed copy?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            request("DELETE", f"/api/skills/{skill_id}")
            self.refresh_skills()
        except Exception as exc:
            self.status.setText(str(exc))

    def refresh_mcp(self):
        try:
            items = request("GET", "/api/mcp").get("servers", [])
            self.servers = {item["id"]: item for item in items}
            self.mcp_list.clear()
            for item in items:
                state = ("connected" if item.get("connected") else "disconnected") + (" · enabled" if item.get("enabled") else " · disabled")
                tools = ", ".join(tool["name"] for tool in item.get("tools", []))
                row = QListWidgetItem(f"{item['name']} · {state}\n{item['url']}\n{tools}")
                row.setData(Qt.UserRole, item["id"])
                self.mcp_list.addItem(row)
        except Exception as exc:
            self.status.setText(str(exc))

    def add_mcp(self):
        name, url = self.mcp_name.text().strip(), self.mcp_url.text().strip()
        if not name or not url:
            self.status.setText("Enter both an MCP server name and URL.")
            return
        try:
            request("POST", "/api/mcp", json={"name": name, "url": url})
            self.mcp_name.clear()
            self.mcp_url.clear()
            self.refresh_mcp()
        except Exception as exc:
            self.status.setText(str(exc))

    def connect_mcp(self):
        server_id = self._selected_id(self.mcp_list)
        if not server_id:
            return
        self._action("POST", f"/api/mcp/{server_id}/connect", {}, self.sender(), lambda _: self.refresh_mcp(), timeout=45)

    def toggle_mcp(self):
        server_id = self._selected_id(self.mcp_list)
        if not server_id:
            return
        enabled = not self.servers[server_id].get("enabled")
        if enabled and QMessageBox.question(self, "Enable MCP tools", "Each MCP tool call will pause for approval. Enable tools from this server?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            request("PUT", f"/api/mcp/{server_id}/enabled", json={"enabled": enabled})
            self.refresh_mcp()
        except Exception as exc:
            self.status.setText(str(exc))

    def disconnect_mcp(self):
        server_id = self._selected_id(self.mcp_list)
        if server_id:
            self._action("POST", f"/api/mcp/{server_id}/disconnect", {}, self.sender(), lambda _: self.refresh_mcp())

    def remove_mcp(self):
        server_id = self._selected_id(self.mcp_list)
        if not server_id:
            return
        if QMessageBox.question(self, "Remove MCP server", "Remove this server configuration?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            request("DELETE", f"/api/mcp/{server_id}")
            self.refresh_mcp()
        except Exception as exc:
            self.status.setText(str(exc))

    def refresh_automation(self):
        try:
            state = request("GET", "/api/browser/status")
            self.browser_status.setText("Playwright installed" if state.get("installed") else "Install requirements-browser.txt and Chromium to use browser automation")
            self.browser_toggle.setText("Close browser session" if state.get("active") else "Start browser session")
        except Exception as exc:
            self.browser_status.setText(str(exc))
        try:
            state = request("GET", "/api/desktop/status")
            self.desktop_status.setText("Available" if state.get("available") else "Install requirements-desktop.txt to use Windows UI Automation")
            self.desktop_toggle.setText("Disable desktop access" if state.get("enabled") else "Enable desktop access")
            self.desktop_toggle.setEnabled(bool(state.get("available")))
        except Exception as exc:
            self.desktop_status.setText(str(exc))

    def toggle_browser(self):
        try:
            state = request("GET", "/api/browser/status")
            if state.get("active"):
                self._action("POST", "/api/browser/close", {}, self.sender(), lambda _: self.refresh_automation())
            else:
                self._action("POST", "/api/browser/start", {"allow_local": self.browser_local.isChecked()}, self.sender(), lambda _: self.refresh_automation())
        except Exception as exc:
            self.browser_status.setText(str(exc))

    def close_browser(self):
        if self.cancel_worker and self.cancel_worker.isRunning():
            return
        self.browser_status.setText("Closing browser session and stopping its current navigation…")
        self.cancel_worker = ApiWorker("POST", "/api/browser/close", {}, 10, self)
        self.cancel_worker.completed.connect(lambda _: self.refresh_automation())
        self.cancel_worker.failed.connect(lambda error: self.browser_status.setText("Could not close browser session: " + error))
        self.cancel_worker.start()

    def browser_navigate(self):
        url = self.browser_url.text().strip()
        if not url:
            self.status.setText("Enter a public HTTPS URL. Loopback requires the explicit session option.")
            return
        self._action("POST", "/api/browser/navigate", {"url": url}, self.sender(),
                     lambda result: self.browser_page.setPlainText(json.dumps(result, indent=2)), timeout=25)

    def browser_inspect(self):
        self._action("GET", "/api/browser/page", None, self.sender(),
                     lambda result: self.browser_page.setPlainText(
                         f"{result.get('title', '')}\n\n{result.get('text', '')}" +
                         ("\n[page text truncated]" if result.get("truncated") else "")), timeout=12)

    def browser_act(self):
        selector = self.browser_selector.text().strip()
        if not selector:
            self.status.setText("Enter a selector before choosing a browser action.")
            return
        value = self.browser_value.text()
        label = f"Fill selector {selector!r} with {value!r}" if value else f"Click selector {selector!r}"
        if QMessageBox.question(self, "Approve browser action", f"{label}? Page content is untrusted.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        path = "/api/browser/fill" if value else "/api/browser/click"
        payload = {"selector": selector, "value": value} if value else {"selector": selector}
        self._action("POST", path, payload, self.sender(),
                     lambda result: self.browser_page.setPlainText(json.dumps(result, indent=2)), timeout=15)

    def browser_capture(self):
        self._action("POST", "/api/browser/screenshot", {}, self.sender(),
                     lambda result: self.show_screenshot("/api/browser/screenshots/" + result["name"], "Browser screenshot"), timeout=15)

    def show_screenshot(self, path: str, title: str):
        try:
            response = httpx.get(BASE_URL + path, timeout=15)
            response.raise_for_status()
            pixmap = QPixmap()
            if not pixmap.loadFromData(response.content):
                raise RuntimeError("Screenshot data could not be decoded.")
            dialog = QDialog(self)
            dialog.setWindowTitle(title)
            dialog.resize(900, 650)
            layout = QVBoxLayout(dialog)
            view = QLabel()
            view.setPixmap(pixmap.scaled(860, 560, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            layout.addWidget(view, 1)
            dialog.exec()
        except Exception as exc:
            self.status.setText("Screenshot error: " + str(exc))

    def toggle_desktop(self):
        try:
            state = request("GET", "/api/desktop/status")
            self._action("PUT", "/api/desktop/enabled", {"enabled": not state.get("enabled")}, self.sender(), lambda _: self.refresh_automation())
        except Exception as exc:
            self.desktop_status.setText(str(exc))

    def refresh_windows(self):
        self._action("GET", "/api/desktop/windows", None, self.sender(), self._show_windows, timeout=15)

    def _show_windows(self, result):
        self.desktop_windows.clear()
        for item in result.get("windows", []):
            self.desktop_windows.addItem(item["title"], item["handle"])
        if not self.desktop_windows.count():
            self.desktop_status.setText("No accessible titled windows were found.")

    def select_window(self):
        handle = self.desktop_windows.currentData()
        if not handle:
            return
        self._action("POST", "/api/desktop/select", {"handle": handle}, self.sender(),
                     lambda result: (self.desktop_status.setText("Selected: " + result.get("title", "")), self.inspect_window()), timeout=15)

    def inspect_window(self):
        handle = self.desktop_windows.currentData()
        if not handle:
            return
        self._action("GET", f"/api/desktop/windows/{handle}", None, self.sender(), self._show_controls, timeout=15)

    def _show_controls(self, result):
        self.desktop_control.clear()
        for item in result.get("controls", []):
            label = f"{item['index']}: {item['type']} {item.get('name') or item.get('automation_id') or '(unnamed)'} · {'enabled' if item.get('enabled') else 'disabled'}"
            self.desktop_control.addItem(label, item["index"])
        self.desktop_details.setPlainText(json.dumps(result, indent=2))

    def desktop_capture(self):
        handle = self.desktop_windows.currentData()
        if not handle:
            return
        self._action("POST", "/api/desktop/screenshot", {"handle": handle}, self.sender(),
                     lambda result: self.show_screenshot("/api/desktop/screenshots/" + result["name"], "Desktop screenshot"), timeout=15)

    def _desktop_action(self, action: str):
        handle, index = self.desktop_windows.currentData(), self.desktop_control.currentData()
        if handle is None or index is None:
            return
        text = self.desktop_text.text() if action == "type" else ""
        label = f"{action.title()} control {self.desktop_control.currentText()}"
        if text:
            label += f" with text {text!r}"
        if QMessageBox.question(self, "Approve Windows interaction", label + "? This interacts with the selected application as your current user.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        payload = {"handle": handle, "index": index}
        if action == "type":
            payload["text"] = text
        path = "/api/desktop/type" if action == "type" else "/api/desktop/click"
        self._action("POST", path, payload, self.sender(), lambda result: self.desktop_status.setText(json.dumps(result)))

    def desktop_click(self):
        self._desktop_action("click")

    def desktop_type(self):
        self._desktop_action("type")

    def inspect_mcp_tools(self, _item=None):
        server_id = self._selected_id(self.mcp_list)
        if not server_id:
            return
        server = self.servers.get(server_id, {})
        self.mcp_details.setPlainText(json.dumps({"server": server.get("name"), "connection_error": server.get("last_error"),
                                                 "tools_and_schemas": server.get("tools", [])}, indent=2)[:12000])

    def closeEvent(self, event):
        if ((self.worker and self.worker.isRunning())
                or (self.cancel_worker and self.cancel_worker.isRunning())):
            self.status.setText("Please wait for the current connection operation to finish before closing.")
            event.ignore()
            return
        event.accept()


class ProviderDialog(QDialog):
    """Small native provider editor backed by the same provider API as the browser."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Provider settings")
        self.resize(720, 780)
        self.providers = []
        self.worker = None
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
        self.numeric = {}
        for key, title, minimum, maximum, value in (
            ("timeout", "Request timeout (seconds)", 5, 300, 60),
            ("max_iterations", "Agent iterations", 1, 100, 12),
            ("max_requests_per_window", "Requests per window", 1, 600, 5),
            ("request_window_seconds", "Request window (seconds)", 1, 3600, 60),
            ("retry_limit", "Retry limit", 0, 5, 2),
            ("max_backoff_seconds", "Maximum backoff (seconds)", 1, 300, 30),
            ("task_timeout_seconds", "Task deadline (seconds)", 30, 7200, 900),
        ):
            spin = QSpinBox()
            spin.setRange(minimum, maximum)
            spin.setValue(value)
            self.numeric[key] = spin
            form.addRow(title, spin)
        self.numeric["min_request_interval_seconds"] = QDoubleSpinBox()
        self.numeric["min_request_interval_seconds"].setRange(0, 3600)
        self.numeric["min_request_interval_seconds"].setDecimals(2)
        self.numeric["min_request_interval_seconds"].setValue(12)
        form.addRow("Minimum request interval (seconds)", self.numeric["min_request_interval_seconds"])
        self.streaming = QCheckBox("Prefer streaming responses")
        form.addRow("Streaming", self.streaming)
        layout.addLayout(form)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        add = QPushButton("New Apinex provider")
        add.clicked.connect(self.new_provider)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self.test_provider)
        save = QPushButton("Save provider")
        save.clicked.connect(self.save_provider)
        default = QPushButton("Make default")
        default.clicked.connect(self.make_default)
        finish = QPushButton("Finish first-run setup")
        finish.clicked.connect(self.finish_setup)
        remove_key = QPushButton("Remove saved key")
        remove_key.clicked.connect(self.remove_key)
        remove_provider = QPushButton("Remove provider")
        remove_provider.clicked.connect(self.remove_provider)
        actions.addWidget(add)
        actions.addWidget(self.test_button)
        actions.addWidget(save)
        actions.addWidget(default)
        actions.addWidget(finish)
        actions.addWidget(remove_key)
        actions.addWidget(remove_provider)
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
        for key, control in self.numeric.items():
            fallback = 12 if key == "min_request_interval_seconds" else control.value()
            control.setValue(item.get(key) if item.get(key) is not None else fallback)
        self.streaming.setChecked(bool(item.get("streaming")))
        self.message.setText("Key configured" if item.get("key_configured") else "No key configured")

    def new_provider(self):
        self.selector.setCurrentIndex(-1)
        for key, value in (("name", "Apinex Luna"), ("provider", "Apinex"), ("base_url", "https://api.apinex.bond/v1"), ("model", "free/gpt-6-luna")):
            self.fields[key].setText(value)
        self.fields["api_key"].clear()
        self.fields["api_key"].setPlaceholderText("Enter API key")
        self.protocol.setCurrentIndex(0)
        defaults = {"timeout": 60, "max_iterations": 12, "max_requests_per_window": 5,
                    "request_window_seconds": 60, "min_request_interval_seconds": 12,
                    "retry_limit": 2, "max_backoff_seconds": 30, "task_timeout_seconds": 900}
        for key, control in self.numeric.items():
            control.setValue(defaults[key])
        self.streaming.setChecked(False)

    def payload(self):
        selected = self.selector.currentData()
        item = next((p for p in self.providers if p["id"] == selected), None)
        data = {"id": selected, **{key: self.fields[key].text().strip() for key in ("name", "provider", "base_url", "model")},
                "protocol": self.protocol.currentData(), "api_key": self.fields["api_key"].text() or None,
                "streaming": self.streaming.isChecked()}
        data.update({key: control.value() for key, control in self.numeric.items()})
        return data

    def test_provider(self):
        self.test_button.setEnabled(False)
        self.message.setText("Testing provider connection…")
        self.worker = ApiWorker("POST", "/api/providers/test", self.payload(), 330, self)
        self.worker.completed.connect(self._test_completed)
        self.worker.failed.connect(self._test_failed)
        self.worker.finished.connect(self._clear_worker)
        self.worker.start()

    def _test_completed(self, result):
        self.message.setText(("Connection succeeded" if result.get("ok") else "Connection failed") + ": " + result.get("message", "No details"))
        self.test_button.setEnabled(True)

    def _test_failed(self, message):
        self.message.setText(message)
        self.test_button.setEnabled(True)

    def _clear_worker(self):
        self.worker = None

    def remove_key(self):
        selected = self.selector.currentData()
        if not selected:
            self.message.setText("Save the provider before removing its key.")
            return
        if QMessageBox.question(self, "Remove API key", "Remove this provider's saved key from local storage?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            request("DELETE", f"/api/providers/{selected}/key")
            self.reload()
            self.message.setText("Saved key removed.")
        except Exception as exc:
            self.message.setText(str(exc))

    def remove_provider(self):
        selected = self.selector.currentData()
        if not selected:
            return
        if QMessageBox.question(self, "Remove provider", "Remove this provider and its saved key?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            request("DELETE", f"/api/providers/{selected}")
            self.reload()
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

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.message.setText("Please wait for the provider connection test to finish before closing.")
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Luna Desk")
    app.setOrganizationName("LunaDesk")
    providers.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(providers.DATA_DIR / "desktop.lock"))
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
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(1500, window.close)
    result = app.exec()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
