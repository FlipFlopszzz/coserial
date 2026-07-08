"""Unified entry point for coserial.

Usage:
    uv run coserial                Start server + Web UI (manual debugging)
    uv run coserial COM20          Reuse or create session on COM20
    uv run coserial list            List all active sessions and exit
"""
import json
import sys
import urllib.error
import urllib.request


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


def main():
    if "list" in sys.argv[1:]:
        sys.exit(_list_sessions())

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
