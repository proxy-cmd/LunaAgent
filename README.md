# Luna Desk

Luna Desk is a local-first coding assistant. It stores provider settings, project history, and automation data on this computer. The server binds to `127.0.0.1`.

## Start on Windows

Python 3.11 or newer is required. In PowerShell, from this folder, run:

```powershell
.\run.ps1
```

Open http://127.0.0.1:8765. First-run settings use Apinex (`https://api.apinex.bond/v1`), model `free/gpt-6-luna`, and Chat Completions. Enter a key, choose **Test Connection**, save the provider, then finish setup. Settings, skills, automation setup, help, and diagnostics remain available if AI is offline.

## Native desktop app (preview)

The desktop client uses PySide6 Qt Widgets and the same local FastAPI process,
provider configuration, permissions, task database, and agent runtime as the
browser edition. From PowerShell, run:

```powershell
.\run-desktop.ps1
```

The script creates or uses the project `.venv` and installs
`requirements-desktop.txt` there. The desktop app starts or connects to the
local service on `127.0.0.1:8765`; it does not open a browser. Use **Open
Project** or Ctrl+O to open a native Windows folder picker. Ctrl+N focuses the
task composer, Ctrl+Enter submits, and Ctrl+H opens saved task history. Provider
settings, connection testing, recent projects, file and content search,
read-only file preview, live task events, cancellation, task history/recovery,
diff restore, and native automation controls are available in panels and dialogs.

The toolbar permission selector maps directly to the backend's supported modes:
Restricted (read-only) and Workspace (edits and approved actions). The backend
does not implement Trusted Full Access. Provider API keys are entered into a
masked native field and stored through the existing local provider mechanism.
The native integrations view manages skills and MCP servers and provides
browser-session and Windows desktop-automation controls. The browser Settings
page at `http://127.0.0.1:8765` retains help and diagnostics.

The terminal starts a persistent PowerShell process in a Windows ConPTY session
only when you choose **Start PowerShell**. Enter commands or program input in
the line field, choose its working folder, and use **Terminate** to stop the
shell Luna started. Output is incremental, combines stdout and stderr, is capped
at 1 MB, and the shell terminates after four hours. The pane does not emulate
all terminal keys or resize behavior, so full-screen terminal applications are
unsupported. Manual shell commands run with your Windows account permissions
and are not OS-sandboxed. Agent commands remain separate and use the backend's
approval policy.

Use **Search file contents** to find text under the active project. Search
returns file and line previews, skips hidden and protected paths, `.gitignore`
matches, symlinks, binary/unreadable files, and files over 500 KB. It scans at
most 10,000 files for 15 seconds and returns at most 100 results; cancellation
closes the streaming request. The file tree marks changes from the latest
reviewed task and the change panel keeps task IDs, operations, and statuses.
Markers are not a complete Git status view and show the latest task only.

Task history shows recorded events and skills. Resume is offered only for an
interrupted task with no tool or command event, and only in its original
project. It retries from the beginning after a confirmation; the permission
mode is chosen again. Other interrupted work can be started as a new task with
the recorded request and response prefilled.

Native automation exposes browser session setup, permitted navigation, page
inspection, screenshots, and confirmed click/fill actions. Local browser access
must be selected before starting a session. Windows desktop controls list and
inspect visible windows; click and type ask for confirmation. Desktop controls
require the optional Windows UI Automation dependency and never elevate or
bypass UAC. Native dialogs and real interactions should be checked on each
target Windows setup.

For a manual launch:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

## Providers and keys

The Settings page supports multiple OpenAI-compatible providers, Chat Completions, and Responses. Connection tests make a real, small request and report the returned model and usage. Agent tasks use the selected protocol and do not silently switch providers or models.

Provider metadata lives in `.luna/providers.json`; API keys live in the ignored root `.env`. The settings API returns only whether a key exists. Use **Change key** or **Remove key** in the provider card. The `.env` file is local plaintext protected by the current Windows account’s file permissions. Do not share it. Rotate a key if it has been exposed.

## Request pacing and retries

Each provider has editable limits for requests per rolling window, the minimum interval between requests, retry count, maximum backoff, request timeout, and overall task deadline. The default for an Apinex `free/*` model is 5 HTTP requests in 60 seconds, paced at least 12 seconds apart, with 2 retries and a 30-second maximum backoff. Other new providers default to 60 requests per 60 seconds with no minimum interval. These are local settings, not claims about provider policy.

APInex's public [Chat Completions documentation](https://apinex.bond/developers/models/chat) describes its endpoint and tool support, but does not define what a “round” means or publish a per-model request cap there. Luna therefore treats the reported 5-round rate as an unverified assumption. A local tool execution is not a model request; the next model call is. The rolling limiter state is saved in `.luna/request_pacing.json` and coordinates tasks in this Luna process across restarts. It does not coordinate separate Luna processes.

On HTTP 429, Luna distinguishes rate limiting from quota and billing errors. It honors a valid `Retry-After` value in seconds or HTTP-date form; otherwise it uses capped exponential backoff with jitter. If a provider's indicated delay exceeds the configured maximum, Luna does not retry early. It retries a small bounded number of connect failures and server errors. It does not retry authentication, quota, or validation failures, and does not restart a stream after partial content. Task history records request attempts, successful responses, rate limits, retries, and wait time when available.

Request limits count HTTP calls; they do not track daily token allowance. Luna does not reset or enforce a provider's daily token balance. Check the provider dashboard for current allowance and usage.

## Projects and agent tasks

Open **Workspace**, enter an existing folder path, and choose **Open Project**. Browsers cannot browse arbitrary host folders, so Luna uses a path field. Project inspection reports common manifests, languages, likely entry points, scripts, Git status, and whether `AGENTS.md` exists; it does not run project code.

Read-only mode can list, search, and read bounded text files. Workspace mode can also edit files, run approved commands, move files or directories, and delete files or directories. Destructive file operations require a separate approval. File changes have snapshots and diffs; restore checks the current file before changing it. Credential files, Git metadata, symlinks, and paths outside the active project are blocked.

For accepted content, the `write_file` tool encodes the supplied text as UTF-8 without changing punctuation, whitespace, or line endings. Its guarantee starts with the content received by the tool; a model can still misunderstand a request for literal text. Read the file back and compare its SHA-256 when exact content matters.

The agent streams provider text and task events to the page with Server-Sent Events. Chat Completions and Responses tool calls use the same bounded workspace tools. Interrupted tasks can be resumed from the beginning only when no tool started before the interruption; resume defaults to read-only. Task history and events are stored in `.luna/history.sqlite3`.

Commands outside the narrow test/build/Git-inspection allowlist pause for approval. Approved PowerShell commands still run as the current Windows user and are not OS-sandboxed. The application does not authenticate other local processes running as the same user.

## Skills

Skills are local folders with `skill.json`, `SKILL.md`, and optional supporting files. `examples/skills/review-notes` is a starter. Install one from the Settings page using its full folder path, inspect it, then enable it. Luna treats skill text as untrusted guidance, limits the total instructions sent with a task, and never executes skill scripts. Skills cannot add tools or bypass approval policy.

## MCP servers

Luna supports MCP Streamable HTTP servers. Add a server in Settings, connect to inspect its tools, then explicitly enable it. Each tool call pauses for approval. Luna does not launch server commands or send the provider key to an MCP server. Remote servers require HTTPS and public DNS addresses; plain HTTP is restricted to an explicitly configured loopback server.

The client negotiates modern MCP `2026-07-28` and falls back to the `2025-11-25` handshake for older servers. It supports tool discovery and calls, JSON/SSE responses, and `x-mcp-header` parameters. It does not support stdio process launch, OAuth credential management, long-lived subscriptions, or multi-round-trip input requests. [MCP Streamable HTTP specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http).

## Browser automation

Install the optional runtime and Chromium from PowerShell:

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

Start a browser session in Settings. It uses a fresh, headless Chromium context without saved cookies or downloads. Public HTTPS sites are allowed; localhost is blocked unless enabled for that browser session. Browser page text is untrusted and bounded. Clicks and form fills from an agent require approval, and credential-like text cannot be entered. Screenshots stay under ignored `.luna/browser/` storage.

## Windows desktop automation

Install the optional package with `python -m pip install -r requirements-desktop.txt`, then enable desktop access in Settings. Luna only inspects the window you select. Clicks and typing require approval; credential-like text is blocked. It does not start applications, elevate privileges, or bypass UAC. UI Automation support varies by application; inaccessible controls fail clearly. Screenshots stay under ignored `.luna/desktop/` storage.

## Tests and limits

Run the automated suite with:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

For desktop dependencies and Qt tests, install `requirements-desktop.txt` into
the same virtual environment before running the suite. Tests use Qt's offscreen
platform where needed; they do not establish that native Windows dialogs or
desktop automation work on a specific machine.

With the browser extra and Chromium installed, the same suite runs a real Playwright interaction against a local fixture. Without it, that test is skipped. Tests do not use provider credentials. The application does not support resume after any tool has started, task-history deletion, custom provider headers, browser file uploads/downloads, or full MCP extensions. Nested empty folders inside non-empty trees may remain after restoring individual file operations. There is no end-to-end browser UI test.

## Build a Windows desktop package

From PowerShell in the repository root, run:

```powershell
.\run-desktop.ps1
.\build-desktop.ps1
```

The PyInstaller build writes `dist\LunaDesk\LunaDesk.exe` and the onedir
`LunaBackend.exe` child and its runtime files beside it. The build script installs packaging and Playwright
Python dependencies into `.venv`; it does not bundle `.env`, `.luna` data,
task history, or projects. Browser automation also needs Chromium installed
separately for the same Python environment with
`.\.venv\Scripts\python.exe -m playwright install chromium`. Packaged user
settings and keys are stored under `%LOCALAPPDATA%\LunaDesk`. The package is
unsigned; no installer or custom application icon is provided.
