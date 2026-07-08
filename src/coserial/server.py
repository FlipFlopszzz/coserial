"""coserial-server: 全机唯一的串口服务进程。

持有 SessionManager 管理所有串口连接，通过 HTTP API 响应 MCP client 调用，
通过 WebSocket 向 Web UI 面板广播实时数据。"""

import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from aiohttp import web

from coserial.session import SessionManager, SerialSession


# ── Global state ─────────────────────────────────────────────────────

_sessions = SessionManager()
_ws_clients: set[web.WebSocketResponse] = set()
_loop: asyncio.AbstractEventLoop | None = None
_server_port: int | None = None
# _tui_process: no longer used (web UI has no subprocess)
_logging_enabled = True

# Web UI static file path (used by launch_with_web / serial_open_monitor)
WEB_UI_FILE = str(Path(__file__).resolve().parent / "web_ui" / "index.html")

# ── Session logging ───────────────────────────────────────────────────

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_log_lock = threading.Lock()
_log_paths: dict[str, Path] = {}  # session_id → log file path


def _log_file_path(session_id: str, port: str) -> Path:
    """Get or create the log file path for a session."""
    if session_id not in _log_paths:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_id = session_id.replace(":", "_").replace("/", "_")
        port_label = port.upper() if port else "UNKNOWN"
        filename = f"{port_label}-{safe_id[:8]}-{ts}.log"
        _log_paths[session_id] = LOG_DIR / filename
    return _log_paths[session_id]


def _remove_log_path(session_id: str):
    _log_paths.pop(session_id, None)


def _write_log(session_id: str, port: str, text: str):
    """Append one line to the session log file. Thread-safe."""
    if not _logging_enabled:
        return
    with _log_lock:
        try:
            path = _log_file_path(session_id, port)
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass


# ── Port discovery ───────────────────────────────────────────────────

def _port_file() -> Path:
    appdata = os.environ.get("LOCALAPPDATA", "")
    return Path(appdata) / "coserial"


def _find_available_port(start: int = 37210, end: int = 37220) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available port in range {start}-{end}")


def _save_port(port: int):
    d = _port_file()
    d.mkdir(parents=True, exist_ok=True)
    (d / "server.port").write_text(str(port))


# ── WebSocket broadcast ──────────────────────────────────────────────

def broadcast(msg: dict):
    """Thread-safe: schedule a broadcast to all Web UI clients.

    Called from serial reader threads (via data callback).
    """
    if not _ws_clients or not _loop or _loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_broadcast_coro(msg), _loop)


async def _broadcast_coro(msg: dict):
    if not _ws_clients:
        return
    text = json.dumps(msg)
    await asyncio.gather(
        *(ws.send_str(text) for ws in _ws_clients.copy()),
        return_exceptions=True,
    )


def _on_serial_data(session_id: str, port: str | None, entry):
    """Callback registered on every SerialSession for broadcasting data to Web UI."""
    broadcast(
        {
            "type": entry.kind,
            "entry": entry.to_dict(),
            "session_id": session_id,
            "port": port or "",
        }
    )
    session = _sessions.get(session_id)
    if session:
        formatted = session.format_log_line(entry)
        _write_log(session_id, port or "", formatted)
    else:
        _write_log(session_id, port or "", entry.raw.decode("utf-8", errors="replace").rstrip("\n"))


def _wire_session(session_id: str):
    """Wire a session's data callback to broadcast to Web UI."""
    session = _sessions.get(session_id)
    if session and session.is_open:
        session.set_data_callback(_on_serial_data)


# ── Web UI handler ───────────────────────────────────────────────────

async def handle_web_ui(request):
    """Serve the Web UI single-page app (static file server)."""
    if not os.path.exists(WEB_UI_FILE):
        return web.Response(text="Web UI not found", status=404)
    return web.FileResponse(WEB_UI_FILE)


# ── MCP HTTP handlers ────────────────────────────────────────────────

async def handle_ping(request):
    return web.json_response({"status": "ok", "service": "coserial-server"})


async def handle_mcp_serial_open(body: dict) -> dict:
    port = body.get("port", "")
    baud = body.get("baud", 115200)
    bytesize = body.get("bytesize", 8)
    parity = body.get("parity", "N")
    stopbits = body.get("stopbits", 1)
    terminator = body.get("terminator")

    # Check if this port already has an open session
    existing = await asyncio.to_thread(_sessions.find_by_port, port)
    if existing:
        session = await asyncio.to_thread(_sessions.get, existing)
        if session:
            kwargs = dict(baud=baud, bytesize=bytesize, parity=parity, stopbits=stopbits)
            if terminator is not None:
                kwargs["terminator"] = terminator
            result = await asyncio.to_thread(session.update_config, **kwargs)
            if result.get("status") == "error":
                return result
            return {
                **result,
                "message": f"Updated existing session {existing} on {port}",
            }
        return {"status": "error", "error": f"Session {existing} not found"}

    try:
        session_id, session = _sessions.create(port, baud, bytesize=bytesize, parity=parity, stopbits=stopbits)
        if terminator is not None:
            session.terminator = terminator
    except Exception as e:
        return {"status": "error", "error": str(e)}
    _wire_session(session_id)
    return {
        "status": "ok",
        "session_id": session_id,
        "port": port,
        "baud": baud,
        "bytesize": bytesize,
        "parity": parity,
        "stopbits": stopbits,
        "terminator": session.terminator,
        "message": f"Session {session_id} opened on {port}",
    }


async def handle_mcp_serial_close(body: dict) -> dict:
    session_id = body.get("session_id", "")
    result = _sessions.close(session_id)
    _remove_log_path(session_id)
    if result is None:
        return {"status": "error", "error": f"Session {session_id} not found"}
    return result


async def handle_mcp_serial_write(body: dict) -> dict:
    return await _run_in_session("write", body)


async def handle_mcp_serial_read(body: dict) -> dict:
    return await _run_in_session("read", body)


async def handle_mcp_serial_wait_for(body: dict) -> dict:
    return await _run_in_session("wait_for", body)


async def handle_mcp_serial_command(body: dict) -> dict:
    return await _run_in_session("command", body)


async def handle_mcp_serial_update_config(body: dict) -> dict:
    return await _run_in_session("update_config", body)


async def handle_mcp_serial_set_params(body: dict) -> dict:
    return await _run_in_session("set_params", body)


async def handle_mcp_serial_get_params(body: dict) -> dict:
    return await _run_in_session("get_params", body)


async def handle_mcp_serial_clear_buffer(body: dict) -> dict:
    return await _run_in_session("clear_buffer", body)


async def handle_mcp_serial_get_buffer(body: dict) -> dict:
    return await _run_in_session("get_buffer", body)


async def handle_mcp_serial_list_ports(body: dict) -> list[dict]:
    return await asyncio.to_thread(SerialSession.list_ports)


async def handle_mcp_serial_list_sessions(body: dict) -> list[dict]:
    return await asyncio.to_thread(_sessions.list)


async def handle_mcp_shutdown(body: dict) -> dict:
    """Shut down the server process."""
    import os
    os._exit(0)


# ── Session helper ───────────────────────────────────────────────────

async def _run_in_session(method: str, body: dict) -> dict:
    """Dispatch a method call to the right session, running blocking ops in a thread."""
    session_id = body.get("session_id", "")
    session = await asyncio.to_thread(_sessions.get, session_id)
    if not session:
        return {"status": "error", "error": f"Session {session_id} not found"}

    meth = getattr(session, method, None)
    if not meth:
        return {"status": "error", "error": f"Unknown method: {method}"}

    # Extract params (everything except session_id)
    params = {k: v for k, v in body.items() if k != "session_id"}
    try:
        # Most session methods are synchronous blocking — run in thread
        return await asyncio.to_thread(meth, **params)
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── MCP dispatch ─────────────────────────────────────────────────────

_MCP_HANDLERS = {
    "serial_open": handle_mcp_serial_open,
    "serial_close": handle_mcp_serial_close,
    "serial_write": handle_mcp_serial_write,
    "serial_read": handle_mcp_serial_read,
    "serial_wait_for": handle_mcp_serial_wait_for,
    "serial_command": handle_mcp_serial_command,
    "serial_set_params": handle_mcp_serial_set_params,
    "serial_get_params": handle_mcp_serial_get_params,
    "serial_clear_buffer": handle_mcp_serial_clear_buffer,
    "serial_get_buffer": handle_mcp_serial_get_buffer,
    "serial_update_config": handle_mcp_serial_update_config,
    "serial_list_ports": handle_mcp_serial_list_ports,
    "serial_list_sessions": handle_mcp_serial_list_sessions,
    "shutdown": handle_mcp_shutdown,
}


def _json_safe(obj):
    """Recursively convert bytes to str in JSON-serializable structures."""
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


async def handle_mcp(request: web.Request) -> web.Response:
    tool = request.match_info["tool"]
    body = await request.json() if request.can_read_body else {}

    handler = _MCP_HANDLERS.get(tool)
    if not handler:
        return web.json_response(
            {"status": "error", "error": f"Unknown tool: {tool}"}, status=404
        )

    result = await handler(body)
    return web.json_response(_json_safe(result))


# ── WebSocket handler ────────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _ws_clients.add(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "tx":
                    # TUI user input → write to serial
                    await _handle_ws_tx(data)
                elif msg_type == "list_sessions":
                    sessions = _sessions.list()
                    await ws.send_json({"type": "session_list", "sessions": sessions})
                elif msg_type == "list_ports":
                    ports = await asyncio.to_thread(SerialSession.list_ports)
                    await ws.send_json({"type": "port_list", "ports": ports})
                elif msg_type == "set_logging":
                    global _logging_enabled
                    _logging_enabled = data.get("enabled", True)
                elif msg_type == "set_log_format":
                    session_id = data.get("session_id", "")
                    session = _sessions.get(session_id)
                    if session:
                        params = {k: v for k, v in data.items() if k not in ("type", "session_id")}
                        result = session.set_log_format(**params)
                        await ws.send_json({
                            "type": "log_format_result",
                            "status": "ok",
                            "session_id": session_id,
                            "log_format": session.log_format,
                        })
                elif msg_type == "get_signals":
                    await _handle_ws_get_signals(ws, data)
                elif msg_type == "get_buffer":
                    await _handle_ws_get_buffer(ws, data)
                elif msg_type == "serial_open":
                    port = data.get("port", "")
                    baud = data.get("baud", 115200)
                    bytesize = data.get("bytesize", 8)
                    parity = data.get("parity", "N")
                    stopbits = data.get("stopbits", 1)
                    terminator = data.get("terminator")
                    existing = await asyncio.to_thread(_sessions.find_by_port, port)
                    if existing:
                        _wire_session(existing)
                        session = _sessions.get(existing)
                        if not session:
                            await ws.send_json({"type": "serial_open_result", "status": "error", "error": f"Session {existing} not found"})
                            return
                        kwargs = dict(baud=baud, bytesize=bytesize, parity=parity, stopbits=stopbits)
                        if terminator is not None:
                            kwargs["terminator"] = terminator
                        result = await asyncio.to_thread(session.update_config, **kwargs)
                        await ws.send_json({
                            "type": "serial_open_result",
                            **result,
                            "message": f"Updated {existing}",
                        })
                    else:
                        try:
                            session_id, session = await asyncio.to_thread(_sessions.create, port, baud, bytesize=bytesize, parity=parity, stopbits=stopbits)
                            if terminator is not None:
                                session.terminator = terminator
                            _wire_session(session_id)
                        except Exception as e:
                            await ws.send_json({"type": "serial_open_result", "status": "error", "error": str(e)})
                            return
                        await ws.send_json({
                            "type": "serial_open_result", "status": "ok",
                            "session_id": session_id, "port": port,
                            "baud": baud, "bytesize": bytesize,
                            "parity": parity, "stopbits": stopbits,
                            "terminator": session.terminator,
                            "message": f"Session {session_id} opened on {port}",
                        })
                elif msg_type == "serial_update_config":
                    session_id = data.get("session_id", "")
                    session = _sessions.get(session_id)
                    if not session:
                        await ws.send_json({"type": "serial_update_config_result", "status": "error", "session_id": session_id, "error": f"Session {session_id} not found"})
                    else:
                        result = await asyncio.to_thread(
                            session.update_config,
                            baud=data.get("baud"),
                            bytesize=data.get("bytesize"),
                            parity=data.get("parity"),
                            stopbits=data.get("stopbits"),
                            terminator=data.get("terminator"),
                        )
                        await ws.send_json({"type": "serial_update_config_result", **result})
                elif msg_type == "set_signals":
                    session_id = data.get("session_id", "")
                    session = _sessions.get(session_id)
                    if session:
                        dtr = data.get("dtr")
                        rts = data.get("rts")
                        await asyncio.to_thread(session.set_signals, dtr=dtr, rts=rts)
                elif msg_type == "serial_close":
                    session_id = data.get("session_id", "")
                    msg = data.get("message", "")
                    await _handle_ws_serial_close(ws, session_id, msg)
    finally:
        _ws_clients.discard(ws)

    return ws


async def _handle_ws_tx(data: dict):
    session_id = data.get("session_id", "")
    raw = data.get("raw") or data.get("data", "")
    if data.get("hex"):
        # HEX 发送：前端传来的是纯 hex 字符串，服务端解码为字节
        raw_bytes = bytes.fromhex(raw.replace(" ", "").replace("\n", ""))
    else:
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
    session = _sessions.get(session_id)
    if session:
        await asyncio.to_thread(session.write_raw, raw_bytes)


async def _handle_ws_get_signals(ws: web.WebSocketResponse, data: dict):
    """Get signal line status for a session."""
    session_id = data.get("session_id", "")
    session = _sessions.get(session_id)
    if not session:
        return
    result = await asyncio.to_thread(session.get_signals)
    if result:
        await ws.send_json({"type": "signals", **result})


async def _handle_ws_get_buffer(ws: web.WebSocketResponse, data: dict):
    session_id = data.get("session_id", "")
    session = _sessions.get(session_id)
    if not session:
        return
    entries = await asyncio.to_thread(session.buffer.peek)
    await ws.send_json(
        {
            "type": "buffer_data",
            "session_id": session_id,
            "entries": entries,
            "size": len(entries),
        }
    )


async def _handle_ws_serial_close(ws: web.WebSocketResponse, session_id: str, message: str):
    """Close a session from Web UI."""
    result = _sessions.close(session_id)
    _remove_log_path(session_id)
    if result is None:
        await ws.send_json({"type": "serial_close_result", "status": "error", "session_id": session_id, "error": f"Session {session_id} not found"})
    else:
        await ws.send_json({"type": "serial_close_result", "status": "ok", "session_id": session_id, "message": message or f"Session {session_id} closed"})


# ── Startup / Shutdown ───────────────────────────────────────────────

async def on_startup(app: web.Application):
    global _loop
    _loop = asyncio.get_running_loop()
    print(f"coserial-server ready on 127.0.0.1:{_server_port}", flush=True)


async def on_shutdown(app: web.Application):
    """Cleanup on server shutdown."""
    pass


# ── Main ─────────────────────────────────────────────────────────────

def main():
    """Start the coserial-server process.

    Checks for an existing server first; only starts a new one if none found.
    Accepts --port CLI argument (used by Claude Desktop Preview).
    """
    global _server_port, _loop

    # Check for --port from CLI (e.g. Preview auto-assign)
    cli_port = None
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            try:
                cli_port = int(sys.argv[i + 1])
            except ValueError:
                pass

    if cli_port:
        port = cli_port
    else:
        port = _discover_server_port()
        if port is not None:
            print(f"coserial-server already running on 127.0.0.1:{port}", flush=True)
            return
        port = _find_available_port()

    _server_port = port
    _save_port(_server_port)

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", handle_web_ui)
    app.router.add_get("/mcp/ping", handle_ping)
    app.router.add_post("/mcp/{tool}", handle_mcp)
    app.router.add_get("/ws", handle_ws)

    web.run_app(app, host="127.0.0.1", port=_server_port, print=lambda *a: None)


def launch_with_web(new_port: str | None = None):
    """Start server in background + open Web UI in browser (manual debugging).

    If a server is already running, connects to it (all web UI windows share
    the same server and see the same session list).
    Only spawns a new server when no server is found.

    Called by `uv run coserial` (via __main__.py).

    Args:
        new_port:  COM port to auto-open (or reuse if session exists)
    """
    # ── Quick check: connect to existing server, or spawn one ──────
    from pathlib import Path
    appdata = os.environ.get("LOCALAPPDATA", "")
    port_file = Path(appdata) / "coserial" / "server.port"
    port = None
    if port_file.exists():
        try:
            p = int(port_file.read_text().strip())
            if _try_ping(p):
                port = p
        except (ValueError, OSError):
            pass
    if port is None and _try_ping(37210):
        port = 37210
    if port is not None:
        # ── 已有 server：用 HTTP 创建 session → 打印 → 退出 ─────
        print(f"  → 连接到已有 server (127.0.0.1:{port})", flush=True)
        if new_port:
            url = f"http://127.0.0.1:{port}/mcp/serial_open"
            data = json.dumps({"port": new_port, "baud": 115200}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read())
                    print(f"  → {'OK' if result.get('status')=='ok' else 'FAIL'}: {result.get('message') or result.get('error','')}", flush=True)
            except Exception as e:
                print(f"  → Open error: {e}", flush=True)
        print(f"Server at http://127.0.0.1:{port}/", flush=True)
        return

    # ── 没有 server：全新启动，在主线程跑 web.run_app ──────────
    port = _find_available_port()
    _save_port(port)
    _server_port = port

    async def _on_startup_extra(app):
        """Setup after server starts: create session, print URL, open browser."""
        if new_port:
            try:
                session_id, session = _sessions.create(new_port, 115200)
                _wire_session(session_id)
                print(f"  → Session {session_id} opened on {new_port}", flush=True)
            except Exception as e:
                print(f"  → Open failed: {e}", flush=True)
        url = f"http://127.0.0.1:{port}/"
        print(f"Server ready at {url}", flush=True)
        webbrowser.open(url)

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_startup.append(_on_startup_extra)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", handle_web_ui)
    app.router.add_get("/mcp/ping", handle_ping)
    app.router.add_post("/mcp/{tool}", handle_mcp)
    app.router.add_get("/ws", handle_ws)
    try:
        web.run_app(app, host="127.0.0.1", port=port, print=lambda *a: None)
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)


def _discover_server_port() -> int | None:
    """Quickly find a running coserial-server. Returns port or None."""
    # 1. Try port file
    appdata = os.environ.get("LOCALAPPDATA", "")
    port_file = Path(appdata) / "coserial" / "server.port"
    try:
        if port_file.exists():
            p = int(port_file.read_text().strip())
            if _try_ping(p):
                return p
    except (ValueError, OSError):
        pass

    # 2. Try fixed port 37210
    if _try_ping(37210):
        return 37210

    return None


def _try_ping(port: int) -> bool:
    """Quick check if a server is listening on the port.

    Uses raw socket connect + minimal HTTP preamble rather than
    urllib, to avoid hanging on half-open or TIME_WAIT ports.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(("127.0.0.1", port))
        s.sendall(b"GET /mcp/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        resp = s.recv(256)
        s.close()
        return b"200" in resp[:64]
    except (OSError, socket.timeout):
        return False


if __name__ == "__main__":
    main()
