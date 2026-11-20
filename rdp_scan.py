#!/usr/bin/env python3
"""rdp_scan - small, authorised TCP port scanner for RDP (3389) and other
services.

Use it only against systems you own or have written permission to scan.

The scan is depth-limited: a TCP connect plus a short banner read. It does
not guess credentials, does not attempt a login, and does not probe
application logic. It answers one question: what TCP ports are listening
and how might they respond.

Examples
--------
# scan a single host
python3 rdp_scan.py 192.168.1.5

# scan a subnet, looking only at the RDP port
python3 rdp_scan.py 192.168.1.0/24 --ports 3389

# scan with a banner read, a per-host delay and a timeout
python3 rdp_scan.py 10.0.0.0/24 --ports 3389,22,443 --delay 0.25 --timeout 3
"""

import argparse
import ipaddress
import json
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_PORTS = [3389]
DEFAULT_DELAY = 0.5
DEFAULT_TIMEOUT = 3.0


def fmt_result(res):
    port = res["port"]
    banner = f"  banner: {res['banner']!r}" if res.get("banner") else ""
    return (f"{res['host']:<20} tcp/{port:<5} {res['state']:<8} "
            f"{res.get('family', ''):<5}{banner}")


def check_port(host, port, timeout):
    """Return {'host','port','state','family','banner'*}."""
    states = []
    banner = None
    for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        af, st, proto, _, sa = res
        s = None
        try:
            s = socket.socket(af, st, proto)
            s.settimeout(timeout)
            s.connect(sa)
            return {"host": host, "port": port, "state": "open",
                    "family": socket.getnameinfo(sa, 0)[0]}
        except socket.timeout:
            states.append("timeout")
        except ConnectionRefusedError:
            states.append("refused")
        except OSError as exc:
            states.append(f"error:{exc.errno}")
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    # All address families blocked/refused -> decide closed vs filtered.
    states = [x for x in states if x not in ("", "ok")]
    if not states:
        state = "closed"
    elif "refused" in states:
        state = "closed"
    else:
        state = "filtered"

    if state == "open":
        # A short banner read tells you whether a real service answers.
        try:
            af, st, proto, _, sa = socket.getaddrinfo(
                host, port, 0, socket.SOCK_STREAM)[0]
            s = socket.socket(af, st, proto)
            s.settimeout(timeout)
            s.connect(sa)
            s.settimeout(timeout)
            banner = s.recv(4096).decode(errors="replace").strip()[:120]
            s.close()
        except Exception:
            pass

    return {"host": host, "port": port, "state": state,
            "family": socket.getnameinfo(
                socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][4], 0)[0],
            "banner": banner}


def expand_target(target, ports):
    """Expand one hostname/CIDR into a list of (host, port, error) triples."""
    if "/" in target:
        try:
            net = ipaddress.ip_network(target, strict=False)
        except ValueError as exc:
            return [(target, None, str(exc))]
        return [(str(addr), p, None) for addr in net.hosts() for p in ports]

    try:
        infos = socket.getaddrinfo(target, None)
    except socket.gaierror as exc:
        return [(target, None, str(exc))]
    seen, out = set(), []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            out.extend((addr, p, None) for p in ports)
    return out


def scan_host(target, ports):
    """Return a list of result dicts for one hostname/CIDR target."""
    results = []
    for host, port, err in expand_target(target, ports):
        if err is not None:
            results.append({"host": host, "port": port,
                            "state": "error", "error": err})
        else:
            results.append({"host": host, "port": port, "state": None})
    return results


def scan_item(item, timeout, banner):
    host, port = item["host"], item["port"]
    res = check_port(host, port, timeout)
    res.pop("family", None)
    if banner and res["state"] == "open":
        res["banner"] = None
        try:
            family, st, proto, _, sa = socket.getaddrinfo(
                host, port, 0, socket.SOCK_STREAM)[0]
            s = socket.socket(family, st, proto)
            s.settimeout(timeout)
            s.connect(sa)
            s.settimeout(timeout)
            res["banner"] = s.recv(4096).decode(errors="replace").strip()[:120]
            s.close()
        except Exception:
            pass
    return res


def collect_targets(targets, ports):
    items = []
    for t in targets:
        items.extend(scan_host(t, ports))
    return items


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Authorised TCP port scanner (RDP and more).")
    ap.add_argument("targets", nargs="+",
                    help="host, CIDR, or hostname(s) to scan")
    ap.add_argument("--ports", default=",".join(map(str, DEFAULT_PORTS)),
                    help=f"comma-separated TCP ports (default: {DEFAULT_PORTS})")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help=f"seconds to wait between hosts (default {DEFAULT_DELAY})")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"per-port timeout, seconds (default {DEFAULT_TIMEOUT})")
    ap.add_argument("--threads", type=int, default=16,
                    help="concurrent workers (default 16)")
    ap.add_argument("--banner", action="store_true",
                    help="read the service banner on open ports")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object per open/filtered/closed result")
    args = ap.parse_args(argv)

    try:
        ports = [int(p) for p in args.ports.split(",") if p.strip()]
    except ValueError:
        ap.error(f"--ports must be comma-separated numbers, got {args.ports!r}")
    if not ports:
        ap.error("--ports is empty")
    if not (1 <= min(ports) <= 65535) or not (1 <= max(ports) <= 65535):
        ap.error("--ports must be in the range 1-65535")

    items = collect_targets(args.targets, ports)
    if not items:
        ap.error("no addresses to scan")

    results = []
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futures = {ex.submit(scan_item, it, args.timeout, args.banner): it
                   for it in items}
        for fut in as_completed(futures):
            results.append(fut.result())
            if args.delay:
                time.sleep(args.delay)

    open_r = [r for r in results if r["state"] == "open"]
    closed_r = [r for r in results if r["state"] == "closed"]
    filtered_r = [r for r in results if r["state"] == "filtered"]
    if args.json:
        for r in results:
            print(json.dumps(r))
        return 0

    print("PORT SCAN\n" + "=" * 40)
    print("targets:  " + ", ".join(args.targets))
    print("ports:    " + ", ".join(str(p) for p in ports))
    print("threads:  " + str(args.threads) + f"   delay: {args.delay}s"
          + f"   timeout: {args.timeout}s")
    print("-" * 40)
    print(f"open:    {len(open_r):>4}    filtered: {len(filtered_r):>4}")
    print(f"closed:  {len(closed_r):>4}")
    if open_r:
        print("\nopen ports:")
        for i, r in enumerate(sorted(open_r, key=lambda x: (x["host"], x["port"])),
                             1):
            line = f"{i:>3}. {r['host']:<20} tcp/{r['port']}"
            if r.get("banner"):
                line += f"  ({r['banner']})"
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
