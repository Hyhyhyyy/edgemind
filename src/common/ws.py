"""最小可用的 WebSocket 实现（RFC6455），纯标准库，零第三方依赖。

为什么自己实现：
1. 让 PoC 在任意装有 Python 的机器上「零安装」即可运行；
2. 直观展示 KubeEdge EdgeHub<->CloudHub 之间那条 **长连接双向通道** 的
   握手、帧封装（masking）、心跳（ping/pong）与断线重连机制。

生产环境当然直接用 `websockets` / `uvicorn` 等库；这里手写为教学与可移植性。
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from typing import Callable, Optional

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# opcode
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BIN = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

FrameHandler = Callable[["Connection", str], None]


class WSFrameError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WSFrameError("connection closed by peer")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket):
    """读取一个完整帧，返回 (opcode, payload, fin)。client->server 需去掩码。"""
    head = _recv_exact(sock, 2)
    fin = (head[0] & 0x80) != 0
    opcode = head[0] & 0x0F
    masked = (head[1] & 0x80) != 0
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length)
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, fin


def _build_frame(opcode: int, payload: bytes, fin: bool = True, mask: Optional[bytes] = None) -> bytes:
    """构造一个帧。mask 仅在 client->server 时由调用方提供（4 字节）。"""
    header = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        out = struct.pack("B", header) + struct.pack("B", length | mask_bit)
    elif length < 65536:
        out = struct.pack("B", header) + struct.pack("B", 126 | mask_bit) + struct.pack(">H", length)
    else:
        out = struct.pack("B", header) + struct.pack("B", 127 | mask_bit) + struct.pack(">Q", length)
    if mask:
        out += mask
        out += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return out


class Connection:
    """表示一个已建立的 WebSocket 连接（服务端接受或客户端发起）。"""

    def __init__(self, sock: socket.socket, peer_id: str):
        self.sock = sock
        self.peer_id = peer_id
        self._lock = threading.Lock()
        self.closed = False

    def send(self, text: str, mask: Optional[bytes] = None) -> None:
        data = text.encode("utf-8")
        frame = _build_frame(_OP_TEXT, data, mask=mask)
        with self._lock:
            if self.closed:
                return
            try:
                self.sock.sendall(frame)
            except OSError:
                self.closed = True

    def send_pong(self, payload: bytes) -> None:
        with self._lock:
            if self.closed:
                return
            try:
                self.sock.sendall(_build_frame(_OP_PONG, payload))
            except OSError:
                self.closed = True

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            try:
                self.sock.sendall(_build_frame(_OP_CLOSE, b""))
            except OSError:
                pass
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class WSServer:
    """极简 WebSocket 服务端（模拟 KubeEdge CloudHub）。

    每接受一个连接起一个线程读取帧，遇到文本帧回调 on_message。
    支持 ping/pong 心跳与按 peer_id 定向发送 / 广播。
    """

    def __init__(self, host: str, port: int, on_message: FrameHandler, on_connect=None, on_disconnect=None):
        self.host = host
        self.port = port
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._conns: dict[str, Connection] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._sock.bind((self.host, self.port))
        self._sock.listen(128)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            try:
                peer = self._handshake(conn)
            except WSFrameError:
                conn.close()
                continue
            self._counter += 1
            cid = f"c{self._counter}"
            connection = Connection(conn, cid)
            with self._lock:
                self._conns[cid] = connection
            if self.on_connect:
                self.on_connect(connection)
            t = threading.Thread(target=self._read_loop, args=(connection,), daemon=True)
            t.start()

    def _handshake(self, conn: socket.socket) -> str:
        # 读取 HTTP 升级请求头
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(1)
            if not chunk:
                raise WSFrameError("no handshake")
            buf += chunk
            if len(buf) > 4096:
                raise WSFrameError("handshake too large")
        headers = {}
        for line in buf.split(b"\r\n")[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode().strip().lower()] = v.decode().strip()
        key = headers.get("sec-websocket-key")
        if not key:
            raise WSFrameError("missing sec-websocket-key")
        accept = base64.b64encode(
            hashlib.sha1((key + _GUID).encode()).digest()
        ).decode()
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        conn.sendall(resp.encode())
        return accept

    def _read_loop(self, connection: Connection) -> None:
        frag_opcode = None
        frag_buf = bytearray()
        try:
            while not connection.closed:
                opcode, payload, fin = _read_frame(connection.sock)
                if opcode == _OP_CLOSE:
                    break
                if opcode == _OP_PING:
                    connection.send_pong(payload)
                    continue
                if opcode == _OP_PONG:
                    continue
                if opcode == _OP_TEXT or opcode == _OP_BIN:
                    if not fin:
                        frag_opcode = opcode
                        frag_buf.extend(payload)
                        continue
                    text = payload.decode("utf-8")
                elif opcode == _OP_CONT:
                    frag_buf.extend(payload)
                    if fin:
                        text = bytes(frag_buf).decode("utf-8")
                        frag_buf.clear()
                        frag_opcode = None
                    else:
                        continue
                else:
                    continue
                try:
                    self.on_message(connection, text)
                except Exception:  # 业务回调异常不应断开连接
                    pass
        except (OSError, WSFrameError):
            pass
        finally:
            connection.close()
            with self._lock:
                self._conns.pop(connection.peer_id, None)
            if self.on_disconnect:
                self.on_disconnect(connection)

    def send(self, peer_id: str, text: str) -> None:
        with self._lock:
            conn = self._conns.get(peer_id)
        if conn:
            conn.send(text)

    def broadcast(self, text: str) -> None:
        with self._lock:
            conns = list(self._conns.values())
        for c in conns:
            c.send(text)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class WSClient:
    """极简 WebSocket 客户端（模拟 KubeEdge EdgeHub 长连接）。

    自动完成握手、发送掩码帧、处理 ping/pong，并在断线后按退避策略重连。
    """

    def __init__(self, url: str, on_message: FrameHandler, on_open=None, on_close=None):
        # url 形如 ws://host:port
        if not url.startswith("ws://"):
            raise ValueError("only ws:// supported")
        hostport = url[len("ws://"):]
        self.host, self.port = hostport.split(":")
        self.port = int(self.port)
        self.on_message = on_message
        self.on_open = on_open
        self.on_close = on_close
        self._conn: Optional[Connection] = None
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False

    def connect(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._dial()
                backoff = 1.0
                self._read_loop()
            except (OSError, WSFrameError):
                pass
            finally:
                self.connected = False
                if self.on_close:
                    self.on_close()
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _dial(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        key = base64.b64encode(os_urandom(16)).decode()
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        # 读取响应头
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(1)
            if not chunk:
                raise WSFrameError("no response")
            buf += chunk
        resp_line = buf.split(b"\r\n")[0].decode()
        if "101" not in resp_line:
            raise WSFrameError(f"handshake failed: {resp_line}")
        # 初始连接用 timeout 防卡死；连接建立后改为阻塞，避免空闲连接被 recv 超时误杀
        sock.settimeout(None)
        self._sock = sock
        self._conn = Connection(sock, "client")
        self.connected = True
        if self.on_open:
            self.on_open()

    def _read_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            opcode, payload, fin = _read_frame(self._sock)
            if opcode == _OP_CLOSE:
                break
            if opcode == _OP_PING:
                self._conn.send_pong(payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode in (_OP_TEXT, _OP_BIN, _OP_CONT):
                if opcode != _OP_CONT:
                    text = payload.decode("utf-8")
                else:
                    text = payload.decode("utf-8")
                if fin:
                    try:
                        self.on_message(self._conn, text)
                    except Exception:
                        pass

    def send(self, text: str) -> None:
        if self._conn is None or self._conn.closed:
            return
        mask = os_urandom(4)
        self._conn.send(text, mask=mask)

    def close(self) -> None:
        self._stop.set()
        if self._conn:
            self._conn.close()


def os_urandom(n: int) -> bytes:
    return _urandom(n)


def _urandom(n: int) -> bytes:
    # 使用标准库 secrets 风格的随机掩码
    import secrets
    return secrets.token_bytes(n)
