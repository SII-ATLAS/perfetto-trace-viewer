---
name: perfetto-trace-viewer
description: 在远程服务器上直接查看 Perfetto / PyTorch profiler trace 的工具 skill。适用于用户需要打开大型 .pt.trace.json、.trace.json、.pftrace 文件，但不想下载到本机、不想手动配置 Perfetto localhost RPC，或正在使用 VS Code Web / Notebook 端口转发环境时。
---

# Perfetto Trace Viewer

用这个 skill 在服务器侧打开大型 profiler trace。脚本会启动两层服务：

- 服务器侧 Perfetto `trace_processor`：直接读取 trace 文件；
- 同源 Perfetto UI wrapper：用户打开转发后的 UI 端口即可看到 Perfetto UI 和已加载 trace。

wrapper 是外挂适配层，不修改 Perfetto 官方脚本、包或下载的二进制。

## 快速使用

从任意位置运行，把 `<skill-dir>` 替换成 `perfetto-trace-viewer` skill 所在目录：

```bash
<skill-dir>/scripts/perfetto-trace open /path/to/trace.pt.trace.json
```

命令结束后，打开输出里提示的 UI 端口。不要打开 RPC 端口，它只给内部 TraceProcessor 使用。
命令不指定端口时会从 `19002/9001` 起自动扫描下一组可用端口，并在输出里明确打印实际采用的 UI/RPC 端口。
如果当前环境提供 `VSCODE_PROXY_URI`，`open` 和 `status` 会直接打印可点击的 Perfetto 跳转链接。

同时打开多个 trace：直接多次运行 `open`，脚本会自动避开已经运行的服务。

```bash
<skill-dir>/scripts/perfetto-trace open /path/to/a.pt.trace.json
<skill-dir>/scripts/perfetto-trace open /path/to/b.pt.trace.json
```

需要固定端口时仍可显式指定：

```bash
<skill-dir>/scripts/perfetto-trace open /path/to/a.pt.trace.json --ui-port 19012 --rpc-port 9012
```

停止服务：

```bash
# 停止全部由本工具管理的服务
<skill-dir>/scripts/perfetto-trace stop

# 只停止某个 UI 端口
<skill-dir>/scripts/perfetto-trace stop --ui-port 19002
```

查看状态、检查连通性、看日志：

```bash
<skill-dir>/scripts/perfetto-trace status
<skill-dir>/scripts/perfetto-trace check
<skill-dir>/scripts/perfetto-trace logs

<skill-dir>/scripts/perfetto-trace status --ui-port 19002
<skill-dir>/scripts/perfetto-trace check --ui-port 19002
<skill-dir>/scripts/perfetto-trace logs --ui-port 19002
```

## Agent 使用流程

用户要求打开 trace 时：

1. 运行 `scripts/perfetto-trace open <trace>`。
2. 等命令打印“Perfetto trace 查看服务已就绪”。
3. 优先告诉用户点击输出中的“跳转链接”；如果没有链接，再告诉用户打开输出中的 UI 端口。未指定端口时必须转述脚本自动选择的 UI 端口。
4. 用户反馈空白/未加载 trace 时，运行：

```bash
scripts/perfetto-trace check --ui-port <port>
scripts/perfetto-trace logs --ui-port <port>
```

如果用户明确要求固定端口，或平台端口策略需要固定端口，使用：

```bash
<skill-dir>/scripts/perfetto-trace open /path/to/trace.pt.trace.json \
  --ui-port 19012 \
  --rpc-port 9012
```

## 脚本做了什么

- 首次使用时下载 Perfetto 官方 `trace_processor` bootstrapper。
- 固定把运行文件、缓存、日志放在 `<skill-dir>/.runtime/`。
- `open` 未指定端口时，从 `19002/9001` 起配对递增扫描，跳过已监听端口和本工具 state 中仍存活的服务。
- 如果环境变量 `VSCODE_PROXY_URI` 存在，自动把其中的 `{{port}}` 替换成 UI 端口，并输出形如 `<proxy-url>/#!/viewer?local_cache_key` 的浏览器跳转链接。
- `status/check/logs/stop` 会读取 `<skill-dir>/.runtime/state.json`，同时从当前进程表补充发现仍在运行的历史实例，避免 state 缺失或旧 runtime 导致状态为空。
- 用 `location.host + location.pathname` 动态适配 VS Code Web / Notebook 的 `/proxy/<port>/` 路径，不依赖具体机器或 URL。
- 打开 trace 后，Perfetto 页面标题和 trace 标识显示 trace 文件名，而不是默认的 `RPC @ <proxy-host>`。
- 把后端 RPC `Origin` 归一化为 TraceProcessor 接受的官方来源，避免浏览器代理域名触发 CORS 拒绝。
- 用 PID/state/log 文件管理后台进程，不依赖 `tmux`。`state.json` 只是服务索引；新启动的服务会记录 PID 和 Linux 进程启动 tick，降低 PID 复用误判风险。`status` 会自动清理已经退出的旧记录，`check` 会进一步验证 HTTP 和 WebSocket 是否真的可用。
- `install` 会缓存官方 bootstrap、平台 `trace_processor` 二进制和 Perfetto UI 核心静态资源；后续 `open` 复用本地缓存，缺失 UI 资源才按需下载。

## 依赖

必须：

- Python 3.9+；
- 首次安装时 CPU 侧有网络；
- `curl`，因为 Perfetto 官方 bootstrapper 会用它拉取平台二进制。

可选：

- 如果网络必须走环境代理，给 `install` 或 `open` 加 `--use-env-proxy`。

只安装/验证：

```bash
<skill-dir>/scripts/perfetto-trace install
<skill-dir>/scripts/perfetto-trace check
```

## 安全约定

默认监听 `127.0.0.1`，适合 VS Code / SSH / Notebook 端口转发。wrapper 和 TraceProcessor RPC 没有认证；只有在可信网络内才考虑 `--bind 0.0.0.0`。
