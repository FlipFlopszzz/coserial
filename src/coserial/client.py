"""MCP stdio client: thin proxy that forwards all tool calls to server via HTTP.

Registered with Claude Desktop's MCP config.  Each tool definition lives here
but the actual serial work happens on the server (coserial-server).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("coserial")
_server_port: int | None = None


# ── Server discovery / spawning ──────────────────────────────────────

def _port_file() -> Path:
    appdata = os.environ.get("LOCALAPPDATA", "")
    return Path(appdata) / "coserial" / "server.port"


def _try_ping(port: int) -> bool:
    """Quick check if a server is listening on the port."""
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


def _try_read_port_file() -> int | None:
    p = _port_file()
    try:
        if p.exists():
            return int(p.read_text().strip())
    except (ValueError, OSError):
        pass
    return None


def _spawn_server():
    """Launch coserial-server as DETACHED_PROCESS (no console window).

    Uses pythonw.exe (GUI subsystem, no console) with PYTHONPATH set to
    the venv's site-packages so the server can find all dependencies.
    pythonw.exe in the venv Scripts/ dir doesn't exist (only python.exe
    is venv-local), so we use the base Python installation's pythonw.exe.
    """
    python = os.path.join(sys.base_prefix, "pythonw.exe")
    if not os.path.exists(python):
        python = sys.executable  # fallback to console version
    # PYTHONPATH needs both venv site-packages (dependencies) and
    # the project src/ dir (coserial package installed via uv).
    src_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    sitepkgs = os.path.join(sys.prefix, "Lib", "site-packages")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (sitepkgs, src_dir) if p)
    try:
        subprocess.Popen(
            [python, "-m", "coserial.server"],
            creationflags=subprocess.DETACHED_PROCESS,
            env=env,
            close_fds=True,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to spawn server: {e}")


def _find_server_port() -> int:
    # 1. Try fixed port 37210
    if _try_ping(37210):
        return 37210

    # 2. Try port file
    port = _try_read_port_file()
    if port and _try_ping(port):
        return port

    # 3. Scan range
    for p in range(37210, 37221):
        if _try_ping(p):
            return p

    # 4. Spawn and wait
    _spawn_server()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        port = _try_read_port_file()
        if port and _try_ping(port):
            return port
        for p in range(37210, 37221):
            if _try_ping(p):
                return p
        time.sleep(0.5)

    raise RuntimeError(
        "Cannot find or start coserial-server. "
        "Try running 'uv run coserial-server' manually."
    )


# ── HTTP forwarding ──────────────────────────────────────────────────

def _call_server(tool: str, **params):
    """Call a server-side tool via HTTP.

    Requires server() to have been called first to set _server_port.
    Does NOT auto-discover or spawn the server.
    """
    global _server_port
    if _server_port is None:
        return {"status": "error", "error": "server not running, call server() first"}
    if not _try_ping(_server_port):
        _server_port = None
        return {"status": "error", "error": "server disconnected, call server() again"}

    url = f"http://127.0.0.1:{_server_port}/mcp/{tool}"
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"status": "error", "error": f"HTTP {e.code}: {body}"}
    except (urllib.error.URLError, OSError) as e:
        return {"status": "error", "error": f"Connection failed: {e}"}


# ── MCP Tools ────────────────────────────────────────────────────────
#
# 标准调用流程：
#   0. server()          — 确保进程运行，获取端口
#   1. list_ports() / list_sessions() — 查看可用串口/已有会话
#   2. open_session()    — 创建会话
#   3. web() / preview() — 打开调试界面（默认需要，除非纯静默调试）
#   4. write/read/wait_for/command — 正式调试
#


@mcp.tool(
    description="【必须先调用】确保 coserial-server 进程运行，返回端口。\n\n"
    "所有其他工具都依赖此工具先执行。\n"
    "如果已有 server 进程则复用，否则自动后台启动（DETACHED_PROCESS）。\n\n"
    "示例：\n"
    '  server() → {"status":"ok","port":37210,"message":"Server ready on 127.0.0.1:37210"}'
)
def server() -> dict:
    global _server_port
    if _server_port and _try_ping(_server_port):
        return {"status": "ok", "port": _server_port, "message": f"Reusing server on 127.0.0.1:{_server_port}"}
    _server_port = _find_server_port()
    return {"status": "ok", "port": _server_port, "message": f"Server ready on 127.0.0.1:{_server_port}"}


@mcp.tool(
    description="打开或复用串口会话。\n\n"
    "创建一个独立 session，返回唯一的 session_id。\n"
    "后续所有操作（write/read/wait_for/command）都需要此 session_id。\n"
    "如果目标端口已被使用，自动复用已有 session。\n"
    "需要先调用 server() 获取端口。\n\n"
    "示例：\n"
    '  open_session(port="COM20") → {"status":"ok","session_id":"sess-abc12345","port":"COM20"}'
)
def open_session(port: str, baud: int = 115200, bytesize: int = 8, parity: str = "N", stopbits: float = 1) -> dict:
    return _call_server("serial_open", port=port, baud=baud, bytesize=bytesize, parity=parity, stopbits=stopbits)


@mcp.tool(
    description="关闭串口会话，释放端口。\n\n"
    "关闭后该 session_id 不再可用。暂时不用也可以不关，\n"
    "其他对话可以复用到已有 session。\n\n"
    "示例：\n"
    '  close_session(session_id="sess-abc12345")'
)
def close_session(session_id: str) -> dict:
    return _call_server("serial_close", session_id=session_id)


@mcp.tool(
    description="向串口发送数据。\n\n"
    "数据以 UTF-8 编码发送，返回的 entry 包含 ts/kind/raw/hex。\n\n"
    "newline 参数控制是否在末尾自动追加 \\r\\n（0x0D 0x0A 真换行）：\n"
    "  newline=True  → 末尾追加真换行（推荐用于 AT 命令等文本协议）\n"
    "  newline=False → 不追加（默认，用于二进制或自定义协议）\n\n"
    "【换行处理规则】服务端会自动处理 data 中的字面 \\r\\n 转义序列：\n"
    "  - data='AT+GMR' + newline=True           → 串口收 'AT+GMR\\r\\n'（8字节，推荐用法）\n"
    "  - data='AT+GMR\\r\\n' + newline=True      → 串口收 'AT+GMR\\r\\n'（字面\\r\\n被解码为真换行，不会双重）\n"
    "  - data='AT+GMR\\r\\n' + newline=False     → 串口收 'AT+GMR\\r\\n'（字面\\r\\n被解码为真换行，兜底）\n"
    "  - data='AT+GMR' + newline=False          → 串口收 'AT+GMR'（无换行，原样发送）\n"
    "  - data='AT\\r\\nAT+RESET\\r\\n' + newline=True → 串口收 'AT\\r\\nAT+RESET\\r\\n'（中间换行保留，末尾统一）\n\n"
    "【边界情况】想发送字面文本 '反斜杠r反斜杠n'（4字节 5C 72 5C 6E）到串口：\n"
    "  - 用 newline=False 且 data 中写 \\\\r\\\\n（四反斜杠）→ 经方案A解码后保留为字面 \\r\\n\n"
    "  - 但串口协议几乎不需要发送字面 \\r\\n 文本，通常用 newline=True 即可\n\n"
    "hex 参数：True 则 data 为十六进制字符串（如 \"48 65 6C 6C 6F\"），服务端解码为原始字节发送。通常配合 newline=False 使用。\n\n"
    "示例：\n"
    '  write(session_id="sess-abc12345", data="AT+GMR", newline=True)  # 推荐\n'
    '  write(session_id="sess-abc12345", data="AT+GMR")  # 无换行\n'
    '  write(session_id="sess-abc12345", data="48 65 6C 6C 6F", hex=True)  # HEX 模式'
)
def write(session_id: str, data: str, newline: bool = False, hex: bool = False) -> dict:
    return _call_server("serial_write", session_id=session_id, data=data, newline=newline, hex=hex)
@mcp.tool(
    description="读取缓冲区数据。\n\n"
    "返回该 session 的缓冲区中的日志条目（entries 数组）。\n"
    "每条 entry 含 {ts, kind, raw, hex}。\n"
    "无数据时等待 timeout 秒后返回空数组。\n"
    "注意：读取是破坏性的——读取后不再出现。\n"
    "控制 size 参数可只预览而不消费（兼容 size 参数相当于 count）。\n"
    "返回值还包含 params 字段，含当前会话的完整参数。\n\n"
    "示例：\n"
    '  read(session_id="sess-abc12345", timeout=2.0)'
)
def read(session_id: str, timeout: float = 1.0, size: int | None = None) -> dict:
    return _call_server("serial_read", session_id=session_id, timeout=timeout, size=size)


@mcp.tool(
    description="【核心工具】等待串口输出匹配正则表达式。\n\n"
    "配合 write 使用：发命令→等响应。\n"
    "返回匹配到的日志条目（entries 数组），每条含 {ts, kind, raw, hex}。\n"
    "超时返回 status='timeout'，不抛异常。\n"
    "返回值还包含 params 字段，含当前会话的完整参数。\n\n"
    "示例：\n"
    '  wait_for(session_id="sess-abc12345", pattern="OK", timeout=5)'
)
def wait_for(session_id: str, pattern: str, timeout: float = 5.0) -> dict:
    return _call_server("serial_wait_for", session_id=session_id, pattern=pattern, timeout=timeout)


@mcp.tool(
    description="【最常用】发送命令并等待响应（write + wait_for 的组合）。\n\n"
    "自动清空缓冲区后再发送，确保匹配本次响应。\n"
    "返回匹配到的日志条目（entries 数组），每条含 {ts, kind, raw, hex}。\n"
    "如果 expect 为空，只发送不等待。\n\n"
    "newline 参数同 write()：True 则在 data 末尾追加 \\r\\n 真换行。\n"
    "推荐 AT 命令等文本协议用 newline=True，二进制协议用 newline=False。\n"
    "hex 参数同 write()：True 则 data 为 hex 字符串，服务端解码为字节。\n"
    "详细换行处理规则见 write() 工具描述。\n\n"
    "工作流：清空 buffer → write(data, newline) → wait_for(expect)\n\n"
    "示例：\n"
    '  command(session_id="sess-abc12345", data="AT+GMR", expect="OK", timeout=5, newline=True)\n'
    '  command(session_id="sess-abc12345", data="AT+GMR", expect="OK", timeout=5)  # 无换行'
)
def command(session_id: str, data: str, expect: str | None = None, timeout: float = 5.0, newline: bool = False, hex: bool = False) -> dict:
    return _call_server("serial_command", session_id=session_id, data=data, expect=expect, timeout=timeout, newline=newline, hex=hex)


@mcp.tool(
    description="关闭 coserial-server 进程（原 shutdown）。\n\n"
    "强制终止正在运行的 server。之后需要再次调用 server() 启动。\n"
    "注意：这会断开所有串口连接。"
)
def shutdown_server() -> dict:
    global _server_port
    if _server_port is None:
        return {"status": "error", "error": "server not running"}
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{_server_port}/mcp/shutdown",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    _server_port = None
    return {"status": "ok", "message": "Server shutdown requested"}


@mcp.tool(
    description="在系统浏览器中打开 Web UI 调试面板。\n\n"
    "人类可实时看到所有 session 的 TX/RX 数据，\n"
    "并可通过底部输入框手动向串口发送数据。\n\n"
    "需要先 server() + open_session() 确保有数据和会话。\n\n"
    "顶部下拉框选 session；底部输入框发送。\n"
    "支持命令：/list /switch <id> /new <port> /clear /help\n\n"
    "示例：\n"
    '  web(port=37210) → {"url":"http://127.0.0.1:37210/"}'
)
def web(port: int) -> dict:
    url = f"http://127.0.0.1:{port}/"
    webbrowser.open(url)
    return {"status": "ok", "url": url, "message": f"Web UI opened at {url}"}


@mcp.tool(
    description="在 Claude Desktop Preview 中打开调试面板。\n\n"
    "此工具不实际打开窗口，而是返回 Preview 所需的 URL 信息。\n"
    "需要配合 Preview MCP 工具使用。\n\n"
    "需要先 server() + open_session() 确保有数据和会话。\n\n"
    "使用步骤：\n"
    '  1. preview_start("coserial-web-ui")  — Preview 内嵌浏览器\n'
    "  2. 落地页自动跳转到真实 server（同端口连 WS，不需额外步骤）\n\n"
    "示例：\n"
    '  preview(port=37210) → {"preview_url":"http://127.0.0.1:37210/"}'
)
def preview(port: int) -> dict:
    url = f"http://127.0.0.1:{port}/"
    return {
        "status": "ok",
        "preview_url": url,
        "server_port": port,
        "hint": "Call preview_start('coserial-web-ui') and the page auto-redirects",
    }


@mcp.tool(
    description="设置会话参数。\n\n"
    "动态调整串口参数（波特率、数据位、校验位、停止位）、DTR/RTS 信号线和换行符，不需重建会话。\n"
    "所有参数可选，只传需要修改的。\n"
    "返回 params 结构体（含当前完整参数状态）。\n\n"
    "terminator 可选值：\"CRLF\"（CR+LF，默认）、\"LF\"（LF）、\"CR\"（CR）。\n\n"
    "示例：\n"
    '  set_params(session_id="sess-abc12345", baud=115200, dtr=False)\n'
    '  set_params(session_id="sess-abc12345", terminator="LF")'
)
def set_params(session_id: str, baud: int | None = None, bytesize: int | None = None, parity: str | None = None, stopbits: float | None = None, dtr: bool | None = None, rts: bool | None = None, terminator: str | None = None) -> dict:
    return _call_server("serial_set_params", session_id=session_id, baud=baud, bytesize=bytesize, parity=parity, stopbits=stopbits, dtr=dtr, rts=rts, terminator=terminator)


@mcp.tool(
    description="获取会话完整参数。\n\n"
    "返回 params 结构体，含串口参数、信号线状态、缓冲区信息。\n"
    "如果用户在 Web UI 调整了参数，Agent 可通过此工具获取最新状态。\n\n"
    "示例：\n"
    '  get_params(session_id="sess-abc12345")'
)
def get_params(session_id: str) -> dict:
    return _call_server("serial_get_params", session_id=session_id)


@mcp.tool(
    description="清空会话缓冲区。\n\n"
    "将 Agent 的读取游标移到缓冲区末尾，后续 wait_for 只匹配新数据。\n"
    "不会删除历史数据——历史仍可通过 get_buffer 获取。\n"
    "常用于开始新命令前清除之前残留的回复。\n\n"
    "示例：\n"
    '  clear_buffer(session_id="sess-abc12345")'
)
def clear_buffer(session_id: str) -> dict:
    return _call_server("serial_clear_buffer", session_id=session_id)


@mcp.tool(
    description="获取历史日志条目。\n\n"
    "返回该 session 的缓冲区中的全部条目，不消费游标（与 read 不同，不破坏性）。\n"
    "每条 entry 含 {ts, kind, raw, hex}。\n"
    "控制 count 参数限制返回条数。\n\n"
    "示例：\n"
    '  get_buffer(session_id="sess-abc12345", count=10)'
)
def get_buffer(session_id: str, count: int | None = None) -> dict:
    return _call_server("serial_get_buffer", session_id=session_id, count=count)


@mcp.tool(
    description="列出系统上所有可用的串口。\n\n"
    "返回端口名、描述、硬件 ID 等信息。\n\n"
    "需要先调用 server()。"
)
def list_ports() -> list[dict]:
    return _call_server("serial_list_ports")


@mcp.tool(
    description="列出所有活跃的串口会话。\n\n"
    "返回 session_id、端口、波特率等。\n"
    "server 是全机唯一的，任何对话打开的 session 都可见。\n\n"
    "需要先调用 server()。"
)
def list_sessions() -> list[dict]:
    return _call_server("serial_list_sessions")


# ── Entry point ──────────────────────────────────────────────────────

def main():
    """Run the MCP stdio client (called by Claude Desktop)."""
    mcp.run()


if __name__ == "__main__":
    main()
