"""Preview 反向代理：Claude Desktop Preview 的中转服务。

启动即绑定端口，HEAD 立即返回 200（Preview 健康检查通过），
后台扫描真实 coserial-server 端口。

所有内容同源（Preview 端口），无跨域重定向问题。"""

import asyncio
import os
import socket
import sys
from pathlib import Path

_WEB_UI = Path(__file__).resolve().parent.parent / "web_ui" / "index.html"


def _scan_server() -> int | None:
    """扫描 37210-37220 返回真实 server 端口。"""
    for p in range(37210, 37221):
        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", p))
            s.sendall(b"GET /mcp/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            if b"200" in s.recv(256)[:64]:
                return p
        except OSError:
            pass
        finally:
            s.close()
    return None


async def _pipe(r, w):
    try:
        while True:
            d = await r.read(65536)
            if not d:
                break
            w.write(d)
            await w.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass


_real_port = None


async def _wait_for_server() -> int | None:
    """等待后台扫描完成，返回真实 server 端口。"""
    global _real_port
    deadline = 0
    while deadline < 30:
        if _real_port is not None:
            return _real_port
        deadline += 0.5
        await asyncio.sleep(0.5)
    return None


async def _background_scan():
    """后台持续扫描，直到找到真实 server。"""
    global _real_port
    for _ in range(10):
        rp = _scan_server()
        if rp is not None:
            _real_port = rp
            print(f"... found on {rp}", flush=True)
            return
        await asyncio.sleep(1)
    print("... server not found", flush=True)


async def _forward(cr: asyncio.StreamReader, cw: asyncio.StreamWriter, real_port: int, req: bytes):
    """将请求转发到真实 server 并回传响应。"""
    rr, rw = await asyncio.open_connection("127.0.0.1", real_port)
    rw.write(req)
    await rw.drain()

    if b"upgrade: websocket" in req.lower():
        # WebSocket：等 101，然后双向管道
        resp = b""
        while b"\r\n" not in resp:
            resp += await rr.read(4096)
        cw.write(resp)
        await cw.drain()
        await asyncio.gather(_pipe(cr, rw), _pipe(rr, cw))
    else:
        while True:
            chunk = await rr.read(65536)
            if not chunk:
                break
            cw.write(chunk)
            await cw.drain()


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter):
    try:
        # ── 读取完整请求（行 + 头 + body） ──
        hdr_end = 0
        req = b""
        while True:
            chunk = await cr.read(4096)
            if not chunk:
                break
            req += chunk
            idx = req.find(b"\r\n\r\n")
            if idx >= 0:
                hdr_end = idx + 4
                break
        if not req:
            return

        # 读取 body
        cl = 0
        for h in req[:hdr_end].split(b"\r\n"):
            if h.lower().startswith(b"content-length:"):
                try:
                    cl = int(h.split(b":", 1)[1])
                except ValueError:
                    pass
        body_len = len(req) - hdr_end
        while body_len < cl:
            chunk = await cr.read(cl - body_len)
            if not chunk:
                break
            req += chunk
            body_len += len(chunk)

        first = req.split(b"\r\n", 1)[0]
        parts = first.split(b" ", 2)
        method = parts[0]
        path = parts[1] if len(parts) > 1 else b"/"

        # ── HEAD / → 立即 200（Preview 健康检查） ──
        if method == b"HEAD" and path == b"/":
            cw.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await cw.drain()
            return

        # ── 其他请求 → 等扫描完成 ──
        rp = await _wait_for_server()
        if rp is None:
            cw.write(
                b"HTTP/1.1 502\r\n"
                b"Content-Type: text/plain\r\n"
                b"Connection: close\r\n\r\n"
                b"server not found"
            )
            await cw.drain()
            return

        # ── GET / → serve index.html + 注入 WS 端口 ──
        if method == b"GET" and path == b"/":
            html = _WEB_UI.read_bytes()
            tag = b"</title>"
            inject = b'<script>window.__WS_PORT__="%d";</script>\n' % rp
            idx = html.find(tag)
            body = html[: idx + len(tag)] + inject + html[idx + len(tag) :] if idx >= 0 else html
            hdrs = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n"
                "\r\n" % len(body)
            ).encode()
            cw.write(hdrs + body)
            await cw.drain()
            return

        # ── 其他（WebSocket 升级 / mcp POST 等）→ 转发 ──
        await _forward(cr, cw, rp, req)

    except Exception:
        pass
    finally:
        try:
            cw.close()
        except Exception:
            pass


async def main():
    listen_port = int(os.environ.get("PORT", 37230))

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    print(f"Preview proxy on {listen_port}", flush=True)

    asyncio.create_task(_background_scan())

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
