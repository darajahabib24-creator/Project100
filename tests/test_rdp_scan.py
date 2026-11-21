#!/usr/bin/env python3
"""Tests for rdp_scan.py.

Pure codec/parsing tests plus end-to-end tests against a small fake RDP
server (and, when available, a real xrdp on localhost).
"""

import asyncio
import csv
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdp_scan import (  # noqa: E402
    PROTOCOL_HYBRID,
    PROTOCOL_HYBRID_EX,
    PROTOCOL_RDP,
    PROTOCOL_RDSTLS,
    PROTOCOL_SSL,
    Finding,
    ScanResult,
    _MCS_CHANNELS,
    _pdu_connect_initial,
    _parse_license_pdu,
    assess_findings,
    build_connection_request,
    build_parser,
    classify_protocol,
    exit_code,
    parse_connection_confirm,
    parse_certificate,
    parse_ports,
    parse_targets,
    probe_host,
    render_table,
    result_records,
    scan_targets,
    write_csv,
    write_json,
)

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime
    HAVE_CRYPTO = True
except Exception:  # pragma: no cover
    HAVE_CRYPTO = False


def make_cert(cn="localhost", days=30, expired=False):
    """Generate a self-signed DER cert for tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    if expired:
        nb = now - datetime.timedelta(days=10)
        na = now - datetime.timedelta(days=1)
    else:
        nb = now - datetime.timedelta(days=1)
        na = now + datetime.timedelta(days=days)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(nb).not_valid_after(na)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


class TestParsePorts(unittest.TestCase):
    def test_single(self):
        self.assertEqual(parse_ports("3389"), [3389])

    def test_list(self):
        self.assertEqual(parse_ports("3389,3390"), [3389, 3390])

    def test_range(self):
        self.assertEqual(parse_ports("3388-3390"), [3388, 3389, 3390])

    def test_mixed_and_spaces(self):
        self.assertEqual(parse_ports("3389, 3391-3392"), [3389, 3391, 3392])

    def test_dedupe_sorted(self):
        self.assertEqual(parse_ports("3390,3389,3389"), [3389, 3390])

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            parse_ports("70000")

    def test_zero(self):
        with self.assertRaises(ValueError):
            parse_ports("0")

    def test_garbage(self):
        with self.assertRaises(ValueError):
            parse_ports("abc")


class TestParseTargets(unittest.TestCase):
    def test_single_host(self):
        self.assertEqual(parse_targets(["192.168.1.1"], [3389]),
                         [("192.168.1.1", 3389)])

    def test_cidr(self):
        hosts = parse_targets(["192.168.1.0/30"], [3389])
        self.assertEqual([h for h, _ in hosts],
                         ["192.168.1.0", "192.168.1.1",
                          "192.168.1.2", "192.168.1.3"])

    def test_range(self):
        hosts = parse_targets(["10.0.0.1-10.0.0.3"], [3389])
        self.assertEqual([h for h, _ in hosts],
                         ["10.0.0.1", "10.0.0.2", "10.0.0.3"])

    def test_host_port_overrides(self):
        self.assertEqual(parse_targets(["10.0.0.1:3390"], [3389]),
                         [("10.0.0.1", 3390)])

    def test_ipv6_host_port(self):
        self.assertEqual(parse_targets(["[::1]:3390"], [3389]),
                         [("::1", 3390)])

    def test_ipv6_bare(self):
        self.assertEqual(parse_targets(["::1"], [3389]), [("::1", 3389)])

    def test_multiple_ports(self):
        hosts = parse_targets(["10.0.0.1"], [3389, 3390])
        self.assertEqual(hosts, [("10.0.0.1", 3389), ("10.0.0.1", 3390)])

    def test_file(self):
        path = os.path.join(os.path.dirname(__file__), "targets.txt")
        with open(path, "w") as fh:
            fh.write("# comment\n10.1.1.1\n10.1.1.2-10.1.1.3\n\n")
        try:
            hosts = parse_targets(["@" + path], [3389])
            self.assertEqual([h for h, _ in hosts],
                             ["10.1.1.1", "10.1.1.2", "10.1.1.3"])
        finally:
            os.unlink(path)

    def test_bad_host(self):
        with self.assertRaises(ValueError):
            parse_targets(["not a host"], [3389])


class TestConnectionRequest(unittest.TestCase):
    def test_known_packet_no_cookie(self):
        pkt = build_connection_request(PROTOCOL_SSL | PROTOCOL_HYBRID)
        self.assertEqual(pkt.hex(),
                         "030000130ee000000000000100080003000000")

    def test_cookie_extends_length(self):
        pkt = build_connection_request(PROTOCOL_RDP, cookie="bob")
        self.assertEqual(pkt[0], 0x03)
        self.assertEqual(pkt[4], 0x0E + len("Cookie: mstshash=bob\r\n"))
        self.assertIn(b"Cookie: mstshash=bob\r\n", pkt)

    def test_roundtrip_requested_protocols(self):
        pkt = build_connection_request(PROTOCOL_HYBRID_EX, cookie="x")
        self.assertEqual(int.from_bytes(pkt[-4:], "little"),
                         PROTOCOL_HYBRID_EX)


def _cc(selected=None, failure=None):
    """Build an X.224 Connection Confirm with a negotiation rsp/failure."""
    body = bytearray()
    body += bytes([0x00, 0x00])          # dst-ref
    body += bytes([0x00, 0x00])          # src-ref
    body += bytes([0x00])                # class
    if selected is not None:
        body += bytes([0x02, 0x00, 0x08, 0x00])
        body += int(selected).to_bytes(4, "little")
    elif failure is not None:
        body += bytes([0x03, 0x00, 0x08, 0x00])
        body += int(failure).to_bytes(4, "little")
    li = len(body) + 1
    x224 = bytes([li, 0xD0]) + bytes(body)
    total = len(x224) + 4
    return bytes([0x03, 0x00]) + total.to_bytes(2, "big") + x224


class TestConnectionConfirm(unittest.TestCase):
    def test_negotiation_response(self):
        r = parse_connection_confirm(_cc(selected=PROTOCOL_HYBRID))
        self.assertEqual(r["selected_protocol"], PROTOCOL_HYBRID)
        self.assertIsNone(r["failure_code"])

    def test_negotiation_failure(self):
        r = parse_connection_confirm(_cc(failure=2))
        self.assertEqual(r["failure_code"], 2)
        self.assertIsNone(r["selected_protocol"])

    def test_win_xp_bare_confirm(self):
        # Windows XP sends a Connection Confirm with no negotiation payload.
        r = parse_connection_confirm(_cc())
        self.assertIsNone(r["selected_protocol"])
        self.assertIsNone(r["failure_code"])
        self.assertTrue(r["bare_confirm"])

    def test_garbage_rejected(self):
        with self.assertRaises(ValueError):
            parse_connection_confirm(b"\x00\x01\x02\x03")

    def test_short_rejected(self):
        with self.assertRaises(ValueError):
            parse_connection_confirm(b"\x03\x00\x00\x05")


class TestClassify(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(classify_protocol(PROTOCOL_RDP), "standard")

    def test_tls(self):
        self.assertEqual(classify_protocol(PROTOCOL_SSL), "tls")

    def test_nla(self):
        self.assertEqual(classify_protocol(PROTOCOL_HYBRID), "nla")

    def test_nla_early_auth(self):
        self.assertEqual(classify_protocol(PROTOCOL_HYBRID_EX), "nla-early")

    def test_rdstls(self):
        self.assertEqual(classify_protocol(PROTOCOL_RDSTLS), "rdstls")

    def test_combined_tls_nla(self):
        self.assertEqual(classify_protocol(PROTOCOL_SSL | PROTOCOL_HYBRID),
                         "nla")


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class TestParseCertificate(unittest.TestCase):
    def test_subject_issuer_san(self):
        info = parse_certificate(make_cert(cn="rdp.example.com"))
        self.assertEqual(info["subject_cn"], "rdp.example.com")
        self.assertEqual(info["issuer_cn"], "rdp.example.com")
        self.assertIn("rdp.example.com", info["san"])
        self.assertTrue(info["self_signed"])
        self.assertFalse(info["expired"])
        self.assertFalse(info["not_yet_valid"])

    def test_expired(self):
        info = parse_certificate(make_cert(expired=True))
        self.assertTrue(info["expired"])

    def test_fingerprint_is_sha256_hex(self):
        info = parse_certificate(make_cert())
        self.assertEqual(len(info["fingerprint_sha256"]), 64)
        int(info["fingerprint_sha256"], 16)

    def test_missing_crypto_is_reported(self):
        # parse_certificate must never raise, even on junk input.
        info = parse_certificate(b"not a certificate")
        self.assertIn("error", info)


class TestFindings(unittest.TestCase):
    def _result(self, **kw):
        base = dict(host="10.0.0.1", port=3389, state="open",
                    selected_protocol=PROTOCOL_HYBRID, protocol_label="nla",
                    tls_version="TLSv1.3", tls_cipher="TLS_AES_256_GCM_SHA384",
                    cert=None, bluekeep="not-run", error=None)
        base.update(kw)
        return ScanResult(**base)

    def test_open_port_is_info(self):
        f = assess_findings(self._result())
        ids = [x.id for x in f]
        self.assertIn("rdp-open", ids)
        self.assertNotIn("nla-disabled", ids)

    def test_standard_rdp_flags_nla_and_legacy(self):
        f = assess_findings(self._result(selected_protocol=PROTOCOL_RDP,
                                         protocol_label="standard"))
        ids = [x.id for x in f]
        self.assertIn("nla-disabled", ids)
        self.assertIn("standard-rdp-security", ids)
        self.assertEqual([x.severity for x in f if x.id == "standard-rdp-security"],
                         ["high"])

    def test_tls_only_flags_nla_disabled(self):
        f = assess_findings(self._result(selected_protocol=PROTOCOL_SSL,
                                         protocol_label="tls"))
        ids = [x.id for x in f]
        self.assertIn("nla-disabled", ids)
        self.assertNotIn("standard-rdp-security", ids)

    def test_weak_tls(self):
        f = assess_findings(self._result(tls_version="TLSv1"))
        self.assertIn("weak-tls", [x.id for x in f])

    def test_weak_cipher(self):
        f = assess_findings(self._result(tls_cipher="TLS_RSA_WITH_RC4_128_SHA"))
        self.assertIn("weak-tls", [x.id for x in f])

    def test_bluekeep_vulnerable_is_critical(self):
        f = assess_findings(self._result(selected_protocol=PROTOCOL_SSL,
                                         protocol_label="tls",
                                         bluekeep="vulnerable"))
        bk = [x for x in f if x.id == "bluekeep-possible"]
        self.assertEqual(len(bk), 1)
        self.assertEqual(bk[0].severity, "critical")

    def test_bluekeep_safe_is_informational(self):
        f = assess_findings(self._result(selected_protocol=PROTOCOL_SSL,
                                         protocol_label="tls",
                                         bluekeep="safe"))
        self.assertIn("bluekeep-safe", [x.id for x in f])

    def test_closed_port_has_no_findings(self):
        self.assertEqual(assess_findings(self._result(state="closed")), [])

    def test_cert_self_signed_and_expired(self):
        cert = {"self_signed": True, "expired": True, "not_yet_valid": False,
                "subject_cn": "x"}
        f = assess_findings(self._result(cert=cert))
        ids = [x.id for x in f]
        self.assertIn("cert-self-signed", ids)
        self.assertIn("cert-expired", ids)


# --------------------------------------------------------------------------- #
# fake RDP server for end-to-end tests
# --------------------------------------------------------------------------- #

def make_cert_pair(cn="localhost"):
    """Return (cert_pem, key_pem) bytes for a self-signed cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    return cert_pem, key_pem


class FakeRdpServer:
    """Minimal RDP listener: answers X.224 and optionally does TLS."""

    def __init__(self, mode="nla", tls=False, cert_pair=None):
        self.mode = mode
        self.tls = tls
        self.received: list[bytes] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._tls_ctx = None
        if tls:
            cert_pem, key_pem = cert_pair or make_cert_pair()
            self._tmp = tempfile.mkdtemp()
            self._cert = os.path.join(self._tmp, "c.pem")
            self._key = os.path.join(self._tmp, "k.pem")
            with open(self._cert, "wb") as fh:
                fh.write(cert_pem)
            with open(self._key, "wb") as fh:
                fh.write(key_pem)
            self._tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._tls_ctx.load_cert_chain(self._cert, self._key)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(5)
        try:
            data = conn.recv(4096)
            if not data:
                return
            self.received.append(data)
            req = int.from_bytes(data[-4:], "little") if len(data) >= 4 else 0
            if self.mode == "legacy":
                if req & (PROTOCOL_SSL | PROTOCOL_HYBRID):
                    conn.sendall(_cc(failure=2))
                    return
                conn.sendall(_cc())          # bare confirm -> standard RDP
                return
            if self.mode == "nla":
                conn.sendall(_cc(selected=PROTOCOL_HYBRID))
            elif self.mode == "tls":
                conn.sendall(_cc(selected=PROTOCOL_SSL))
            elif self.mode == "rdp":
                conn.sendall(_cc(selected=PROTOCOL_RDP))
            elif self.mode == "failure":
                conn.sendall(_cc(failure=1))
            else:
                conn.sendall(_cc())
            if self.tls:
                tls = self._tls_ctx.wrap_socket(conn, server_side=True)
                try:
                    tls.recv(4096)
                except Exception:  # noqa: BLE001
                    pass
                tls.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class TestProbeHost(unittest.TestCase):
    def test_nla_negotiation(self):
        srv = FakeRdpServer(mode="nla")
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5)
            self.assertEqual(r.state, "open")
            self.assertEqual(r.selected_protocol, PROTOCOL_HYBRID)
            self.assertEqual(r.protocol_label, "nla")
            self.assertEqual(r.bluekeep, "skipped-nla")
        finally:
            srv.close()

    def test_tls_cert_captured(self):
        srv = FakeRdpServer(mode="tls", tls=True)
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5)
            self.assertEqual(r.selected_protocol, PROTOCOL_SSL)
            self.assertEqual(r.protocol_label, "tls")
            self.assertIsNotNone(r.tls_version)
            self.assertTrue(r.tls_version.startswith("TLSv1."))
            self.assertIsNotNone(r.cert)
            self.assertEqual(r.cert.get("subject_cn"), "localhost")
            self.assertTrue(r.cert.get("self_signed"))
        finally:
            srv.close()

    def test_legacy_fallback_to_standard_rdp(self):
        srv = FakeRdpServer(mode="legacy")
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5)
            self.assertEqual(r.selected_protocol, PROTOCOL_RDP)
            self.assertEqual(r.protocol_label, "standard")
            self.assertEqual(len(srv.received), 2)   # two negotiations
        finally:
            srv.close()

    def test_closed_port(self):
        srv = FakeRdpServer(mode="nla")
        port = srv.port
        srv.close()
        r = probe_host("127.0.0.1", port, timeout=2)
        self.assertEqual(r.state, "closed")

    def test_unexpected_failure_is_error(self):
        srv = FakeRdpServer(mode="failure")
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5)
            self.assertEqual(r.state, "error")
            self.assertIsNotNone(r.error)
        finally:
            srv.close()

    def test_scan_targets_async(self):
        srv = FakeRdpServer(mode="nla")
        try:
            targets = [("127.0.0.1", srv.port), ("127.0.0.1", 1)]
            results = asyncio.run(scan_targets(targets, timeout=5, concurrency=4))
            self.assertEqual(len(results), 2)
            by_port = {r.port: r for r in results}
            self.assertEqual(by_port[srv.port].state, "open")
            self.assertEqual(by_port[1].state, "closed")
        finally:
            srv.close()


def _recv_tpkt(sock):
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            raise EOFError
        hdr += chunk
    length = int.from_bytes(hdr[2:4], "big")
    body = b""
    while len(body) < length - 4:
        chunk = sock.recv(length - 4 - len(body))
        if not chunk:
            raise EOFError
        body += chunk
    return hdr + body


def _data_tpdu(data):
    return (b"\x03\x00" + (len(data) + 7).to_bytes(2, "big")
            + b"\x02\xf0\x80" + data)


_ULTIMATUM = bytes.fromhex("0300000902f0802180")


def _mcs_channel(pkt):
    """Channel id from an MCS Send Data Request, or None."""
    if len(pkt) < 12 or pkt[7] != 0x64:
        return None
    return int.from_bytes(pkt[10:12], "big")


def _license_issued_pdu(user_id=1009):
    """LICENSE_ERROR_ALERT with LICENSE_ISSUED, as a real listener sends."""
    payload = bytes.fromhex("80020000ff031000070000000200000004000000")
    mcs = (b"\x64" + user_id.to_bytes(2, "big") + b"\x03\xeb" + b"\x70"
           + bytes([len(payload)]) + payload)
    return _data_tpdu(mcs)


class _FakeSock:
    """Minimal socket double: an inbound buffer plus recorded sends."""

    def __init__(self, chunks=()):
        self.buf = b"".join(chunks)
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _license_request_pdu(user_id=1009):
    payload = b"\x80\x02\x00\x00" + bytes.fromhex("01031000") + b"\x00" * 12
    mcs = (b"\x64" + user_id.to_bytes(2, "big") + b"\x03\xeb" + b"\x70"
           + bytes([len(payload)]) + payload)
    return _data_tpdu(mcs)


class TestLicensePdu(unittest.TestCase):
    def test_issued_alert_proceeds(self):
        sock = _FakeSock([_license_issued_pdu(1009)])
        self.assertTrue(_parse_license_pdu(_recv_tpkt(sock), sock, 1009))
        self.assertEqual(sock.sent, [])

    def test_other_error_alert_stops(self):
        payload = bytes.fromhex("80020000ff031000060000000200000004000000")
        mcs = (b"\x64" + (1009).to_bytes(2, "big") + b"\x03\xeb" + b"\x70"
               + bytes([len(payload)]) + payload)
        self.assertFalse(_parse_license_pdu(_data_tpdu(mcs), _FakeSock(), 1009))

    def test_request_is_answered_with_new_license_request(self):
        sock = _FakeSock([_license_issued_pdu(1009)])
        self.assertTrue(_parse_license_pdu(_license_request_pdu(1009), sock, 1009))
        self.assertEqual(len(sock.sent), 1)
        rsp = sock.sent[0]
        self.assertEqual(_mcs_channel(rsp), 1003)
        self.assertEqual(rsp[19], 0x13)          # LICENSE_NEW_LICENSE_REQ


class FakeBlueKeepServer(FakeRdpServer):
    """TLS RDP server that completes the MCS session and answers the probe."""

    def __init__(self, vulnerable=True, license_request=False):
        super().__init__(mode="tls", tls=True)
        self.vulnerable = vulnerable
        self.license_request = license_request
        self.saw_ms_t120 = False
        self.saw_session = False
        self.probe_packets = 0

    def _handle(self, conn):
        conn.settimeout(5)
        data = conn.recv(4096)                       # X.224 Connection Request
        if not data:
            return
        conn.sendall(_cc(selected=PROTOCOL_SSL))
        tls = self._tls_ctx.wrap_socket(conn, server_side=True)
        tls.settimeout(5)
        try:
            init = _recv_tpkt(tls)                   # MCS Connect Initial
            if b"MS_T120" in init:
                self.saw_ms_t120 = True
            tls.sendall(_data_tpdu(b"\x7f\x66\x82\x00\x00"))
            _recv_tpkt(tls)                          # Erect Domain Request
            _recv_tpkt(tls)                          # Attach User Request
            tls.sendall(_data_tpdu(b"\x02\x00\x03\xf1\x00\x00"))
            for _ in range(7):                       # Channel Join Requests
                _recv_tpkt(tls)
                tls.sendall(_data_tpdu(b"\x02\x00\x03\xf1\x03\xeb"))
            info = _recv_tpkt(tls)                   # Client Info PDU
            if _mcs_channel(info) != 1003:
                return
            if self.license_request:                 # Windows asks first
                tls.sendall(_license_request_pdu())
                _recv_tpkt(tls)                      # client's New License Req
            tls.sendall(_license_issued_pdu())       # license exchange
            tls.sendall(_data_tpdu(b"\x00" * 16))    # Server Demand Active
            for _ in range(6):                       # Confirm Active .. Font List
                _recv_tpkt(tls)
            self.saw_session = True
            if self.vulnerable:
                pkt = _recv_tpkt(tls)
                self.probe_packets += 1
                tls.sendall(_ULTIMATUM)
            else:
                while True:
                    pkt = _recv_tpkt(tls)
                    self.probe_packets += 1
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                tls.close()
            except OSError:
                pass


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class TestBlueKeepProbe(unittest.TestCase):
    def test_connect_initial_binds_ms_t120(self):
        pkt = _pdu_connect_initial(_MCS_CHANNELS, PROTOCOL_SSL)
        self.assertIn(b"MS_T120", pkt)
        self.assertIn(PROTOCOL_SSL.to_bytes(4, "little"), pkt)
        self.assertEqual(pkt[0], 0x03)
        self.assertEqual(pkt[4:7], b"\x02\xf0\x80")

    def test_vulnerable_server_is_flagged(self):
        srv = FakeBlueKeepServer(vulnerable=True)
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5, bluekeep=True)
            self.assertEqual(r.bluekeep, "vulnerable")
            self.assertTrue(srv.saw_ms_t120)
            self.assertTrue(srv.saw_session)
            self.assertGreaterEqual(srv.probe_packets, 1)
        finally:
            srv.close()

    def test_patched_server_is_safe(self):
        srv = FakeBlueKeepServer(vulnerable=False)
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5, bluekeep=True)
            self.assertEqual(r.bluekeep, "safe")
            self.assertTrue(srv.saw_ms_t120)
            self.assertTrue(srv.saw_session)
        finally:
            srv.close()

    def test_license_request_path_is_handled(self):
        srv = FakeBlueKeepServer(vulnerable=True, license_request=True)
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5, bluekeep=True)
            self.assertEqual(r.bluekeep, "vulnerable")
            self.assertTrue(srv.saw_session)
            self.assertGreaterEqual(srv.probe_packets, 1)
        finally:
            srv.close()

    def test_standard_rdp_probe_is_skipped(self):
        srv = FakeRdpServer(mode="rdp")
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5, bluekeep=True)
            self.assertEqual(r.bluekeep, "skipped-standard-rdp")
        finally:
            srv.close()

    def test_nla_probe_is_skipped(self):
        srv = FakeRdpServer(mode="nla")
        try:
            r = probe_host("127.0.0.1", srv.port, timeout=5, bluekeep=True)
            self.assertEqual(r.bluekeep, "skipped-nla")
        finally:
            srv.close()


class TestOutput(unittest.TestCase):
    def _results(self):
        return [
            ScanResult(host="10.0.0.1", port=3389, state="open",
                       selected_protocol=PROTOCOL_SSL, protocol_label="tls",
                       tls_version="TLSv1.2", tls_cipher="TLS_AES_256_GCM_SHA384",
                       cert=None, bluekeep="safe"),
            ScanResult(host="10.0.0.2", port=3389, state="closed"),
        ]

    def test_result_records_embed_findings(self):
        recs = result_records(self._results(), assess_findings(self._results()[0]))
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["host"], "10.0.0.1")
        self.assertIn("findings", recs[0])
        self.assertEqual(recs[1]["findings"], [])

    def test_render_table_mentions_host_and_finding(self):
        results = self._results()
        text = render_table(results, assess_findings(results[0]))
        self.assertIn("10.0.0.1", text)
        self.assertIn("nla-disabled", text)

    def test_json_roundtrip(self):
        path = os.path.join(tempfile.mkdtemp(), "out.json")
        write_json(path, self._results(), assess_findings(self._results()[0]))
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(data[0]["host"], "10.0.0.1")
        self.assertTrue(data[0]["findings"])

    def test_csv_roundtrip(self):
        path = os.path.join(tempfile.mkdtemp(), "out.csv")
        write_csv(path, self._results(), assess_findings(self._results()[0]))
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["host"], "10.0.0.1")
        self.assertEqual(rows[1]["state"], "closed")


class TestExitCode(unittest.TestCase):
    def test_high_finding_fails(self):
        f = [Finding("h", 1, "x", "high", "t")]
        self.assertEqual(exit_code(f, "high"), 1)

    def test_info_only_passes(self):
        f = [Finding("h", 1, "x", "info", "t")]
        self.assertEqual(exit_code(f, "high"), 0)

    def test_critical_threshold(self):
        f = [Finding("h", 1, "x", "high", "t")]
        self.assertEqual(exit_code(f, "critical"), 0)

    def test_never(self):
        f = [Finding("h", 1, "x", "critical", "t")]
        self.assertEqual(exit_code(f, "never"), 0)

    def test_no_findings(self):
        self.assertEqual(exit_code([], "high"), 0)


class TestParser(unittest.TestCase):
    def test_defaults(self):
        a = build_parser().parse_args(["10.0.0.1"])
        self.assertEqual(a.ports, "3389")
        self.assertEqual(a.timeout, 5.0)
        self.assertFalse(a.bluekeep)

    def test_targets_optional(self):
        a = build_parser().parse_args([])
        self.assertEqual(a.targets, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
