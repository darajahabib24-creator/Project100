# rdp_scan

A professional RDP (Remote Desktop Protocol) scanner in a single Python file.
It finds RDP listeners, negotiates the MS-RDPBCGR security layer, and reports
what it finds: whether Network Level Authentication (CredSSP/NLA) is required,
whether only legacy Standard RDP Security is offered, the TLS version/cipher and
certificate, and — optionally — whether a non-NLA listener answers the
crash-safe MS_T120 probe used to flag CVE-2019-0708 (BlueKeep).

Only scan hosts you own or are authorised to test.

## Pieces

- `rdp_scan.py` — the scanner (stdlib only; `cryptography` is used if present
  to parse certificates, otherwise the cert fields are skipped).
- `tests/test_rdp_scan.py` — 70 tests: codec/parsing, findings, a fake RDP
  server for end-to-end negotiation, and a fake BlueKeep server that completes
  the session (license exchange included) and answers the probe.

## Requirements

- Python 3.10+ (developed on 3.14). No third-party packages required.
- Optional: `pip install cryptography` for certificate details.
- Nothing to install on the target; the scanner is a client.

## Usage

```bash
python3 rdp_scan.py 192.168.1.0/24
python3 rdp_scan.py 10.0.0.1:3389 --bluekeep
python3 rdp_scan.py @targets.txt --ports 3389,3390 --json out.json --csv out.csv
python3 rdp_scan.py 10.0.0.1-10.0.0.20 --timeout 3 --concurrency 20
python3 rdp_scan.py --selftest
```

Targets can be a host, `host:port`, a CIDR (`10.0.0.0/24`), a range
(`10.0.0.1-10.0.0.20`), an IPv6 literal, or `@file` (one target per line, `#`
comments). `--ports` accepts a list or range (`3389,3390-3392`).

## What it checks

- **Listener discovery** — TCP connect with bounded concurrency.
- **Security negotiation** — X.224 Connection Request with an RDP Negotiation
  Request. It reads the Negotiation Response / Failure, retries once for
  SSL-only and legacy servers, and understands the Windows XP bare-confirm case.
- **NLA** — `selectedProtocol` HYBRID / HYBRID_EX means CredSSP (NLA) is
  required; anything else is reported as `nla-disabled`.
- **Legacy security** — `selectedProtocol` 0 (Standard RDP Security) means no
  TLS and no CredSSP: reported as `standard-rdp-security`.
- **TLS** — protocol version, cipher, and the certificate (subject, issuer, SAN,
  validity, SHA-256 fingerprint, self-signed / expired / not-yet-valid).
- **BlueKeep (optional, `--bluekeep`)** — on listeners that do not require NLA
  and offer TLS, it establishes the RDP session (client info, license,
  confirm active), then binds the MS_T120 static virtual channel outside its
  normal slot and sends a small crash-safe payload. A listener that answers
  with an MCS Disconnect Provider Ultimatum is reported as `bluekeep-possible`.

## Findings and exit code

Findings carry a severity: `critical`, `high`, `medium`, `low`, `info`.
`--fail-on` (default `high`) makes the process exit 1 when a finding at or
above that severity exists, so it can gate a script or CI step. Use
`--fail-on never` to always exit 0.

## BlueKeep probe: scope and limits

- The probe is **non-intrusive**: it sends the same non-DoS channel payloads as
  the Metasploit `cve_2019_0708_bluekeep` check module; it never sends an
  exploit payload and does not attempt to crash the target.
- It establishes the RDP session first (Client Info PDU, license exchange,
  Confirm Active), then binds and probes MS_T120. Binding the channel before
  the connection sequence completes is not reliable, so the probe does the
  client side of that sequence like the reference check does.
- It runs only when the listener does **not** require NLA and offers TLS.
  Listeners that require NLA are reported as not applicable; listeners that
  offer only Standard RDP Security are reported as not run (the probe needs a
  TLS session).
- A `vulnerable` result means the server sent the Ultimatum PDU. Treat it as
  "missing the patch" until verified; a `safe` result means the probe completed
  and the server stayed quiet. `error` means the MCS handshake could not
  complete (result unknown, not a clean bill of health).

## Verify

```bash
python3 rdp_scan.py --selftest
python3 -m unittest tests.test_rdp_scan -v
```

## Safety / trust

- Scanning produces traffic against the target. Only scan hosts you own or are
  authorised to test; many networks treat port scanning as abuse.
- The BlueKeep probe is crash-safe by design but is still an intrusive check —
  run it against systems you are responsible for.
- Results are only as good as the network path: a filtered port or a middlebox
  can make an open listener look closed.
