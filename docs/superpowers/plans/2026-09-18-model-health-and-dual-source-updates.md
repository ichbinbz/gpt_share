# Model Health and Dual-Source Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release CWS Codex v0.1.7 with proactive per-model account health checks, health-aware scheduling and administration, plus authenticated Broker-first/GitHub-fallback client updates.

**Architecture:** Add a focused model-health module that owns filtering, classification, persistence, and availability decisions; the existing Broker owns OAuth-backed model discovery/probes, scheduling, API exposure, and the background lifecycle. Add an authenticated release-manifest/download boundary to the Broker, then update Windows and Linux clients to consume it before falling back to GitHub.

**Tech Stack:** Python 3.11+, FastAPI, httpx, pytest, PowerShell 5.1, NSIS, POSIX shell, systemd.

**Completion:** Implemented and verified on 2026-09-18. See [release handoff](2026-09-18-model-health-and-dual-source-updates-handoff.md) for final checks and the live 024 recovery exception.

**Spec:** `docs/superpowers/specs/2026-09-18-model-health-and-dual-source-updates-design.md`

## Global Constraints

- Broker probe interval is `900` seconds and capacity cooldown is `1800` seconds by default.
- Probe concurrency defaults to `2`; probe timeout defaults to `45` seconds.
- At least one available model keeps an account schedulable; every supported model blocked makes it unschedulable.
- A transient network/probe error must not overwrite a recent known-good result.
- Broker remains bound to `127.0.0.1:8765`; no public listener is introduced.
- Release metadata and downloads require an existing valid device Bearer token.
- Every downloaded executable/archive is SHA-256 verified before use.
- No access token, refresh token, admin token, device token, prompt, or probe response body is persisted in health state or returned by admin APIs.
- Target client, Broker, package, installer, tag, and release version is `0.1.7`.

---

### Task 1: Model-health domain and persistence

**Files:**
- Create: `scripts/codex_model_health.py`
- Modify: `scripts/build_server_package.py`
- Modify: `server-package/install-server.sh`
- Test: `tests/test_codex_model_health.py`
- Test: `tests/test_build_server_package.py`

**Interfaces:**
- Produces: `filter_codex_text_models(payload: dict[str, Any]) -> list[str]`
- Produces: `classify_probe_failure(http_status: int | None, error_code: str | None, message: str | None) -> str`
- Produces: `account_probe_available(record: dict[str, Any] | None, now: float) -> bool | None`
- Produces: `merge_model_probe(previous: dict[str, Any] | None, result: dict[str, Any], now: float, cooldown_seconds: int) -> dict[str, Any]`
- Produces: `ModelHealthStore(path: Path)` with `load()`, `save(state)`, `account(alias)`, and `replace_account(alias, record)`.

- [x] **Step 1: Write failing filtering and classification tests**

```python
def test_filters_only_available_codex_text_models():
    payload = {"models": [
        {"slug": "gpt-text", "supports_text": True, "available": True},
        {"slug": "gpt-image", "supports_text": False, "available": True},
        {"slug": "gpt-disabled", "supports_text": True, "available": False},
    ]}
    assert filter_codex_text_models(payload) == ["gpt-text"]


@pytest.mark.parametrize(("status", "code", "message", "expected"), [
    (503, "server_overloaded", "Selected model is at capacity. Please try a different model.", "capacity"),
    (429, "rate_limit_exceeded", "usage limit reached", "quota"),
    (401, "token_expired", "unauthorized", "auth"),
    (404, "model_not_found", "model not found", "unsupported"),
    (None, None, "connection reset", "probe_error"),
])
def test_classifies_probe_failures(status, code, message, expected):
    assert classify_probe_failure(status, code, message) == expected
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest -q tests/test_codex_model_health.py`

Expected: collection fails because `scripts.codex_model_health` does not exist.

- [x] **Step 3: Implement filtering and classification**

Implement normalization that accepts the observed Codex list shapes (`models` or `data`, `slug` or `id`), sorts/deduplicates names, excludes unavailable and non-text models, and never logs the payload. Match capacity by normalized code `server_overloaded` or case-insensitive text `selected model is at capacity`; match quota/auth/unsupported before falling back to `probe_error`.

- [x] **Step 4: Write failing persistence and availability tests**

```python
def test_transient_probe_error_preserves_recent_success(tmp_path):
    now = 2_000.0
    previous = {"status": "available", "available": True, "last_probe_at": 1_900.0}
    merged = merge_model_probe(previous, {"status": "probe_error", "available": None}, now, 1800)
    assert merged["status"] == "available"
    assert merged["last_probe_error_at"] == now


def test_account_is_blocked_only_when_every_supported_model_is_blocked():
    record = {"models": {
        "one": {"status": "capacity", "available": False, "cooldown_until": 3000},
        "two": {"status": "available", "available": True, "last_probe_at": 2000},
    }}
    assert account_probe_available(record, 2100) is True
    record["models"]["two"] = {"status": "auth", "available": False}
    assert account_probe_available(record, 2100) is False
```

- [x] **Step 5: Run persistence tests and verify RED**

Run: `python -m pytest -q tests/test_codex_model_health.py`

Expected: failure because merge, availability, and store behavior are missing.

- [x] **Step 6: Implement merge, tri-state availability, and atomic store**

Use `True` for known usable, `False` for known fully blocked, and `None` for unknown. Persist JSON through a same-directory temporary file, `os.replace`, and mode `0600`. Truncate saved error messages to 500 characters and allow only the documented fields.

- [x] **Step 7: Add the module to the server package**

Add `scripts/codex_model_health.py -> codex_model_health.py` to `PACKAGE_FILES` and install it as mode `0755` in `/opt/cws-codex`.

- [x] **Step 8: Run focused tests and commit**

Run: `python -m pytest -q tests/test_codex_model_health.py tests/test_build_server_package.py`

Expected: PASS.

Commit:

```bash
git add scripts/codex_model_health.py scripts/build_server_package.py server-package/install-server.sh tests/test_codex_model_health.py tests/test_build_server_package.py
git commit -m "feat: add persistent model health state"
```

---

### Task 2: OAuth model discovery, active probing, and lifecycle

**Files:**
- Modify: `scripts/codex_plus_broker.py`
- Modify: `deploy/cws-codex-broker.env.example`
- Test: `tests/test_codex_plus_share.py`

**Interfaces:**
- Consumes: Task 1 health helpers and `ModelHealthStore`.
- Produces: `AccountRecord.discover_models(client, payload, timeout) -> list[str]`
- Produces: `AccountRecord.probe_model(client, payload, model, timeout) -> dict[str, Any]`
- Produces: `TokenBroker.probe_models_once(now: float | None = None) -> dict[str, Any]`
- Produces: `TokenBroker.model_probe_loop() -> None`
- Produces: FastAPI lifespan that starts one probe loop and cancels it on shutdown.

- [x] **Step 1: Write failing HTTP probe tests using `httpx.MockTransport`**

Create complete fake responses for model discovery, a successful minimal response, JSON `server_overloaded`, SSE error, 401-refresh-retry, and timeout. Assert request headers include `Authorization`, `ChatGPT-Account-Id`, and JSON requests include `store=false`, no tools, and fixed non-user probe text.

```python
assert request.url.path.endswith("/backend-api/codex/models")
assert request.headers["chatgpt-account-id"] == "acct-test"
assert json.loads(request.content)["store"] is False
assert "company" not in request.content.decode().lower()
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest -q tests/test_codex_plus_share.py -k 'discover_models or probe_model'`

Expected: failures because probe methods and settings do not exist.

- [x] **Step 3: Add exact environment-backed settings**

Add integer validation with minimums: interval `>=60`, cooldown `>=60`, concurrency `1..8`, timeout `5..120`; add health path, fallback list, and overridable HTTPS Codex endpoints. Reject non-HTTPS endpoint overrides at startup except `http://127.0.0.1` used by tests.

- [x] **Step 4: Implement discovery and minimal responses probing**

Reuse `ensure_fresh`; parse both JSON and SSE error events without persisting response text. On a 401, force one OAuth refresh and retry once. Do not retry capacity, quota, unsupported, or auth outcomes inside the same cycle.

- [x] **Step 5: Write failing orchestration and lifecycle tests**

Test seven fake accounts, concurrency never exceeding two, fallback models when discovery fails, one immediate probe at startup, cancellation at shutdown, and no overlapping rounds when one round is slow.

- [x] **Step 6: Run orchestration tests and verify RED**

Run: `python -m pytest -q tests/test_codex_plus_share.py -k 'probe_models_once or probe_loop or lifespan'`

Expected: failures because orchestration/lifespan is missing.

- [x] **Step 7: Implement orchestration and FastAPI lifespan**

Use `asyncio.Semaphore(settings.model_probe_concurrency)`, one broker-level probe lock, and `asyncio.wait_for` per request. Start the loop through `@asynccontextmanager`; in `finally`, cancel/await the task and close the owned `httpx.AsyncClient`.

- [x] **Step 8: Run focused tests and commit**

Run: `python -m pytest -q tests/test_codex_model_health.py tests/test_codex_plus_share.py`

Expected: PASS.

Commit:

```bash
git add scripts/codex_plus_broker.py deploy/cws-codex-broker.env.example tests/test_codex_plus_share.py
git commit -m "feat: actively probe Codex model capacity"
```

---

### Task 3: Health-aware scheduling and admin display

**Files:**
- Modify: `scripts/codex_plus_broker.py`
- Modify: `scripts/codex_quota_dashboard.html`
- Modify: `server-package/README-Server.txt`
- Modify: `CODEX_PLUS_SHARE.zh-CN.md`
- Modify: `SERVER_ACCOUNT_MANAGEMENT.zh-CN.md`
- Test: `tests/test_codex_plus_share.py`

**Interfaces:**
- Consumes: `account_probe_available` and persistent records from Tasks 1-2.
- Extends: `TokenBroker.lease(identity, client_device_id, requested_lease_id, client_ip=None, source_ip=None)` candidate filtering and affinity invalidation.
- Extends: `TokenBroker.account_status(refresh_usage=False)` with sanitized `model_health` data.

- [x] **Step 1: Write failing scheduler tests**

```python
def health_aware_broker(now, state):
    broker = TokenBroker.__new__(TokenBroker)
    broker.settings = SimpleNamespace(lease_seconds=28_800, lease_rotation_seconds=86_400)
    broker.accounts = {"chatgpt010": "ten", "chatgpt024": "twenty-four"}
    broker.state_lock = asyncio.Lock()
    broker._load_state = lambda: state
    broker._save_state = lambda value: state.update(value)
    broker.model_health_store = SimpleNamespace(account=lambda alias: {
        "chatgpt010": {"models": {"gpt-test": {"status": "available", "available": True}}},
        "chatgpt024": {"models": {"gpt-test": {"status": "capacity", "available": False, "cooldown_until": now + 1800}}},
    }[alias])

    async def snapshot(account):
        account_id = "acct-ten" if account == "ten" else "acct-twenty-four"
        access_token = jwt({
            "exp": now + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        })
        used = 50 if account == "ten" else 1
        return {"tokens": {"access_token": access_token}}, {
            "rate_limit": {"primary_window": {"used_percent": used, "reset_after_seconds": 3600}}
        }

    broker._account_snapshot = snapshot
    return broker


def test_new_lease_excludes_account_when_all_models_are_blocked(monkeypatch):
    now = 2_000_000_000
    broker = health_aware_broker(now, {"leases": {}})
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)
    identity = DeviceIdentity(token_id="device-a", label="employee-a")
    result = asyncio.run(broker.lease(identity, "pc", None))
    assert result["account_alias"] == "chatgpt010"


def test_existing_affinity_is_dropped_when_account_becomes_blocked(monkeypatch):
    now = 2_000_000_000
    device_hash = hashlib.sha256(b"pc").hexdigest()
    state = {"leases": {"old-024-lease": {
        "account_alias": "chatgpt024",
        "device_token_id": "device-a",
        "client_device_hash": device_hash,
        "created_at": now,
        "expires_at": now + 100,
    }}}
    broker = health_aware_broker(now, state)
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)
    identity = DeviceIdentity(token_id="device-a", label="employee-a")
    result = asyncio.run(broker.lease(identity, "pc", "old-024-lease"))
    assert result["lease_id"] != "old-024-lease"
    assert result["account_alias"] != "chatgpt024"
```

Also test unknown health does not exclude an account and all known-blocked accounts return 503.

- [x] **Step 2: Run scheduler tests and verify RED**

Run: `python -m pytest -q tests/test_codex_plus_share.py -k 'blocked or probe_unknown'`

Expected: 024 is still selected or affinity is retained.

- [x] **Step 3: Implement candidate filtering and affinity invalidation**

Load one immutable health snapshot while holding the lease state lock. Exclude only explicit `False`; retain `True` and `None`. Remove a blocked existing lease before counting active leases and selection.

- [x] **Step 4: Write failing sanitized status tests**

Assert `/v1/admin/accounts` includes model name/status/timestamps/cooldown/truncated error but does not include `access_token`, `refresh_token`, request body, probe output, or authorization headers.

- [x] **Step 5: Implement status fields and dashboard rendering**

Render an account badge and a model table using the existing DOM helper, which assigns dynamic strings through `textContent`. Use Chinese labels: `模型可用`, `容量超限`, `额度耗尽`, `鉴权失败`, `不支持`, `探测异常`, `探测未知`.

- [x] **Step 6: Document operations and run tests**

Document the six probe environment variables, 15-minute schedule, 30-minute cooldown, and how to interpret status. Run:

`python -m pytest -q tests/test_codex_model_health.py tests/test_codex_plus_share.py`

Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add scripts/codex_plus_broker.py scripts/codex_quota_dashboard.html server-package/README-Server.txt CODEX_PLUS_SHARE.zh-CN.md SERVER_ACCOUNT_MANAGEMENT.zh-CN.md tests/test_codex_plus_share.py
git commit -m "feat: avoid accounts with blocked models"
```

---

### Task 4: Authenticated server release mirror

**Files:**
- Create: `scripts/codex_release_manifest.py`
- Modify: `scripts/codex_plus_broker.py`
- Modify: `scripts/build_server_package.py`
- Modify: `server-package/install-server.sh`
- Modify: `deploy/cws-codex-broker.env.example`
- Test: `tests/test_release_manifest.py`
- Test: `tests/test_codex_plus_share.py`
- Test: `tests/test_build_server_package.py`

**Interfaces:**
- Produces: `build_manifest(version: str, release_dir: Path, asset_names: list[str], published_at: str) -> dict[str, Any]`
- Adds: `BrokerSettings.release_dir`, default `/var/lib/cws-codex/releases`.
- Adds: authenticated `GET /v1/client/releases/latest`.
- Adds: authenticated `GET /v1/client/releases/download/{filename}`.

- [x] **Step 1: Write failing deterministic manifest tests**

```python
def test_manifest_records_literal_size_and_sha256(tmp_path):
    asset = tmp_path / "CWS-Codex-Setup-v0.1.7.exe"
    asset.write_bytes(b"installer")
    manifest = build_manifest("0.1.7", tmp_path, [asset.name], "2026-09-18T00:00:00Z")
    assert manifest["release_version"] == "0.1.7"
    assert manifest["assets"][0]["size"] == 9
    assert manifest["assets"][0]["sha256"] == "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
```

Use the full precomputed SHA-256 literal in the test, not the production helper.

- [x] **Step 2: Run manifest tests and verify RED**

Run: `python -m pytest -q tests/test_release_manifest.py`

Expected: import failure.

- [x] **Step 3: Implement deterministic manifest builder and CLI**

Reject filenames containing `/`, `\\`, `..`, or not matching the exact v0.1.7 asset allowlist. Write `latest.json` atomically and emit no file content.

- [x] **Step 4: Write failing API security tests**

Use `TestClient`: unauthenticated calls return 401; a valid device token gets the manifest; traversal and unlisted files return 404; tampered size/hash returns 503; valid download bytes and headers match.

- [x] **Step 5: Run API tests and verify RED**

Run: `python -m pytest -q tests/test_codex_plus_share.py -k 'client_release'`

Expected: routes return 404.

- [x] **Step 6: Implement the two routes and install layout**

Use `Depends(require_device)`, parse and validate `latest.json` for every request, resolve the candidate path and assert `candidate.parent == release_dir.resolve()`, then use `FileResponse`. `install-server.sh` creates the release directory but never deletes existing assets or overwrites `latest.json`.

- [x] **Step 7: Run focused tests and commit**

Run: `python -m pytest -q tests/test_release_manifest.py tests/test_codex_plus_share.py tests/test_build_server_package.py`

Expected: PASS.

Commit:

```bash
git add scripts/codex_release_manifest.py scripts/codex_plus_broker.py scripts/build_server_package.py server-package/install-server.sh deploy/cws-codex-broker.env.example tests/test_release_manifest.py tests/test_codex_plus_share.py tests/test_build_server_package.py
git commit -m "feat: serve authenticated client releases"
```

---

### Task 5: Windows version display, TLS fix, and dual-source update

**Files:**
- Modify: `windows-client/Common-CwsCodex.ps1`
- Modify: `windows-client/Launch-CwsCodex.ps1`
- Modify: `windows-client/Update-CwsCodex.ps1`
- Modify: `windows-client/README-Windows.txt`
- Modify: `windows-client/使用说明.txt`
- Create: `tests/test_windows_update_runtime.py`
- Modify: `tests/test_windows_client_bundle.py`

**Interfaces:**
- Produces: `Enable-CwsTls12` preserving existing protocols.
- Adds: `Launch-CwsCodex.ps1 -StatusOnly`.
- Update source result shape: `{ source, version, name, size, sha256, download_url, headers }`.

- [x] **Step 1: Write the failing real PowerShell runtime tests**

Use `unittest` and `subprocess.run` to execute PowerShell. One test creates a minimal config with `client_version=0.1.6`, invokes launcher `-StatusOnly`, expects exit 0 and `CWS Codex 员工端 v0.1.6`, and asserts no launch/sync logs are created. A second sets TLS 1.1, dot-sources Common, calls `Enable-CwsTls12`, and asserts both TLS 1.1 and TLS 1.2 flags remain.

- [x] **Step 2: Run runtime tests and verify RED safely**

Run only the TLS test first, then the launcher test with a 10-second subprocess timeout:

`python -m unittest -v tests.test_windows_update_runtime.WindowsUpdateRuntimeTests.test_tls_initialization_enables_tls12_without_dropping_existing_protocols`

`python -m unittest -v tests.test_windows_update_runtime.WindowsUpdateRuntimeTests.test_launcher_status_mode_displays_installed_version`

Expected: missing function and unrecognized `-StatusOnly`; terminate on timeout and confirm no VS Code child was launched before continuing.

- [x] **Step 3: Implement TLS and status-only mode**

`Enable-CwsTls12` ORs `[Net.SecurityProtocolType]::Tls12` into the current value. Launcher loads config, prints the version, and returns immediately on `-StatusOnly` before updater, extension, sync, VS Code, or watcher logic.

- [x] **Step 4: Write failing dual-source selection tests**

Dot-source updater in library mode and inject two fetch scriptblocks. Assert Broker success prevents GitHub invocation; Broker failure invokes GitHub; both failures return a combined error; Broker metadata creates an Authorization header while GitHub does not. Use complete literal release objects.

- [x] **Step 5: Run selection tests and verify RED**

Run: `python -m unittest -v tests.test_windows_update_runtime`

Expected: missing dual-source functions.

- [x] **Step 6: Implement server-first update checks**

Read the DPAPI token through existing Common functions; request `$Config.broker_url/v1/client/releases/latest` through configured proxy; validate semantic version, exact installer name, size, SHA-256, and same-origin server download URL. On any Broker exception, log/show the reason and fetch GitHub after calling `Enable-CwsTls12`. Remove the old 24-hour early return for Broker checks. Preserve the existing confirmation and `/S /AUTOUPDATE` behavior.

- [x] **Step 7: Run Windows tests and parser checks**

Run:

```powershell
python -m unittest -v tests.test_windows_update_runtime
python -m pytest -q tests/test_windows_client_bundle.py tests/test_windows_installer_definition.py tests/test_build_windows_client_zip.py tests/test_build_windows_release.py
$errors=@(); Get-ChildItem windows-client -Filter *.ps1 | % { [void][Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$null,[ref]$errors) }; if($errors){$errors; exit 1}
```

Expected: all tests pass and parser errors are empty.

- [x] **Step 8: Commit**

```bash
git add windows-client/Common-CwsCodex.ps1 windows-client/Launch-CwsCodex.ps1 windows-client/Update-CwsCodex.ps1 windows-client/README-Windows.txt windows-client/使用说明.txt tests/test_windows_update_runtime.py tests/test_windows_client_bundle.py
git commit -m "feat: add Broker-first Windows updates"
```

---

### Task 6: Linux dual-source update

**Files:**
- Modify: `linux-client/cws_codex.py`
- Modify: `linux-client/README-Linux.txt`
- Modify: `tests/test_linux_client.py`

**Interfaces:**
- Produces: `broker_release_candidate(config, device_token) -> dict[str, Any]`
- Produces: `github_release_candidate(config) -> dict[str, Any]`
- Produces: `select_release_candidate(config, device_token) -> dict[str, Any]`.

- [x] **Step 1: Write failing fallback tests**

Monkeypatch the network boundary, not selection logic. Test Broker success skips GitHub; Broker error calls GitHub; both failures raise a combined Chinese diagnostic; server download includes Bearer auth; hash mismatch prevents installer execution.

- [x] **Step 2: Run Linux tests and verify RED**

Run: `python -m pytest -q tests/test_linux_client.py -k 'broker_release or update_fallback or hash_mismatch'`

Expected: missing functions/incorrect GitHub-only behavior.

- [x] **Step 3: Implement Broker-first selection and authenticated download**

Reuse `request_json`/proxy behavior, validate same-origin Broker URL and exact Linux archive name, print current/source/latest versions, and retain GitHub URL allowlisting. Verify SHA-256 before opening the tar archive.

- [x] **Step 4: Run Linux tests and commit**

Run: `python -m pytest -q tests/test_linux_client.py`

Expected: PASS.

Commit:

```bash
git add linux-client/cws_codex.py linux-client/README-Linux.txt tests/test_linux_client.py
git commit -m "feat: add Broker-first Linux updates"
```

---

### Task 7: Version 0.1.7 packaging, full verification, deployment, and release

**Files:**
- Modify: `README.md`
- Modify: `CODEX_PLUS_SHARE.zh-CN.md`
- Modify: `scripts/codex_plus_broker.py`
- Modify: `scripts/build_server_package.py`
- Modify: `scripts/build_windows_client_zip.py`
- Modify: `scripts/build_windows_release.py`
- Modify: `scripts/build_linux_client.py`
- Modify: `windows-client/Install-CwsCodex.ps1`
- Modify: `windows-client/Update-CwsCodex.ps1`
- Modify: `windows-client/README-Windows.txt`
- Modify: `windows-client/使用说明.txt`
- Modify: `linux-client/cws_codex.py`
- Modify: `linux-client/install.sh`
- Modify: `linux-client/README-Linux.txt`
- Modify: `windows-installer/CwsCodex.nsi`
- Modify: version assertions under `tests/`
- Regenerate: five `dist/*v0.1.7*` artifacts
- Generate: server `/var/lib/cws-codex/releases/latest.json`

**Interfaces:**
- Consumes all prior tasks.
- Produces Git tag/Release `v0.1.7` and matching server mirror.

- [x] **Step 1: Update every release/version constant to 0.1.7**

Use `rg -n '0\.1\.6'` and change only active version declarations, filenames, installer metadata, docs, and tests; retain historical changelog/release references when semantically historical.

- [x] **Step 2: Run the complete relevant suite**

Run all Broker, model health, manifest, Windows, Linux, installer, and build tests. Run Python compile checks, PowerShell parser checks, `git diff --check`, and archive secret scans. Expected: zero failures; documented platform-only skips are allowed.

- [x] **Step 3: Perform live protocol validation before deployment**

Upload only a standalone probe script that imports the release candidate code without replacing the service. Against one known healthy account and `chatgpt024`, validate model discovery and one minimal response per discovered model. Confirm 024 returns a classified `server_overloaded`/capacity result and no tokens or response bodies print. If this check fails, stop deployment and revise the adapter/tests.

- [x] **Step 4: Build five artifacts and manifest**

Build:

- `CWS-Codex-Setup-v0.1.7.exe`
- `CWS-Codex-Release-v0.1.7.zip`
- `CWS-Codex-Windows-v0.1.7.zip`
- `CWS-Codex-Linux-v0.1.7.tar.gz`
- `CWS-Codex-Server-v0.1.7.tar.gz`

Run `scripts/codex_release_manifest.py` with those exact names and a UTC published timestamp. Verify the recommended ZIP embeds the byte-identical installer and no archive contains runtime credentials.

- [x] **Step 5: Commit release artifacts**

```bash
git add README.md CODEX_PLUS_SHARE.zh-CN.md scripts windows-client linux-client windows-installer tests dist
git commit -m "release: publish CWS Codex v0.1.7"
```

- [x] **Step 6: Back up and deploy Broker v0.1.7**

Back up `/opt/cws-codex`, `/etc/cws-codex/broker.env`, and the current systemd unit to `/var/backups/cws-codex/pre-v0.1.7-<UTC timestamp>`. Install code/pages, add explicit probe configuration, copy the five artifacts plus `latest.json` to the release directory, restart, then verify active status and loopback-only bind.

- [x] **Step 7: Verify production behavior**

Check health, unauthorized release 401, authorized manifest/download hash, admin model status, and lease selection. Force a new lease from the test device and assert it does not select 024 while all 024 models are blocked.

- [x] **Step 8: Push, tag, and publish GitHub Release**

Push `main`, create annotated `v0.1.7`, upload the five verified artifacts, and re-read the Release API to confirm every asset exists.

- [x] **Step 9: Bootstrap and verify this Windows client**

Run the v0.1.7 installer once with auto-update preservation. Confirm installed `config.json.client_version == 0.1.7`, `Launch-CwsCodex.ps1 -StatusOnly` displays v0.1.7, a forced check reads the server source, and normal launch synchronizes credentials without choosing a blocked account.

- [x] **Step 10: Final clean-state verification**

Confirm `git status --short --branch` is clean and aligned with `origin/main`; record commit SHA, release URL, artifact SHA-256 values, server backup path, probe summary, and local client result in the handoff.
