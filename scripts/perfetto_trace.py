#!/usr/bin/env python3
"""通过转发端口在服务器侧查看 Perfetto / PyTorch profiler trace。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import mimetypes
import os
import posixpath
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = SKILL_DIR / ".runtime"
BOOTSTRAP_URL = "https://get.perfetto.dev/trace_processor"
UPSTREAM_UI_ORIGIN = "https://ui.perfetto.dev"
DEFAULT_UI_VERSION = "v56.1-c794fceab"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_RPC_PORT = 9001
DEFAULT_UI_PORT = 19002
AUTO_PORT_SCAN_LIMIT = 200
STATE_FILE_NAME = "state.json"
UI_PREFETCH_PATHS = (
    "/",
    "/{version}/frontend_bundle.js",
    "/{version}/frontend.css",
    "/{version}/engine_bundle.js",
    "/{version}/trace_processor.wasm",
    "/{version}/trace_processor_memory64.wasm",
    "/{version}/stdlib_docs.json",
    "/{version}/assets/MaterialSymbolsOutlined.woff2",
    "/{version}/assets/Roboto.woff2",
    "/{version}/assets/RobotoMono-Regular.woff2",
    "/{version}/assets/brand.png",
    "/{version}/assets/favicon.png",
    "/{version}/assets/logo-3d.png",
)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
PROXY_ENV_KEYS = {
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
}


def color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM", "") != "dumb"


def green(text: str) -> str:
    if not color_enabled():
        return text
    return f"\033[32m{text}\033[0m"


def visible_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_visible(text: str, width: int) -> str:
    return text + " " * max(0, width - visible_width(text))


def shorten_middle(text: str, max_chars: int = 78) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(8, max_chars - 3)
    left = keep // 2
    right = keep - left
    return f"{text[:left]}...{text[-right:]}"


def print_box(title: str, rows: list[str]) -> None:
    inner_width = max(58, visible_width(title) + 8, *(visible_width(row) for row in rows))
    top_dash_count = max(1, inner_width - visible_width(title) - 1)
    print(green(f"╭─ {title} " + "─" * top_dash_count + "╮"))
    for row in rows:
        print(green("│ ") + pad_visible(row, inner_width) + green(" │"))
    print(green("╰" + "─" * (inner_width + 2) + "╯"))


def runtime_dir(args: argparse.Namespace | None = None) -> Path:
    return DEFAULT_RUNTIME_DIR.resolve()


def state_path(rt: Path) -> Path:
    return rt / STATE_FILE_NAME


def bootstrap_path(rt: Path) -> Path:
    return rt / "bin" / "trace_processor"


def home_dir(rt: Path) -> Path:
    return rt / "home"


def cache_dir(rt: Path) -> Path:
    return rt / "ui_cache"


def logs_dir(rt: Path) -> Path:
    return rt / "logs"


def ensure_dirs(rt: Path) -> None:
    for path in (rt / "bin", home_dir(rt), cache_dir(rt), logs_dir(rt)):
        path.mkdir(parents=True, exist_ok=True)


def clean_env(rt: Path, use_env_proxy: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir(rt))
    if not use_env_proxy:
        for key in PROXY_ENV_KEYS:
            env.pop(key, None)
    return env


def url_opener(use_env_proxy: bool = False):
    if use_env_proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def download_file(url: str, dest: Path, use_env_proxy: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    opener = url_opener(use_env_proxy)
    req = urllib.request.Request(url, headers={"User-Agent": "perfetto-trace-viewer/1.0"})
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with opener.open(req, timeout=120) as resp:
        tmp.write_bytes(resp.read())
    tmp.chmod(0o755)
    tmp.replace(dest)


def fetch_ui_asset(cache_root: Path, request_path: str, user_agent: str, use_env_proxy: bool = False) -> tuple[Path, bool]:
    cache_path = safe_cache_path(cache_root, request_path)
    if cache_path.exists():
        return cache_path, False
    parsed = urllib.parse.urlparse(request_path)
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    upstream_url = f"{UPSTREAM_UI_ORIGIN}{path}{query}"
    opener = url_opener(use_env_proxy)
    req = urllib.request.Request(upstream_url, headers={"User-Agent": user_agent})
    with opener.open(req, timeout=60) as resp:
        data = resp.read()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return cache_path, True


def prefetch_ui_cache(rt: Path, version: str, use_env_proxy: bool = False) -> tuple[int, int]:
    fetched = 0
    cached = 0
    user_agent = f"perfetto-trace-viewer/{version}"
    for template in UI_PREFETCH_PATHS:
        request_path = template.format(version=version)
        _, downloaded = fetch_ui_asset(cache_dir(rt), request_path, user_agent, use_env_proxy=use_env_proxy)
        if downloaded:
            fetched += 1
        else:
            cached += 1
    return fetched, cached


def trace_display_name(trace: Path) -> str:
    return trace.name


def viewer_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/#!/viewer?local_cache_key"


def proxy_url_for_port(port: int) -> str | None:
    template = os.environ.get("VSCODE_PROXY_URI", "").strip()
    if not template:
        return None
    replacements = {
        "{{port}}": str(port),
        "{port}": str(port),
        "$PORT": str(port),
        "%PORT%": str(port),
    }
    url = template
    replaced = False
    for old, new in replacements.items():
        if old in url:
            url = url.replace(old, new)
            replaced = True
    if not replaced:
        url = url.rstrip("/") + f"/{port}/"
    return viewer_url(url)


def run_bootstrap_version(rt: Path, use_env_proxy: bool = False) -> str:
    cmd = [sys.executable, str(bootstrap_path(rt)), "--version"]
    proc = subprocess.run(
        cmd,
        env=clean_env(rt, use_env_proxy=use_env_proxy),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        check=True,
    )
    match = re.search(r"Perfetto\s+(v\S+)", proc.stdout)
    if not match:
        raise RuntimeError(f"无法从下面输出解析 Perfetto 版本：\n{proc.stdout}")
    return match.group(1)


def install_perfetto(args: argparse.Namespace) -> str:
    rt = runtime_dir(args)
    ensure_dirs(rt)
    bootstrap = bootstrap_path(rt)
    if args.force or not bootstrap.exists():
        print(f"正在下载 Perfetto 官方 bootstrap：{BOOTSTRAP_URL}")
        download_file(BOOTSTRAP_URL, bootstrap, use_env_proxy=args.use_env_proxy)
    version = run_bootstrap_version(rt, use_env_proxy=args.use_env_proxy)
    if not getattr(args, "skip_ui_cache", False):
        fetched, cached = prefetch_ui_cache(rt, version, use_env_proxy=args.use_env_proxy)
        print(f"Perfetto UI 资源缓存已就绪：新增 {fetched} 个，复用 {cached} 个。")
    print(f"Perfetto trace_processor 已就绪：{version}")
    print(f"运行/缓存目录：{rt}")
    print(f"trace_processor 二进制缓存：{home_dir(rt) / '.local/share/perfetto/prebuilts'}")
    print(f"Perfetto UI 缓存：{cache_dir(rt)}")
    return version


def ensure_installed(args: argparse.Namespace) -> str:
    rt = runtime_dir(args)
    if not bootstrap_path(rt).exists():
        return install_perfetto(args)
    return run_bootstrap_version(rt, use_env_proxy=getattr(args, "use_env_proxy", False))


def read_state(rt: Path) -> dict:
    path = state_path(rt)
    if not path.exists():
        return {"instances": {}}
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"instances": {}}
    if "instances" in state and isinstance(state["instances"], dict):
        return state
    if "ui_port" in state:
        return {"instances": {str(state["ui_port"]): state}}
    return {"instances": {}}


def write_state(rt: Path, state: dict) -> None:
    rt.mkdir(parents=True, exist_ok=True)
    state_path(rt).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def instance_belongs_to_runtime(instance: dict, rt: Path) -> bool:
    runtime = instance.get("runtime_dir")
    if not runtime:
        return True
    try:
        return Path(runtime).expanduser().resolve() == rt.resolve()
    except OSError:
        return False


def persistent_instance(instance: dict) -> dict:
    return {key: value for key, value in instance.items() if not key.startswith("_")}


def get_instances(rt: Path) -> dict[str, dict]:
    return read_state(rt).get("instances", {})


def write_instances(rt: Path, instances: dict[str, dict]) -> None:
    instances = {
        key: persistent_instance(instance)
        for key, instance in instances.items()
        if instance_belongs_to_runtime(instance, rt)
    }
    if instances:
        write_state(rt, {"instances": instances})
    else:
        path = state_path(rt)
        if path.exists():
            path.unlink()


def load_instances(rt: Path, prune_stale: bool = True) -> dict[str, dict]:
    instances = get_instances(rt)
    if not prune_stale:
        return instances
    live_instances = {}
    removed = []
    changed = False
    for key, instance in instances.items():
        if not instance_belongs_to_runtime(instance, rt):
            removed.append(key)
            continue
        for pid_key in ("ui_pid", "rpc_pid"):
            tick_key = f"{pid_key}_start_ticks"
            if instance.get(pid_key) and instance.get(tick_key) is None and pid_alive(instance.get(pid_key)):
                instance[tick_key] = process_start_ticks(instance.get(pid_key))
                changed = True
        ui_live = pid_alive(instance.get("ui_pid"), instance.get("ui_pid_start_ticks"))
        rpc_live = pid_alive(instance.get("rpc_pid"), instance.get("rpc_pid_start_ticks"))
        if ui_live or rpc_live:
            live_instances[key] = instance
        else:
            removed.append(key)
    if removed or changed:
        write_instances(rt, live_instances)
    return live_instances


def process_start_ticks(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        after_comm = stat.rsplit(") ", 1)[1]
        return int(after_comm.split()[19])
    except (IndexError, ValueError):
        return None


def boot_time_epoch() -> int | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def process_start_time_iso(pid: int | None) -> str | None:
    ticks = process_start_ticks(pid)
    boot_time = boot_time_epoch()
    if ticks is None or boot_time is None:
        return None
    try:
        hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    except (KeyError, ValueError, OSError):
        hz = 100
    started = boot_time + ticks / hz
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))


def pid_alive(pid: int | None, start_ticks: int | None = None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        if start_ticks is None:
            return True
        return process_start_ticks(pid) == start_ticks
    except ProcessLookupError:
        return False
    except PermissionError:
        if start_ticks is None:
            return True
        return process_start_ticks(pid) == start_ticks


def stop_pid(pid: int | None, timeout: float = 8.0, start_ticks: int | None = None) -> None:
    if not pid_alive(pid, start_ticks):
        return
    assert pid is not None
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid, start_ticks):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_instance(instance: dict) -> list[str]:
    stopped = []
    for key in ("ui_pid", "rpc_pid"):
        pid = instance.get(key)
        start_ticks = instance.get(f"{key}_start_ticks")
        if pid_alive(pid, start_ticks):
            stop_pid(pid, start_ticks=start_ticks)
            stopped.append(f"{key}={pid}")
    return stopped


def port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def iter_process_argv() -> list[tuple[int, list[str]]]:
    processes = []
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc_dir.name)
            raw = (proc_dir / "cmdline").read_bytes()
        except (OSError, ValueError):
            continue
        if not raw:
            continue
        argv = [part.decode(errors="replace") for part in raw.rstrip(b"\0").split(b"\0") if part]
        if argv:
            processes.append((pid, argv))
    return processes


def flag_value(argv: list[str], flag: str) -> str | None:
    try:
        idx = argv.index(flag)
    except ValueError:
        return None
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def int_flag_value(argv: list[str], flag: str) -> int | None:
    value = flag_value(argv, flag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def runtime_from_cache_path(cache_path: str | None) -> Path | None:
    if not cache_path:
        return None
    path = Path(cache_path).expanduser()
    if path.name == "ui_cache":
        return path.parent
    return None


def runtime_from_trace_processor_path(path: str) -> Path | None:
    marker = f"{os.sep}home{os.sep}.local{os.sep}share{os.sep}perfetto{os.sep}prebuilts{os.sep}"
    if marker not in path:
        return None
    return Path(path.split(marker, 1)[0])


def log_paths_for_instance(runtime: Path | None, rpc_port: int | None, ui_port: int | None) -> dict[str, str]:
    if runtime is None:
        return {}
    log_dir = runtime / "logs"
    logs = {}
    if rpc_port is not None:
        candidates = [log_dir / f"trace_processor_{rpc_port}.log", log_dir / f"perfetto_tp_{rpc_port}.log"]
        for path in candidates:
            if path.exists():
                logs["trace_processor"] = str(path)
                break
        logs.setdefault("trace_processor", str(candidates[0]))
    if ui_port is not None:
        candidates = [log_dir / f"ui_wrapper_{ui_port}.log", log_dir / f"perfetto_ui_{ui_port}.log"]
        for path in candidates:
            if path.exists():
                logs["ui_wrapper"] = str(path)
                break
        logs.setdefault("ui_wrapper", str(candidates[0]))
    return logs


def parse_rpc_process(pid: int, argv: list[str]) -> dict | None:
    if "server" not in argv or "http" not in argv:
        return None
    rpc_port = int_flag_value(argv, "--port")
    if rpc_port is None:
        return None
    bind = flag_value(argv, "--ip-address") or DEFAULT_BIND
    trace = None
    port_idx = argv.index("--port") if "--port" in argv else -1
    for item in argv[port_idx + 2 :]:
        if not item.startswith("-"):
            trace = item
    runtime = runtime_from_trace_processor_path(argv[0])
    return {
        "bind": bind,
        "rpc_port": rpc_port,
        "rpc_pid": pid,
        "rpc_pid_start_ticks": process_start_ticks(pid),
        "trace": trace,
        "runtime_dir": str(runtime) if runtime else None,
        "started_at": process_start_time_iso(pid),
    }


def parse_ui_process(pid: int, argv: list[str]) -> dict | None:
    script_names = {Path(item).name for item in argv}
    is_current_wrapper = "serve-ui" in argv and "perfetto_trace.py" in script_names
    is_legacy_wrapper = "serve_perfetto_ui.py" in script_names
    if not (is_current_wrapper or is_legacy_wrapper):
        return None
    ui_port = int_flag_value(argv, "--ui-port")
    rpc_port = int_flag_value(argv, "--rpc-port")
    if ui_port is None or rpc_port is None:
        return None
    bind = flag_value(argv, "--bind") or DEFAULT_BIND
    cache_path = flag_value(argv, "--cache-dir")
    runtime = runtime_from_cache_path(cache_path)
    version = flag_value(argv, "--version") or DEFAULT_UI_VERSION
    trace_title = flag_value(argv, "--trace-title")
    return {
        "bind": bind,
        "rpc_port": rpc_port,
        "ui_port": ui_port,
        "ui_pid": pid,
        "ui_pid_start_ticks": process_start_ticks(pid),
        "trace_title": trace_title,
        "runtime_dir": str(runtime) if runtime else None,
        "perfetto_version": version,
        "started_at": process_start_time_iso(pid),
        "logs": log_paths_for_instance(runtime, rpc_port, ui_port),
        "_source": "process",
    }


def merge_instance(base: dict | None, incoming: dict) -> dict:
    merged = dict(base or {})
    for key, value in incoming.items():
        if value is not None and value != {}:
            merged[key] = value
    if "logs" in (base or {}) or "logs" in incoming:
        logs = {}
        logs.update((base or {}).get("logs", {}))
        logs.update(incoming.get("logs", {}))
        merged["logs"] = logs
    return merged


def discover_process_instances() -> dict[str, dict]:
    rpc_by_port = {}
    ui_by_port = {}
    for pid, argv in iter_process_argv():
        rpc = parse_rpc_process(pid, argv)
        if rpc:
            rpc_by_port[rpc["rpc_port"]] = rpc
            continue
        ui = parse_ui_process(pid, argv)
        if ui:
            ui_by_port[ui["ui_port"]] = ui

    instances = {}
    for ui_port, ui in ui_by_port.items():
        rpc = rpc_by_port.get(ui["rpc_port"], {})
        instance = merge_instance(rpc, ui)
        if not instance.get("trace"):
            instance["trace"] = "<进程发现：trace 路径未知>"
        if not instance.get("logs"):
            runtime = Path(instance["runtime_dir"]) if instance.get("runtime_dir") else None
            instance["logs"] = log_paths_for_instance(runtime, instance.get("rpc_port"), instance.get("ui_port"))
        instances[str(ui_port)] = instance
    return instances


def legacy_state_paths(rt: Path) -> list[Path]:
    paths = []
    tmp_root = Path(tempfile.gettempdir())
    for path in sorted(tmp_root.glob("perfetto-trace-viewer-*/state.json")):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved != state_path(rt).resolve():
            paths.append(resolved)
    return paths


def load_state_file_instances(path: Path) -> dict[str, dict]:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    raw_instances = state.get("instances", {})
    if not isinstance(raw_instances, dict):
        return {}
    instances = {}
    runtime = path.parent
    for key, instance in raw_instances.items():
        if not isinstance(instance, dict):
            continue
        instance = dict(instance)
        instance.setdefault("runtime_dir", str(runtime))
        instance.setdefault("_source", str(path))
        ui_live = pid_alive(instance.get("ui_pid"), instance.get("ui_pid_start_ticks"))
        rpc_live = pid_alive(instance.get("rpc_pid"), instance.get("rpc_pid_start_ticks"))
        if ui_live or rpc_live:
            instances[str(key)] = instance
    return instances


def load_visible_instances(rt: Path) -> dict[str, dict]:
    instances = load_instances(rt)
    for path in legacy_state_paths(rt):
        for key, instance in load_state_file_instances(path).items():
            instances[key] = merge_instance(instances.get(key), instance)
    for key, instance in discover_process_instances().items():
        instances[key] = merge_instance(instances.get(key), instance)
    return dict(sorted(instances.items(), key=lambda item: int(item[0])))


def instance_ports(instances: dict[str, dict], name: str) -> set[int]:
    ports = set()
    for instance in instances.values():
        try:
            ports.add(int(instance.get(name)))
        except (TypeError, ValueError):
            pass
    return ports


def port_available(host: str, port: int, managed_ports: set[int]) -> bool:
    return port not in managed_ports and not port_listening(host, port)


def choose_open_ports(args: argparse.Namespace, instances: dict[str, dict]) -> tuple[int, int, list[str]]:
    ui_given = args.ui_port is not None
    rpc_given = args.rpc_port is not None
    ui_ports = instance_ports(instances, "ui_port")
    rpc_ports = instance_ports(instances, "rpc_port")
    notes = []

    if ui_given and rpc_given:
        return args.ui_port, args.rpc_port, notes

    if ui_given:
        ui_port = args.ui_port
        start_rpc = DEFAULT_RPC_PORT + max(0, ui_port - DEFAULT_UI_PORT)
        for rpc_port in range(start_rpc, start_rpc + AUTO_PORT_SCAN_LIMIT):
            if port_available(args.bind, rpc_port, rpc_ports):
                notes.append(f"未指定 RPC 端口，自动选择 {rpc_port}。")
                return ui_port, rpc_port, notes
        raise RuntimeError(f"未能在 {start_rpc} 起的 {AUTO_PORT_SCAN_LIMIT} 个端口内找到可用 RPC 端口。")

    if rpc_given:
        rpc_port = args.rpc_port
        if not port_available(args.bind, rpc_port, rpc_ports):
            raise RuntimeError(f"RPC 端口 {args.bind}:{rpc_port} 已被占用。")
        for ui_port in range(DEFAULT_UI_PORT, DEFAULT_UI_PORT + AUTO_PORT_SCAN_LIMIT):
            if port_available(args.bind, ui_port, ui_ports):
                notes.append(f"未指定 UI 端口，自动选择 {ui_port}。")
                return ui_port, rpc_port, notes
        raise RuntimeError(f"未能在 {DEFAULT_UI_PORT} 起的 {AUTO_PORT_SCAN_LIMIT} 个端口内找到可用 UI 端口。")

    for offset in range(AUTO_PORT_SCAN_LIMIT):
        ui_port = DEFAULT_UI_PORT + offset
        rpc_port = DEFAULT_RPC_PORT + offset
        if port_available(args.bind, ui_port, ui_ports) and port_available(args.bind, rpc_port, rpc_ports):
            notes.append(f"未指定端口，自动选择 UI {ui_port} / RPC {rpc_port}。")
            return ui_port, rpc_port, notes
    raise RuntimeError(
        f"未能在 UI {DEFAULT_UI_PORT}-{DEFAULT_UI_PORT + AUTO_PORT_SCAN_LIMIT - 1} "
        f"和 RPC {DEFAULT_RPC_PORT}-{DEFAULT_RPC_PORT + AUTO_PORT_SCAN_LIMIT - 1} 范围内找到可用端口。"
    )


def selected_instances(instances: dict[str, dict], ui_port: int | None) -> dict[str, dict]:
    if ui_port is None:
        return dict(instances)
    key = str(ui_port)
    return {key: instances[key]} if key in instances else {}


def stop_services(args: argparse.Namespace) -> None:
    rt = runtime_dir(args)
    instances = load_visible_instances(rt)
    targets = selected_instances(instances, getattr(args, "ui_port", None))
    if not targets:
        if getattr(args, "ui_port", None) is None:
            print("没有找到由本工具管理的 Perfetto trace 查看服务。")
        else:
            print(f"没有找到 UI 端口 {args.ui_port} 对应的服务。")
        return
    for key, instance in targets.items():
        stopped = stop_instance(instance)
        trace = instance.get("trace", "<unknown>")
        if stopped:
            print(f"已停止 UI 端口 {key}：{', '.join(stopped)}")
        else:
            print(f"UI 端口 {key} 没有存活进程，已清理状态。")
        print(f"  trace：{trace}")
        instances.pop(key, None)
    write_instances(rt, instances)


def http_request(host: str, port: int, method: str, path: str, timeout: float = 3.0, origin: str | None = None):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    headers = {}
    if origin:
        headers["Origin"] = origin
    try:
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def wait_for_status(host: str, port: int, timeout: float, label: str, path: str = "/status") -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            status, _, _ = http_request(host, port, "POST", path, timeout=3, origin=UPSTREAM_UI_ORIGIN)
            if status == 200:
                return
            last_error = f"HTTP {status}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise TimeoutError(f"等待 {label} 就绪超时：{host}:{port}{path}，最后错误：{last_error}")


def wait_for_health(host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            status, _, data = http_request(host, port, "GET", "/__wrapper_health", timeout=3)
            if status == 200 and data == b"ok\n":
                return
            last_error = f"HTTP {status}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"等待 UI wrapper 就绪超时：{host}:{port}，最后错误：{last_error}")


def websocket_handshake(host: str, port: int, origin: str = "https://example.invalid", path: str = "/websocket") -> str:
    sock = socket.create_connection((host, port), timeout=5)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Origin: {origin}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
        sock.sendall(request)
        response = sock.recv(4096)
        return response.split(b"\r\n", 1)[0].decode(errors="replace")
    finally:
        sock.close()


def tail_file(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    data = path.read_text(errors="replace").splitlines()
    return "\n".join(data[-lines:])


def start_process(cmd: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)


def open_trace(args: argparse.Namespace) -> None:
    rt = runtime_dir(args)
    ensure_dirs(rt)
    trace = Path(args.trace).expanduser().resolve()
    if not trace.exists():
        raise FileNotFoundError(f"trace 文件不存在：{trace}")

    version = ensure_installed(args)
    display_name = trace_display_name(trace)
    instances = load_visible_instances(rt)
    ui_port, rpc_port, port_notes = choose_open_ports(args, instances)
    args.ui_port = ui_port
    args.rpc_port = rpc_port
    instance_key = str(args.ui_port)

    existing = instances.get(instance_key)
    if existing:
        print(f"UI 端口 {args.ui_port} 已有本工具管理的服务，先停止旧实例再打开新 trace。")
        stopped = stop_instance(existing)
        if stopped:
            print(f"  已停止旧实例：{', '.join(stopped)}")
        instances.pop(instance_key, None)
        write_instances(rt, instances)

    for key, instance in list(instances.items()):
        if not pid_alive(instance.get("ui_pid"), instance.get("ui_pid_start_ticks")) and not pid_alive(
            instance.get("rpc_pid"), instance.get("rpc_pid_start_ticks")
        ):
            instances.pop(key, None)
            continue
        if int(instance.get("rpc_port", -1)) == args.rpc_port:
            raise RuntimeError(
                f"RPC 端口 {args.rpc_port} 已被 UI 端口 {key} 的服务占用。"
                "请换一个 --rpc-port，或先 stop 对应服务。"
            )
        if int(instance.get("ui_port", -1)) == args.ui_port:
            raise RuntimeError(f"UI 端口 {args.ui_port} 已被占用。")
    write_instances(rt, instances)

    if port_listening(args.bind, args.rpc_port):
        raise RuntimeError(f"RPC 端口 {args.bind}:{args.rpc_port} 已经被其它进程占用。")
    if port_listening(args.bind, args.ui_port):
        raise RuntimeError(f"UI 端口 {args.bind}:{args.ui_port} 已经被其它进程占用。")

    rpc_log = logs_dir(rt) / f"trace_processor_{args.rpc_port}.log"
    ui_log = logs_dir(rt) / f"ui_wrapper_{args.ui_port}.log"

    rpc_cmd = [
        sys.executable,
        str(bootstrap_path(rt)),
        "server",
        "http",
        "--ip-address",
        args.bind,
        "--port",
        str(args.rpc_port),
        str(trace),
    ]
    rpc_proc = start_process(rpc_cmd, clean_env(rt, use_env_proxy=args.use_env_proxy), rpc_log)
    instance = {
        "trace": str(trace),
        "trace_title": display_name,
        "bind": args.bind,
        "rpc_port": args.rpc_port,
        "ui_port": args.ui_port,
        "rpc_pid": rpc_proc.pid,
        "rpc_pid_start_ticks": process_start_ticks(rpc_proc.pid),
        "ui_pid": None,
        "ui_pid_start_ticks": None,
        "perfetto_version": version,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "logs": {"trace_processor": str(rpc_log), "ui_wrapper": str(ui_log)},
    }
    instances[instance_key] = instance
    write_instances(rt, instances)

    print(f"正在把 trace 加载到服务器侧 TraceProcessor：{args.bind}:{args.rpc_port}")
    try:
        wait_for_status(args.bind, args.rpc_port, args.load_timeout, "TraceProcessor")
    except Exception:
        print(tail_file(rpc_log), file=sys.stderr)
        stop_instance(instance)
        instances.pop(instance_key, None)
        write_instances(rt, instances)
        raise

    ui_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "serve-ui",
        "--bind",
        args.bind,
        "--ui-port",
        str(args.ui_port),
        "--rpc-host",
        args.bind,
        "--rpc-port",
        str(args.rpc_port),
        "--cache-dir",
        str(cache_dir(rt)),
        "--version",
        version,
        "--trace-title",
        display_name,
    ]
    if args.use_env_proxy:
        ui_cmd.append("--use-env-proxy")
    ui_proc = start_process(ui_cmd, os.environ.copy(), ui_log)
    instance["ui_pid"] = ui_proc.pid
    instance["ui_pid_start_ticks"] = process_start_ticks(ui_proc.pid)
    instances[instance_key] = instance
    write_instances(rt, instances)

    try:
        wait_for_health(args.bind, args.ui_port, 20)
        wait_for_status(args.bind, args.ui_port, 20, "UI wrapper")
        ws_status = websocket_handshake(args.bind, args.ui_port)
        if "101" not in ws_status:
            raise RuntimeError(f"UI wrapper WebSocket 检查失败：{ws_status}")
    except Exception:
        print(tail_file(ui_log), file=sys.stderr)
        stop_instance(instance)
        instances.pop(instance_key, None)
        write_instances(rt, instances)
        raise

    ui_url = proxy_url_for_port(args.ui_port)
    open_url = ui_url or viewer_url(f"http://{args.bind}:{args.ui_port}")
    port_mode = "自动选择" if port_notes else "用户指定"

    print("")
    print_box(
        "Perfetto trace 已就绪",
        [
            "✅ 服务已启动，浏览器打开后会自动加载 trace",
            f"Trace  {shorten_middle(display_name)}",
            f"端口   {port_mode}  UI={args.ui_port}  RPC={args.rpc_port}",
            f"UI     {args.bind}:{args.ui_port}  pid={ui_proc.pid}",
            f"RPC    {args.bind}:{args.rpc_port}  pid={rpc_proc.pid}",
            f"日志   {shorten_middle(str(ui_log))}",
        ],
    )
    print(f"打开链接：{open_url}")


def print_status(args: argparse.Namespace) -> None:
    rt = runtime_dir(args)
    instances = selected_instances(load_visible_instances(rt), getattr(args, "ui_port", None))
    if not instances:
        if getattr(args, "ui_port", None) is None:
            print("当前没有由本工具管理的 Perfetto trace 查看服务。")
        else:
            print(f"没有找到 UI 端口 {args.ui_port} 对应的服务。")
        return
    print(f"默认运行/缓存目录：{rt}")
    print(f"服务数量：{len(instances)}")
    for key, state in sorted(instances.items(), key=lambda item: int(item[0])):
        rpc_ok = pid_alive(state.get("rpc_pid"), state.get("rpc_pid_start_ticks"))
        ui_ok = pid_alive(state.get("ui_pid"), state.get("ui_pid_start_ticks"))
        print("")
        print(f"[UI 端口 {key}]")
        print(f"  trace：   {state.get('trace')}")
        if state.get("trace_title"):
            print(f"  名称：    {state.get('trace_title')}")
        print(f"  UI：      {state.get('bind')}:{state.get('ui_port')} pid={state.get('ui_pid')} 存活={ui_ok}")
        print(f"  RPC：     {state.get('bind')}:{state.get('rpc_port')} pid={state.get('rpc_pid')} 存活={rpc_ok}")
        print(f"  版本：    {state.get('perfetto_version')}")
        print(f"  启动时间：{state.get('started_at')}")
        if state.get("runtime_dir"):
            print(f"  实例目录：{state.get('runtime_dir')}")
        ui_url = proxy_url_for_port(int(state.get("ui_port")))
        if ui_url:
            print(f"  跳转链接：{ui_url}")
        for name, path in state.get("logs", {}).items():
            print(f"  日志 {name}：{path}")
    if args.json:
        enriched = {}
        for key, state in instances.items():
            enriched[key] = {
                **persistent_instance(state),
                "ui_alive": pid_alive(state.get("ui_pid"), state.get("ui_pid_start_ticks")),
                "rpc_alive": pid_alive(state.get("rpc_pid"), state.get("rpc_pid_start_ticks")),
            }
        print(json.dumps({"instances": enriched}, indent=2, sort_keys=True, ensure_ascii=False))


def show_logs(args: argparse.Namespace) -> None:
    rt = runtime_dir(args)
    instances = selected_instances(load_visible_instances(rt), getattr(args, "ui_port", None))
    if not instances:
        if getattr(args, "ui_port", None) is None:
            print("当前没有由本工具管理的 Perfetto trace 查看服务。")
        else:
            print(f"没有找到 UI 端口 {args.ui_port} 对应的服务。")
        return
    for key, state in sorted(instances.items(), key=lambda item: int(item[0])):
        logs = state.get("logs", {})
        selected = logs.values() if args.which == "all" else [logs.get(args.which)]
        for path_str in selected:
            if not path_str:
                continue
            path = Path(path_str)
            print(f"==> UI 端口 {key} / {path} <==")
            print(tail_file(path, lines=args.lines))


def check(args: argparse.Namespace) -> None:
    version = ensure_installed(args)
    print(f"[ok] trace_processor 已安装：{version}")
    rt = runtime_dir(args)
    instances = selected_instances(load_visible_instances(rt), getattr(args, "ui_port", None))
    if not instances:
        if getattr(args, "ui_port", None) is None:
            print("[info] 当前没有运行中的服务状态。")
        else:
            print(f"[info] 没有找到 UI 端口 {args.ui_port} 对应的服务。")
        return
    for key, state in sorted(instances.items(), key=lambda item: int(item[0])):
        bind = state.get("bind", DEFAULT_BIND)
        ui_port = int(state.get("ui_port", DEFAULT_UI_PORT))
        rpc_port = int(state.get("rpc_port", DEFAULT_RPC_PORT))
        print("")
        print(f"[检查 UI 端口 {key}]")
        print(f"[info] trace：{state.get('trace')}")
        print(f"[info] RPC 进程存活：{pid_alive(state.get('rpc_pid'), state.get('rpc_pid_start_ticks'))}")
        print(f"[info] UI 进程存活：{pid_alive(state.get('ui_pid'), state.get('ui_pid_start_ticks'))}")
        try:
            status, _, data = http_request(bind, ui_port, "GET", "/__wrapper_health", timeout=3)
            print(f"[ok] UI 健康检查：HTTP {status}, {data.decode(errors='replace').strip()}")
        except OSError as exc:
            print(f"[fail] UI 健康检查：{exc}")
        try:
            status, _, data = http_request(bind, ui_port, "POST", "/status", timeout=5, origin="https://example.invalid")
            print(f"[ok] wrapper /status：HTTP {status}, {len(data)} bytes")
        except OSError as exc:
            print(f"[fail] wrapper /status：{exc}")
        try:
            status, _, data = http_request(bind, rpc_port, "POST", "/status", timeout=5, origin=UPSTREAM_UI_ORIGIN)
            print(f"[ok] RPC /status：HTTP {status}, {len(data)} bytes")
        except OSError as exc:
            print(f"[fail] RPC /status：{exc}")
        try:
            ws_status = websocket_handshake(bind, ui_port)
            print(f"[ok] wrapper /websocket：{ws_status}")
        except OSError as exc:
            print(f"[fail] wrapper /websocket：{exc}")


def content_type_for(path: str) -> str:
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    if path.endswith(".wasm"):
        return "application/wasm"
    if path.endswith(".woff2"):
        return "font/woff2"
    if path.endswith(".html") or path == "/":
        return "text/html; charset=utf-8"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def safe_cache_path(cache_root: Path, request_path: str) -> Path:
    parsed = urllib.parse.urlparse(request_path)
    path = parsed.path or "/"
    if path == "/":
        rel = "index.html"
    else:
        rel = posixpath.normpath(path).lstrip("/")
        if rel.startswith("../") or rel == "..":
            raise ValueError(f"不安全的请求路径：{request_path}")
    return cache_root / rel


def patch_index_html(data: bytes, version: str) -> bytes:
    text = data.decode("utf-8")
    version_json = json.dumps({"stable": version, "canary": version, "autopush": version})
    text = re.sub(r"data-perfetto_version='[^']*'", f"data-perfetto_version='{version_json}'", text)
    return text.encode("utf-8")


def patch_frontend_bundle(data: bytes, trace_title: str | None = None) -> bytes:
    text = data.decode("utf-8")
    replacements = {
        "const wsUrl = `ws://${_HttpRpcEngine.hostAndPort}/websocket`;":
            'const wsUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${_HttpRpcEngine.hostAndPort}/websocket`;',
        "const RPC_URL = `http://${_HttpRpcEngine.hostAndPort}/`;":
            "const RPC_URL = `${location.protocol}//${_HttpRpcEngine.hostAndPort}/`;",
        "return `127.0.0.1:${_HttpRpcEngine.rpcPort}`;":
            'return location.host + location.pathname.replace(/\\/$/, "");',
        '"https:",\n        // Allow any HTTPS; service worker firewall adds granular filtering.':
            '"https:",\n        "wss:",\n        "ws:",\n        // Allow any HTTPS; service worker firewall adds granular filtering.',
        "AppImpl.instance.serviceWorkerController.install();":
            "/* service worker disabled by perfetto-trace-viewer */;",
        "const result = await showDialogToUsePreloadedTrace(tpStatus);":
            'const result = "useRpcWithPreloadedTrace";',
        "traceTitle = `RPC @ ${HttpRpcEngine.hostAndPort}`;":
            f"traceTitle = {json.dumps(trace_title or '')} || `RPC @ ${{HttpRpcEngine.hostAndPort}}`;",
    }
    missing = []
    for old, new in replacements.items():
        if old not in text:
            missing.append(old)
        else:
            text = text.replace(old, new, 1)
    if missing:
        raise RuntimeError("找不到预期的 Perfetto UI patch 锚点：" + ", ".join(missing))
    return text.encode("utf-8")


class PerfettoUiProxy(BaseHTTPRequestHandler):
    server_version = "PerfettoUiProxy/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        rpc_path = self.rpc_path(parsed.path)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.proxy_websocket(rpc_path)
            return
        if parsed.path == "/__wrapper_health":
            self.send_bytes(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if rpc_path == "/status":
            self.proxy_http("/status" + (f"?{parsed.query}" if parsed.query else ""))
            return
        self.serve_ui_asset()

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        status = 200 if parsed.path == "/__wrapper_health" or not self.rpc_path(parsed.path) else 405
        self.send_response(status)
        self.send_header("Content-Type", content_type_for(parsed.path or "/"))
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        rpc_path = self.rpc_path(parsed.path)
        if rpc_path:
            self.proxy_http(rpc_path + (f"?{parsed.query}" if parsed.query else ""))
            return
        self.send_error(404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.end_headers()

    @staticmethod
    def rpc_path(path: str) -> str | None:
        if path == "/status" or path.endswith("/status"):
            return "/status"
        if path == "/websocket" or path.endswith("/websocket"):
            return "/websocket"
        return None

    def send_bytes(self, status: int, data: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        no_store = self.path.endswith("frontend_bundle.js") or content_type.startswith("text/html")
        self.send_header("Cache-Control", "no-store" if no_store else "public, max-age=3600")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def serve_ui_asset(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        try:
            data = self.get_or_fetch_asset(self.path)
            if path == "/" or path.endswith("/index.html"):
                data = patch_index_html(data, self.server.ui_version)
            if path.endswith("/frontend_bundle.js"):
                data = patch_frontend_bundle(data, self.server.trace_title)
        except Exception as exc:
            message = f"提供 Perfetto UI 资源失败：{self.path}: {exc}\n".encode()
            self.send_bytes(502, message, "text/plain; charset=utf-8")
            return
        self.send_bytes(200, data, content_type_for(path))

    def get_or_fetch_asset(self, request_path: str) -> bytes:
        cache_path, _ = fetch_ui_asset(
            self.server.cache_dir,
            request_path,
            self.server.user_agent,
            use_env_proxy=self.server.use_env_proxy,
        )
        return cache_path.read_bytes()

    def proxy_http(self, backend_path: str) -> None:
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(body_len) if body_len else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"host", "origin"}
        }
        headers["Host"] = f"{self.server.rpc_host}:{self.server.rpc_port}"
        headers["Origin"] = UPSTREAM_UI_ORIGIN

        conn = http.client.HTTPConnection(self.server.rpc_host, self.server.rpc_port, timeout=120)
        try:
            conn.request(self.command, backend_path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP_HEADERS and not key.lower().startswith("access-control-"):
                    self.send_header(key, value)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            message = f"代理 TraceProcessor RPC 请求失败：{exc}\n".encode()
            self.send_bytes(502, message, "text/plain; charset=utf-8")
        finally:
            conn.close()

    def proxy_websocket(self, rpc_path: str | None) -> None:
        if rpc_path != "/websocket":
            self.send_error(404)
            return
        client_key = self.headers.get("Sec-WebSocket-Key")
        if not client_key:
            self.send_error(400, "缺少 Sec-WebSocket-Key")
            return
        try:
            backend = self.open_backend_websocket()
        except Exception as exc:
            message = f"连接 TraceProcessor WebSocket 失败：{exc}\n".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
            return

        accept = base64.b64encode(
            hashlib.sha1((client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.tunnel_sockets(self.connection, backend)
        finally:
            try:
                backend.close()
            except OSError:
                pass

    def open_backend_websocket(self) -> socket.socket:
        sock = socket.create_connection((self.server.rpc_host, self.server.rpc_port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET /websocket HTTP/1.1\r\n"
            f"Host: {self.server.rpc_host}:{self.server.rpc_port}\r\n"
            f"Origin: {UPSTREAM_UI_ORIGIN}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
        sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("后端 WebSocket 在握手期间关闭")
            response += chunk
            if len(response) > 65536:
                raise RuntimeError("后端 WebSocket 握手响应过大")
        if not response.startswith(b"HTTP/1.1 101") and not response.startswith(b"HTTP/1.0 101"):
            raise RuntimeError(response.split(b"\r\n", 1)[0].decode(errors="replace"))
        return sock

    @staticmethod
    def tunnel_sockets(client: socket.socket, backend: socket.socket) -> None:
        sockets = [client, backend]
        for sock in sockets:
            sock.setblocking(False)
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 60)
            if exceptional:
                return
            for src in readable:
                try:
                    data = src.recv(65536)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                dst = backend if src is client else client
                try:
                    dst.sendall(data)
                except OSError:
                    return


def serve_ui(args: argparse.Namespace) -> None:
    cache_root = Path(args.cache_dir).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.bind, args.ui_port), PerfettoUiProxy)
    server.rpc_host = args.rpc_host
    server.rpc_port = args.rpc_port
    server.cache_dir = cache_root
    server.ui_version = args.version
    server.user_agent = f"perfetto-trace-viewer/{args.version}"
    server.use_env_proxy = args.use_env_proxy
    server.trace_title = args.trace_title
    print(
        f"正在提供 Perfetto UI：http://{args.bind}:{args.ui_port}；"
        f"后端 TraceProcessor RPC：{args.rpc_host}:{args.rpc_port}",
        flush=True,
    )
    server.serve_forever()


def add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出。")
    parser._positionals.title = "位置参数"
    parser._optionals.title = "选项"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过服务器侧 UI wrapper 打开 Perfetto / PyTorch profiler trace。",
        add_help=False,
    )
    add_help(parser)
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        title="命令",
        metavar="{install,open,stop,status,logs,check}",
    )

    install_p = sub.add_parser("install", help="下载并验证 Perfetto trace_processor。", add_help=False)
    add_help(install_p)
    install_p.add_argument("--force", action="store_true", help="重新下载官方 bootstrap 脚本。")
    install_p.add_argument("--skip-ui-cache", action="store_true", help="只安装 trace_processor，不预取 Perfetto UI 静态资源。")
    install_p.add_argument("--use-env-proxy", action="store_true", help="下载时使用当前环境中的代理变量。")
    install_p.set_defaults(func=install_perfetto)

    open_p = sub.add_parser("open", help="打开一个 trace 服务；不同 trace 使用不同端口即可同时查看。", add_help=False)
    add_help(open_p)
    open_p.add_argument("trace", help="trace 文件路径，例如 .pt.trace.json、.trace.json 或 .pftrace。")
    open_p.add_argument("--bind", default=DEFAULT_BIND, help="监听地址。默认：127.0.0.1。")
    open_p.add_argument("--rpc-port", type=int, help="内部 TraceProcessor RPC 端口。默认自动从 9001 起扫描。")
    open_p.add_argument("--ui-port", type=int, help="浏览器应打开/转发的 UI 端口。默认自动从 19002 起扫描。")
    open_p.add_argument("--load-timeout", type=float, default=300, help="等待 TraceProcessor 加载 trace 的秒数。")
    open_p.add_argument("--force", action="store_true", help="如需安装，强制重新下载官方 bootstrap 脚本。")
    open_p.add_argument("--use-env-proxy", action="store_true", help="安装时使用当前环境中的代理变量。")
    open_p.set_defaults(func=open_trace)

    stop_p = sub.add_parser("stop", help="停止服务。不指定 --ui-port 时停止本工具管理的全部服务。", add_help=False)
    add_help(stop_p)
    stop_p.add_argument("--ui-port", type=int, help="只停止指定 UI 端口对应的服务。")
    stop_p.set_defaults(func=stop_services)

    status_p = sub.add_parser("status", help="查看服务状态。", add_help=False)
    add_help(status_p)
    status_p.add_argument("--ui-port", type=int, help="只查看指定 UI 端口。")
    status_p.add_argument("--json", action="store_true")
    status_p.set_defaults(func=print_status)

    logs_p = sub.add_parser("logs", help="查看最近日志。", add_help=False)
    add_help(logs_p)
    logs_p.add_argument("--ui-port", type=int, help="只查看指定 UI 端口对应服务的日志。")
    logs_p.add_argument("which", nargs="?", default="all", choices=["all", "trace_processor", "ui_wrapper"])
    logs_p.add_argument("-n", "--lines", type=int, default=80)
    logs_p.set_defaults(func=show_logs)

    check_p = sub.add_parser("check", help="检查安装和运行中的服务。", add_help=False)
    add_help(check_p)
    check_p.add_argument("--ui-port", type=int, help="只检查指定 UI 端口。")
    check_p.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    check_p.add_argument("--use-env-proxy", action="store_true", help="安装时使用当前环境中的代理变量。")
    check_p.set_defaults(func=check)

    return parser


def build_serve_ui_parser() -> argparse.ArgumentParser:
    serve_p = argparse.ArgumentParser(description="内部 UI wrapper 服务。", add_help=False)
    serve_p.add_argument("--bind", required=True)
    serve_p.add_argument("--ui-port", type=int, required=True)
    serve_p.add_argument("--rpc-host", required=True)
    serve_p.add_argument("--rpc-port", type=int, required=True)
    serve_p.add_argument("--cache-dir", required=True)
    serve_p.add_argument("--version", default=DEFAULT_UI_VERSION)
    serve_p.add_argument("--trace-title", default="")
    serve_p.add_argument("--use-env-proxy", action="store_true")
    serve_p.set_defaults(func=serve_ui)
    return serve_p


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "serve-ui":
        parser = build_serve_ui_parser()
        args = parser.parse_args(sys.argv[2:])
    else:
        parser = build_parser()
        args = parser.parse_args()
    try:
        result = args.func(args)
        if result is None:
            return 0
        if isinstance(result, int):
            return result
        return 0
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
