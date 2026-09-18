# v0.1.7 release verification

Verified on 2026-09-18, Asia/Shanghai. Release code commit: `3448bb438ee103a6c0bd12fcb618ccd024f76345`.

Release: https://github.com/ichbinbz/gpt_share/releases/tag/v0.1.7

## Implementation and validation

Tasks 1–6 were implemented in `codex/model-health-updates-v017` and fast-forwarded into main. Live Codex findings required `client_version` on discovery, support for the current catalog schema, terminal SSE handling, and a total timeout across refresh/retry. The final server archive contains those repaired Broker bytes.

Final supported test matrix: **187 passed, 2 skipped**, including **18 real Windows PowerShell runtime tests**. The skips concern Windows symlink privileges and POSIX permission bits. Python compile checks, PowerShell parser checks and `git diff --check` passed. Five asset sizes/hashes, 41 archive members, embedded installer byte identity, runtime credential filename checks and device-token pattern scans passed. Server-only token-management/import tests require `fcntl` and cannot be collected on Windows; they were not counted as passing.

Full startup testing additionally found and fixed two Windows defects: the updater looked for the DPAPI token in the state directory instead of the installation directory, and a reused proxy connection caused a subsequent lease request to fail with HTTP 502 on PowerShell 5.1. The updater now reads the installed token and closes its Broker check connection. A runtime test exercises the normal updater entry point and requires both behaviors. Normal startup then completed Broker update checking, new-lease synchronization and VS Code launch.

## Production

- Broker active, `/healthz`: `{"ok":true,"accounts":7,"version":"0.1.7"}`.
- Listener: **127.0.0.1:8765 only**.
- Deployment backup: `/var/backups/cws-codex/pre-v0.1.7-20260918T1700Z`.
- Mirror rollback copy: that backup's `release-mirror-before-token-path-fix` directory.
- Authenticated mirror: five assets validated before replacement; manifest installed last. Installed client download hash matches the current server installer. Unauthenticated manifest request returns **401**.
- Administrator account API returns seven accounts with sanitized model health; no exact access/refresh/admin/device token fields. Health persistence mode: **0600**.
- Six accounts have five available models each. `account-06` has five **quota** results and is blocked. `account-02` is chatgpt024; `account-05` is chatgpt010.
- Live pre-deploy validation and production state show all five visible models usable for both 010 and 024. **024's earlier capacity error was no longer reproducible**; it remains eligible while healthy. Capacity exclusion and blocked-affinity replacement are covered by automated tests rather than a fabricated production failure.
- New local test-device leases selected **account-05**, whose model health is available, rather than blocked account-06.

## Windows client

Installed at `C:\Users\bynav\AppData\Local\CWS Codex`, upgraded from **0.1.5 to 0.1.7** using `/S /AUTOUPDATE`. Configuration and encrypted token preserved. Installer exit **0**; `-StatusOnly` prints `CWS Codex 员工端 v0.1.7`; forced check selects **Broker / 0.1.7**. Normal startup synchronizes account-05 and launches VS Code. Intermediate 502 failures were reproduced, corrected by disabling update connection reuse, and followed by successful startup verification.

## Final asset SHA-256

| Asset | SHA-256 |
| --- | --- |
| CWS-Codex-Setup-v0.1.7.exe | `4ccdfe3d19c0dbac5c3d4288e7a098d7c9e48b9e96c0ea228400e8e54ebd0ab2` |
| CWS-Codex-Release-v0.1.7.zip | `46b772e908bf9f5bfe08214cb6b1ab881062bf806626e4d5f7b8deedeacd22b3` |
| CWS-Codex-Windows-v0.1.7.zip | `616f06adc3057f44d15ebe65a1e954b7e51cab73162398e337f4b21031fab301` |
| CWS-Codex-Linux-v0.1.7.tar.gz | `545f8eccab91a000a709c088be9b82f773ffa58d906aa1b8c5f12a61a925314e` |
| CWS-Codex-Server-v0.1.7.tar.gz | `286957a7384acaa1827ee760c40545a4759fd0e2fe318bff98d403ee2e62a19f` |

GitHub asset digest and size verification uses the generated `dist/latest.json`. Release tag points to final release code; subsequent main documentation records this handoff.
