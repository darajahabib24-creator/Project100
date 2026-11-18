#!/bin/sh
# make-rootfs.sh - build the arm64 rootfs tarball bundled with arm64-rootfs.sh.
#
# Runs on an x86_64 (or arm64) Linux host. It downloads the arm64 packages with
# debootstrap's first stage (which needs no emulation - it only unpacks .debs),
# then runs debootstrap's second stage *inside* the arm64 rootfs under
# qemu-aarch64 so dpkg configures arm64 binaries for real.
#
# Requirements: debootstrap, qemu-user-static (qemu-aarch64-static), dpkg-deb,
#               tar, gzip, ca-certificates.
#
# Output: ubuntu-arm64.tar.gz  (move it next to arm64-rootfs.sh)
#
# Usage:  sudo ./make-rootfs.sh [ubuntu|debian] [suite]

set -eu

DISTRO=${1:-ubuntu}
SUITE=${2:-noble}
ARCH=arm64
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORK=${WORK:-$HERE/.build}
TARGET=$WORK/rootfs
COMPONENTS=${COMPONENTS:-main}
INCLUDE=${INCLUDE:-apt,ca-certificates,gpgv,gnupg,htop,nano,curl,wget,procps}

msg() { printf '[make-rootfs] %s\n' "$*"; }
die() { printf '[make-rootfs] ERROR: %s\n' "$*" >&2; exit 1; }

case "$DISTRO" in
    ubuntu) MIRROR=${MIRROR:-http://ports.ubuntu.com/ubuntu-ports} ;;
    debian) MIRROR=${MIRROR:-http://deb.debian.org/debian} ;;
    *) die "unknown distro $DISTRO" ;;
esac

command -v qemu-aarch64-static >/dev/null || command -v qemu-aarch64 >/dev/null \
    || die "qemu-aarch64-static not found (apt-get install qemu-user-static)"
QEMU=$(command -v qemu-aarch64-static || command -v qemu-aarch64)

[ "$(id -u)" = 0 ] || die "run as root (sudo $0)"

mkdir -p "$WORK"
rm -rf "$TARGET"
mkdir -p "$TARGET"

# ---- stage 1: download + unpack arm64 packages (no emulation needed) --------
msg "debootstrap first stage: $DISTRO/$SUITE $ARCH"
debootstrap \
    --arch=$ARCH \
    --variant=minbase \
    --components=$COMPONENTS \
    --include="$INCLUDE" \
    --no-check-gpg \
    "$SUITE" "$TARGET" "$MIRROR"

# ---- make the arm64 rootfs executable under qemu ---------------------------
# debootstrap's second stage runs /sbin/ldconfig and arm64 dpkg; bind mount the
# qemu interpreter into the rootfs so chroot works.
mkdir -p "$TARGET/usr/bin"
cp "$QEMU" "$TARGET/usr/bin/qemu-aarch64-static"
chmod 755 "$TARGET/usr/bin/qemu-aarch64-static"

msg "debootstrap second stage (under $QEMU)"
chroot "$TARGET" /debootstrap/debootstrap --second-stage

# ---- post-fix: the two things apt needs on Android -------------------------
chmod 1777 "$TARGET/tmp" "$TARGET/var/tmp"
cat > "$TARGET/etc/resolv.conf" <<'EOF'
nameserver 8.8.8.8
nameserver 8.8.4.4
nameserver 1.1.1.1
options edns0 trust-ad
EOF
printf '127.0.0.1 localhost\n::1 localhost ip6-localhost\n' > "$TARGET/etc/hosts"
rm -f "$TARGET/usr/bin/qemu-aarch64-static"

# ---- package it ------------------------------------------------------------
OUT=$HERE/$DISTRO-$SUITE-arm64.tar.gz
msg "packing $OUT"
tar -czf "$OUT" --exclude='./dev/*' -C "$TARGET" .
msg "done: $OUT"
msg "sha256: $(sha256sum "$OUT" | cut -d' ' -f1)"
