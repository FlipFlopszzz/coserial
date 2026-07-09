# coserial

[简体中文](README.zh-CN.md) | English

**Collaborative Serial Debug Bridge.** Humans and AI Agents co-control serial ports via MCP tools or Web UI, sharing all IO data in real time. Humans, Agents, and hardware — all on the same page.

## Architecture

```
Agent A ── MCP stdio ── coserial-client ──┐
Agent B ── MCP stdio ── coserial-client ──┤
Agent C ── MCP stdio ── coserial-client ──┤
...                                       │ HTTP (localhost)
Human A ── Web UI ────────────────────────┤
Human B ── Web UI ────────────────────────┤
...                                       ▼
                         coserial-server
                         ├── HTTP API (/mcp/…)     ← MCP tool calls
                         ├── WebSocket /ws         ← Web UI real-time data
                         ├── GET /                 ← Web UI (HTML/CSS/JS)
                         ├── SessionManager
                         │   ├── COM1
                         │   ├── COM2
                         │   └── ...
                         └── Web UI Monitor
```

## Features

- 🤝 **Human-Agent Collaboration** — Humans and AI Agents share the same serial session, debugging in real time
- 🔌 **MCP Tool Control** — Agents read/write serial ports, wait for pattern matches, send commands, etc. via standard MCP protocol
- 🖥️ **Web UI Monitor** — Dark theme, real-time RX/TX data stream, HEX display, search highlighting, log export
- 🔄 **Three-Party Data Sync** — Agent I/O and Web UI I/O write to the same buffer — nobody misses anything
- 📡 **Multi-Session Management** — Connect to multiple serial ports simultaneously, switch via Web UI dropdown
- 🔧 **Signal Line Control** — DTR/RTS hardware reset support
- 📝 **Log Persistence** — Serial I/O automatically recorded to file
- 🎯 **Zero-Build Frontend** — Vanilla HTML/CSS/JS, no frontend toolchain required

## Quick Start

### Install

```bash
# Clone the repo
git clone https://github.com/FlipFlopszzz/coserial.git
cd coserial

# Install dependencies (requires uv)
uv sync
```

Or with pip:

```bash
pip install -e .
```

### Register with Claude Code

After installation, register coserial as an MCP server so Claude Code can use it:

```bash
# Global (recommended — available in all projects)
uv run coserial init --global

# Or project-level (only in a specific directory)
cd /path/to/your-project
uv run coserial init
```

`init --global` uses `claude mcp add --scope user` to write to `~/.claude.json`.

`init` (project-level) creates `.mcp.json` + `.claude/launch.json` in the target directory, enabling both MCP tools and Claude Desktop Preview.

### Launch

```bash
# Start server + open Web UI (manual debug mode)
uv run coserial

# Start and auto-connect to COM20
uv run coserial COM20

# List all active sessions
uv run coserial list

# Start server only (headless)
uv run coserial-server
```

### Use in Claude Code

Once registered, call MCP tools directly in any Claude Code session:

```
server()                          → Start/discover server process
open_session(port="COM20")        → Open serial port
preview(port=37210)               → Get Preview URL
preview_start("coserial-web-ui")  → Open Web UI in Claude Desktop
command(session_id, "AT+GMR", expect="OK", newline=True)  → Send & wait
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `server()` | Discover/start server process, returns port |
| `open_session(port, baud)` | Create a serial session, returns session_id |
| `close_session(session_id)` | Close a specific session |
| `write(session_id, data)` | Send data to serial port |
| `read(session_id, timeout, size)` | Read buffer data |
| `wait_for(session_id, pattern, timeout)` | Wait for output matching a regex |
| `command(session_id, data, expect, timeout)` | Send command and wait for response |
| `set_params(session_id, baud, ...)` | Dynamically adjust serial parameters |
| `get_params(session_id)` | Get full session parameters |
| `set_signals(session_id, dtr, rts)` | Control DTR/RTS signal lines |
| `list_ports()` | List available serial ports |
| `list_sessions()` | List all active sessions |
| `web(port)` | Open Web UI in system browser |
| `preview(port)` | Return URL for Agent's embedded browser |
| `shutdown_server()` | Shut down server process |

## Web UI

Open `http://127.0.0.1:37210/` directly in your system browser, or via any Agent tool's embedded browser.

## Project Structure

```
coserial/
├── pyproject.toml
├── src/coserial/
│   ├── __main__.py       # Entry: uv run coserial
│   ├── client.py         # MCP stdio client → HTTP proxy to server
│   ├── server.py         # Server: HTTP API + WebSocket + Web UI
│   ├── session.py        # SessionBuffer + SerialSession + SessionManager
│   └── web_ui/
│       └── index.html    # Single-page HTML, inline CSS/JS
```

## Dependencies

- Python >= 3.11
- [pyserial](https://pypi.org/project/pyserial/) — Serial communication
- [mcp](https://pypi.org/project/mcp/) — MCP SDK (FastMCP)
- [websockets](https://pypi.org/project/websockets/) — WebSocket server
- [aiohttp](https://pypi.org/project/aiohttp/) — HTTP server
- [pywin32](https://pypi.org/project/pywin32/) — Windows process management
- Icons from [IconPark](https://iconpark.oceanengine.com/)

## License

[MIT License](LICENSE)
