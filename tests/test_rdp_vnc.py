#!/usr/bin/env python3
"""Tests for rdp_vnc.py - mock VNC (RFB) and RDP (X.224) servers, no network."""

import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rdp_vnc
from rdp_vnc import (REQUESTED_PF, PixelFormat, VNCClient, VNCError, des_encrypt,
                     identify, parse_ports, vnc_des_key)

SO_REUSEADDR = socket.SO_REUSEADDR


def _read_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("connection closed")
        buf += chunk
    return buf


class MockVNC:
    """A tiny RFB 3.8 server: handshake, optional VNC auth, one raw update."""

    def __init__(self, auth=False, password="pw", color=(0, 0, 255, 0)):
        self.auth = auth
        self.password = password
        self.color = color
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def __enter__(self):
        return self.port

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass
        return False

    def _run(self):
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(5)
            conn.sendall(b"RFB 003.008\n")
            _read_exact(conn, 12)                      # client version
            if self.auth:
                conn.sendall(b"\x01\x02")              # one type: VNC auth
                _read_exact(conn, 1)                   # chosen type
                challenge = bytes(range(16))
                conn.sendall(challenge)
                resp = _read_exact(conn, 16)
                ok = resp == des_encrypt(challenge, vnc_des_key(self.password))
                conn.sendall(struct.pack(">I", 0 if ok else 1))
            else:
                conn.sendall(b"\x01\x01")              # one type: None
                _read_exact(conn, 1)                   # chosen type
            _read_exact(conn, 1)                       # ClientInit shared flag
            conn.sendall(struct.pack(">HH", 4, 4) + REQUESTED_PF
                         + struct.pack(">I", 4) + b"mock")
            # SetPixelFormat: type 0 + 3 pad + 16
            assert _read_exact(conn, 1) == b"\x00"
            _read_exact(conn, 3 + 16)
            # SetEncodings: type 2 + pad + count + N*4
            assert _read_exact(conn, 1) == b"\x02"
            _read_exact(conn, 1)
            n = struct.unpack(">H", _read_exact(conn, 2))[0]
            _read_exact(conn, 4 * n)
            # FramebufferUpdateRequest: type 3 + incremental + 4x uint16
            assert _read_exact(conn, 1) == b"\x03"
            _read_exact(conn, 1 + 8)
            conn.sendall(struct.pack(">BBH", 0, 0, 1))  # FramebufferUpdate
            conn.sendall(struct.pack(">HHHH", 0, 0, 4, 4))
            conn.sendall(struct.pack(">i", 0))          # Raw
            conn.sendall(bytes(self.color) * 16)
            time.sleep(0.2)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            self.error = exc


class MockRDP:
    """A tiny X.224 server that answers a CR with a negotiation response."""

    def __init__(self, selected=2):
        self.selected = selected
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def __enter__(self):
        return self.port

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass
        return False

    def _run(self):
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(5)
            _read_exact(conn, 19)                      # the CR
            negot = bytes([0x02, 0x00, 0x08, 0x00]) + struct.pack(
                "<I", self.selected)
            cc = bytes([0xD0, 0x00, 0x00, 0x00, 0x00, 0x00])
            body = bytes([len(cc)]) + cc + negot
            conn.sendall(bytes([0x03, 0x00]) + struct.pack(">H", 4 + len(body))
                         + body)
            time.sleep(0.2)
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #

def test_parse_ports():
    assert parse_ports("3389,5900-5902") == [3389, 5900, 5901, 5902]
    assert parse_ports("22") == [22]
    assert parse_ports("5900-5910")[0] == 5900
    assert parse_ports("5900-5910")[-1] == 5910


def test_vnc_des_key():
    # bits of each byte reversed; password padded to 8 with NULs
    assert vnc_des_key("12345678") == bytes(
        [0x8C, 0x4C, 0xCC, 0x2C, 0xAC, 0x6C, 0xEC, 0x1C])
    assert len(vnc_des_key("ab")) == 8
    assert vnc_des_key("ab")[2:] == b"\x00" * 6


def test_des_encrypt_is_single_des():
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    key = b"12345678"
    data = bytes(range(8))
    enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    assert des_encrypt(data, key) == enc.update(data) + enc.finalize()


def test_pixel_format_bgrx():
    pf = PixelFormat(REQUESTED_PF)
    assert pf.is_bgrx()
    assert pf.bytes_pp == 4


def test_vnc_none_capture():
    with MockVNC(auth=False) as port:
        client = VNCClient("127.0.0.1", port, timeout=5)
        img = client.capture()
        client.close()
    assert img.size == (4, 4)
    assert img.getpixel((0, 0)) == (255, 0, 0)
    assert img.getpixel((3, 3)) == (255, 0, 0)


def test_vnc_auth_capture():
    with MockVNC(auth=True, password="pw") as port:
        client = VNCClient("127.0.0.1", port, timeout=5, password="pw")
        img = client.capture()
        client.close()
    assert img.getpixel((0, 0)) == (255, 0, 0)


def test_vnc_auth_wrong_password():
    with MockVNC(auth=True, password="right") as port:
        client = VNCClient("127.0.0.1", port, timeout=5, password="wrong")
        try:
            client.connect()
        except VNCError:
            pass
        else:
            raise AssertionError("expected VNCError for wrong password")
        finally:
            client.close()


def test_identify_vnc():
    with MockVNC(auth=False) as port:
        out = identify("127.0.0.1", port, timeout=1.0)
    assert out["service"] == "vnc"
    assert "003.008" in out["version"]


def test_identify_rdp():
    with MockRDP(selected=2) as port:
        out = identify("127.0.0.1", port, timeout=1.0)
    assert out["service"] == "rdp"
    assert out["selected_protocol"] == 2
    assert "CredSSP" in out["protocol_name"]


def test_requires_authorised():
    try:
        rdp_vnc.main(["scan", "127.0.0.1", "--ports", "3389"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit without --authorised")


def test_cli_screen(tmp_path):
    with MockVNC(auth=True, password="pw") as port:
        out = str(tmp_path / "screen.png")
        rc = rdp_vnc.main(["screen", f"127.0.0.1:{port}", "--password", "pw",
                           "--out", out, "--authorised"])
        assert rc == 0
        assert os.path.exists(out)
        assert os.path.getsize(out) > 0


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
