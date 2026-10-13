# AGENTS.md — Where is my VPN (`OL/2_Where_is_my_VPN`)

> Scope: only this directory. Git root (`MyProjectsVSCode`) is a shared workspace of unrelated projects — do not add root-level config.

## Run

- Windows + PowerShell 5.1. No `&&` chaining; use `cmd1; if ($?) { cmd2 }`.
- No venv, `requirements.txt`, `pyproject.toml`, tests, linter, or CI. Deps are implicit from `app.py` imports: `pip install fastapi uvicorn qrcode pillow curl_cffi` (plus `tkinter` from stdlib for the file dialog; `requests`/`zstandard`/`ordered-set` are only Nuitka build accelerators). ChatGPT probing uses `curl_cffi` with Chrome TLS/HTTP impersonation through the sing-box HTTP proxy to avoid false Cloudflare 403 responses from urllib.
- Dev run from this dir: `python app.py` or `python app.py <port>` (default `8079`, `http://localhost:8079`). Public page `/`, admin panel `/vpn-control-panel-x7k9` (`admin/vpn2026` — hardcoded in `app.py:54-58`, change there).
- Frontend is static `index.html` / `admin.html` served via `FileResponse(get_resource_path(...))`; Tailwind comes from CDN, so pages need internet. No build step for HTML.
- Needs `sing-box-bin/sing-box.exe` (v1.10.3) plus internet access to `api.github.com` / `raw.githack.com` and GitHub mirrors. Graceful fallback is `FALLBACK_LISTS` in `app.py` when the GitHub API is unreachable.

## Build (portable .exe)

- Full command is in `Инструкция по сборке дистрибутива.txt`; from this dir after `rmdir /s /q dist`:
  `python -m nuitka --standalone --onefile --output-filename="Where is my VPN.exe" --windows-icon-from-ico=icon.ico --include-data-files=index.html=index.html --include-data-files=admin.html=admin.html --include-data-dir=sing-box-bin=sing-box-bin --enable-plugin=anti-bloat --output-dir=dist app.py`
- Path resolution is exe-aware (`APP_DIR`, `get_resource_path()`): in `--onefile` mode HTML/bin/config resolve next to `sys.executable`. User override is stored in `.singbox_config.json` next to the exe (gitignored runtime artifact, do not commit).
- `dist/Where is my VPN.exe` and `sing-box-bin/sing-box.exe` are committed binaries — do not delete/rebuild without need.

## Architecture (modular: `app.py` ~1100 lines + 6 modules)

- `app.py` — FastAPI endpoints, check orchestration (`run_check_core` / `_run_check_core`), maintenance loops, global state (`public_data`, `auto_update_settings`, `CHECK_STATS`).
- `paths.py` — exe-aware path resolution (`APP_DIR`, `get_resource_path()`); in Nuitka --onefile mode HTML/bin/config resolve next to `sys.executable`.
- `config_parser.py` — URI → sing-box outbound parsers, one function per protocol (vless/trojan/ss/vmess/hysteria2); `tuic://` is collected but has no parser and always lands in "unparsable". Owns `URI_PREFIXES`.
- `storage.py` — atomic persistence: `.singbox_config.json`, pool (`save_pool(public_data, pool_file)`), per-pool checkpoints (`load_check_schedule(schedule_file)`). Paths are parameters so tests can patch `app.POOL_FILE` / `app.SCHEDULE_FILE`.
- `singbox_manager.py` — find/validate/download sing-box. `run_singbox_version()` is the single validation point (was 4 copies). `find_singbox(get_saved_path)` takes the config reader as a callback.
- `catalog.py` — source discovery (GitHub API + `FALLBACK_LISTS`), daily catalog with conditional GETs, dedupe by `canonical_endpoint()`, `assemble_catalog()`. Owns `urlopen_insecure()` (unverified SSL for lists/GitHub API only — corporate MITM workaround; binary downloads stay verified; do NOT re-enable the global `ssl._create_default_https_context` override).
- `prober.py` — sing-box probing: `_probe_outbound` (OS-allocated ports via `bind 127.0.0.1:0`, no pre-scan), `probe_uri_standalone` (2 independent probes, `probe=` injectable), `probe_services` (ChatGPT via curl_cffi Chrome TLS + speed measurement), all check tuning constants.
- `check_scheduler.py` — pure pool math (`build_pool_plan` / `interleave_ranges`), unchanged.
- Test-compat layer in `app.py`: thin wrappers (`probe_uri_standalone`, `measure_download_speed`, `get_source_catalog`, `load_pool`, `save_pool`, `find_singbox`) forward to module implementations while reading `app.*` attributes at call time, so `patch.object(app, ...)` keeps working. `app.SOURCE_CATALOG` is synced with `catalog.SOURCE_CATALOG` inside the `get_source_catalog` wrapper.
- `print()` to stdout is the intentional logging mechanism (portable exe shows console); no `logging` module by design.
- No type hints — project style; docstrings on all public functions instead.

## Run

- `python app.py` or `python app.py <port>` (default `8079`). Public page `/`, admin `/vpn-control-panel-x7k9` (`admin/vpn2026`, env-overridable via `VPN_ADMIN_USER`/`VPN_ADMIN_PASSWORD`).
- Deps: `pip install fastapi uvicorn qrcode pillow curl_cffi` (tkinter from stdlib for the file dialog).
- Tests: `python -m unittest test_check_scheduling test_validation` (no pytest). Known pre-existing failures in `test_validation.py` (7): tests patch `urllib.request.urlopen` while catalog code calls `urlopen_insecure` — fix tests, not the wrapper.

## Known dead code — do not "fix" blindly

- `admin_page()`: `open(...).read()` after `return FileResponse(...)` is unreachable.
- `test_all_configs()` inner `progress_callback` contains a `yield` inside a non-generator — the queue-based wrapper below it is the real path.
