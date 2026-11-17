"""
A minimal WebSocket Secure client and JSON-RPC wrapper for the Emotiv Cortex
service, written against the Python standard library only.

Cortex listens on wss://localhost:6868 with a self-signed certificate. There is
no WebSocket client in the standard library, so one is implemented here rather
than taking a dependency: the whole protocol surface needed is the opening
handshake, masked text frames out, and unmasked frames in.

Nothing here talks to Emotiv's cloud. Every call is local to this machine.
"""

import base64
import json
import os
import socket
import ssl
import struct
import time

CORTEX_URL_HOST = "localhost"
CORTEX_URL_PORT = 6868


class CortexError(RuntimeError):
    """A JSON-RPC error returned by Cortex, or a transport failure."""


class WebSocket:
    """Just enough RFC 6455 to carry JSON-RPC over TLS to a local service."""

    def __init__(self, host, port, timeout=20.0):
        raw = socket.create_connection((host, port), timeout=timeout)
        # The service presents a self-signed certificate for 'localhost'.
        # Verification is pointless against a loopback peer we already trust
        # by virtue of it being a process on this machine.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake(host, port)

    def _handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CortexError("Cortex closed the connection during handshake")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise CortexError(f"Cortex refused the WebSocket upgrade: {status}")
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CortexError("Cortex closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text):
        payload = text.encode()
        n = len(payload)
        header = bytearray([0x81])  # FIN + text opcode
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        """Return the next complete text message, handling fragmentation."""
        parts = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            # A server frame to a client is never masked.
            data = self._recv_exact(length) if length else b""
            if opcode == 0x8:  # close
                raise CortexError("Cortex closed the WebSocket")
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8a" + bytes([0x80 | len(data)]) + os.urandom(4) + data)
                continue
            if opcode == 0xA:  # pong
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode(errors="replace")

    def close(self):
        try:
            self.sock.sendall(b"\x88\x80" + os.urandom(4))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Cortex:
    """JSON-RPC over the WebSocket, with request/response correlation."""

    def __init__(self, timeout=20.0):
        self.ws = WebSocket(CORTEX_URL_HOST, CORTEX_URL_PORT, timeout=timeout)
        self._id = 0
        self.timeout = timeout
        self.warnings = []   # unsolicited 'warning' messages Cortex pushes
        self.streams = []    # unsolicited data-stream messages

    def call(self, method, params=None, timeout=None):
        """Send one request and return its result, skipping stream traffic."""
        self._id += 1
        want = self._id
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": want,
                                 "method": method, "params": params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            self.ws.sock.settimeout(max(0.5, deadline - time.time()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == want:
                if "error" in msg:
                    err = msg["error"]
                    raise CortexError(f"{method}: {err.get('message')} ({err.get('code')})")
                return msg.get("result")
            if "warning" in msg:
                self.warnings.append(msg["warning"])
            elif "id" not in msg:
                self.streams.append(msg)
        raise CortexError(f"{method}: no reply within {timeout or self.timeout:.0f} s")

    def next_stream(self, timeout=5.0):
        """Return the next pushed stream message, or None on timeout."""
        if self.streams:
            return self.streams.pop(0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.ws.sock.settimeout(max(0.2, deadline - time.time()))
                msg = json.loads(self.ws.recv())
            except (socket.timeout, ssl.SSLError, OSError):
                return None
            if "warning" in msg:
                self.warnings.append(msg["warning"])
            elif "id" not in msg:
                return msg
        return None

    def close(self):
        self.ws.close()
