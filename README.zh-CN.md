# coserial

简体中文 | [English](README.md)

**协作式串口调试桥。** 人和 AI Agent 通过 MCP 工具或 Web UI 共同操控串口，实时共享所有 IO 数据。人、Agent、硬件三端数据互通。

## 架构

```
Agent A ── MCP stdio ── coserial-client ──┐
Agent B ── MCP stdio ── coserial-client ──┤
Agent C ── MCP stdio ── coserial-client ──┤
...                                       │ HTTP (localhost)
人 A ──── Web UI ─────────────────────────┤
人 B ──── Web UI ─────────────────────────┤
...                                       ▼
                         coserial-server
                         ├── HTTP API (/mcp/…)     ← MCP 工具调用
                         ├── WebSocket /ws         ← Web UI 实时数据
                         ├── GET /                 ← Web UI (HTML/CSS/JS)
                         ├── SessionManager
                         │   ├── COM1
                         │   ├── COM2
                         │   └── ...
                         └── Web UI Monitor
```

## 功能特性

- 🤝 **人机协作** — 人类和 AI Agent 共享同一串口会话，实时协同调试
- 🔌 **MCP 工具操控** — Agent 通过标准 MCP 协议读写串口、等待匹配、发命令等
- 🖥️ **Web UI 监控面板** — 深色主题，实时 RX/TX 数据流，支持 HEX 显示、搜索高亮、日志导出
- 🔄 **三方数据互通** — Agent 收发、Web UI 收发写入同一缓冲区，任何一方都不错过
- 📡 **多 Session 管理** — 同时连接多个串口，Web UI 下拉切换
- 🔧 **信号线控制** — DTR/RTS 硬件复位支持
- 📝 **日志持久化** — 串口 IO 自动记录到文件
- 🎯 **零构建前端** — 原生 HTML/CSS/JS，无需任何前端工具链

## 快速开始

### 安装

```bash
# 克隆仓库
git clone https://github.com/FlipFlopszzz/coserial.git
cd coserial

# 安装依赖（需要 uv）
uv sync
```

或使用 pip：

```bash
pip install -e .
```

### 注册到 Claude Code

安装后，将 coserial 注册为 MCP 服务器，Claude Code 即可使用：

```bash
# 全局注册（推荐 — 所有项目可用）
uv run coserial init --global

# 或项目级注册（仅在指定目录生效）
cd /path/to/your-project
uv run coserial init
```

`init --global` 通过 `claude mcp add --scope user` 写入 `~/.claude.json`。

`init`（项目级）在目标目录创建 `.mcp.json` + `.claude/launch.json`，同时启用 MCP 工具和 Claude Desktop Preview。

### 启动

```bash
# 启动 server + 打开 Web UI（手动调试模式）
uv run coserial

# 启动并自动连接 COM20
uv run coserial COM20

# 查看所有活跃 session
uv run coserial list

# 只启动 server（headless）
uv run coserial-server
```

### 在 Claude Code 中使用

注册后，在任意 Claude Code 会话中直接调用 MCP 工具：

```
server()                          → 启动/发现 server 进程
open_session(port="COM20")        → 打开串口
preview(port=37210)               → 获取 Preview URL
preview_start("coserial-web-ui")  → 在 Claude Desktop 内嵌查看 Web UI
command(session_id, "AT+GMR", expect="OK", newline=True)  → 发命令等响应
```

## MCP 工具

| 工具 | 用途 |
|------|------|
| `server()` | 发现/启动 server 进程，返回端口 |
| `open_session(port, baud)` | 创建串口 session，返回 session_id |
| `close_session(session_id)` | 关闭指定 session |
| `write(session_id, data)` | 向串口发送数据 |
| `read(session_id, timeout, size)` | 读取缓冲区数据 |
| `wait_for(session_id, pattern, timeout)` | 等待输出匹配正则 |
| `command(session_id, data, expect, timeout)` | 发命令并等待响应 |
| `set_params(session_id, baud, ...)` | 动态调整串口参数 |
| `get_params(session_id)` | 获取会话完整参数 |
| `set_signals(session_id, dtr, rts)` | 控制 DTR/RTS 信号线 |
| `list_ports()` | 列出系统可用串口 |
| `list_sessions()` | 列出所有活跃 session |
| `web(port)` | 在系统浏览器中打开 Web UI |
| `preview(port)` | 返回 URL 供 Claude Desktop Preview 打开 |
| `shutdown_server()` | 关闭 server 进程 |

## Web UI

直接在系统浏览器访问 `http://127.0.0.1:37210/`，或通过各 Agent 工具的内嵌浏览器打开。

## 项目结构

```
coserial/
├── pyproject.toml
├── src/coserial/
│   ├── __main__.py       # 入口: uv run coserial
│   ├── client.py         # MCP stdio client → HTTP 转发到 server
│   ├── server.py         # Server: HTTP API + WebSocket + Web UI
│   ├── session.py        # SessionBuffer + SerialSession + SessionManager
│   └── web_ui/
│       └── index.html    # 单页 HTML，内联 CSS/JS
```

## 依赖

- Python >= 3.11
- [pyserial](https://pypi.org/project/pyserial/) — 串口通信
- [mcp](https://pypi.org/project/mcp/) — MCP SDK (FastMCP)
- [websockets](https://pypi.org/project/websockets/) — WebSocket 服务
- [aiohttp](https://pypi.org/project/aiohttp/) — HTTP 服务
- [pywin32](https://pypi.org/project/pywin32/) — Windows 进程管理
- 图标来自 [IconPark](https://iconpark.oceanengine.com/)

## 许可证

[MIT License](LICENSE)
