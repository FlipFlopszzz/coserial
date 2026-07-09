"""Unified entry point for coserial.

Usage:
    uv run coserial                Start server + Web UI (manual debugging)
    uv run coserial COM20          Reuse or create session on COM20
    uv run coserial list            List all active sessions and exit
    uv run coserial init [dir]      Create .mcp.json + .claude/launch.json in target dir
    uv run coserial init --global   Add coserial MCP to ~/.claude.json (all projects)
    uv run coserial -h              Show this help

Other entry points:
    uv run coserial-server          Start server only (headless)
    uv run coserial-client          MCP stdio client (called by Claude Desktop)
    uv run coserial-preview         Preview reverse proxy (called by launch.json)
"""
import json
import sys
import urllib.error
import urllib.request


HELP_TEXT = """\
coserial - Collaborative Serial Debug Bridge

Usage:
  coserial                Start server + Web UI (manual debugging)
  coserial COMx           Start and auto-connect to COMx
  coserial list            List all active sessions
  coserial init [dir]      Create .mcp.json + .claude/launch.json in target dir
  coserial init --global   Add coserial MCP to ~/.claude.json (all projects)
  coserial -h / --help     Show this help

Other entry points:
  coserial-server          Start server only (headless)
  coserial-client          MCP stdio client (called by Claude Desktop)
  coserial-preview         Preview reverse proxy (called by launch.json)

Examples:
  uv run coserial                    # Start server + open Web UI
  uv run coserial COM3               # Start + auto-connect COM3
  uv run coserial list               # Show active sessions
  uv run coserial init --global      # Register MCP for all projects
  uv run coserial init ./my-project  # Register MCP for one project
"""


def _list_sessions():
    """Discover server and print all active sessions."""
    from coserial.server import _discover_server_port, _try_ping

    port = _discover_server_port()
    if port is None:
        print("coserial-server 未运行。", flush=True)
        return 1

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp/serial_list_sessions",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            sessions = json.loads(r.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"查询失败: {e}", flush=True)
        return 1

    if not sessions:
        print(f"没有活跃 session（server: 127.0.0.1:{port}）", flush=True)
        return 0

    print(f"Server: 127.0.0.1:{port}", flush=True)
    print(f"活跃 session: {len(sessions)}", flush=True)
    print(f"{'session_id':<22} {'端口':<10} {'波特率':<8} {'buffer':<8} {'未读':<8}", flush=True)
    print("-" * 60, flush=True)
    for s in sessions:
        sid = s["session_id"]
        port_name = s.get("port") or "?"
        baud = s.get("baud", 115200)
        buf_size = s.get("buffer_size", 0)
        unread = s.get("unread", 0)
        print(
            f"{sid:<22} {port_name:<10} {baud:<8} {buf_size:<8} {unread:<8}",
            flush=True,
        )
    return 0


def _init(target_dir: str | None = None, global_scope: bool = False):
    """创建 MCP + Preview 配置文件。"""
    from coserial.mcp_config_init import do_init

    return do_init(target_dir, global_scope=global_scope)


def main():
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print(HELP_TEXT, flush=True)
        sys.exit(0)

    if "list" in args:
        sys.exit(_list_sessions())

    if "init" in args:
        idx = args.index("init")
        global_scope = "--global" in args[idx + 1:]
        target = None
        # 取 init 后第一个非 flag 参数作为目标目录
        for a in args[idx + 1:]:
            if not a.startswith("-"):
                target = a
                break
        sys.exit(_init(target, global_scope=global_scope))

    # Bare COM port → reuse or create session
    new_port = None
    for a in sys.argv[1:]:
        if not a.startswith("-") and a.upper().startswith("COM"):
            new_port = a
            break

    from coserial.server import launch_with_web
    launch_with_web(new_port=new_port)


if __name__ == "__main__":
    main()
