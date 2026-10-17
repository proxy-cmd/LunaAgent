# Luna Desk project status

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

## Known limits

- The folder chooser is a path field, not a native file picker.
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

- `.venv\Scripts\python.exe -m pytest -q`: **57 passed**. Starlette emitted
  one AnyIO `BlockingPortal` deprecation warning.
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

Approved shell commands run with the current Windows user's permissions and
are not OS-sandboxed. The project currently rejects symlink and junction paths
for its tool operations, but filesystem races between validation and access
cannot be ruled out completely.
