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
- `config_parser.py` — URI → sing-box outbound parsers, one function per protocol (vless/trojan/ss/vmess/hysteria2/tuic); owns `URI_PREFIXES`. hysteria2 parses `obfs`/`obfs-password` (salamander) — without it a server with obfuscation silently fails. tuic parses `congestion_control`, `udp_relay_mode`, `alpn`.
- `storage.py` — atomic persistence: `.singbox_config.json`, pool (`save_pool(public_data, pool_file)`), per-pool checkpoints (`load_check_schedule(schedule_file)`). Paths are parameters so tests can patch `app.POOL_FILE` / `app.SCHEDULE_FILE`.
- `singbox_manager.py` — find/validate/download sing-box. `run_singbox_version()` is the single validation point (was 4 copies). `find_singbox(get_saved_path)` takes the config reader as a callback.
- `catalog.py` — source discovery (GitHub API + `FALLBACK_LISTS`), daily catalog with conditional GETs, dedupe by `canonical_endpoint()`, `assemble_catalog()`. Owns `urlopen_insecure()` (unverified SSL for lists/GitHub API only — corporate MITM workaround; binary downloads stay verified; do NOT re-enable the global `ssl._create_default_https_context` override). `parse_subscription_text()` handles both plain and base64 subscription bodies — several sources (Epodonios/Splitted-By-Protocol, MatinGhanbari) return base64, and without decoding they look non-empty but yield zero URIs and vanish silently. `decode_subscription_bytes()` decodes with `errors="replace"`: a strict decode dropped a whole source (argh94) over one non-UTF-8 byte. `EXTRA_SOURCES` is a hand-verified registry of extra public sources; every entry must be checked live before adding, because some repos ship only READMEs and some files are misnamed (Argh94's `ShadowSocks.txt` actually holds vless/vmess). `GOIDA_FALLBACK_LISTS` is intentionally empty — the upstream repo was deleted (404) and its 26 URLs produced 26 errors per refresh for zero configs; the fetch code is kept so it self-heals if the repo returns. TGParse was evaluated and dropped: all 1206 of its URIs duplicate Epodonios/barry-far/argh94.
- `prober.py` — sing-box probing: `_probe_outbound` (OS-allocated ports via `bind 127.0.0.1:0`, no pre-scan), `probe_uri_standalone` (2 independent probes, `probe=` injectable), `probe_services` (service targets via curl_cffi Chrome TLS + speed measurement), all check tuning constants. `_detect_interception()` rejects DPI/RKN stub responses: the test URL must return an empty body (generate_204), so any non-empty body is analysed for stub markers and reported as `intercepted` instead of a working server. `SERVICE_TARGETS` (gpt/yt/tg/ig) build a per-server block profile; `REQUIRED_SERVICES` (`gpt`) is the subset that gates the public "Services" table — extra targets must NOT be added to it or the table empties. Targets probe in parallel. `measure_quic_reachability()` proves UDP/QUIC works end-to-end via `http_version=3` on curl_cffi; a TCP fallback (`http_version=2`) counts as failure, since hysteria2/tuic ride QUIC and DPI throttles UDP while leaving TCP intact. The report nests it as `services.quic` — there is no separate top-level `quic` field.
- `check_scheduler.py` — pure pool math (`build_pool_plan` / `interleave_ranges`), unchanged.
- `clash_export.py` — URI → Clash/Mihomo YAML (`build_clash_config`). Hand-rolled YAML writer, no PyYAML dependency in the shipped exe; `_yaml_scalar` must render nested dicts as inline YAML mappings, not Python `repr` (`{'a': 'b'}` is invalid YAML and Clash refuses to start). Names come from untrusted `#remark`, so colons are stripped and quotes escaped.
- `geoip.py` — country/ASN lookup via ip-api.com (`lookup_ip`), in-process cache with 24h TTL and a 1.4s throttle (free tier is 45 req/min). Returns `None` on any failure; `is_ru` flags servers inside RU. `VPN_GEOIP_ENABLED=0` disables it entirely. `extract_host()` in `config_parser.py` gets the host from the parsed outbound, because vmess hides it in Base64 JSON and `urlparse` corrupts ss base64.
- Test-compat layer in `app.py`: thin wrappers (`probe_uri_standalone`, `measure_download_speed`, `get_source_catalog`, `load_pool`, `save_pool`, `find_singbox`) forward to module implementations while reading `app.*` attributes at call time, so `patch.object(app, ...)` keeps working. `app.SOURCE_CATALOG` is synced with `catalog.SOURCE_CATALOG` inside the `get_source_catalog` wrapper.
- `print()` to stdout is the intentional logging mechanism (portable exe shows console); no `logging` module by design.
- No type hints — project style; docstrings on all public functions instead.

## Docker

- `Dockerfile` (python:3.13-slim), `docker-compose.yml`, `deploy.sh`, `DOCKER.md`, `docker/fetch_singbox.py`. Full guide is `DOCKER.md`.
- Build context is switchable via `VPN_BUILD_CONTEXT` in `.env`: unset = GitHub repo URL, `.` = local folder. Local mode is required to use a hand-placed `sing-box-bin/sing-box`; GitHub mode clones the repo and ignores local files.
- The Dockerfile downloads sing-box linux-amd64 through `docker/fetch_singbox.py`, which replaced a bare `urlretrieve` — that call had no timeout and hung the build forever on an unreachable GitHub, never reaching the mirrors. Keep per-attempt timeouts (`VPN_DL_TIMEOUT`/`VPN_DL_RETRIES` build-args) when editing.
- `deploy.sh` is the single entry point (idempotent): creates `.env`, fetches sing-box, builds, starts, waits for healthcheck. Never touches `.env` or the `vpn-data` volume.
- `deploy.sh --update` downloads the main-branch archive with host `curl` and unpacks it over the current folder, preserving `.env`, `sing-box-bin/` and pool files. This is the supported update path when GitHub is unreachable *from containers* but works from the host — a common DPI setup.
- Mode is auto-detected: a valid ELF at `sing-box-bin/sing-box` forces local build, because GitHub-mode would clone the repo and re-download the binary (minutes of waiting, then failure, on networks without GitHub). The check reads the ELF magic bytes, not `./sing-box version` — deploy.sh also runs under Git Bash on Windows, where ELF cannot execute. `--github` disables the switch.
- `.dockerignore` excludes only `sing-box-bin/sing-box.exe` (Windows), so a linux `sing-box-bin/sing-box` does reach the image.
- The Dockerfile copies modules with `COPY *.py ./`, not an explicit list. A hand-maintained list was forgotten when `clash_export.py` and `geoip.py` were added, and the container died at startup with `ModuleNotFoundError: No module named 'clash_export'` — the build itself succeeded, so the failure only surfaced in the logs. Keep the glob; `test_*.py` is excluded by `.dockerignore`. New modules must not add third-party imports without updating `requirements.txt` (`clash_export.py` and `geoip.py` are stdlib-only by design).

## Run

- `python app.py` or `python app.py <port>` (default `8079`). Public page `/`, admin `/vpn-control-panel-x7k9` (`admin/vpn2026`, env-overridable via `VPN_ADMIN_USER`/`VPN_ADMIN_PASSWORD`).
- Deps: `pip install fastapi uvicorn qrcode pillow curl_cffi` (tkinter from stdlib for the file dialog).
- Tests: `python -m unittest test_check_scheduling test_validation test_dpi_hardening test_export_geo test_sources` (no pytest). Known pre-existing failures in `test_validation.py` (6 observed; AGENTS previously said 7): tests patch `urllib.request.urlopen` while catalog code calls `urlopen_insecure` — fix tests, not the wrapper. `test_dpi_hardening.py` covers hysteria2 obfs, the tuic parser, the interception detector and the `REQUIRED_SERVICES` gate; its `SchemaValidationTests` shells out to the real `sing-box check` and skips if the binary is absent. `test_export_geo.py` covers Clash YAML (parsed with PyYAML when installed, skipped otherwise), `extract_host`, the QUIC probe and geo lookup with a stubbed API. `test_sources.py` covers plain/base64 subscription parsing, non-UTF-8 tolerance and the source-registry invariants (labels present, no key collisions, githack URLs). None of the three new files needs network access.

## Known dead code — do not "fix" blindly

- `admin_page()`: `open(...).read()` after `return FileResponse(...)` is unreachable.
- `test_all_configs()` inner `progress_callback` contains a `yield` inside a non-generator — the queue-based wrapper below it is the real path.
