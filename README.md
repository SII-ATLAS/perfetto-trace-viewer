# Perfetto Trace Viewer

在远程服务器上直接查看 Perfetto / PyTorch profiler trace，不把大型 trace 下载到本机，也不手动配置 Perfetto 的 localhost RPC。

这个工具适合下面这种场景：

- trace 文件很大，例如 PyTorch profiler 生成的 `.pt.trace.json`、`.trace.json`、`.pftrace`；
- 人在浏览器里的 VS Code Web / Notebook 里操作远程服务器；
- 希望打开一个端口转发页面后，Perfetto UI 自动加载 trace；
- 希望同时打开多个 trace，用不同端口并排比较；
- 希望命令行直接打印可点击的转发链接。

## 工作原理

脚本启动两层服务：

- `trace_processor server http` 在服务器本机读取 trace 文件；
- 一个轻量 UI wrapper 提供 Perfetto UI，并把 UI 的 HTTP/WebSocket RPC 请求代理到本机 `trace_processor`。

wrapper 只在运行时适配 Perfetto UI，不修改官方脚本、包或下载的二进制。运行文件、缓存、日志和状态默认放在本 skill 目录下的 `.runtime/`。

## 依赖

必须：

- Linux；
- Python 3.9+；
- `curl`；
- 首次安装时服务器能访问 Perfetto 官方下载源。

可选：

- VS Code Web / Notebook 环境里的 `VSCODE_PROXY_URI`。如果存在，命令会自动打印可点击跳转链接。
- 如果服务器下载必须走环境代理，给 `install` 或 `open` 加 `--use-env-proxy`。

## 安装

把本目录放到任意位置，例如：

```bash
git clone <repo-url> ~/tools/perfetto-trace-viewer
cd ~/tools/perfetto-trace-viewer
```

预安装并缓存 Perfetto 组件：

```bash
scripts/perfetto-trace install
```

首次 `install` 会下载官方 `trace_processor` bootstrapper、平台二进制，以及 Perfetto UI 核心静态资源。后续会复用 `.runtime/` 缓存。

检查安装：

```bash
scripts/perfetto-trace check
```

## 全局命令

为了能在任意目录直接运行 `perfetto-trace`，推荐把 skill 的 `scripts/` 加到 `PATH`。

临时生效：

```bash
export PERFETTO_TRACE_VIEWER_DIR="$HOME/tools/perfetto-trace-viewer"
export PATH="$PERFETTO_TRACE_VIEWER_DIR/scripts:$PATH"
```

长期生效，写入 `~/.bashrc`：

```bash
cat >> ~/.bashrc <<'EOF'
export PERFETTO_TRACE_VIEWER_DIR="$HOME/tools/perfetto-trace-viewer"
export PATH="$PERFETTO_TRACE_VIEWER_DIR/scripts:$PATH"
EOF
source ~/.bashrc
```

确认命令可用：

```bash
command -v perfetto-trace
perfetto-trace --help
```

之后可以在任意目录运行：

```bash
perfetto-trace open /path/to/trace.pt.trace.json
```

## 基本用法

打开一个 trace：

```bash
perfetto-trace open /path/to/trace.pt.trace.json
```

不指定端口时，脚本从 `19002/9001` 起自动扫描下一组可用端口。命令完成后会打印：

- trace 文件路径；
- trace 显示名称；
- UI 端口；
- RPC 端口；
- 后台进程 PID；
- 日志路径；
- 可点击跳转链接，如果当前环境提供 `VSCODE_PROXY_URI`。

同时打开多个 trace：

```bash
perfetto-trace open /path/to/a.pt.trace.json
perfetto-trace open /path/to/b.pt.trace.json
```

固定端口打开：

```bash
perfetto-trace open /path/to/trace.pt.trace.json --ui-port 19012 --rpc-port 9012
```

查看状态：

```bash
perfetto-trace status
perfetto-trace status --ui-port 19002
```

检查连通性：

```bash
perfetto-trace check
perfetto-trace check --ui-port 19002
```

查看日志：

```bash
perfetto-trace logs
perfetto-trace logs --ui-port 19002
perfetto-trace logs --ui-port 19002 trace_processor
perfetto-trace logs --ui-port 19002 ui_wrapper
```

停止服务：

```bash
perfetto-trace stop --ui-port 19002
perfetto-trace stop
```

`stop` 不指定 `--ui-port` 时，会停止当前工具能发现的全部 trace 服务。

## VS Code Web / Notebook

在 VS Code Web / Notebook 里，平台通常会设置类似下面的环境变量：

```bash
VSCODE_PROXY_URI='https://example/proxy/{{port}}/'
```

如果它存在，`open` 和 `status` 会自动把 `{{port}}` 替换成 UI 端口，并输出类似：

```text
跳转链接：https://example/proxy/19002/#!/viewer?local_cache_key
```

点击这个链接后，Perfetto UI 应该直接打开并加载 trace。Perfetto 页面的 trace 标题会显示 trace 文件名，而不是默认的 `RPC @ <proxy-host>`。

## Agent 使用流程

当用户要求打开 trace：

1. 运行 `perfetto-trace open <trace-path>`。
2. 等输出出现 `Perfetto trace 查看服务已就绪。`。
3. 把输出中的 `跳转链接` 和 `UI 端口` 告诉用户。
4. 如果用户说页面空白或 trace 没加载，运行：

```bash
perfetto-trace check --ui-port <port>
perfetto-trace logs --ui-port <port>
```

当用户要求换一个 trace：

- 不需要 stop 旧 trace，直接再运行一次 `open`，脚本会自动选择下一组端口；
- 如果用户明确要替换同一个端口，用相同 `--ui-port` 和 `--rpc-port` 再运行 `open`。

当用户要求关闭：

```bash
perfetto-trace stop --ui-port <port>
```

## 状态与缓存

`.runtime/` 里主要包含：

- `bin/trace_processor`：官方 bootstrapper；
- `home/.local/share/perfetto/prebuilts/`：官方平台二进制缓存；
- `ui_cache/`：Perfetto UI 静态资源缓存；
- `logs/`：各端口日志；
- `state.json`：本工具管理的运行实例索引。

`state.json` 只是索引。`status/check/logs/stop` 还会扫描当前进程，补充发现仍在运行的旧实例，避免 state 文件滞后造成误判。

如果要清理缓存，先停止服务：

```bash
perfetto-trace stop
rm -rf .runtime
```

下次 `install` 或 `open` 会重新下载需要的组件。

## 常见问题

### 只能看到 `Perfetto Trace Processor RPC Server`

你打开的是 RPC 端口，不是 UI 端口。请打开命令输出里的 `UI 端口` 或 `跳转链接`。

### 页面打开了但没有 trace

先检查服务：

```bash
perfetto-trace check --ui-port <port>
```

再看日志：

```bash
perfetto-trace logs --ui-port <port>
```

常见原因是 RPC 端口没有起来、WebSocket 被代理拦截，或打开了错误端口。

### 端口冲突

默认 `open` 会自动避开已使用端口。需要手动指定时：

```bash
perfetto-trace open /path/to/trace.pt.trace.json --ui-port 19020 --rpc-port 9020
```

### 下载失败

如果服务器必须走代理：

```bash
perfetto-trace install --use-env-proxy
perfetto-trace open /path/to/trace.pt.trace.json --use-env-proxy
```

### `status` 看到旧实例

这是预期行为。工具会从进程表发现仍在运行的历史 wrapper / trace_processor。需要关闭时运行：

```bash
perfetto-trace stop --ui-port <port>
```

## 安全边界

默认只监听 `127.0.0.1`，适合通过 VS Code、SSH 或 Notebook 端口转发访问。UI wrapper 和 TraceProcessor RPC 没有认证，不建议在不可信网络上使用 `--bind 0.0.0.0`。

## 文件结构

```text
perfetto-trace-viewer/
├── README.md
├── SKILL.md
├── agents/
│   └── openai.yaml
└── scripts/
    ├── perfetto-trace
    └── perfetto_trace.py
```

作为 Codex skill 使用时，`SKILL.md` 是 agent 的最小上下文入口；作为 GitHub repo 阅读时，`README.md` 是人类和 agent 的完整使用说明。
