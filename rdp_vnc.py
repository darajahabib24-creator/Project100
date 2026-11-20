#!/usr/bin/env python3
"""rdp_vnc - authorised RDP/VNC discovery, service check and screen capture.

For systems you OWN or have WRITTEN PERMISSION to test. Every network command
requires the explicit --authorised flag; without it the tool refuses to run.

What it does
------------
  scan    TCP-scan targets for RDP (3389) and VNC (5900-5910) and identify
          which service is listening on each open port.
  check   probe one host:port and report RDP vs VNC, version and capabilities.
  screen  connect to a VNC server and capture the current screen to a PNG.
  connect same capture for VNC; for RDP it reports that a real client
          (xfreerdp) is required and prints the command to run.

It never guesses credentials, never brute-forces, and never logs in to RDP.
VNC login uses a password you supply, on a system you are authorised to access.

Examples
--------
  python3 rdp_vnc.py scan 192.168.1.0/24 --authorised
  python3 rdp_vnc.py check 192.168.1.5:5900 --authorised
  python3 rdp_vnc.py screen 192.168.1.5:5900 --password secret --out screen.png --authorised
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - optional dependency
    Image = ImageDraw = None

try:  # VNC password auth needs single-DES; TripleDES with K1=K2=K3 is DES.
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
except ImportError:  # pragma: no cover - optional dependency
    TripleDES = Cipher = modes = None

import rdp_scan  # reuse the CIDR/hostname expansion helpers

RDP_PORT = 3389
VNC_PORTS = tuple(range(5900, 5911))
DEFAULT_PORTS = "3389,5900-5910"
DEFAULT_TIMEOUT = 4.0

# RFB encodings we advertise and decode. Raw/CopyRect/RRE/Zlib cover the
# common servers; Tight and Hextile are deliberately not requested.
VNC_ENCODINGS = [0, 1, 2, 6]

# 32bpp, depth 24, little-endian, true colour, R/G/B max 255 shifts 16/8/0.
# Bytes on the wire are then B,G,R,X -> PIL raw mode "BGRX".
REQUESTED_PF = bytes([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0,
                      0, 0, 0])

RDP_PROTOCOLS = {0: "RDP", 1: "SSL", 2: "CredSSP/NLA", 8: "CredSSP/NLA (HYBRID_EX)"}


class VNCError(Exception):
    pass


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def parse_ports(spec: str) -> list[int]:
    """'3389,5900-5910,22' -> [3389, 5900..5910, 22]."""
    ports: list[int] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
            if not (1 <= lo <= hi <= 65535):
                raise ValueError(f"bad port range {part!r}")
            ports.extend(range(lo, hi + 1))
        else:
            p = int(part)
            if not (1 <= p <= 65535):
                raise ValueError(f"bad port {p}")
            ports.append(p)
    return ports


def tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def split_target(target: str) -> tuple[str, int]:
    """Accept 'host:port' or ('host', port)."""
    if ":" in target:
        host, _, port = target.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            pass
    raise ValueError(f"expected host:port, got {target!r}")


# --------------------------------------------------------------------------- #
# VNC (RFB 3.8) client
# --------------------------------------------------------------------------- #

def vnc_des_key(password: str) -> bytes:
    """VNC turns the password into an 8-byte DES key, reversing each byte."""
    pw = password.encode("latin-1", "ignore")[:8]
    pw = pw + b"\x00" * (8 - len(pw))
    return bytes(int(f"{b:08b}"[::-1], 2) for b in pw)


def des_encrypt(data: bytes, key8: bytes) -> bytes:
    """Single-DES ECB (TripleDES with K1=K2=K3)."""
    if TripleDES is None:
        raise VNCError("VNC password auth needs 'cryptography'; install it")
    c = Cipher(TripleDES(key8 * 3), modes.ECB()).encryptor()
    return c.update(data) + c.finalize()


class PixelFormat:
    def __init__(self, raw: bytes):
        (self.bpp, self.depth, self.big_endian, self.true_color,
         self.red_max, self.green_max, self.blue_max,
         self.red_shift, self.green_shift, self.blue_shift) = struct.unpack(
            ">BBBBHHHBBB", raw[:13])
        self.raw = raw
        self.bytes_pp = max(1, self.bpp // 8)

    def is_bgrx(self) -> bool:
        return bool(self.bpp == 32 and self.depth == 24 and self.true_color
                    and not self.big_endian
                    and (self.red_max, self.green_max, self.blue_max) == (255, 255, 255)
                    and (self.red_shift, self.green_shift, self.blue_shift) == (16, 8, 0))


class VNCClient:
    """Minimal RFB 3.8 client: handshake, security, framebuffer capture."""

    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT,
                 password: str | None = None):
        self.host, self.port = host, port
        self.timeout = timeout
        self.password = password
        self.sock: socket.socket | None = None
        self.version = ""
        self.security_types: list[int] = []
        self.width = self.height = 0
        self.name = ""
        self.pf: PixelFormat | None = None

    # -- wire helpers ------------------------------------------------------ #

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise VNCError("connection closed by server")
            buf += chunk
        return buf

    def _send(self, data: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(data)

    # -- handshake --------------------------------------------------------- #

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        ver = self._recv_exact(12)
        if not ver.startswith(b"RFB "):
            raise VNCError(f"not a VNC server (banner {ver[:12]!r})")
        self.version = ver.decode("latin-1").strip()
        self._send(b"RFB 003.008\n")

        n = self._recv_exact(1)[0]
        if n == 0:
            ln = struct.unpack(">I", self._recv_exact(4))[0]
            raise VNCError("VNC refused: " +
                           self._recv_exact(ln).decode("latin-1", "replace"))
        self.security_types = list(self._recv_exact(n))

        if 2 in self.security_types and self.password is not None:
            self._send(b"\x02")
            self._vnc_auth()
        elif 1 in self.security_types:
            self._send(b"\x01")
        elif 2 in self.security_types:
            raise VNCError("server requires a VNC password; pass --password")
        else:
            raise VNCError(f"no supported security type: {self.security_types}")

        self._client_init()

    def _vnc_auth(self) -> None:
        challenge = self._recv_exact(16)
        self._send(des_encrypt(challenge, vnc_des_key(self.password or "")))
        result = struct.unpack(">I", self._recv_exact(4))[0]
        if result != 0:
            raise VNCError(f"VNC authentication failed (code {result})")

    def _client_init(self) -> None:
        self._send(b"\x01")  # shared flag
        self.width, self.height = struct.unpack(">HH", self._recv_exact(4))
        self.pf = PixelFormat(self._recv_exact(16))
        name_len = struct.unpack(">I", self._recv_exact(4))[0]
        self.name = self._recv_exact(name_len).decode("utf-8", "replace")

    # -- pixel / drawing --------------------------------------------------- #

    def _pixel_rgb(self, chunk: bytes) -> tuple[int, int, int]:
        pf = self.pf
        assert pf is not None
        v = int.from_bytes(chunk, "big" if pf.big_endian else "little")
        r = (v >> pf.red_shift) & pf.red_max
        g = (v >> pf.green_shift) & pf.green_max
        b = (v >> pf.blue_shift) & pf.blue_max
        if pf.red_max:
            r = r * 255 // pf.red_max
        if pf.green_max:
            g = g * 255 // pf.green_max
        if pf.blue_max:
            b = b * 255 // pf.blue_max
        return (r, g, b)

    def _raw_image(self, w: int, h: int, data: bytes) -> "Image.Image":
        pf = self.pf
        assert pf is not None
        if pf.is_bgrx():
            return Image.frombytes("RGB", (w, h), data, "raw", "BGRX")
        img = Image.new("RGB", (w, h))
        px = img.load()
        bpp = pf.bytes_pp
        for row in range(h):
            base = row * w * bpp
            for col in range(w):
                px[col, row] = self._pixel_rgb(data[base + col * bpp:
                                                    base + (col + 1) * bpp])
        return img

    def _fill(self, img, x, y, w, h, color) -> None:
        ImageDraw.rectangle(img, [x, y, x + w - 1, y + h - 1], fill=color)

    # -- messages ---------------------------------------------------------- #

    def set_pixel_format(self) -> None:
        self._send(b"\x00\x00\x00\x00" + REQUESTED_PF)  # type + 3 pad + 16
        self.pf = PixelFormat(REQUESTED_PF)

    def set_encodings(self) -> None:
        payload = struct.pack(">BBH", 2, 0, len(VNC_ENCODINGS))
        payload += b"".join(struct.pack(">i", e) for e in VNC_ENCODINGS)
        self._send(payload)

    def request_update(self) -> None:
        self._send(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))

    def _read_message(self):
        mtype = self._recv_exact(1)[0]
        if mtype == 0:
            return self._read_framebuffer_update()
        if mtype == 1:  # SetColourMapEntries
            self._recv_exact(3)
            n = struct.unpack(">H", self._recv_exact(2))[0]
            self._recv_exact(n * 6)
            return None
        if mtype == 2:  # Bell
            return None
        if mtype == 3:  # ServerCutText
            self._recv_exact(3)
            ln = struct.unpack(">I", self._recv_exact(4))[0]
            self._recv_exact(ln)
            return None
        raise VNCError(f"unknown server message type {mtype}")

    def _read_framebuffer_update(self) -> "Image.Image":
        self._recv_exact(1)  # padding
        n = struct.unpack(">H", self._recv_exact(2))[0]
        img = Image.new("RGB", (self.width, self.height))
        for _ in range(n):
            x, y, w, h = struct.unpack(">HHHH", self._recv_exact(8))
            enc = struct.unpack(">i", self._recv_exact(4))[0]
            self._decode_rect(img, x, y, w, h, enc)
        return img

    def _decode_rect(self, img, x, y, w, h, enc) -> None:
        assert self.pf is not None
        if enc == 0:  # Raw
            self._paste_raw(img, x, y, w, h, self._recv_exact(w * h * self.pf.bytes_pp))
        elif enc == 6:  # Zlib (raw pixels, zlib-compressed)
            ln = struct.unpack(">I", self._recv_exact(4))[0]
            data = zlib.decompress(self._recv_exact(ln))
            self._paste_raw(img, x, y, w, h, data)
        elif enc == 1:  # CopyRect
            sx, sy = struct.unpack(">HH", self._recv_exact(4))
            img.paste(img.crop((sx, sy, sx + w, sy + h)), (x, y))
        elif enc == 2:  # RRE
            nsub = struct.unpack(">I", self._recv_exact(4))[0]
            bg = self._pixel_rgb(self._recv_exact(self.pf.bytes_pp))
            self._fill(img, x, y, w, h, bg)
            for _ in range(nsub):
                color = self._pixel_rgb(self._recv_exact(self.pf.bytes_pp))
                px, py, pw, ph = struct.unpack(">HHHH", self._recv_exact(8))
                self._fill(img, x + px, y + py, pw, ph, color)
        else:
            raise VNCError(f"unsupported encoding {enc} (server ignored our list)")

    def _paste_raw(self, img, x, y, w, h, data) -> None:
        img.paste(self._raw_image(w, h, data), (x, y))

    def capture(self) -> "Image.Image":
        """Connect (if needed) and return the current screen as a PIL image."""
        if self.sock is None:
            self.connect()
        self.set_pixel_format()
        self.set_encodings()
        self.request_update()
        while True:
            img = self._read_message()
            if img is not None:
                return img

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# --------------------------------------------------------------------------- #
# RDP detection (X.224 Connection Request / Negotiation Response)
# --------------------------------------------------------------------------- #

def _rdp_connection_request() -> bytes:
    """TPKT + X.224 CR carrying an RDP Negotiation Request (all protocols)."""
    negot = bytes([0x01, 0x00, 0x08, 0x00, 0x0F, 0x00, 0x00, 0x00])
    cr = bytes([0xE0, 0x00, 0x00, 0x00, 0x00, 0x00])
    body = bytes([len(cr)]) + cr + negot
    total = 4 + len(body)
    return bytes([0x03, 0x00]) + struct.pack(">H", total) + body


def _recv_tpkt(sock) -> bytes:
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            return head
        head += chunk
    if len(head) < 4:
        return head
    total = struct.unpack(">H", head[2:4])[0]
    body = b""
    while len(body) < total - 4:
        chunk = sock.recv(total - 4 - len(body))
        if not chunk:
            break
        body += chunk
    return head + body


def _parse_rdp_response(data: bytes) -> dict:
    if len(data) < 6 or data[0] != 0x03:
        return {"service": "unknown", "reason": "no RDP negotiation response"}
    x224 = data[5] if len(data) > 5 else None
    if x224 != 0xD0:
        return {"service": "unknown", "reason": f"X.224 code 0x{x224 or 0:02X}"}
    # scan for an RDP Negotiation Response (type 0x02) or Failure (0x06)
    for i in range(6, len(data) - 7):
        if data[i] == 0x02:
            selected = struct.unpack("<I", data[i + 4:i + 8])[0]
            return {"service": "rdp", "selected_protocol": selected,
                    "protocol_name": RDP_PROTOCOLS.get(selected, f"0x{selected:08X}")}
        if data[i] == 0x06:
            return {"service": "rdp", "error": "negotiation failed"}
    return {"service": "rdp", "selected_protocol": None,
            "protocol_name": "RDP (no negotiation response)"}


def identify(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return {'service': 'vnc'|'rdp'|'unknown'|'none', ...} for host:port."""
    try:
        sock = socket.create_connection((host, port), timeout)
    except OSError as exc:
        return {"service": "none", "host": host, "port": port,
                "error": f"connect: {exc}"}
    sock.settimeout(max(1.0, min(timeout, 3.0)))
    banner = b""
    try:
        while len(banner) < 12:
            chunk = sock.recv(12 - len(banner))
            if not chunk:
                break
            banner += chunk
    except OSError:
        pass
    if banner.startswith(b"RFB "):
        sock.close()
        return {"service": "vnc", "host": host, "port": port,
                "version": banner.decode("latin-1").strip()}
    sock.settimeout(timeout)
    try:
        sock.sendall(_rdp_connection_request())
        data = _recv_tpkt(sock)
    except OSError as exc:
        sock.close()
        return {"service": "unknown", "host": host, "port": port,
                "error": str(exc)}
    sock.close()
    out = _parse_rdp_response(data)
    out.update({"host": host, "port": port})
    return out


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_scan(args) -> int:
    ports = parse_ports(args.ports)
    items = []
    for t in args.targets:
        items.extend(rdp_scan.expand_target(t, ports))
    results: list[dict] = []

    def one(item):
        host, port, err = item
        if err is not None:
            return {"host": host, "port": port, "state": "error", "error": err}
        if not tcp_open(host, port, args.timeout):
            return {"host": host, "port": port, "state": "closed"}
        res = {"host": host, "port": port, "state": "open"}
        if args.identify:
            res.update(identify(host, port, args.timeout))
        return res

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = [ex.submit(one, it) for it in items]
        for f in as_completed(futs):
            results.append(f.result())
            if args.delay:
                time.sleep(args.delay)

    results.sort(key=lambda r: (r["host"], r["port"]))
    if args.json:
        for r in results:
            print(json.dumps(r))
        return 0

    open_r = [r for r in results if r["state"] == "open"]
    closed = sum(1 for r in results if r["state"] == "closed")
    errors = sum(1 for r in results if r["state"] == "error")
    print("RDP/VNC SCAN\n" + "=" * 50)
    print("targets: " + ", ".join(args.targets))
    print("ports:   " + ", ".join(str(p) for p in ports))
    print("-" * 50)
    print(f"open: {len(open_r)}   closed: {closed}   error: {errors}")
    for i, r in enumerate(open_r, 1):
        svc = r.get("service", "open")
        extra = ""
        if svc == "vnc":
            extra = f"  VNC {r.get('version', '')}"
        elif svc == "rdp":
            extra = f"  RDP {r.get('protocol_name', '')}"
        print(f"{i:>3}. {r['host']:<18} tcp/{r['port']:<5}{extra}")
    return 0


def cmd_check(args) -> int:
    host, port = split_target(args.target)
    out = identify(host, port, args.timeout)
    if args.json:
        print(json.dumps(out))
    else:
        print(json.dumps(out, indent=2))
    return 0


def _capture_vnc(host: str, port: int, password: str | None, out: str | None,
                 timeout: float) -> str:
    if Image is None:
        raise SystemExit("error: screen capture needs Pillow (pip install pillow)")
    client = VNCClient(host, port, timeout, password)
    try:
        img = client.capture()
    finally:
        client.close()
    out = out or f"vnc_{host.replace(':', '_').replace('.', '_')}_{port}.png"
    img.save(out)
    return out


def cmd_screen(args) -> int:
    host, port = split_target(args.target)
    path = _capture_vnc(host, port, args.password, args.out, args.timeout)
    if args.json:
        print(json.dumps({"saved": path}))
    else:
        print(f"saved {path}")
    return 0


def cmd_connect(args) -> int:
    host, port = split_target(args.target)
    info = identify(host, port, args.timeout)
    svc = info.get("service")
    if svc == "vnc":
        path = _capture_vnc(host, port, args.password, args.out, args.timeout)
        print(f"connected to VNC {host}:{port}; screen saved to {path}")
        return 0
    if svc == "rdp":
        exe = _find(["xfreerdp3", "xfreerdp", "rdesktop"])
        if exe:
            cmd = f"{exe} /v:{host}:{port}"
            print(f"RDP connect needs a client. Run: {cmd}")
        else:
            print("RDP detected, but no RDP client is installed "
                  "(xfreerdp/rdesktop). Install one to connect.")
        return 3
    print(f"no RDP/VNC service at {host}:{port} ({info.get('error', svc)})")
    return 1


def _find(names):
    import shutil
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def require_authorised(args) -> None:
    if not getattr(args, "authorised", False):
        print("refusing to run: this tool scans or connects to remote systems.\n"
              "Pass --authorised ONLY if you own the target or have written\n"
              "permission to test it.", file=sys.stderr)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rdp_vnc",
        description="Authorised RDP/VNC scan, check and screen capture.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, needs_auth=True):
        if needs_auth:
            p.add_argument("--authorised", action="store_true",
                           help="you own the target or have written permission")
        p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                       help=f"per-connection timeout (default {DEFAULT_TIMEOUT})")
        p.add_argument("--json", action="store_true", help="JSON output")

    p = sub.add_parser("scan", help="scan targets for RDP/VNC")
    p.add_argument("targets", nargs="+", help="hosts or CIDRs")
    p.add_argument("--ports", default=DEFAULT_PORTS,
                   help=f"ports/ranges (default {DEFAULT_PORTS})")
    p.add_argument("--threads", type=int, default=16)
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--identify", action="store_true",
                   help="probe open ports to identify RDP vs VNC")
    common(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("check", help="identify the service on one host:port")
    p.add_argument("target", help="host:port")
    common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("screen", help="capture a VNC screen to PNG")
    p.add_argument("target", help="host:port")
    p.add_argument("--password", help="VNC password")
    p.add_argument("--out", help="output PNG path")
    common(p)
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("connect", help="connect (VNC capture / RDP client hint)")
    p.add_argument("target", help="host:port")
    p.add_argument("--password", help="VNC password")
    p.add_argument("--out", help="output PNG path")
    common(p)
    p.set_defaults(func=cmd_connect)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    require_authorised(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
