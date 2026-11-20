# rdp_vnc

Authorised RDP/VNC discovery, service check and screen capture. For systems
you **own** or have **written permission** to test. Every network command
requires the explicit `--authorised` flag; without it the tool refuses to run.

It never guesses credentials, never brute-forces, and never logs in to RDP.
VNC login uses a password you supply, on a system you are authorised to access.

## Install

Core scan/check needs only the standard library. Screen capture and VNC
password auth need two optional packages:

```bash
pip install Pillow cryptography      # or: pip install '.[rdp-vnc]'
```

## Commands

```bash
# scan a host or subnet for RDP (3389) and VNC (5900-5910)
python3 rdp_vnc.py scan 192.168.1.0/24 --authorised
python3 rdp_vnc.py scan 192.168.1.0/24 --identify --authorised   # probe services

# identify one service
python3 rdp_vnc.py check 192.168.1.5:5900 --authorised

# capture a VNC screen to PNG (password optional)
python3 rdp_vnc.py screen 192.168.1.5:5900 --password secret --out screen.png --authorised

# connect: VNC capture, or an RDP client hint
python3 rdp_vnc.py connect 192.168.1.5:3389 --authorised
```

Options: `--ports` (default `3389,5900-5910`), `--timeout`, `--threads`,
`--delay`, `--json`, `--password`, `--out`.

## What it does

- **scan** — TCP-connect scan; `--identify` probes each open port to tell RDP
  from VNC (RFB banner vs. RDP X.224 negotiation).
- **check** — one host:port: `service` is `vnc`, `rdp`, `unknown` or `none`,
  with version / selected protocol.
- **screen / connect (VNC)** — speaks RFB 3.8: handshake, security (None or
  VNC password), then SetPixelFormat/SetEncodings and a FramebufferUpdate.
  Decodes Raw, CopyRect, RRE and Zlib rectangles into a PNG.
- **connect (RDP)** — detection only. A real RDP client is not bundled; if
  `xfreerdp`/`rdesktop` is installed the tool prints the command to run.

## Limits

- RDP connect is not implemented (no client in the stdlib); use xfreerdp.
- VNC encodings: Raw, CopyRect, RRE, Zlib. Tight and Hextile are not
  requested; a server that insists on them will be reported as unsupported.
- One screen capture per connection; no remote control / input injection.

## Verify

```bash
python3 tests/test_rdp_vnc.py     # 11 tests, mock VNC/RDP servers, no network
```
