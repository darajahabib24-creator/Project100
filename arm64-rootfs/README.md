# arm64 apt inside an armv7 terminal

Run a real **arm64/aarch64 Ubuntu 24.04** (or Debian 12) rootfs inside a
32-bit armv7 terminal with **no root** and **no emulator**, using `proot`.

    arm64-rootfs/
      arm64-rootfs.sh     launcher: `./arm64-rootfs.sh` or `./arm64-rootfs.sh apt-get update`
      ubuntu-arm64.tar.gz the arm64 rootfs (83 MB, extracts to ./ubuntu-arm64)
      proot               arm64 proot 5.1.0 (for arm64 hosts)
      proot-armv7         armhf proot 5.1.0 (for armv7/Android hosts)
      libtalloc.so.2      proot's only extra shared library
      make-rootfs.sh      rebuilds the tarball from scratch (x86_64/arm64 host)

## Why it works

`apt`/`dpkg` in the rootfs are genuine arm64 ELF binaries. A 32-bit CPU cannot
run a 64-bit kernel, so the usual chroot route is out. proot avoids that: it
intercepts syscalls with ptrace and translates them, so arm64 *userland* runs
natively on the armv7 kernel. `proot -R <rootfs>` makes `<rootfs>` appear as `/`
and links it for the rootfs's own architecture, so `apt-get install` just works.

On an arm64 host this needs no proot emulation tricks at all — the same command
runs the arm64 binaries directly.

## On the device (armv7 Android / Termux)

1. Copy this folder to the device.
2. `pkg install proot tar` (Termux proot supports foreign-arch linking).
3. `./arm64-rootfs.sh` — you get an arm64 bash. Then:

       apt-get update
       apt-get install -y htop

`./arm64-rootfs.sh apt-get update` runs a single command and exits.

`PROOT=/path/to/proot ./arm64-rootfs.sh` forces a specific proot binary.

## Verification done here (x86_64 host)

* `debootstrap --arch=arm64 noble` + second stage under qemu-aarch64 builds the
  rootfs; `dpkg --print-architecture` reports `arm64`, `apt-get` reports
  `apt 2.7.14 (arm64)`.
* `apt-get update` and `apt-get install -y htop` succeed inside the rootfs
  (htop runs and prints `htop 3.3.0`).
* Bundled `proot` is a real aarch64 ELF and loads (`proot --version` -> 5.1.0).

Not verified here: on-device behaviour — proot needs ptrace, which qemu-user
cannot provide, so the armv7+proot path was not executed on this machine.

## Notes / pitfalls

* `/tmp` and `/var/tmp` must be `1777` (sticky) or apt's `_apt` user cannot
  write its temp files ("Couldn't create temporary file ... passing config to
  apt-key" -> "repository is not signed").
* `proot -R` binds the host `/etc/resolv.conf` only if it exists; Android has
  none, so the rootfs ships its own with public resolvers.
* The tarball excludes `/dev/*` (Android tar cannot `mknod`); proot fakes
  `/dev/null`, `/dev/zero`, `/dev/random`, `/dev/tty` and `/dev/ptmx`.
* Device nodes are excluded from the tarball, so the extracted rootfs has an
  empty `/dev` plus `pts/` and `shm/` directories.
