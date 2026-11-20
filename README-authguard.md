# authguard

Detects brute-force attacks against Linux services from auth logs, and can block
the offenders at the firewall. Defensive only — it reads logs and adds drop
rules; it never attempts a login.

Reads: `/var/log/auth.log` (Debian/Ubuntu), `/var/log/secure` (RHEL/Fedora),
`/var/log/xrdp*.log` (xrdp), plus exported Windows Security logs (Event ID 4625).

Detects: SSH (`Failed password`, `Invalid user`, `maximum authentication attempts
exceeded`), PAM failures with `rhost=`, xrdp login failures, sudo failures, and
RDP failures.

## Quick start

```bash
# look before you block: report only
sudo python3 authguard.py --once

# watch live, block after 5 failures in 5 minutes
sudo python3 authguard.py --follow --block --window 300 --max-fail 5

# preview the firewall commands without changing anything
sudo python3 authguard.py --follow --block --dry-run

# self-test (no logs, no root needed)
python3 authguard.py --selftest
```

## Options

```
--logs PATH...   log files to read (default: auth.log / secure / xrdp*)
--once           scan the current log and print a report
--follow         keep watching (default)
--window N       sliding window in seconds (default 300)
--max-fail N     failures in the window before blocking (default 5)
--block-ttl N    how long a block lasts, seconds (default 3600)
--block          actually block (needs root)
--dry-run        with --block: print the commands, change nothing
--backend        auto | nft | iptables | fail2ban | none
--whitelist      comma-separated IPs/CIDRs never blocked (default 127.0.0.1,::1)
--json           machine-readable output (one object per event + a summary)
--selftest       parse/tracking self-test
```

## What blocking does

- **nftables** (preferred): creates a dedicated `inet authguard` table and a
  `block`/`block6` set with per-entry timeouts. Your existing rules are untouched.
- **iptables**: creates an `AUTHGUARD-authguard` chain per family, jumps to it
  from `INPUT`, and adds `-s <ip> -j DROP` rules.
- **fail2ban**: calls `fail2ban-client set sshd banip <ip>`.

`--dry-run` prints the exact commands. `--block` without `--dry-run` needs root.

## Behaviour worth knowing

- A successful login from an address clears its failure count.
- Blocks expire after `--block-ttl`; the nftables set uses native timeouts.
- Whitelisted addresses are never blocked, and `127.0.0.1`/`::1` are whitelisted
  by default — do not remove localhost or you can lock yourself out.
- `--once` on a live log is a point-in-time snapshot; use `--follow` for
  continuous protection.
- It counts failures; it does not distinguish a forgotten password from an
  attack. Tune `--window`/`--max-fail` to your noise level before enabling
  `--block`.

## Verify it is working

```bash
python3 authguard.py --selftest
python3 authguard.py --once --logs /var/log/auth.log
```

Feed it a synthetic log:

```bash
python3 - <<'EOF'
open('/tmp/fake.log','w').write("\n".join(
  f"Nov 20 12:00:0{i} h sshd[{i}]: Failed password for root from 9.9.9.9 port {i} ssh2"
  for i in range(6)))
EOF
python3 authguard.py --once --logs /tmp/fake.log --max-fail 3
```
