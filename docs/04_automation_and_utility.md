---
log:
2026-08-15: Hardened the compatibility automation boundary after credential-bearing request metadata, response bodies, Drive authorization URLs, and stdin values were found in local diagnostics. HTTP logs are now metadata-only and query-free behind a rotating private sink with defense-in-depth redaction; Drive propagation uses a bounded, cancellable coordinator with strict response and redirect validation; interactive execution has a real 600-second wall-clock deadline plus best-effort kernel interrupt; generated Drive/install source uses Python literals; and legacy history can be scrubbed idempotently without racing active writers. Canary tests assert secrets are absent from every persisted and rendered sink.
2026-08-15: Fixed the compatibility runtime's Jupyter stdin handling. Current `jupyter-kernel-client` passes a full `input_request` message to hooks, invokes them synchronously, and ignores return values, so the former prompt-string wrapper collected OAuth codes locally without sending them to the VM. The wrapper now mirrors the installed hook contract, explicitly sends `input_reply`, avoids replying to stale requests, uses `getpass` for password prompts, and records only `<redacted>` in history. Added regression coverage for normal, password, stale-request, EOF, cancellation, and invalid/foreign-message paths.
2026-07-18: Routed `better-colab install` through durable execution while retaining `colab install` as the direct compatibility path. Package arguments are emitted as safe Python literals; local requirements bytes are embedded in the protected queued source, recreated in a deterministic remote temporary file, and removed after pip/uv finishes. JSON mode suppresses installer output and maps proven execution failure to `INSTALL_FAILED`/exit 1. Also fixed `whoami` to resolve the callback-configured auth state lazily.
2026-06-11: Replaced the `oauth2` provider's `run_local_server()` (localhost redirect) with a remote copy-paste flow (`_run_remote_flow` in `auth.py`). The CLI now prints an authorization URL built with `redirect_uri=https://sdk.cloud.google.com/applicationdefaultauthcode.html` and `token_usage=remote`, then reads the pasted authorization code via `input()` and exchanges it with `flow.fetch_token(code=...)`. This is the same flow `gcloud auth application-default login` uses and works identically in local and remote/headless/container environments, removing the heuristic of whether to auto-open a browser. Confirmed server-side acceptance with a live GET-only check against the bundled cloud-SDK client (`764086051850-...`); the OOB redirect and a non-bundled client id were both verified to be rejected (`OOB flow has been blocked` / `redirect_uri_mismatch`). Unit tests in `tests/test_auth.py` assert no localhost server is started, the redirect URI + `token_usage=remote` are set, and the pasted code is exchanged.
2026-06-01: Enabled `colab update --install` self-update on macOS in addition to Linux. Refactored platform check logic to keep the implementation DRY and updated both tests and documentation. Also, on these platforms, an additional message is shown recommending `colab update --install` to upgrade in place, positioned above the standard `pip`/`uv` installation command.
2026-05-29: Added default OAuth2 client config (`oauth_config.json`) as a bundled package resource and restored fallback loading logic in `get_credentials()`. The CLI now falls back to using these default credentials when no explicit local config is found. Added `integration/repro_bundled_oauth` integration test.
2026-05-27: Refactored `colab README` and `colab AGENT` to bundle `README.md` and `AGENTS.md` via Hatchling's `force-include` and read them using `importlib.resources` instead of `importlib.metadata`. `colab AGENT` now correctly prints `AGENTS.md`.
2026-05-27: Extended `colab update --install` to detect if the CLI was installed via `uv tool install` (by checking if `sys.executable` contains `/uv/`) and if so, use `uv tool install -U google-colab-cli` to upgrade.
2026-05-27: Updated auto-update upgrade hint to recommend `pip install --upgrade google-colab-cli` instead of `colab`, aligning with the PyPI package name.
2026-05-27: `colab url` now emits BOTH the `?dbu=<urlencoded path>` query parameter (existing) AND a new `#datalabBackendUrl=<full URL>` hash fragment (new). Format: `https://<host>/notebooks/empty.ipynb?dbu=%2Ftun%2Fm%2F<endpoint>#datalabBackendUrl=<host>/tun/m/<endpoint>`. Why both: some Colab frontend code paths consult the hash fragment first and ignore `dbu` entirely, so the previously-emitted query-only form failed silently for those users (the frontend fell through to allocating a fresh VM via `/tun/m/assign`). The fragment value is a FULL URL with scheme + host (NOT just the path) and is emitted RAW (no URL encoding) because browsers don't decode the fragment before passing `location.hash` to page JS — Colab's parser calls `new URL(rawString)` directly. The fragment host always matches `--host` so Colab's same-origin enforcement on embedded backend URLs doesn't block the connection, and sandbox/dev users (`--host https://colab.sandbox.google.com`) get a sandbox fragment automatically. Three new test cases in `tests/test_url.py` cover the raw-encoding requirement (`%3A`/`%2F` must NOT appear in the fragment), the both-signals-present invariant, and `--open` propagating the fragment to `webbrowser.open()`. Integration-verified live against synthetic session state with three host shapes (default, sandbox, trailing-slash); all produced correctly-shaped URLs with no `//` artifacts.
2026-05-07: Added a developer-only `colab whoami` subcommand (hidden from `colab --help`). Mints an access token via the same `auth.get_credentials(...)` path the rest of the CLI uses (honoring the global `--auth=...` flag), refreshes the credentials, then queries `https://oauth2.googleapis.com/tokeninfo` to print the email, scopes, audience, and expiry of whatever the CLI is about to send. Built specifically to short-circuit the "why is my call to colab.pa.googleapis.com 403-ing" debugging loop — the answer is almost always "missing scope" or "wrong identity", both of which `whoami` makes immediately visible. Hidden via `app.command(hidden=True)`; reachable via `colab whoami` or `colab whoami --help`. Suppressed from the daily-update banner check (added to `_AUTO_UPDATE_SUPPRESSED` in `cli.py`) so the banner doesn't obscure the auth output.
2026-05-11: Removed the local-file update source (`update_file_path` setting and `_fetch_local` helper); `colab update` now consults PyPI only. Switched the default `update_url` to the canonical PyPI JSON API (`https://pypi.org/pypi/google-colab-cli/json`), which already exposes the `info.version` schema the auto-update subsystem expects. Re-added `colab update --install` as a public self-install path that runs `pip install -U google-colab-cli` against the current `sys.executable`; Linux-only (other platforms exit non-zero with an explanatory message), and a silent no-op when the cached `latest_version` is already at or below the current install.
2026-05-12: Added an optional `timeout=` parameter to `ColabRuntime.execute_code` that flows through to both the `execute()` and `execute_interactive()` branches. `colab auth` and `colab drivemount` now pass `timeout=600` (10 min) via a shared `INTERACTIVE_AUTOMATION_TIMEOUT_SEC` constant in `commands/automation.py`. Background: `jupyter_kernel_client` defaults to a 10s timeout that is consumed while the kernel waits on `input_request`. With the drivefs hook prompting the user to OAuth in their browser, users hit `TimeoutError` and saw "drivemount failed" even when the mount later succeeded server-side. Superseded 2026-08-15: `ColabRuntime` now wraps the complete synchronous call in a real wall-clock deadline rather than assuming upstream message activity provides a total bound.
---

# Design: Automation and Utility (`auth`, `install`, `log`, `pay`, `version`, `update`, `whoami`)

## Overview

These subcommands are implemented by executing Python code on the Colab VM,
managing local state, or inspecting the environment.

## Authentication Strategies (CLI Backend)

The CLI supports two authentication strategies for talking to the Colab
backend, selected via the global `--auth=<provider>` flag:

1.  **`oauth2`** (default): Public `InstalledAppFlow` via
    `google-auth-oauthlib`, but run with a **remote copy-paste flow** rather
    than a localhost server. The CLI prints an authorization URL (with
    `token_usage=remote`) using the registered HTTPS landing page
    `https://sdk.cloud.google.com/applicationdefaultauthcode.html`; the user
    signs in, copies the code Google displays, and pastes it back at the
    prompt. The refresh token is cached at `~/.config/colab-cli/token.json`.
    This is the same mechanism `gcloud auth application-default login` uses,
    and it behaves identically on local, remote, headless, and container
    hosts (no auto-opened browser, no bound port). We deliberately do **not**
    use `run_local_server()` (environment-dependent) or the out-of-band (OOB)
    redirect `urn:ietf:wg:oauth:2.0:oob` (blocked by Google in 2022 — see
    `_run_remote_flow` / `REMOTE_REDIRECT_URI` in `auth.py`). The
    `sdk.cloud.google.com` redirect is registered to the cloud-SDK OAuth
    client (`764086051850-...`), which is also the client shipped in the
    bundled `oauth_config.json`; reusing it with any other client id yields
    `redirect_uri_mismatch`. If no local config is provided via
    `-c/--client-oauth-config` or found at `~/.colab-cli-oauth-config.json`,
    it falls back to that bundled `oauth_config.json`. The cached token and its
    stable lock file are mode `0600`, leaf symlinks and other unsafe file types
    fail closed, and refresh/new-token updates use same-directory atomic
    replacement while holding the write lock.
2.  **`adc`**: Application Default Credentials via `google.auth.default()`.
    Honors the standard ADC discovery chain
    (`GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth application-default
    login`, GCE/GKE metadata server). Useful when running the CLI from
    environments that already have ambient Google credentials.

The choices are encoded as the `AuthProvider` string-enum in `auth.py`. The
`get_credentials(config_path, provider)` entry point dispatches on this enum,
allowing the core `Client` to remain authentication-agnostic — it only sees a
`requests.AuthorizedSession`.

### Required Scopes

The CLI talks to the Colab session backend at `colab.research.google.com`
for assignment, unassignment, the contents API, **and keep-alive** (the TFE
tunnel ping — see `01_session_management.md`). The `userinfo.email` scope is
sufficient for this host.

> Historical note: keep-alive previously used the `RuntimeService`
> (`KeepAliveAssignment`) at `colab.pa.googleapis.com`, which required the
> `https://www.googleapis.com/auth/colaboratory` scope **and** the caller to
> be a `serviceusage` consumer of Colab's internal project `1014160490159`.
> The latter is impossible for ordinary user accounts, which made keep-alive
> fail with HTTP 403 `USER_PROJECT_DENIED` for all external users (issue #14).
> Keep-alive no longer touches `colab.pa.googleapis.com`.

How each provider supplies the scope:

-   **`oauth2`**: `PUBLIC_SCOPES` already includes `colaboratory`, so the
    InstalledAppFlow consent screen lists it. Existing cached tokens at
    `~/.config/colab-cli/token.json` that were minted before this change must
    be deleted to trigger a fresh consent flow.
-   **`adc`**: `google.auth.default(scopes=PUBLIC_SCOPES)` is called, and for
    credential subclasses that support `with_scopes` (service accounts,
    GCE/GKE metadata, impersonated) we re-apply via `creds.with_scopes(...)`.
    User credentials from `gcloud auth application-default login` ignore the
    `scopes=` kwarg AND raise `NotImplementedError` on `with_scopes`; those
    users must explicitly re-authenticate:

    ```
    gcloud auth application-default login \
        --scopes=openid,\
    https://www.googleapis.com/auth/cloud-platform,\
    https://www.googleapis.com/auth/userinfo.email,\
    https://www.googleapis.com/auth/colaboratory
    ```

    `userinfo.email` is required for the session backend at
    `colab.research.google.com` (otherwise assign/unassign/sessions/keep-alive
    return HTTP 401); `colaboratory` is retained for forward compatibility and
    other Colab features; `openid` and `cloud-platform` are mandated by
    `gcloud` itself (`gcloud auth application-default login` rejects scope
    lists that omit `cloud-platform` with `Invalid value for [--scopes]`).

`colab new` performs a one-shot keep-alive pre-flight after `assign`
succeeds so missing-scope failures surface immediately (with per-provider
remediation guidance) rather than silently after ~1 minute via the daemon.

## Approach

### 1. Authentication (`colab auth`)

-   **Action**: Execute code on the VM to trigger user-interactive
    authentication using the classic Gcloud fallback.
-   **Code**: `python import os os.environ['USE_AUTH_EPHEM'] = '0' from
    google.colab import auth auth.authenticate_user()`
-   **Handling**: Setting `USE_AUTH_EPHEM` to `'0'` forces the kernel to print a
    standard `gcloud` verification URL and trigger an `input_request` message on
    the `stdin` channel. The CLI's synchronous hook receives the full Jupyter message,
    prompts locally, and explicitly sends the resulting `input_reply` through
    the kernel client. Ordinary hook return values are not replies and are
    ignored by current `jupyter_client`. Before sending, the hook checks for a
    newer stdin or shell message so a slow user cannot answer an obsolete
    prompt. Verification codes and other interactive replies are always logged
    as `<redacted>` rather than plaintext.

### 2. Package Installation (`colab install`)

-   **Action**: Execute `pip` on the VM.
-   **Code**: `python import sys, subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "..."])`
-   **Requirements File**: Upload `requirements.txt` if provided with `-r` and
    then run `pip install -r`.

On the primary surface, `better-colab install` resolves an existing durable
session and submits the installer as a normal controller execution. It never
uses the legacy Contents API: requirement bytes are base64-embedded in the
mode-`0600` queued source, decoded to a digest-named `/tmp` file on the VM,
and removed in a `finally` block. `uv pip install --system` is attempted first,
then ordinary `pip` is the fallback. Text mode renders bounded output pages.
JSON mode emits one schema-v1 result and maps a proven kernel error to
`INSTALL_FAILED` with exit 1.

### 3. Drive Mounting (`colab drivemount`)

-   **Action**: Execute `drive.mount()` and transparently proxy Colab's
    proprietary credential propagation flow.
-   **Code**: `python from google.colab import drive
    drive.mount('/content/drive')`
-   **Handling**: Because `drivefs` enforces the ephemeral side-channel
    propagation (`colab_request` over websocket), the CLI intercepts these
    messages using `ColabRuntime.colab_request_hook`. The websocket callback
    only validates and queues Drive requests; a daemon coordinator performs
    the bounded HTTP sequence against `/tun/m/credentials-propagation/` so the
    callback never blocks on network or terminal input. It validates 2xx JSON
    responses, a 1 MiB parser limit, per-request timeouts, exact Drive auth
    type, correlated message IDs, and an HTTPS `accounts.google.com/o/oauth2/`
    consent origin. Duplicate IDs are de-duplicated, queue-drain races are
    serialized, and every accepted request receives exactly one correlated
    `colab_reply`, including sanitized failure and cancellation paths.
-   **Timeout**: `INTERACTIVE_AUTOMATION_TIMEOUT_SEC` is 600 seconds for both
    `colab auth` and `colab drivemount`. `ColabRuntime` enforces it as a real
    POSIX wall-clock deadline around the complete synchronous call rather than
    relying on upstream activity-based socket timeouts. Timeout and Ctrl-C
    trigger a bounded best-effort remote-kernel interrupt before the original
    exception is re-raised; finite deadlines fail explicitly off the main
    thread or on platforms without POSIX interval timers.

### 4. Logging and Notebook Capture (`colab log`)

-   **Action**: Capture the session's command history and outputs.
-   **Storage**: Maintain a local JSON-L file of all major operations,
    executions, and stdin interactions in
    `~/.config/colab-cli/history/<session_name>.jsonl`.
-   **Sensitive input**: Record that an input reply occurred, but persist only
    `<redacted>`. If execution advances while the user is responding, record an
    `input_reply_skipped` event and do not send the stale value.
-   **HTTP diagnostics**: Persist only method, query-free endpoint, status,
    reason, and response byte count. Request parameters, headers, response
    headers, bodies, authorization URLs, cookies, and stdin values are not log
    inputs. The rotating 5 MiB file sink keeps three mode-`0600` backups and
    applies credential-pattern redaction as defense in depth.
-   **Legacy cleanup**: `scrub_legacy_history()` removes known historical
    response-body fields and redacts old Drive URI/stdin fields using atomic
    replacement under the same per-session lock as normal appends. Malformed
    files abort unchanged rather than being partially rewritten.
-   **Viewing**: `colab log list` and `colab log show <session>`.
-   **Conversion (Planned)**: Future expansion to convert history logs to
    `.ipynb` or `.html`.

### 5. Subscription Management (`colab pay`)

-   **Action**: Open the Colab signup page in the user's browser.
-   **Implementation**: Uses
    `webbrowser.open("https://colab.research.google.com/signup")`.

### 6. Version Information (`colab version`)

-   **Action**: Show the current version of the Colab CLI.
-   **Implementation**:
    -   Attempts to retrieve the version using
        `importlib.metadata.version("colab")`.
    -   If not installed (e.g., running from source), it falls back to the short
        Git commit hash using `git rev-parse --short HEAD`.
    -   Dynamic versioning is supported in the build system via `hatch-vcs`.

### 7. Auto-Update (`colab update`)

-   **Action**: Check if a new version of the Colab CLI is available.
-   **Auto-check**: The CLI automatically checks for updates once every 24 hours
    during the execution of any command. Independently, the cached
    `latest_version` (see below) is consulted on **every** invocation so the
    upgrade banner remains visible between fetches without requiring a network
    round-trip.
-   **Suppressed subcommands**: To keep machine-parseable output clean, the
    daily fetch and the cached banner are suppressed for `update` (which
    runs its own check), `version`, `log`, `pay`, `url`, `help`, and
    `whoami`. The list lives as `_AUTO_UPDATE_SUPPRESSED` in the global
    Typer callback in `cli.py`.
-   **Manual-check**: `colab update` forces a check and prints the status.
-   **Implementation**:
    -   Fetches a PyPI-style JSON document from a configurable `update_url`
        (default: `https://pypi.org/pypi/google-colab-cli/json`) and reads
        `info.version`.
    -   Compares the fetched version with the current CLI version using
        PEP 440 / semantic versioning, falling back to string equality when a
        version is unparseable.
    -   Persists the following fields in `~/.config/colab-cli/settings.json`:
        -   `update_url`: source configuration.
        -   `last_check`: timestamp of the last fetch (drives the daily
            throttle).
        -   `enable_update_check`: master switch for both the daily fetch and
            the cached banner.
        -   `latest_version`: highest version observed during the most
            recent successful check. Updated whenever a strictly-newer
            version is observed (never downgraded), and preserved verbatim
            across failed checks so transient network issues do not erase
            the cache.
-   **Notification**: If a new version is found, a non-intrusive message is
    printed to the console with a `Run 'pip install --upgrade google-colab-cli' to
    update.` hint. On Linux and macOS platforms where `--install` self-update is supported,
    an additional hint `You can run 'colab update --install' to upgrade in place.`
    is displayed above the pip/uv install command. The cached banner shown between
    fetches uses the generic `Run 'colab update' to update.` hint.
-   **Self-install (`--install`)**: An opt-in `--install` flag (default
    `False`) makes `colab update` upgrade the CLI in place (**Linux and macOS**).
    It detects how the CLI was installed:
    - If `sys.executable` contains `/uv/tools` (indicating it was installed via
      `uv tool install`), it runs `uv tool install -U google-colab-cli`.
    - Otherwise, runs `pip install -U google-colab-cli` using `sys.executable`
      to ensure the upgrade lands in the same interpreter.
    On other platforms, the command exits non-zero with an explanatory
    message. When the cached `latest_version` is already at or below the
    current install, the flag is a silent no-op so it is safe to wire into
    automation. If the upgrade command exits non-zero, `colab update --install`
    propagates the same exit code.

### 8. Identity Inspection (`colab whoami`) [developer-only]

-   **Action**: Resolve the active credentials, mint an access token, and
    print the email, audience, scopes, and expiry of that token.
-   **Visibility**: Registered with `hidden=True` so it does not appear in
    `colab --help`. Discoverable via source code, `colab whoami --help`, or
    word-of-mouth. The intent is to keep the public surface focused on
    end-user commands while still giving developers a one-shot debugging
    aid.
-   **Implementation**:
    -   Calls `auth.get_credentials(state.client_oauth_config,
        provider=state.auth_provider)` — the exact same code path the
        `Client` uses — so the token reflects what the rest of the CLI
        would actually send.
    -   Always calls `creds.refresh(Request())` before reading
        `creds.token`. Service-account, GCE/GKE-metadata, and some
        impersonated credentials lazy-mint the token; without an explicit
        refresh `creds.token` is `None` even for valid credentials.
    -   Hits `https://oauth2.googleapis.com/tokeninfo?access_token=<token>`
        via stdlib `urllib.request` rather than the already-authorized
        `requests.AuthorizedSession`. The tokeninfo endpoint accepts the
        token as a query parameter and does NOT want a `Bearer` header
        alongside it.
    -   Renders `expires_in` (seconds) as minutes for readability.
    -   On HTTP 4xx from tokeninfo (typical for revoked/expired tokens),
        the JSON error body is surfaced verbatim rather than being
        swallowed; the developer needs to see *why* the token was
        rejected.
-   **Output shape**:
    ```
    Auth provider: adc
    Email:         user@example.com
    Audience:      764086051850-...apps.googleusercontent.com
    Expires in:    47m
    Scopes:
      - email
      - https://www.googleapis.com/auth/cloud-platform
      - https://www.googleapis.com/auth/colaboratory
      - https://www.googleapis.com/auth/userinfo.email
      - openid
    ```

### 9. README and AGENT (`colab README`, `colab AGENT`)

-   **Action**: Print the bundled `README.md` or `AGENTS.md` file.
-   **Implementation**:
    -   Uses `importlib.resources.files("colab_cli").joinpath(...)` to read the
        bundled `README.md` (for `colab README`) or `AGENTS.md` (for `colab AGENT`)
        from the package resources.
    -   The files are bundled into the package via Hatchling's `force-include`
        configuration in `pyproject.toml`.
    -   If reading from resources fails (e.g. during development when not
        installed), it falls back to reading the files from the project root.
    -   Prints the content to stdout.

## Implementation Details

-   **Code Injection**: Use a standard `run_code(session, code)` helper via
    `ColabRuntime`.
-   **History Management**: Use `HistoryLogger` class to append structured
    events to session-specific `.jsonl` files.
-   **Interactive Prompts**: Instrumented `stdin_hook` and `colab_request_hook`
    to record redacted interaction metadata and proprietary backend requests.
    The stdin hook follows the message-shaped `jupyter_client` contract and
    explicitly transmits replies.
-   **Generated Source**: User-controlled Drive paths and package arguments are
    emitted with `repr()` as single Python literals. The installer fallback
    catches only process/OS launch errors; it does not turn arbitrary program
    failures into a second execution path.

## Testing Strategy

TDD is mandatory for all automation features.

### 1. Mock Kernel Injection

-   **Test Case**: Verify `colab auth` correctly injects `from google.colab
    import auth; auth.authenticate_user()`.
-   **Test Case**: Verify `colab install` correctly injects `pip install` or `uv
    install` commands to the remote VM kernel.
-   **Test Case**: Verify `colab drivemount` correctly injects `drive.mount()`
    commands and registers the `colab_request_hook` to intercept credential
    propagation events.

### 2. History Capture

-   **Test Case**: Verify all code sent via `exec` is correctly appended to the
    JSON-L history file for that session.
-   **Test Case**: Verify stdin replies are explicitly sent to the kernel while
    history contains only `<redacted>`.
-   **Test Case**: Verify password prompts use `getpass`, and obsolete prompt
    replies are skipped when a newer stdin or shell message is already ready.
-   **Test Case**: Verify EOF, Ctrl-C, invalid/foreign messages, hard wall-clock
    timeout, and off-main-thread deadline rejection without persisting values.
-   **Test Case**: Exercise Drive success, consent, malformed/oversized/non-2xx
    responses, malicious redirects, duplicate IDs, queue-drain concurrency,
    network timeout, cancellation, and reply failure; every accepted request
    must unblock exactly once and every error must remain sanitized.
-   **Test Case**: Seed unique canaries in HTTP query parameters, headers,
    cookies, response bodies, authorization URLs, prompts, and replies, then
    assert each canary is absent from log/history files and raised exception
    text. Rendered output may contain only the intentional user-facing prompt
    and a validated Google consent URL; submitted replies, transport
    credentials, cookies, and response bodies must remain absent.
-   **Test Case**: Verify `colab log` correctly generates an `.ipynb` from a
    populated history file.

### 3. `whoami` Identity Resolution

-   **Test Case**: Mock the credentials + `urllib.request.urlopen` to return a
    fake tokeninfo payload; verify the printed output contains the email, the
    active auth provider name, the scopes (one per line), and a human-readable
    expires-in (minutes, not raw seconds).
-   **Test Case**: When `urlopen` raises `HTTPError(400)` (revoked/expired
    token), `whoami` exits non-zero with a message identifying the failure
    rather than emitting an unhandled traceback.
-   **Test Case**: `colab --help` does NOT mention `whoami` (regression
    against accidental un-hiding) but `colab whoami --help` still shows the
    command's own help text.
-   **Test Case**: `creds.refresh()` is called before `creds.token` is read
    (regression against silently-`None` tokens for service-account /
    GCE-metadata creds).

### 4. `README` and `AGENT` Commands

-   **Test Case**: Verify `colab README` prints the expected content when package metadata is available.
-   **Test Case**: Verify `colab AGENT` prints the same content.
-   **Test Case**: Verify fallback to local `README.md` file when metadata is not available.
-   **Test Case**: Verify error exit when both metadata and local file are unavailable.
