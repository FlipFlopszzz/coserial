import re
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

import serial
import serial.tools.list_ports


@dataclass
class Entry:
    """一条串口 IO 日志条目。

    ts:   时间戳 "HH:MM:SS.mmm"
    kind: 方向 "rx"（收到）或 "tx"（发出）
    raw:  串口原始字节（纯，不包含时间戳/方向等装饰）
    hex:  raw.hex()
    """

    ts: str
    kind: str
    raw: bytes
    hex: str

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "raw": self.raw.decode("utf-8", errors="replace"),
            "hex": self.hex,
        }


class SessionBuffer:
    """结构化日志缓冲区。

    每条 IO 数据存储为 Entry（ts/kind/raw/hex），
    Agent 通过读游标（_pos）追踪已消费的条目，
    前端通过 peek() 拉取全部条目。

    所有操作线程安全。
    """

    def __init__(self, max_entries: int = 10000):
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self._max = max_entries
        self._event = threading.Event()
        self._pos = 0  # 游标：在此之前的条目已被 Agent 消费
        self._truncated = 0  # 因 trim 而移除的条目数，用于修正 _pos

    def append(self, raw: bytes, kind: str) -> Entry | None:
        """创建并存储一条条目。返回 Entry 对象。"""
        if not raw:
            return None
        now = time.time()
        ms = int((now - int(now)) * 1000)
        t = time.strftime("%H:%M:%S", time.localtime(now))
        ts = f"{t}.{ms:03d}"
        entry = Entry(ts=ts, kind=kind, raw=raw, hex=raw.hex())
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max:
                excess = len(self._entries) - self._max
                self._entries = self._entries[-self._max:]
                self._pos = max(0, self._pos - excess)
            self._event.set()
        return entry

    def read(self, count: int | None = None) -> list[dict]:
        """从游标位置读取条目，推进游标。返回 dict 列表。"""
        with self._lock:
            if self._pos >= len(self._entries):
                return []
            if count is None or self._pos + count >= len(self._entries):
                entries = [e.to_dict() for e in self._entries[self._pos:]]
                self._pos = len(self._entries)
            else:
                entries = [e.to_dict() for e in self._entries[self._pos:self._pos + count]]
                self._pos += count
            return entries

    def search_re(self, compiled: re.Pattern) -> re.Match | None:
        """在游标之后所有条目的扁平 raw bytes 中搜索。返回 match 或 None。"""
        with self._lock:
            flat = b"".join(e.raw for e in self._entries[self._pos:])
            return compiled.search(flat)

    def read_until_match(self, compiled: re.Pattern) -> list[dict] | None:
        """从游标到匹配条目末尾的所有条目。推进游标到匹配条目的下一条。

        跨条目匹配时（pattern 跨越两条 raw 的边界），
        返回从游标到匹配结束条目的全部条目。
        """
        with self._lock:
            # 构建扁平 raw + 条目索引映射
            flat = bytearray()
            idx_list = []
            for i in range(self._pos, len(self._entries)):
                e = self._entries[i]
                flat.extend(e.raw)
                idx_list.extend([i] * len(e.raw))
            m = compiled.search(flat)
            if not m:
                return None
            # 匹配结束位置所在的条目索引
            end_byte = m.end()
            end_idx = idx_list[end_byte - 1] if end_byte > 0 else self._pos
            entries = [e.to_dict() for e in self._entries[self._pos:end_idx + 1]]
            self._pos = end_idx + 1
            return entries

    def peek(self) -> list[dict]:
        """返回全部条目（用于前端历史加载）。不影响游标。"""
        with self._lock:
            return [e.to_dict() for e in self._entries]

    def consume_all(self):
        """游标移到末尾——之后 wait_for 只看到新条目。"""
        with self._lock:
            self._pos = len(self._entries)
            self._event.clear()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._pos = 0
            self._event.clear()

    def wait(self, timeout: float = 1.0):
        """等待有新条目追加。"""
        self._event.wait(timeout=timeout)
        self._event.clear()

    @property
    def size(self) -> int:
        """总条目数（含已消费的）。"""
        with self._lock:
            return len(self._entries)

    @property
    def unread(self) -> int:
        """未消费条目数（游标到末尾）。"""
        with self._lock:
            return max(0, len(self._entries) - self._pos)

    def __len__(self) -> int:
        return self.size


class SerialSession:
    """Manages one serial port connection with background reader and buffer.

    Each session is identified by a unique session_id.  All IO — hardware RX,
    Agent TX, and Web UI TX — is recorded in the session's SessionBuffer so that
    any party can read the full conversation history.
    """

    TERMINATOR_MAP = {
        "CRLF": b"\r\n",
        "LF": b"\n",
        "CR": b"\r",
    }

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.port_name: str | None = None
        self.baud: int = 115200
        self._bytesize: int = 8
        self._parity: str = "N"
        self._stopbits: float = 1
        self.terminator: str = "CRLF"
        self.log_format = {
            "timestamp": True,
            "direction": True,
            "dir_symbols": "← →",
            "ts_precision": "ms",
        }
        self._serial: serial.Serial | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self.buffer = SessionBuffer()
        self._data_callback = None

    def set_data_callback(self, callback):
        """Register a callback for real-time data events.

        callback(session_id: str, port: str | None, entry: Entry)
        """
        self._data_callback = callback

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._serial is not None and self._serial.is_open

    def open(
        self,
        port: str,
        baud: int = 115200,
        bytesize: int = serial.EIGHTBITS,
        parity: str = serial.PARITY_NONE,
        stopbits: int = serial.STOPBITS_ONE,
        xonxoff: bool = False,
        rtscts: bool = False,
    ) -> dict:
        if self.is_open:
            self.close()

        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            xonxoff=xonxoff,
            rtscts=rtscts,
            timeout=0.05,
        )

        with self._lock:
            self._serial = ser
            self._running = True
            self.port_name = port
            self.baud = baud
            self._bytesize = bytesize
            self._parity = parity
            self._stopbits = stopbits

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        return {
            "status": "ok",
            "port": port,
            "baud": baud,
            "bytesize": bytesize,
            "parity": parity,
            "stopbits": stopbits,
            "session_id": self.session_id,
        }

    def update_config(
        self,
        baud: int | None = None,
        bytesize: int | None = None,
        parity: str | None = None,
        stopbits: float | None = None,
        terminator: str | None = None,
    ) -> dict:
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return {"status": "error", "error": "Serial port not open"}
            if baud is not None:
                self._serial.baudrate = baud
                self.baud = baud
            if bytesize is not None:
                self._serial.bytesize = bytesize
                self._bytesize = bytesize
            if parity is not None:
                self._serial.parity = parity
                self._parity = parity
            if stopbits is not None:
                self._serial.stopbits = stopbits
                self._stopbits = stopbits
            if terminator is not None:
                self.terminator = terminator
            return {
                "status": "ok",
                "session_id": self.session_id,
                "port": self.port_name,
                "baud": self.baud,
                "bytesize": self._bytesize,
                "parity": self._parity,
                "stopbits": self._stopbits,
                "terminator": self.terminator,
            }

    def close(self) -> dict:
        was_port = self.port_name
        with self._lock:
            self._running = False

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2)

        with self._lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
            self.port_name = None

        return {"status": "ok", "port": was_port}

    def _reader_loop(self):
        while True:
            with self._lock:
                if not self._running:
                    return
                ser = self._serial
                if ser is None or not ser.is_open:
                    return

            try:
                if ser.in_waiting:
                    data = ser.read(max(ser.in_waiting, 1024))
                    if data:
                        entry = self.buffer.append(data, "rx")
                        cb = self._data_callback
                        if cb:
                            cb(self.session_id, self.port_name, entry)
                else:
                    time.sleep(0.01)
            except serial.SerialException:
                with self._lock:
                    self._running = False
                return
            except Exception:
                with self._lock:
                    self._running = False
                return

    def write(self, data: bytes | str, newline: bool = False, hex: bool = False) -> dict:
        """Write data to serial and record in session buffer.

        Args:
            data: 要发送的数据。
            newline: True 则在末尾追加 session.terminator 指定的换行符（默认 CRLF→\\r\\n）。
            hex: True 则 data 为 hex 字符串，用 bytes.fromhex() 解码为字节。
        """
        term = self.TERMINATOR_MAP.get(self.terminator, b"\r\n")
        if isinstance(data, str):
            if hex:
                data = bytes.fromhex(data.replace(" ", "").replace("\n", ""))
            else:
                if newline:
                    data = data.rstrip('\r\n').encode("utf-8") + term
                else:
                    data = data.encode("utf-8")
        elif newline:
            data = data.rstrip(b'\r\n') + term
        with self._lock:
            if not self._serial or not self._serial.is_open:
                raise RuntimeError("Serial port not open")
            written = self._serial.write(data)
        entry = self.buffer.append(data, "tx")
        cb = self._data_callback
        if cb:
            cb(self.session_id, self.port_name, entry)
        return {
            "status": "ok",
            "sent": written,
            "session_id": self.session_id,
            "port": self.port_name,
            "entry": entry.to_dict(),
        }

    def write_raw(self, data: bytes | str):
        """Write data from Web UI to the serial port and inject into session buffer.

        Writes to the serial port AND injects into the session buffer
        so the Agent can see it via serial_read.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return
            self._serial.write(data)
        entry = self.buffer.append(data, "tx")
        cb = self._data_callback
        if cb:
            cb(self.session_id, self.port_name, entry)

    def _build_params(self) -> dict:
        """返回标准化的 params 结构体（会话参数 + 信号线 + 缓冲区状态）。"""
        base = self.info()
        signals = {}
        with self._lock:
            if self._serial and self._serial.is_open:
                signals = {
                    "dtr": self._serial.dtr,
                    "rts": self._serial.rts,
                    "cts": self._serial.cts,
                    "dsr": self._serial.dsr,
                    "ri": self._serial.ri,
                    "cd": self._serial.cd,
                }
        return {**base, **signals}

    def read(self, timeout: float = 1.0, count: int | None = None, size: int | None = None) -> dict:
        """Read entries from buffer.

        Args:
            timeout: 无数据时最多等待秒数
            count:   最大返回条数（None=全返回）
            size:    兼容旧参数，相当于 count
        """
        if count is None and size is not None:
            count = size
        if not self.is_open:
            return {"status": "error", "error": "Serial port not open", "entries": []}

        if self.buffer.unread > 0:
            entries = self.buffer.read(count)
            return {"status": "ok", "entries": entries, "params": self._build_params()}

        self.buffer.wait(timeout)

        entries = self.buffer.read(count)
        return {"status": "ok", "entries": entries, "params": self._build_params()}

    def wait_for(self, pattern: str, timeout: float = 5.0) -> dict:
        if not self.is_open:
            return {"status": "error", "error": "Serial port not open", "entries": []}

        compiled = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            entries = self.buffer.read_until_match(compiled)
            if entries:
                return {"status": "ok", "entries": entries, "params": self._build_params()}
            self.buffer.wait(min(0.1, remaining))

        entries = self.buffer.read_until_match(compiled)
        if entries:
            return {"status": "ok", "entries": entries, "params": self._build_params()}

        return {"status": "timeout", "entries": [], "error": f"Pattern '{pattern}' not found within {timeout}s", "params": self._build_params()}

    def set_params(
        self,
        baud: int | None = None,
        bytesize: int | None = None,
        parity: str | None = None,
        stopbits: float | None = None,
        dtr: bool | None = None,
        rts: bool | None = None,
        terminator: str | None = None,
    ) -> dict:
        """统一设置会话参数（串口参数 + DTR/RTS 信号线 + 换行符）。"""
        result = self.update_config(baud=baud, bytesize=bytesize, parity=parity, stopbits=stopbits, terminator=terminator)
        if result.get("status") == "error":
            return {**result, "params": self._build_params()}
        if dtr is not None or rts is not None:
            sig = self.set_signals(dtr=dtr, rts=rts)
            if sig.get("status") == "error":
                return {**sig, "params": self._build_params()}
        return {"status": "ok", "params": self._build_params()}

    def get_params(self) -> dict:
        """获取当前会话完整参数。"""
        return {"status": "ok", "params": self._build_params()}

    def clear_buffer(self) -> dict:
        """清空 Agent 游标（后续 wait_for 只看到新数据）。"""
        self.buffer.consume_all()
        return {"status": "ok", "session_id": self.session_id}

    def get_buffer(self, count: int | None = None) -> dict:
        """获取历史日志条目（不消费游标，peek 性质）。"""
        entries = self.buffer.peek()
        if count is not None:
            entries = entries[:count]
        return {"status": "ok", "entries": entries, "params": self._build_params()}

    def command(self, data: bytes | str, expect: str | None = None, timeout: float = 5.0, newline: bool = False, hex: bool = False) -> dict:
        """Send data then wait for expected pattern.

        Moves the Agent cursor to end of buffer before sending,
        so wait_for only matches new data from this command.
        Old data is still in the buffer and available via peek().
        """
        if not self.is_open:
            return {"status": "error", "error": "Serial port not open"}

        self.buffer.consume_all()
        result = self.write(data, newline=newline, hex=hex)

        if expect:
            response = self.wait_for(expect, timeout)
            return {
                "status": response.get("status", "ok"),
                "sent": result["sent"],
                "session_id": self.session_id,
                "port": self.port_name,
                "entries": response.get("entries", []),
                "error": response.get("error"),
            }

        return {
            "status": "ok",
            "sent": result["sent"],
            "session_id": self.session_id,
            "port": self.port_name,
            "entries": [],
        }

    def set_signals(self, dtr: bool | None = None, rts: bool | None = None) -> dict:
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return {"status": "error", "error": "Serial port not open"}
            if dtr is not None:
                self._serial.dtr = dtr
            if rts is not None:
                self._serial.rts = rts
        return {
            "status": "ok",
            "session_id": self.session_id,
            "port": self.port_name,
            "dtr": self._serial.dtr if self._serial else None,
            "rts": self._serial.rts if self._serial else None,
        }

    def get_signals(self) -> dict:
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return {"status": "error", "error": "Serial port not open"}
            return {
                "status": "ok",
                "session_id": self.session_id,
                "port": self.port_name,
                "dtr": self._serial.dtr,
                "rts": self._serial.rts,
                "cts": self._serial.cts,
                "dsr": self._serial.dsr,
                "ri": self._serial.ri,
                "cd": self._serial.cd,
            }

    @staticmethod
    def list_ports() -> list[dict]:
        ports = serial.tools.list_ports.comports()
        return [
            {
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": p.vid,
                "pid": p.pid,
                "serial_number": p.serial_number,
            }
            for p in ports
        ]

    def format_log_line(self, entry: Entry) -> str:
        """根据会话的 log_format 设置将 Entry 格式化为日志行。"""
        parts = []
        lf = self.log_format

        # 时间戳
        if lf.get("timestamp", True):
            ts = entry.ts  # "HH:MM:SS.mmm"
            if lf.get("ts_precision", "ms") == "s":
                ts = ts.split(".")[0]
            parts.append(f"[{ts}]")

        # 方向
        if lf.get("direction", True):
            symbols = lf.get("dir_symbols", "← →")
            sym_parts = symbols.split()
            rx_sym = sym_parts[0] if len(sym_parts) >= 1 else "←"
            tx_sym = sym_parts[1] if len(sym_parts) >= 2 else "→"
            arrow = rx_sym if entry.kind == "rx" else tx_sym
            parts.append(arrow)

        # 原始数据
        parts.append(entry.raw.decode("utf-8", errors="replace").rstrip("\n"))

        return " ".join(parts)

    def set_log_format(self, **kwargs) -> dict:
        """更新日志格式设置。只接受 log_format 字典中的有效键。"""
        valid_keys = {"timestamp", "direction", "dir_symbols", "ts_precision"}
        for k, v in kwargs.items():
            if k in valid_keys:
                self.log_format[k] = v
        return {"status": "ok", "log_format": self.log_format}

    def info(self) -> dict:
        """Return a snapshot of the session state."""
        return {
            "session_id": self.session_id,
            "port": self.port_name,
            "baud": self.baud,
            "bytesize": self._bytesize,
            "parity": self._parity,
            "stopbits": self._stopbits,
            "terminator": self.terminator,
            "log_format": self.log_format,
            "is_open": self.is_open,
            "buffer_size": self.buffer.size,
            "unread": self.buffer.unread,
        }


class SessionManager:
    """Manages a pool of SerialSession instances.

    Each call to create() spawns a new session with a unique session_id.
    Sessions are independent — each has its own serial port, reader thread,
    and session buffer.
    """

    def __init__(self):
        self._sessions: dict[str, SerialSession] = {}
        self._lock = threading.Lock()

    def create(self, port: str, baud: int = 115200, bytesize: int = 8, parity: str = "N", stopbits: float = 1) -> tuple[str, SerialSession]:
        """Open a new serial session. Returns (session_id, session)."""
        session_id = f"sess-{uuid4().hex[:8]}"
        session = SerialSession(session_id)
        session.open(port, baud, bytesize=bytesize, parity=parity, stopbits=stopbits)
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str) -> SerialSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def close(self, session_id: str) -> dict | None:
        session = None
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            result = session.close()
            result["session_id"] = session_id
            return result
        return None

    def find_by_port(self, port: str) -> str | None:
        """Find an existing session by port name. Returns session_id or None."""
        with self._lock:
            for sid, s in self._sessions.items():
                if s.is_open and s.port_name == port:
                    return sid
            return None

    def list(self) -> list[dict]:
        with self._lock:
            return [s.info() for s in self._sessions.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
