# Luna Desk project status

## Final completion pass checklist

- [x] Confirm `main`, `origin/main`, the three reported desktop commits, and a
  clean starting tree; baseline `.venv\Scripts\python.exe -m pytest -q`:
  **66 passed**.
- [x] Add native interactive terminal subset with explicit process lifecycle,
  incremental bounded output, and documented ConPTY limitations.
- [x] Add recursive content search and cancellation in the active project,
  including ignored, hidden, binary, large, and protected-file handling.
- [x] Add task recovery/resume controls and changed-file indicators to the
  native client.
- [x] Audit and complete native browser/desktop automation inspection and
  action controls; verify skills/MCP schemas and task skill history.
- [x] Review shared backend permissions, recovery, provider pacing, and
  browser regressions; add focused tests for any gaps found.
- [x] Create reproducible Windows packaging configuration, update user docs,
  run the feasible verification suite, then commit and push verified changes.

Items are checked only after implementation and evidence are recorded below.

## Implemented

- First-run provider setup with prefilled Apinex settings, masked key entry,
  connection testing, provider editing, default selection, and key removal.
- Local provider configuration in an ignored `.env` file and non-secret
  provider metadata under `.luna`.
- A provider-scoped rolling request limiter shared by connection tests, agent
  calls, follow-up calls, and streaming requests. Limit state persists in
  `.luna/request_pacing.json` and includes only hashed quota scopes.
- Bounded retries for rate limits, connection failures, and server errors;
  cancellation-aware waits; overall task deadlines; request statistics in
  task history; and editable per-provider pacing settings.
- Chat Completions and Responses request support, tool-call normalization,
  streaming events, task cancellation, safe task resume, and persisted history.
- Project selection and inspection; bounded file search and reads; guarded
  writes, moves, deletes, diffs, and conflict-checked restore. File writes
  preserve supplied UTF-8 text exactly, including newlines and whitespace;
  writes use unique exclusive temporary files.
- Explicit approval for risky file, shell, browser, desktop, and MCP actions.
- Optional skills, Playwright browser automation, Windows UI Automation, and
  HTTP MCP server integrations. Optional runtimes are documented separately.
- A local-only FastAPI service, launcher, README, and backend/frontend tests.
- A PySide6 Qt Widgets desktop client using the same FastAPI backend, provider
  settings, agent loop, permissions, and task history as the browser edition.
- Native folder selection, project file tree and read-only text preview,
  recent-project restoration, permission-mode selection, provider setup and
  connection testing, streamed task events, cancellation, task history, and
  diff review/restore controls.
- A `run-desktop.ps1` source launcher. It starts an owned loopback backend or
  connects to the existing local service without opening a browser.
- Browser API errors convert structured validation details to readable text.
- Native action approval dialogs and a command-output panel display actual,
  redacted output and exit codes emitted by the shared command runner.
- Native provider pacing/retry settings, masked key replacement/removal,
  skill management, MCP server management, and automation state controls.
- Persistent native PowerShell ConPTY child with line input, combined
  incremental stdout/stderr, working-directory selection, a 1 MB output cap,
  four-hour timeout, exit state, and child-only termination. Full-screen TUI
  key controls and dynamic terminal resizing are not supported.
- Streamed, cancellable native content search scoped to the active workspace;
  hidden/sensitive/binary/large files and ignored paths are skipped, with file,
  time, and result bounds.
- Native task recovery details, recorded event and skill history, guarded
  resume for interrupted tasks with no tool event, and create-new-from-context.
- Latest-task file change panel and explorer labels for added, modified,
  deleted, moved, and restored paths.
- Native browser navigation, page text, screenshot, explicitly confirmed click
  and fill, session cancellation/close, and loopback opt-in controls. Native
  desktop window selection, control inspection, screenshot, and confirmed
  click/type controls. MCP tool schemas and errors can be inspected.
- Reproducible PyInstaller specs and a PowerShell packaging script. Frozen
  settings and credentials use `%LOCALAPPDATA%\\LunaDesk` instead of the
  application bundle directory.

## Known limits

- The browser folder chooser remains a path field because browsers cannot
  browse arbitrary host folders. The desktop app uses a native folder picker.
- The native terminal uses Windows ConPTY for line-oriented interaction. Full-
  screen, raw-key, and resize-dependent terminal programs are unsupported.
  Manual terminal commands are run as the current user and are not OS-sandboxed.
- Content search skips hidden paths and uses a bounded `.gitignore` matcher;
  it is not a complete Git status implementation. Explorer labels show only
  the latest reviewed task; Git-external edits do not receive task markers.
- Task resume replays the original request from the beginning and is available
  only when interrupted before any recorded tool/command event. It cannot
  resume after a tool execution, inspect external side effects, or reconstruct
  a provider's exact internal checkpoint.
- Trusted Full Access is not supported by the shared backend. The desktop UI
  exposes only the enforced read-only and workspace modes.
- The file tree is rooted at the selected project and its built-in preview is
  read-only. It is not a separate filesystem permission boundary; model
  operations remain subject to backend workspace checks.
- PySide6 Qt Widgets tests run offscreen and do not verify Windows native
  dialogs or native desktop interactions.
- Browser/desktop native controls are covered with mocks and share the APIs
  used by the browser edition; this pass did not manually approve a real click
  or type action in an external target application.
- The PyInstaller output is an unsigned folder package, not an installer.
  Chromium remains an external runtime, and the package has no custom icon.
- Windows command execution is not an OS-level sandbox. Commands run as the
  current user, so approvals do not restrict all possible filesystem access.
- Desktop automation was exercised with a real UIA click. Desktop text entry
  has mock coverage but has not been verified against a real application.
- Browser automation has an actual local Playwright fixture test. Public-site
  interaction was not exercised as part of this pass.
- MCP was tested against a controlled local HTTP fixture. OAuth, stdio
  transports, and long-lived subscriptions are not implemented.
- Provider streaming and Responses tool cycles are covered by mocked tests;
  they were not verified live with Apinex during this pass.
- APInex's public Chat Completions documentation does not define “round” or
  publish a model-specific request cap. Five requests per 60 seconds with a
  12-second minimum spacing is a configurable conservative default for
  Apinex `free/*`, not a verified provider limit.
- The reviewed [APInex Chat Completions docs](https://apinex.bond/developers/models/chat)
  document the endpoint and feature support, but publish no rate-window or
  “round” definition for the free Luna model.
- The limiter coordinates tasks in one Luna process and persists across
  restarts. Separate Luna processes do not share an atomic request queue.
- Daily token allowance is separate from HTTP request pacing and is not
  enforced locally; check provider dashboard usage.
- Restoring moves of directories recreates file entries, but empty nested
  directories are not represented in the file change records.
- A prior live agent test received extra punctuation from the model. The file
  writer now preserves tool input exactly, but cannot guarantee that a model
  interprets a literal-content request correctly. Read back and hash the file.

## Live provider check

The configured Apinex Chat Completions connection test succeeded for
`free/gpt-6-luna`, returned that model name, and reported 313 total tokens.
The key is stored in the local ignored `.env` file and is not included here.
It was exposed in chat during setup; rotate it after verification.

## Verification

- `.venv\Scripts\python.exe -m pytest -q`: **66 passed** at the desktop
  integration check. Qt tests used the offscreen platform. One
  Starlette AnyIO deprecation warning remains.
- Final completion pass adds search scope/ignore/result-cap/disconnect tests and
  Qt tests for a real PowerShell Read-Host exchange, output bounds, protected previews, and
  explorer change labels. Latest run: `.venv\\Scripts\\python.exe -m pytest -q`
  — **73 passed**.
- Frontend JavaScript passed `node --check`; `git diff --check` passed. Tests
  cover exact UTF-8 bytes, symlink boundaries, and safe temporary-file creation.
- Mocked agent end-to-end tests use disposable project folders. A fresh local
  startup smoke test returned HTTP 200 for `/`, `/api/setup-status`, and
  `/api/providers` with isolated temporary data.
- `run.ps1 -NoBrowser` started successfully. The page, setup status, and
  providers endpoints returned HTTP 200. Provider responses include the new
  pacing settings and do not return API keys.
- The real Playwright browser fixture passed in the virtual environment.
- A harmless real UI Automation click passed. Desktop typing was mock-tested.
- An earlier live Apinex connection test passed. Its agent test reached tool
  execution but failed exact output because the model added punctuation; a
  correction attempt hit a provider rate limit. Streaming and Responses remain
  mock-verified.
- No live provider request was made during this hardening pass. Provider
  pacing and retries remain verified with deterministic mocked tests.
- `.\build-desktop.ps1` produced the onedir package. The packaged app returned
  HTTP 200 from `/api/setup-status`, `/api/providers`, and
  `/api/browser/status` using isolated temporary data. Closing its smoke-test
  window exited both packaged executables and released port 8765.
- PowerShell ConPTY interaction passed a real `Read-Host` input/output round
  trip and preserved shell exit code 7. Native dialogs, external desktop
  automation, and packaged ConPTY interaction were not manually exercised.

Approved shell commands run with the current Windows user's permissions and
are not OS-sandboxed. The project currently rejects symlink and junction paths
for its tool operations, but filesystem races between validation and access
cannot be ruled out completely.
