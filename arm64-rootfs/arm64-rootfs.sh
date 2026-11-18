#!/bin/sh
# arm64-rootfs.sh - one-command launcher for an arm64/aarch64 Ubuntu (or Debian)
# rootfs under proot, on a 32-bit armv7 Android device - no root required.
#
# WHY THIS WORKS
#   The terminal is armv7 (32-bit ARM). An arm64 apt/dpkg cannot execute there
#   directly, and you cannot run a 64-bit kernel on a 32-bit CPU. proot solves
#   it without emulation: it runs arm64 *userland* programs natively by
#   translating syscall arguments (ptrace-based), while the host kernel stays
#   armv7. `proot -R <rootfs>` makes <rootfs> appear as / and links it for the
#   rootfs's own architecture, so /usr/bin/apt-get is a real arm64 binary.
#
#   On an arm64 host (or an arm64 device) this same script runs natively and
#   needs no emulator at all. On armv7 you need a proot build that supports
#   foreign-arch linking - Termux proot 5.1.107+ does (use proot-static).
#
# USAGE
#   ./arm64-rootfs.sh                     # interactive arm64 bash
#   ./arm64-rootfs.sh apt-get update      # run one command, then exit
#   ./arm64-rootfs.sh -u                  # (re)extract the bundled tarball
#   ./arm64-rootfs.sh -d debian           # use Debian bookworm instead
#
# FILES
#   arm64-rootfs.sh        this launcher
#   ubuntu-arm64.tar.gz    bundled rootfs (extracts to ./ubuntu-arm64)
#   proot                   optional: bundled arm64 proot (used on arm64 hosts)
#   libtalloc.so.2          proot's only extra library
#
# Requirements on the device: proot, tar, and ~250 MB free in this folder.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DISTRO=${DISTRO:-ubuntu}

case "$DISTRO" in
    ubuntu) ROOTFS_DIR=${ROOTFS_DIR:-$SCRIPT_DIR/ubuntu-arm64}
            ROOTFS_TGZ=${ROOTFS_TGZ:-$SCRIPT_DIR/ubuntu-arm64.tar.gz} ;;
    debian) ROOTFS_DIR=${ROOTFS_DIR:-$SCRIPT_DIR/debian-arm64}
            ROOTFS_TGZ=${ROOTFS_TGZ:-$SCRIPT_DIR/debian-arm64.tar.gz} ;;
    *) printf '[arm64-rootfs] ERROR: unknown DISTRO=%s (use ubuntu or debian)\n' "$DISTRO" >&2; exit 1 ;;
esac

msg() { printf '[arm64-rootfs] %s\n' "$*"; }
die() { printf '[arm64-rootfs] ERROR: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- proot ----
find_proot() {
    if [ -n "${PROOT:-}" ]; then
        [ -x "$PROOT" ] && { echo "$PROOT"; return 0; }
        die "PROOT=$PROOT is not executable"
    fi
    # Termux
    for p in /data/data/com.termux/files/usr/bin/proot \
             /data/data/com.termux/files/usr/bin/proot-static \
             /data/data/com.termux/files/usr/bin/proot-arm64; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    command -v proot >/dev/null 2>&1 && { command -v proot; return 0; }
    # bundled (native arm64 host)
    for p in "$SCRIPT_DIR/proot" "$SCRIPT_DIR/bin/proot"; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    die "no proot found. Install Termux: pkg install proot (or proot-distro), or set PROOT=/path/to/proot"
}

# -------------------------------------------------------------- rootfs ----
extract_rootfs() {
    [ -d "$ROOTFS_DIR/usr/bin" ] && return 0
    [ -f "$ROOTFS_TGZ" ] || die "missing $ROOTFS_TGZ (this folder must keep the tarball)"
    msg "extracting $(basename "$ROOTFS_TGZ") -> $ROOTFS_DIR"
    mkdir -p "$ROOTFS_DIR" || die "cannot create $ROOTFS_DIR"
    # --no-same-owner: Android tar warns on chown; harmless on a normal host.
    # Device nodes are excluded from the tarball (Android tar cannot mknod);
    # proot fakes /dev/null, /dev/zero, /dev/random, /dev/tty and /dev/ptmx.
    tar -xzf "$ROOTFS_TGZ" -C "$ROOTFS_DIR" --no-same-owner 2>/dev/null \
        || tar -xzf "$ROOTFS_TGZ" -C "$ROOTFS_DIR" \
        || die "tar extraction failed"
    mkdir -p "$ROOTFS_DIR/dev/pts" "$ROOTFS_DIR/dev/shm"
    [ -x "$ROOTFS_DIR/usr/bin/apt-get" ] || die "rootfs has no /usr/bin/apt-get - bad tarball?"
}

# apt drops to the _apt user for its helpers; without a sticky /tmp it fails
# with "Couldn't create temporary file ... for passing config to apt-key".
fix_rootfs() {
    chmod 1777 "$ROOTFS_DIR/tmp" "$ROOTFS_DIR/var/tmp" 2>/dev/null
    # Android has no host resolv.conf for proot -R to bind; write public DNS.
    cat > "$ROOTFS_DIR/etc/resolv.conf" <<'EOF'
nameserver 8.8.8.8
nameserver 8.8.4.4
nameserver 1.1.1.1
options edns0 trust-ad
EOF
    printf '127.0.0.1 localhost\n::1 localhost ip6-localhost\n' > "$ROOTFS_DIR/etc/hosts" 2>/dev/null
}

# --------------------------------------------------------------- main ----
case "${1:-}" in
    -u|--update|--extract)
        rm -rf "$ROOTFS_DIR"
        extract_rootfs
        fix_rootfs
        msg "ready: $ROOTFS_DIR"
        exit 0 ;;
    -h|--help)
        sed -n '2,40p' "$0"; exit 0 ;;
esac

PROOT=$(find_proot)
extract_rootfs
fix_rootfs

# proot's -R binds ROOTFS_DIR as / and links it for the rootfs arch.
# -b /dev,/proc,/sys: device/proc/sys access; -w /: start in /.
msg "starting arm64 rootfs with $PROOT"
exec "$PROOT" -R "$ROOTFS_DIR" \
    -b /dev \
    -b /proc \
    -b /sys \
    -w / \
    /bin/bash -lc 'cd /; exec "$@"' bash "$@"
