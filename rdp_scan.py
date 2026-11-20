#!/usr/bin/env python3
"""rdp_scan.py - a professional RDP (Remote Desktop Protocol) scanner.

Scans hosts for RDP listeners, negotiates the X.224/RDP security layer and
reports what it finds: whether Network Level Authentication (CredSSP/NLA) is
required, whether only legacy Standard RDP Security is offered, the TLS
certificate and protocol versions, and - optionally - whether the listener
answers the non-intrusive MS_T120 probe used to flag CVE-2019-0708 (BlueKeep)
on listeners that do not require NLA.

Only scan hosts you own or are authorised to test. The BlueKeep check sends a
small, crash-safe channel probe; it never sends an exploit payload.

Usage:
  python3 rdp_scan.py 192.168.1.0/24
  python3 rdp_scan.py 10.0.0.1:3389 --bluekeep --json
  python3 rdp_scan.py @targets.txt --ports 3389,3390 --csv out.csv
"""

from __future__ import annotations

import asyncio
import argparse
import csv
import ipaddress
import json
import re
import socket
import ssl
import sys
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

# --- MS-RDPBCGR 2.2.1.1.1 / 2.2.1.2.1 requested/selected protocol flags ---- #
PROTOCOL_RDP = 0x00000000        # Standard RDP Security (legacy)
PROTOCOL_SSL = 0x00000001        # TLS 1.0/1.1/1.2
PROTOCOL_HYBRID = 0x00000002     # CredSSP (NLA)
PROTOCOL_RDSTLS = 0x00000004     # RDSTLS
PROTOCOL_HYBRID_EX = 0x00000008  # CredSSP with early user auth

PROTOCOL_NAMES = {
    PROTOCOL_RDP: "standard",
    PROTOCOL_SSL: "tls",
    PROTOCOL_HYBRID: "nla",
    PROTOCOL_RDSTLS: "rdstls",
    PROTOCOL_HYBRID_EX: "nla-early",
}

# MS-RDPBCGR 2.2.1.2.2 negotiation failure codes
NEG_FAILURES = {
    0x01: "SSL_REQUIRED_BY_SERVER",
    0x02: "SSL_NOT_ALLOWED_BY_SERVER",
    0x03: "SSL_CERT_NOT_ON_SERVER",
    0x04: "INCONSISTENT_FLAGS",
    0x05: "HYBRID_REQUIRED_BY_SERVER",
    0x06: "SSL_WITH_USER_AUTH_REQUIRED_BY_SERVER",
}

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


# --------------------------------------------------------------------------- #
# target / port parsing
# --------------------------------------------------------------------------- #

def parse_ports(spec: str) -> list[int]:
    """Parse a port spec: '3389', '3389,3390', '3388-3390'. Sorted, deduped."""
    ports: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                raise ValueError(f"bad port range {part!r}")
            for p in range(lo, hi + 1):
                _check_port(p)
            ports.update(range(lo, hi + 1))
        else:
            p = int(part)
            _check_port(p)
            ports.add(p)
    if not ports:
        raise ValueError(f"no ports in {spec!r}")
    return sorted(ports)


def _check_port(p: int) -> None:
    if not 1 <= p <= 65535:
        raise ValueError(f"port out of range: {p}")


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _expand_spec(spec: str) -> list[str]:
    """Expand one target spec into a list of host strings."""
    spec = spec.strip()
    if not spec:
        return []
    if "/" in spec:
        try:
            net = ipaddress.ip_network(spec, strict=False)
        except ValueError as exc:
            raise ValueError(f"bad network {spec!r}: {exc}") from exc
        return [str(h) for h in net]
    if "-" in spec and _is_ip(spec.split("-", 1)[0]) and _is_ip(spec.split("-", 1)[1]):
        lo_s, hi_s = spec.split("-", 1)
        lo, hi = ipaddress.ip_address(lo_s), ipaddress.ip_address(hi_s)
        if lo.version != hi.version or int(lo) > int(hi):
            raise ValueError(f"bad range {spec!r}")
        out = []
        cur = int(lo)
        while cur <= int(hi):
            out.append(str(ipaddress.ip_address(cur)))
            cur += 1
        return out
    if not (_is_ip(spec) or _HOSTNAME_RE.match(spec)):
        raise ValueError(f"bad target {spec!r}")
    return [spec]


def parse_targets(specs, ports) -> list[tuple[str, int]]:
    """Expand target specs into (host, port) pairs.

    Specs may be a host, host:port, CIDR, an a-b range, or @file (one target
    per line, '#' comments). An explicit host:port wins over --ports.
    """
    pairs: list[tuple[str, int]] = []
    for spec in specs:
        spec = str(spec).strip()
        if not spec:
            continue
        if spec.startswith("@"):
            path = spec[1:]
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            pairs.extend(parse_targets(
                [ln.split("#", 1)[0].strip() for ln in lines], ports))
            continue
        if spec.startswith("[") and "]:" in spec:
            host, port_s = spec.split("]:", 1)
            host = host[1:]
            _check_port(int(port_s))
            pairs.append((host, int(port_s)))
            continue
        if spec.count(":") == 1 and not spec.startswith("["):
            host, port_s = spec.rsplit(":", 1)
            _check_port(int(port_s))
            for h in _expand_spec(host):
                pairs.append((h, int(port_s)))
            continue
        for h in _expand_spec(spec):
            for p in ports:
                pairs.append((h, p))
    return pairs


# --------------------------------------------------------------------------- #
# X.224 / RDP negotiation
# --------------------------------------------------------------------------- #

def build_connection_request(requested_protocols: int, cookie: str | None = None) -> bytes:
    """Build a TPKT-wrapped X.224 Connection Request with RDP Negotiation Request."""
    body = bytearray()
    body += b"\x00\x00"          # dst-reference
    body += b"\x00\x00"          # src-reference
    body += b"\x00"              # class and options
    if cookie is not None:
        body += f"Cookie: mstshash={cookie}\r\n".encode()
    body += bytes([0x01, 0x00, 0x08, 0x00])          # RDP_NEG_REQ header
    body += int(requested_protocols).to_bytes(4, "little")
    x224 = bytes([len(body) + 1, 0xE0]) + bytes(body)
    return b"\x03\x00" + (len(x224) + 4).to_bytes(2, "big") + x224


def parse_connection_confirm(data: bytes) -> dict:
    """Parse an X.224 Connection Confirm into selected/failure protocol fields."""
    if len(data) < 11 or data[0] != 0x03 or data[5] != 0xD0:
        raise ValueError("not an X.224 Connection Confirm")
    result = {"selected_protocol": None, "failure_code": None,
              "bare_confirm": len(data) == 11}
    if len(data) == 11:
        # Windows XP/2003 send a bare confirm with no negotiation payload.
        return result
    if len(data) < 19:
        raise ValueError("negotiation payload truncated")
    neg_type = data[11]
    value = int.from_bytes(data[15:19], "little")
    if neg_type == 0x02:
        result["selected_protocol"] = value
    elif neg_type == 0x03:
        result["failure_code"] = value
    else:
        raise ValueError(f"unknown negotiation type {neg_type:#x}")
    return result


def classify_protocol(selected_protocol: int | None) -> str:
    """Turn a selectedProtocol bitmask into a short security label."""
    if selected_protocol is None:
        return "unknown"
    if selected_protocol & PROTOCOL_HYBRID_EX:
        return PROTOCOL_NAMES[PROTOCOL_HYBRID_EX]
    if selected_protocol & PROTOCOL_HYBRID:
        return PROTOCOL_NAMES[PROTOCOL_HYBRID]
    if selected_protocol & PROTOCOL_RDSTLS:
        return PROTOCOL_NAMES[PROTOCOL_RDSTLS]
    if selected_protocol & PROTOCOL_SSL:
        return PROTOCOL_NAMES[PROTOCOL_SSL]
    if selected_protocol == PROTOCOL_RDP:
        return PROTOCOL_NAMES[PROTOCOL_RDP]
    return "unknown"


def protocol_label(selected_protocol: int | None) -> str:
    """Human-readable name for a selectedProtocol value."""
    if selected_protocol is None:
        return "standard (no negotiation response)"
    if selected_protocol == PROTOCOL_RDP:
        return "Standard RDP Security"
    names = [name for flag, name in PROTOCOL_NAMES.items()
             if selected_protocol & flag and flag]
    return " | ".join(names) if names else f"unknown ({selected_protocol:#x})"


# --------------------------------------------------------------------------- #
# results and findings
# --------------------------------------------------------------------------- #

@dataclass
class ScanResult:
    host: str
    port: int
    state: str = "open"          # open | closed | filtered | error
    selected_protocol: int | None = None
    protocol_label: str = ""
    tls_version: str | None = None
    tls_cipher: str | None = None
    cert: dict | None = None
    bluekeep: str = "not-run"    # vulnerable | safe | skipped-nla | error | not-run
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Finding:
    host: str
    port: int
    id: str
    severity: str              # critical | high | medium | low | info
    title: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

_WEAK_TLS_VERSIONS = {"SSLv2", "SSLv3", "TLSv1", "TLSv1.1"}
_WEAK_CIPHER_MARKERS = ("RC4", "DES", "3DES", "MD5", "NULL", "EXPORT")


def parse_certificate(der: bytes) -> dict:
    """Parse a DER X.509 certificate into a flat dict. Never raises."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        return {"error": "cryptography module not available; cannot parse certificate"}
    try:
        cert = x509.load_der_x509_certificate(der)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"cannot parse certificate: {exc}"}

    def _cn(name):
        attrs = name.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        return attrs[0].value if attrs else ""

    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except Exception:  # noqa: BLE001
        san = []
    nb = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
    na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    now = datetime.now(timezone.utc)
    if nb.tzinfo is None:
        nb = nb.replace(tzinfo=timezone.utc)
    if na.tzinfo is None:
        na = na.replace(tzinfo=timezone.utc)
    return {
        "subject_cn": _cn(cert.subject),
        "issuer_cn": _cn(cert.issuer),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "san": san,
        "not_before": nb.isoformat(),
        "not_after": na.isoformat(),
        "self_signed": cert.subject == cert.issuer,
        "expired": na < now,
        "not_yet_valid": nb > now,
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "serial": format(cert.serial_number, "x"),
    }


def assess_findings(result: ScanResult) -> list[Finding]:
    """Turn a ScanResult into a prioritised list of Findings."""
    if result.state != "open":
        return []
    out: list[Finding] = []

    def add(fid, severity, title, detail=""):
        out.append(Finding(result.host, result.port, fid, severity, title, detail))

    add("rdp-open", "info", "RDP listener open",
        f"{result.host}:{result.port} speaks MS-RDPBCGR")

    sel = result.selected_protocol
    if sel is not None and not (sel & (PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX)):
        add("nla-disabled", "high", "Network Level Authentication not required",
            "Clients can reach the logon screen before authenticating; "
            "credentials and sessions are exposed to network attackers.")
    if sel == PROTOCOL_RDP:
        add("standard-rdp-security", "high", "Legacy Standard RDP Security only",
            "No TLS and no CredSSP: traffic is encrypted with the legacy "
            "RDP RC4 scheme, which is weak and vulnerable to MitM.")

    if result.tls_version in _WEAK_TLS_VERSIONS:
        add("weak-tls", "medium", "Weak TLS version",
            f"Negotiated {result.tls_version}; TLS 1.2 or newer is expected.")
    if result.tls_cipher and any(m in result.tls_cipher
                                 for m in _WEAK_CIPHER_MARKERS):
        add("weak-tls", "medium", "Weak TLS cipher",
            f"Negotiated cipher {result.tls_cipher}.")

    cert = result.cert or {}
    if cert.get("self_signed"):
        add("cert-self-signed", "low", "Self-signed RDP certificate",
            f"Subject CN={cert.get('subject_cn', '?')!r}.")
    if cert.get("expired"):
        add("cert-expired", "medium", "Expired RDP certificate",
            f"Expired at {cert.get('not_after', '?')}.")
    if cert.get("not_yet_valid"):
        add("cert-not-yet-valid", "medium", "RDP certificate not yet valid",
            f"Valid from {cert.get('not_before', '?')}.")

    if result.bluekeep == "vulnerable":
        add("bluekeep-possible", "critical",
            "CVE-2019-0708 (BlueKeep) probe matched",
            "The listener did not require NLA and answered the MS_T120 "
            "channel probe like an unpatched system. Treat as exploitable "
            "until patched and re-tested.")
    elif result.bluekeep == "safe":
        add("bluekeep-safe", "info", "BlueKeep probe: no match",
            "The MS_T120 channel probe did not trigger the unpatched response.")
    elif result.bluekeep == "skipped-nla":
        add("bluekeep-not-applicable", "info", "BlueKeep probe skipped",
            "NLA is required, which mitigates CVE-2019-0708.")
    elif result.bluekeep == "skipped-standard-rdp":
        add("bluekeep-not-run", "info", "BlueKeep probe not run",
            "Server offers only Standard RDP Security; the MS_T120 probe "
            "needs a TLS session, so BlueKeep status is unknown.")
    elif result.bluekeep == "error":
        add("bluekeep-error", "info", "BlueKeep probe failed",
            "The MS_T120 probe could not complete; result unknown.")

    return out


# --------------------------------------------------------------------------- #
# network probe
# --------------------------------------------------------------------------- #

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return bytes(buf)


def _recv_tpkt(sock: socket.socket) -> bytes:
    """Read one TPKT packet (header + body) from a blocking socket."""
    hdr = _recv_exact(sock, 4)
    if hdr[0] != 0x03:
        raise ValueError("not a TPKT packet")
    length = int.from_bytes(hdr[2:4], "big")
    if length < 4:
        raise ValueError("bad TPKT length")
    body = _recv_exact(sock, length - 4) if length > 4 else b""
    return hdr + body


def _negotiate(host: str, port: int, timeout: float, cookie: str | None):
    """Connect and negotiate the RDP security layer.

    Returns (socket, selected_protocol). One retry is attempted when the
    server rejects the first proposal (legacy servers, SSL-only servers).
    """
    requested = PROTOCOL_SSL | PROTOCOL_HYBRID
    last_failure = None
    for attempt in (0, 1):
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        try:
            sock.sendall(build_connection_request(requested, cookie))
            cc = parse_connection_confirm(_recv_tpkt(sock))
        except Exception:
            sock.close()
            raise
        if cc["selected_protocol"] is not None:
            return sock, cc["selected_protocol"]
        if cc["bare_confirm"]:
            # Windows XP/2003: bare Connection Confirm == Standard RDP Security.
            return sock, PROTOCOL_RDP
        last_failure = cc["failure_code"]
        sock.close()
        if attempt == 0 and last_failure in (0x01, 0x02, 0x05, 0x06):
            requested = {
                0x01: PROTOCOL_SSL,                       # SSL required
                0x02: PROTOCOL_RDP,                       # SSL not allowed
                0x05: PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX,
                0x06: PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX,
            }[last_failure]
            continue
        break
    raise ValueError(f"negotiation failed: "
                     f"{NEG_FAILURES.get(last_failure, hex(last_failure or 0))}")


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Deliberately allow TLS 1.0 so legacy listeners are still fingerprinted
    # (and reported as weak). Python 3.14 deprecates the enum, hence the guard.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, AttributeError):  # very old OpenSSL
            pass
    return ctx


def _upgrade_tls(sock: socket.socket, host: str, result: ScanResult):
    """Wrap sock in TLS, record version/cipher/cert, and return the TLS socket.

    Returns None (and records the reason) if the handshake fails.
    """
    try:
        tls = _tls_context().wrap_socket(sock, server_hostname=host)
    except (ssl.SSLError, OSError) as exc:
        result.error = f"TLS handshake failed: {exc}"
        return None
    result.tls_version = tls.version()
    cipher = tls.cipher()
    result.tls_cipher = cipher[0] if cipher else None
    der = tls.getpeercert(binary_form=True)
    if der:
        result.cert = parse_certificate(der)
    return tls


# --------------------------------------------------------------------------- #
# CVE-2019-0708 (BlueKeep) MS_T120 probe
# --------------------------------------------------------------------------- #
#
# A listener that does not require NLA is probed by binding the MS_T120 static
# virtual channel outside its normal slot (channel 1005 instead of 31) and
# sending a small, crash-safe payload on it. An unpatched termdd answers with
# an MCS Disconnect Provider Ultimatum; a patched one ignores it.
# References: MS-RDPBCGR 2.2.1.4 (GCC), 2.2.2.3 (Ultimatum).

_MCS_CHANNELS = (
    ("cliprdr", 0xC0A00000),
    ("MS_T120", 0x80800000),
    ("rdpsnd", 0xC0000000),
    ("snddbg", 0xC0000000),
    ("rdpdr", 0x80800000),
)

_ULTIMATUM = bytes.fromhex("0300000902f0802180")
_BLUEKEEP_X86 = bytes.fromhex("00000000020000000000000000000000")
_BLUEKEEP_X64 = bytes.fromhex(
    "0000000000000000020000000000000000000000000000000000000000000000")


def _ber_int(value: int) -> bytes:
    if value < 2 ** 8:
        raw = value.to_bytes(1, "big")
    elif value < 2 ** 16:
        raw = value.to_bytes(2, "big")
    else:
        raw = value.to_bytes(4, "big")
    return b"\x02" + bytes([len(raw)]) + raw


def _ber_data(data: bytes) -> bytes:
    return b"\x82" + len(data).to_bytes(2, "big") + data


def _per_object(body: bytes) -> bytes:
    return b"\x00" + bytes([len(body)]) + body


def _per_data(data: bytes) -> bytes:
    if len(data) < 0x4000:
        return (len(data) | 0x8000).to_bytes(2, "big") + data
    return b"\xa2" + len(data).to_bytes(2, "big") + data


def _domain_parameters(max_chan_ids: int, max_user_ids: int,
                       max_token_ids: int = 0, num_priorities: int = 1,
                       min_throughput: int = 0, max_height: int = 1,
                       max_mcspdu_size: int = 65535,
                       protocol_ver: int = 2) -> bytes:
    body = b"".join(_ber_int(v) for v in (
        max_chan_ids, max_user_ids, max_token_ids, num_priorities,
        min_throughput, max_height, max_mcspdu_size, protocol_ver))
    return b"\x30" + bytes([len(body)]) + body


def _cs_core_data(selected_protocol: int, client_name: str = "rdpscan") -> bytes:
    name = client_name[:16].encode("utf-16le")
    name = name + b"\x00" * (32 - len(name))
    body = b"".join([
        (0x00080004).to_bytes(4, "little"),   # clientVersion
        (800).to_bytes(2, "little"),           # desktopWidth
        (600).to_bytes(2, "little"),           # desktopHeight
        b"\x01\xca",                           # colorDepth 8BPP
        b"\x03\xaa",                           # SASSequence
        (1033).to_bytes(4, "little"),          # keyboardLayout
        (2600).to_bytes(4, "little"),          # clientBuild
        name,                                  # clientName (32 bytes UTF-16LE)
        (4).to_bytes(4, "little"),             # keyboardType
        (0).to_bytes(4, "little"),             # keyboardSubtype
        (12).to_bytes(4, "little"),            # keyboardFunctionKey
        b"\x00" * 64,                          # imeFileName
        b"\x01\xca",                           # postBeta2ColorDepth
        (1).to_bytes(2, "little"),             # clientProductId
        (0).to_bytes(4, "little"),             # serialNumber
        b"\x18\x00",                           # highColorDepth
        b"\x07\x00",                           # supportedColorDepths
        b"\x01\x00",                           # earlyCapabilityFlags
        b"\x00" * 64,                          # clientDigProductId
        b"\x00",                               # connectionType
        b"\x00",                               # pad1octet
        selected_protocol.to_bytes(4, "little"),
    ])
    return bytes([0x01, 0xC0]) + (len(body) + 4).to_bytes(2, "little") + body


def _cs_cluster_data() -> bytes:
    body = (0x9).to_bytes(4, "little") + (0).to_bytes(4, "little")
    return bytes([0x04, 0xC0]) + (len(body) + 4).to_bytes(2, "little") + body


def _cs_security_data(encryption_methods: int = 0x3) -> bytes:
    body = encryption_methods.to_bytes(4, "little") + (0).to_bytes(4, "little")
    return bytes([0x02, 0xC0]) + (len(body) + 4).to_bytes(2, "little") + body


def _cs_network_data(channels) -> bytes:
    chan = b"".join(
        name.encode("ascii")[:8].ljust(8, b"\x00") + opt.to_bytes(4, "little")
        for name, opt in channels)
    body = len(channels).to_bytes(4, "little") + chan
    return bytes([0x03, 0xC0]) + (len(body) + 4).to_bytes(2, "little") + body


def _data_tpdu(data: bytes) -> bytes:
    """Wrap an MCS PDU in TPKT + X.224 Data TPDU."""
    return (b"\x03\x00" + (len(data) + 7).to_bytes(2, "big")
            + b"\x02\xf0\x80" + data)


def _pdu_connect_initial(channels, selected_protocol: int) -> bytes:
    """MCS Connect Initial (T.125 Connect-Initial + GCC Conference Create Request)."""
    h221_key = b"\x00\x14\x7c\x00\x01"
    user_data = _per_object(h221_key) + _per_data(
        b"\x00\x08\x00\x10\x00\x01\xc0\x00Duca"
        + _per_data(_cs_core_data(selected_protocol) + _cs_cluster_data()
                    + _cs_security_data() + _cs_network_data(channels)))
    content = b"".join([
        b"\x04\x01\x01",                               # callingDomainSelector
        b"\x04\x01\x01",                               # calledDomainSelector
        b"\x01\x01\xff",                               # upwardFlag
        _domain_parameters(0x22, 0x2),                 # targetParameters
        _domain_parameters(0x1, 0x1, 0x1, max_mcspdu_size=0x0420),
        _domain_parameters(0xFFFF, 0xFC17, 0xFFFF),    # maximumParameters
        b"\x04" + _ber_data(user_data),                # userData OCTET STRING
    ])
    return _data_tpdu(b"\x7f\x65" + _ber_data(content))


def _virtual_channel_data(user_id: int, channel_id: int, payload: bytes,
                          flags: int = 0x3) -> bytes:
    """MCS Send Data Request carrying a Virtual Channel PDU."""
    chan_pdu = (len(payload).to_bytes(4, "little")
                + flags.to_bytes(4, "little") + payload)
    mcs = (b"\x64" + user_id.to_bytes(2, "big")
           + channel_id.to_bytes(2, "big") + b"\x70"
           + (len(chan_pdu) | 0x8000).to_bytes(2, "big") + chan_pdu)
    return _data_tpdu(mcs)


def _bluekeep_probe(sock: socket.socket, selected: int) -> str:
    """Bind MS_T120 outside its slot and send the crash-safe probe payload.

    Returns 'vulnerable' only when the server sends the MCS Disconnect
    Provider Ultimatum; 'safe' when it completes the session and stays quiet;
    'error' when the MCS handshake cannot be completed.
    """
    try:
        sock.sendall(_pdu_connect_initial(_MCS_CHANNELS, selected))
        _recv_tpkt(sock)                                  # MCS Connect Response
        sock.sendall(_data_tpdu(b"\x04\x01\x00\x01\x00"))  # Erect Domain Request
        sock.sendall(_data_tpdu(b"\x28"))                  # Attach User Request
        attach = _recv_tpkt(sock)                         # Attach User Confirm
        if len(attach) < 11:
            return "error"
        user_id = int.from_bytes(attach[9:11], "big")
        for chan in (1009, 1003, 1004, 1005, 1006, 1007, 1008):
            sock.sendall(_data_tpdu(b"\x38" + user_id.to_bytes(2, "big")
                                    + chan.to_bytes(2, "big")))
            _recv_tpkt(sock)                              # Channel Join Confirm
        sock.settimeout(1.0)
        for _ in range(6):
            for payload in (_BLUEKEEP_X86, _BLUEKEEP_X64):
                sock.sendall(_virtual_channel_data(user_id, 1005, payload))
            try:
                data = _recv_tpkt(sock)
            except socket.timeout:
                continue
            if _ULTIMATUM in data:
                return "vulnerable"
        return "safe"
    except (socket.timeout, TimeoutError):
        return "error"
    except (EOFError, OSError, ValueError):
        return "error"


def probe_host(host: str, port: int, timeout: float = 5.0,
               bluekeep: bool = False, cookie: str | None = None) -> ScanResult:
    """Probe one host:port. Blocking; designed to run in a worker thread."""
    result = ScanResult(host=host, port=port)
    try:
        sock, selected = _negotiate(host, port, timeout, cookie)
    except ConnectionRefusedError:
        result.state = "closed"
        return result
    except socket.timeout:
        result.state = "error"
        result.error = "connection timed out"
        return result
    except OSError as exc:
        result.state = "closed" if getattr(exc, "errno", None) in (
            111, 10061) else "error"
        result.error = str(exc)
        return result
    except (EOFError, ValueError) as exc:
        result.state = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.state = "open"
    result.selected_protocol = selected
    result.protocol_label = classify_protocol(selected)
    tls_sock = None
    try:
        if selected & (PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX
                       | PROTOCOL_RDSTLS):
            tls_sock = _upgrade_tls(sock, host, result)
        probe_sock = tls_sock or sock
        if selected & (PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX):
            result.bluekeep = "skipped-nla"
        elif bluekeep:
            if selected & PROTOCOL_SSL and tls_sock is not None:
                result.bluekeep = _bluekeep_probe(tls_sock, selected)
            elif selected == PROTOCOL_RDP:
                result.bluekeep = "skipped-standard-rdp"
            else:
                result.bluekeep = "not-run"
    except (socket.timeout, TimeoutError):
        result.state = "error"
        result.error = "timed out during RDP handshake"
    except (EOFError, OSError, ValueError, ssl.SSLError) as exc:
        result.state = "error"
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        for s in (tls_sock, sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
    return result


async def scan_targets(targets, timeout: float = 5.0, concurrency: int = 50,
                       bluekeep: bool = False, cookie: str | None = None) -> list[ScanResult]:
    """Probe many (host, port) pairs with bounded concurrency."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(host: str, port: int) -> ScanResult:
        async with sem:
            return await asyncio.to_thread(probe_host, host, port, timeout,
                                           bluekeep, cookie)

    return list(await asyncio.gather(*(one(h, p) for h, p in targets)))


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

_SEVERITY_COLOR = {
    "critical": "\033[1;31m",
    "high": "\033[31m",
    "medium": "\033[33m",
    "low": "\033[36m",
    "info": "\033[90m",
}
_RESET = "\033[0m"


def result_records(results, findings) -> list[dict]:
    """Merge results with their findings into flat records."""
    by_target: dict[tuple[str, int], list[dict]] = {}
    for f in findings:
        by_target.setdefault((f.host, f.port), []).append(f.to_dict())
    records = []
    for r in results:
        rec = r.to_dict()
        rec["findings"] = by_target.get((r.host, r.port), [])
        records.append(rec)
    return records


def render_table(results, findings, use_color: bool = True) -> str:
    by_target: dict[tuple[str, int], list[Finding]] = {}
    for f in findings:
        by_target.setdefault((f.host, f.port), []).append(f)
    header = (f"{'HOST':<18} {'PORT':>5}  {'STATE':<7} {'PROTOCOL':<24} "
              f"{'TLS':<9} {'BLUEKEEP':<20} FINDINGS")
    lines = [header, "-" * len(header)]
    for r in results:
        fs = by_target.get((r.host, r.port), [])
        ftxt = ",".join(sorted({f.id for f in fs})) if fs else "-"
        if use_color and fs:
            worst = min(SEVERITY_ORDER[f.severity] for f in fs)
            sev = next(s for s, v in SEVERITY_ORDER.items() if v == worst)
            ftxt = _SEVERITY_COLOR[sev] + ftxt + _RESET
        lines.append(f"{r.host:<18} {r.port:>5}  {r.state:<7} "
                     f"{protocol_label(r.selected_protocol):<24} "
                     f"{r.tls_version or '-':<9} "
                     f"{r.bluekeep:<20} {ftxt}")
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    if counts:
        lines.append("")
        summary = ", ".join(f"{n} {s}" for s, n in sorted(
            counts.items(), key=lambda kv: SEVERITY_ORDER[kv[0]]))
        lines.append(f"Findings: {summary}")
    return "\n".join(lines)


def write_json(path: str, results, findings) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result_records(results, findings), fh, indent=2)
        fh.write("\n")


def write_csv(path: str, results, findings) -> None:
    records = result_records(results, findings)
    cols = ["host", "port", "state", "protocol", "tls_version", "tls_cipher",
            "bluekeep", "error", "findings"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for rec in records:
            row = {k: rec.get(k) for k in cols
                   if k not in ("findings", "protocol")}
            row["protocol"] = rec.get("protocol_label")
            row["findings"] = ",".join(f["id"] for f in rec["findings"])
            writer.writerow(row)


def exit_code(findings, fail_on: str = "high") -> int:
    """1 when a finding at or above `fail_on` severity exists, else 0."""
    if fail_on == "never" or not findings:
        return 0
    threshold = SEVERITY_ORDER.get(fail_on, SEVERITY_ORDER["high"])
    return 1 if min(SEVERITY_ORDER[f.severity] for f in findings) <= threshold else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rdp_scan",
        description="Scan RDP listeners: NLA/TLS/legacy security, TLS "
                    "certificate, and optional CVE-2019-0708 (BlueKeep) probe.")
    ap.add_argument("targets", nargs="*", metavar="TARGET",
                    help="host, host:port, CIDR, a-b range, or @file")
    ap.add_argument("--ports", default="3389",
                    help="port list/range (default 3389)")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="per-host timeout in seconds (default 5)")
    ap.add_argument("--concurrency", type=int, default=50,
                    help="parallel probes (default 50)")
    ap.add_argument("--bluekeep", action="store_true",
                    help="run the CVE-2019-0708 MS_T120 probe on non-NLA hosts")
    ap.add_argument("--cookie", default=None,
                    help="mstshash cookie value to send (default none)")
    ap.add_argument("--json", metavar="FILE", help="write JSON results")
    ap.add_argument("--csv", metavar="FILE", help="write CSV results")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    ap.add_argument("--fail-on", default="high",
                    choices=["critical", "high", "medium", "low", "info", "never"],
                    help="exit 1 when a finding at/above this severity exists "
                         "(default high)")
    ap.add_argument("--max-targets", type=int, default=100000,
                    help="refuse to scan more than this many targets")
    ap.add_argument("--selftest", action="store_true",
                    help="run built-in checks and exit")
    return ap


def _selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + name)
        ok = ok and cond

    pkt = build_connection_request(PROTOCOL_SSL | PROTOCOL_HYBRID)
    check("connection request is 19 bytes", len(pkt) == 19)
    check("connection request TPKT length", pkt[2:4] == b"\x00\x13")
    cc = parse_connection_confirm(_TEST_CONFIRM)
    check("connection confirm parses", cc["selected_protocol"] == PROTOCOL_HYBRID)
    check("ports parse", parse_ports("3389,3390-3391") == [3389, 3390, 3391])
    check("targets expand", len(parse_targets(["10.0.0.0/30"], [3389])) == 4)
    init = _pdu_connect_initial(_MCS_CHANNELS, PROTOCOL_SSL)
    check("MCS connect initial binds MS_T120", b"MS_T120" in init)
    check("classify nla", classify_protocol(PROTOCOL_HYBRID) == "nla")
    check("classify standard", classify_protocol(PROTOCOL_RDP) == "standard")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


_TEST_CONFIRM = bytes.fromhex("030000130ed000000000000200080002000000")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.targets:
        print("error: no targets (host, CIDR, range, host:port, or @file)",
              file=sys.stderr)
        return 2
    try:
        ports = parse_ports(args.ports)
        targets = parse_targets(args.targets, ports)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if len(targets) > args.max_targets:
        print(f"error: {len(targets)} targets exceeds --max-targets "
              f"{args.max_targets}", file=sys.stderr)
        return 2

    results = asyncio.run(scan_targets(targets, timeout=args.timeout,
                                       concurrency=args.concurrency,
                                       bluekeep=args.bluekeep,
                                       cookie=args.cookie))
    findings = [f for r in results for f in assess_findings(r)]
    use_color = sys.stdout.isatty() and not args.no_color
    print(render_table(results, findings, use_color=use_color))
    if args.json:
        write_json(args.json, results, findings)
        print(f"wrote {args.json}")
    if args.csv:
        write_csv(args.csv, results, findings)
        print(f"wrote {args.csv}")
    return exit_code(findings, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
