#!/usr/bin/env python3
"""authguard.py - detect brute-force attacks from Linux auth logs.

Defensive tool. It watches authentication logs, counts failures per source
address, prints a report, and can block repeat offenders at the firewall.

Supported log sources:
    * /var/log/auth.log        (Debian/Ubuntu)
    * /var/log/secure          (RHEL/Fedora)
    * /var/log/xrdp*.log       (xrdp RDP on Linux)
    * systemd journal          (journalctl -u ssh -u xrdp)
    * Windows Security evtx    (only if you export text; Event ID 4625)

Modes:
    --once      scan the current log once and print a report
    --follow    tail the log and keep watching (default)
    --dry-run   never touch the firewall, only report (default when not --block)
    --block     actually block offending IPs (nftables, iptables or fail2ban)

Examples:
    sudo python3 authguard.py --once
    sudo python3 authguard.py --follow --block --window 300 --max-fail 5
    python3 authguard.py --selftest

No network access, no external dependencies. Only reads logs and, with
--block, asks your firewall to drop an address.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time

VERSION = "1.0"

# --------------------------------------------------------------------------- #
# Log line parsing
# --------------------------------------------------------------------------- #

# A real address, not a stray hex character from a word like "failed".
IPV4 = r"(?:\d{1,3}\.){3}\d{1,3}"
IPV6 = r"(?:[0-9a-fA-F]{1,4}:){1,7}[0-9a-fA-F]{0,4}"
IP_G = rf"(?P<ip>\b(?:{IPV4}|{IPV6})\b)"

FAILURE_PATTERNS = [
    # sshd: Failed password for root from 203.0.113.9 port 51000 ssh2
    ("ssh", re.compile(
        r"sshd\[\d+\]:\s+Failed (?:password|publickey|keyboard-interactive)"
        r"(?: for (?:invalid user\s+)?(?P<user>\S+))?"
        r".*?\sfrom\s+" + IP_G + r"\s+port\s+\d+")),
    # sshd: Invalid user admin from 203.0.113.9 port 51000
    ("ssh", re.compile(
        r"sshd\[\d+\]:\s+Invalid user\s+(?P<user>\S+)\s+from\s+" + IP_G)),
    # sshd: Connection closed by authenticating user bob 203.0.113.9 port 51000
    ("ssh", re.compile(
        r"sshd\[\d+\]:\s+Connection closed by authenticating user\s+"
        r"(?P<user>\S+)\s+" + IP_G + r"\s+port\s+\d+")),
    # sshd: error: maximum authentication attempts exceeded for root
    ("ssh", re.compile(
        r"sshd\[\d+\]:\s+error: maximum authentication attempts exceeded"
        r"(?: for (?P<user>\S+))?.*?from\s+" + IP_G)),
    # PAM generic: pam_unix(...): authentication failure; ... rhost=1.2.3.4
    ("pam", re.compile(
        r"pam_unix\([^)]*\):\s+authentication failure;.*?"
        r"(?:rhost|raddr)=(?P<rhost>" + IPV4 + r")"
        r"(?:.*?\buser=(?P<user>\S+))?")),
    # xrdp sesman: login failed for user administrator from 203.0.113.9
    ("rdp", re.compile(
        r"xrdp(?:-sesman)?\[\d+\]:.*?login failed.*?"
        r"(?:user[=: ]+(?P<user>\S+))?.*?" + IP_G, re.I)),
    ("rdp", re.compile(
        r"xrdp(?:-sesman)?\[\d+\]:.*?(?:authentication failed|login failure|"
        r"user \S+ failed).*?" + IP_G, re.I)),
    # Windows event 4625 (an account failed to log on) - if logs are exported
    ("rdp", re.compile(
        r"Event\s*ID:?\s*4625.*?Source Network Address:\s*" + IP_G, re.I)),
    ("rdp", re.compile(
        r"EventID[\"']?\s*[:=]\s*[\"']?4625.*?"
        r"Address[\"']?\s*[:=]\s*[\"']?" + IP_G, re.I)),
    # sudo: pam_unix(sudo:auth): authentication failure; ... user=bob
    ("sudo", re.compile(
        r"pam_unix\(sudo:auth\):\s+authentication failure;"
        r".*?\buser=(?P<user>\S+)")),
]

# Lines that indicate a *success* - used to trust/clear an address.
SUCCESS_PATTERNS = [
    ("ssh", re.compile(
        r"sshd\[\d+\]:\s+Accepted (?:password|publickey|keyboard-interactive)"
        r"(?: for (?P<user>\S+))?.*?\sfrom\s+" + IP_G)),
    ("rdp", re.compile(
        r"xrdp(?:-sesman)?\[\d+\]:.*?login succeeded.*?" + IP_G, re.I)),
]


TS_RE = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})")


def parse_ts(line: str) -> float | None:
    """Log timestamp as epoch seconds. Assumes the current year (logs omit it)."""
    m = TS_RE.match(line)
    if not m:
        return None
    now = _dt.datetime.now()
    try:
        dt = _dt.datetime.strptime(f"{now.year} {m.group(1)}",
                                   "%Y %b %d %H:%M:%S")
    except ValueError:
        return None
    if dt > now + _dt.timedelta(days=1):
        dt = dt.replace(year=now.year - 1)
    return dt.timestamp()


def parse_line(line: str) -> dict | None:
    """Return {'service','ip','user','kind':'fail'|'ok'} or None."""
    for service, rx in FAILURE_PATTERNS:
        m = rx.search(line)
        if m:
            d = m.groupdict()
            ip = d.get("ip") or d.get("rhost")
            if not ip:
                # sudo and other local failures have no remote address
                if service == "sudo":
                    return {"service": service, "ip": "local",
                            "user": (d.get("user") or "").strip(),
                            "kind": "fail", "line": line.strip()[:300]}
                return None
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return None
            return {"service": service, "ip": ip,
                    "user": (d.get("user") or "").strip(),
                    "kind": "fail", "ts": parse_ts(line),
                    "line": line.strip()[:300]}
    for service, rx in SUCCESS_PATTERNS:
        m = rx.search(line)
        if m:
            try:
                ipaddress.ip_address(m.group("ip"))
            except ValueError:
                continue
            return {"service": service, "ip": m.group("ip"),
                    "user": (m.groupdict().get("user") or "").strip(),
                    "kind": "ok", "line": line.strip()[:300]}
    return None


# --------------------------------------------------------------------------- #
# State: sliding-window failure counter per address
# --------------------------------------------------------------------------- #

class Tracker:
    def __init__(self, window: int, max_fail: int, whitelist, decay_ok: int = 2):
        self.window = window
        self.max_fail = max_fail
        self.whitelist = whitelist
        self.decay_ok = decay_ok
        self.events: dict[str, list[tuple[float, str]]] = collections.defaultdict(list)
        self.blocked: dict[str, float] = {}
        self.total_fail = 0
        self.total_ok = 0

    def whitelisted(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for w in self.whitelist:
            try:
                if "/" in w:
                    if addr in ipaddress.ip_network(w, strict=False):
                        return True
                elif addr == ipaddress.ip_address(w):
                    return True
            except ValueError:
                continue
        return False

    def record(self, ev: dict) -> bool:
        """Record an event. Returns True if the address should be blocked."""
        ip = ev["ip"]
        now = time.time()
        if self.whitelisted(ip):
            return False
        if ev["kind"] == "ok":
            self.total_ok += 1
            self.events[ip] = [e for e in self.events[ip]
                               if now - e[0] <= self.window][:0] or \
                [(t, s) for t, s in self.events[ip] if now - t <= self.window]
            # a success clears the slate for that address
            self.events[ip] = []
            self.blocked.pop(ip, None)
            return False

        self.total_fail += 1
        ts = ev.get("ts") or now
        self.events[ip] = [(t, s) for t, s in self.events[ip]
                           if now - t <= self.window]
        self.events[ip].append((ts, ev["service"]))
        return len(self.events[ip]) >= self.max_fail and ip not in self.blocked

    def unblock_expired(self, block_ttl: int) -> list[str]:
        now = time.time()
        freed = [ip for ip, t in self.blocked.items() if now - t > block_ttl]
        for ip in freed:
            self.blocked.pop(ip, None)
        return freed

    def summary(self) -> list[dict]:
        now = time.time()
        rows = []
        for ip, evs in self.events.items():
            live = [(t, s) for t, s in evs if now - t <= self.window]
            if not live:
                continue
            services = collections.Counter(s for _, s in live)
            rows.append({
                "ip": ip,
                "failures": len(live),
                "services": dict(services),
                "first": _dt.datetime.fromtimestamp(live[0][0]).isoformat(),
                "last": _dt.datetime.fromtimestamp(live[-1][0]).isoformat(),
                "blocked": ip in self.blocked,
            })
        rows.sort(key=lambda r: r["failures"], reverse=True)
        return rows


# --------------------------------------------------------------------------- #
# Firewall backends
# --------------------------------------------------------------------------- #

class Firewall:
    """Blocking backends. All idempotent and scoped to a dedicated chain/set."""

    def __init__(self, backend: str, chain: str, dry_run: bool = True):
        self.backend = backend
        self.chain = chain
        self.dry_run = dry_run
        self._done: set[str] = set()

    def _run(self, cmd: list[str]) -> tuple[int, str]:
        if self.dry_run:
            return 0, "would run: " + " ".join(cmd)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return p.returncode, (p.stderr or p.stdout).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)

    def setup(self) -> bool:
        if self.backend == "nft":
            # dedicated table+set so we never touch the user's rules
            cmds = [
                ["nft", "add table inet authguard"],
                ["nft", "add set inet authguard block { type ipv4_addr; "
                        "flags timeout; }"],
                ["nft", "add set inet authguard block6 { type ipv6_addr; "
                        "flags timeout; }"],
                ["nft", "add chain inet authguard input { type filter hook "
                        "input priority -10; policy accept; }"],
                ["nft", "add rule inet authguard input ip saddr "
                        "@authguard_block drop"],
                ["nft", "add rule inet authguard input ip6 saddr "
                        "@authguard_block6 drop"],
            ]
        elif self.backend == "iptables":
            cmds = []
            for binary, v6 in (("iptables", False), ("ip6tables", True)):
                c = f"AUTHGUARD-{self.chain}"
                cmds += [
                    [binary, "-n", "-L", c],          # probe
                ]
            # we create per-family chains below; probe first
            cmds = []
            for binary in ("iptables", "ip6tables"):
                c = f"AUTHGUARD-{self.chain}"
                cmds.append([binary, "-N", c])
                cmds.append([binary, "-I", "INPUT", "1", "-j", c])
                cmds.append([binary, "-A", c, "-j", "RETURN"])
        elif self.backend == "fail2ban":
            return True
        else:
            return False

        for cmd in cmds:
            rc, out = self._run(cmd)
            # -N fails if the chain exists; that is fine
            if rc != 0 and "File exists" not in out and "Chain already exists" not in out:
                if cmd[-1] not in ("RETURN",):
                    pass
        return True

    def block(self, ip: str, ttl: int) -> tuple[bool, str]:
        if ip in self._done:
            return True, "already blocked"
        if self.backend == "nft":
            fam = "block6" if ":" in ip else "block"
            rc, out = self._run(["nft", "add element inet authguard", fam,
                                 "{", ip, f"timeout {ttl}s", "}"])
        elif self.backend == "iptables":
            binary = "ip6tables" if ":" in ip else "iptables"
            rc, out = self._run([binary, "-I", f"AUTHGUARD-{self.chain}", "1",
                                 "-s", ip, "-j", "DROP"])
        elif self.backend == "fail2ban":
            rc, out = self._run(["fail2ban-client", "set", "sshd",
                                 "banip", ip])
        else:
            return False, "no backend"
        ok = rc == 0
        if ok:
            self._done.add(ip)
        return ok, out

    def unblock(self, ip: str) -> tuple[bool, str]:
        self._done.discard(ip)
        if self.backend == "nft":
            fam = "block6" if ":" in ip else "block"
            rc, out = self._run(["nft", "delete element inet authguard", fam,
                                 "{", ip, "}"])
        elif self.backend == "iptables":
            binary = "ip6tables" if ":" in ip else "iptables"
            rc, out = self._run([binary, "-D", f"AUTHGUARD-{self.chain}",
                                 "-s", ip, "-j", "DROP"])
        elif self.backend == "fail2ban":
            rc, out = self._run(["fail2ban-client", "set", "sshd",
                                 "unbanip", ip])
        else:
            return False, "no backend"
        return rc == 0, out

    def teardown(self) -> None:
        if self.dry_run:
            return
        if self.backend == "nft":
            self._run(["nft", "delete table inet authguard"])
        elif self.backend == "iptables":
            for binary in ("iptables", "ip6tables"):
                c = f"AUTHGUARD-{self.chain}"
                self._run([binary, "-D", "INPUT", "-j", c])
                self._run([binary, "-F", c])
                self._run([binary, "-X", c])


def pick_backend(preferred: str) -> str:
    if preferred != "auto":
        return preferred
    if shutil.which("nft"):
        return "nft"
    if shutil.which("fail2ban-client"):
        return "fail2ban"
    if shutil.which("iptables"):
        return "iptables"
    return "none"


# --------------------------------------------------------------------------- #
# Log reading
# --------------------------------------------------------------------------- #

def candidate_logs(explicit: list[str]) -> list[str]:
    paths = list(explicit)
    if not paths:
        for p in ("/var/log/auth.log", "/var/log/secure",
                  "/var/log/xrdp.log", "/var/log/xrdp-sesman.log"):
            if os.path.isfile(p) and os.access(p, os.R_OK):
                paths.append(p)
    return paths


def follow(paths: list[str]):
    """Yield lines from the given files, tailing them like `tail -F`."""
    if not paths:
        return
    # subprocess tail handles rotation and multiple files cleanly
    proc = subprocess.Popen(["tail", "-n", "0", "-F", *paths],
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        proc.terminate()


def read_once(paths: list[str]) -> list[str]:
    lines = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                lines.extend(fh.read().splitlines())
        except OSError:
            continue
    return lines


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="authguard",
        description="Detect brute-force attacks from Linux auth logs.")
    p.add_argument("--logs", nargs="*", default=[],
                   help="log files to read (default: auth.log/secure/xrdp*)")
    p.add_argument("--once", action="store_true",
                   help="scan once and print a report")
    p.add_argument("--follow", action="store_true",
                   help="keep watching (default)")
    p.add_argument("--window", type=int, default=300,
                   help="sliding window in seconds (default 300)")
    p.add_argument("--max-fail", type=int, default=5,
                   help="failures in the window before blocking (default 5)")
    p.add_argument("--block-ttl", type=int, default=3600,
                   help="how long a block lasts, seconds (default 3600)")
    p.add_argument("--block", action="store_true",
                   help="actually block offenders (default: report only)")
    p.add_argument("--dry-run", action="store_true",
                   help="with --block: print the firewall commands, change nothing")
    p.add_argument("--backend", default="auto",
                   choices=["auto", "nft", "iptables", "fail2ban", "none"])
    p.add_argument("--whitelist", default="127.0.0.1,::1",
                   help="comma-separated IPs/CIDRs never blocked")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--version", action="version", version=f"authguard {VERSION}")
    return p.parse_args(argv)


def selftest() -> int:
    lines = [
        'Dec 20 01:02:03 host sshd[100]: Failed password for root from 203.0.113.9 port 51000 ssh2',
        'Dec 20 01:02:05 host sshd[100]: Failed password for invalid user admin from 203.0.113.9 port 51001 ssh2',
        'Dec 20 01:02:09 host sshd[101]: Invalid user oracle from 198.51.100.7 port 51002',
        'Dec 20 01:02:12 host sshd[100]: Accepted password for bob from 192.0.2.5 port 51003 ssh2',
        'Dec 20 01:02:20 host xrdp-sesman[55]: login failed for user administrator from 203.0.113.9',
        'Dec 20 01:02:21 host xrdp[56]: authentication failed for 203.0.113.9',
        'Dec 20 01:02:22 host su: pam_unix(sudo:auth): authentication failure; logname=bob uid=1000 tty=/dev/pts/0 user=bob',
    ]
    ok = True
    for ln in lines:
        ev = parse_line(ln)
        status = "NOT MATCHED" if ev is None else f"{ev['kind']:4} {ev['service']:5} {ev['ip']}"
        print(f"{status:32} | {ln[:70]}")
    # assertions
    ev = parse_line(lines[0])
    ok &= ev is not None and ev["ip"] == "203.0.113.9" and ev["kind"] == "fail"
    ev = parse_line(lines[3])
    ok &= ev is not None and ev["kind"] == "ok" and ev["ip"] == "192.0.2.5"
    ev = parse_line(lines[5])
    ok &= ev is not None and ev["service"] == "rdp"
    ev = parse_line("Dec 20 01:00:00 host sshd[1]: Server listening on 0.0.0.0 port 22.")
    ok &= ev is None
    # tracker
    t = Tracker(window=300, max_fail=3, whitelist=["127.0.0.1"])
    for _ in range(2):
        t.record({"ip": "203.0.113.9", "service": "ssh", "kind": "fail"})
    ok &= not t.record({"ip": "203.0.113.9", "service": "ssh", "kind": "fail"}) is False
    ok &= t.record({"ip": "203.0.113.9", "service": "ssh", "kind": "fail"})
    ok &= not t.record({"ip": "127.0.0.1", "service": "ssh", "kind": "fail"})
    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return selftest()

    whitelist = [w.strip() for w in args.whitelist.split(",") if w.strip()]
    tracker = Tracker(window=args.window, max_fail=args.max_fail,
                      whitelist=whitelist)
    paths = candidate_logs(args.logs)
    backend = pick_backend(args.backend)
    fw = Firewall(backend, "authguard", dry_run=not args.block or args.dry_run)

    if not paths:
        print("error: no readable auth log found (tried auth.log, secure, "
              "xrdp*.log). Pass --logs PATH.", file=sys.stderr)
        return 2

    if args.block and not args.dry_run:
        if os.geteuid() != 0:
            print("error: --block needs root (it changes firewall rules).",
                  file=sys.stderr)
            return 2
        if backend == "none":
            print("error: no firewall backend found (nft/iptables/fail2ban).",
                  file=sys.stderr)
            return 2
        fw.setup()

    def emit(text: str) -> None:
        print(text, flush=True)

    def handle(line: str) -> None:
        ev = parse_line(line)
        if ev is None:
            return
        should_block = tracker.record(ev)
        if args.json:
            emit(json.dumps({"event": ev, "blocked": should_block}))
        elif should_block:
            ok, out = fw.block(ev["ip"], args.block_ttl)
            tracker.blocked[ev["ip"]] = time.time()
            msg = (f"[BLOCK] {ev['ip']} after {len(tracker.events[ev['ip']])} "
                   f"failures ({ev['service']})")
            if fw.dry_run:
                msg += f"  ({out})"
            elif not ok:
                msg += f"  FAILED: {out}"
            emit(msg)
        elif ev["kind"] == "fail" and not args.json and not args.once:
            emit(f"[fail] {ev['ip']:16} {ev['service']:5} "
                 f"{len(tracker.events[ev['ip']])}/{args.max_fail}")

    if args.once:
        for line in read_once(paths):
            handle(line)
        rows = tracker.summary()
        if args.json:
            print(json.dumps({"failures": tracker.total_fail,
                              "successes": tracker.total_ok,
                              "attackers": rows}, indent=2))
        else:
            print(f"\nscanned {len(paths)} log(s): "
                  f"{tracker.total_fail} failures, {tracker.total_ok} successes")
            if not rows:
                print("no repeated failures found")
                return 0
            print(f"\n{'ADDRESS':18}{'FAILS':>7}  {'SERVICES':18} LAST")
            for r in rows:
                svc = ",".join(r["services"])
                print(f"{r['ip']:18}{r['failures']:>7}  {svc:18} {r['last']}")
            print(f"\nblock threshold: {args.max_fail} in {args.window}s "
                  f"(use --block to enforce)")
        return 0

    # follow mode
    if not args.json:
        emit(f"authguard {VERSION}: watching {', '.join(paths)} "
             f"(window {args.window}s, threshold {args.max_fail}, "
             f"block={'on' if args.block else 'off'})")
    try:
        for line in follow(paths):
            handle(line)
    except KeyboardInterrupt:
        pass
    finally:
        if args.block:
            fw.teardown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
