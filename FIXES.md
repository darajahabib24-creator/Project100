# VS Code Shell 0.2.1 -> 0.2.2 fixes

Fixed APK: `VSCodeShell-0.2.2-armv7.apk` (armv7, minSdk 23, targetSdk 34)
SHA-256: `7808f2c5f5eebdbef3156e57413fcf33a4968f6b12a0c80dd194b3c053b6b6e3`
Signing key: `vscode-debug.keystore` (store/key password `android`, alias `androiddebugkey`).
The key is new, so uninstall 0.2.1 before installing this build.

## 1. Editor white screen / "server not up yet — check Terminal"

`MainActivity.showTab("Editor")` resolved the URL *before* starting the server:

```kotlin
val url = resolveUrl(intent?.data)   // server not running -> "about:blank"
if (!shell.isServerRunning()) { setStatus("starting vscode-server…"); shell.startServer() }
v.loadUrl(url)                       // loads about:blank -> white page + the status text
```

Fix: `resolveUrl` now returns `file:///android_asset/www/loading.html` instead of
`about:blank` when the server is not up. That page polls `http://127.0.0.1:8124/`
and redirects to the workbench once it answers; after ~90 s it offers a Retry
button that calls `Android.serverStart()`.

New asset: `assets/www/loading.html`.

## 2. Terminal was read-only (stdin closed immediately)

`ShellManager.startTerminal()` passed `stdin = true` to `run()`, and `run()` starts
a thread that closes the process output stream when that flag is set. The terminal
process therefore got EOF on stdin right away, so `writeTerminal()` (typing) failed.

Fix: pass `false` so the stdin pipe stays open.

```kotlin
this.termProc = run(cmd, onOutput, onExit, false)   // was true
```

## 3. Packages: apt did not work

Two independent rootfs problems, both fixed at runtime in `ShellManager.prepare()`
via a new `fixEnvironment()` called at the end of `prepare()`:

* `/tmp` and `/var/tmp` ended up mode `0755`. `TarGzExtractor` only calls
  `mkdirs()` for directories and `setExecutable(true,false)` for files; it never
  applies the tar directory modes, so the `1777` sticky bit is lost. apt drops to
  the `_apt` user for its helpers and could not create `/tmp/apt.conf.XXX`
  ("Couldn't create temporary file ... for passing config to apt-key" ->
  "The repository ... is not signed").
  Fix: `Os.chmod("/tmp", 01777)` and `Os.chmod("/var/tmp", 01777)`.
* `/etc/resolv.conf` was the build machine's systemd-resolved stub
  (`nameserver 127.0.0.53` + an Azure internal search domain). On Android that
  stub does not exist, so DNS failed. `proot -R` only binds the host
  `/etc/resolv.conf` when it exists (it does not on Android), so the guest file is
  used.
  Fix: overwrite `rootfs/etc/resolv.conf` with public resolvers
  (`8.8.8.8`, `8.8.4.4`, `1.1.1.1`).

```kotlin
private fun fixEnvironment() {
    try {                                   // DNS must work for apt / extensions
        File(rootfs, "etc/resolv.conf").writeText(
            "nameserver 8.8.8.8\nnameserver 8.8.4.4\nnameserver 1.1.1.1\noptions edns0 trust-ad\n")
    } catch (e: Exception) { Log.e("VSCodeShell", "fixEnvironment: resolv.conf failed", e) }
    try {                                   // apt helpers run as _apt and need sticky /tmp
        Os.chmod(File(rootfs, "tmp").absolutePath, 0b111_111_111_111)      // 01777
        Os.chmod(File(rootfs, "var/tmp").absolutePath, 0b111_111_111_111)  // 01777
    } catch (e: Exception) { Log.e("VSCodeShell", "fixEnvironment: chmod failed", e) }
}
```

## Verification done here (x86_64 host, armhf rootfs under chroot + qemu-arm)

* `apt-get update` and `apt-get install -y htop` succeed after the `/tmp` + resolv.conf fix.
* `/opt/vscode-server/bin/code-server --host 127.0.0.1 --port 8124 ...` binds and
  serves HTTP 200; `loading.html` was loaded in a browser and redirected into the
  real workbench ("Welcome - Visual Studio Code").
* `socat - EXEC:'/bin/bash --norc -i',pty,echo=0,stderr,setsid` with stdin held open
  echoes typed input back (bash prompt + command output).

Not verified: on-device behaviour (no Android device/emulator here). The APK parses,
zips align and verifies with `apksigner`.

## 4. Launch crash in the first 0.2.2 build (fixed)

Root cause: invalid smali in the added `ShellManager.fixEnvironment()`. `getRootfs()`
was emitted as `invoke-direct {v1, p0}` — called on an uninitialized `java.io.File`
with the manager passed as an argument — and the `File` constructors were invoked on
already-constructed objects. ART rejects the method with a `VerifyError` the first time
`prepare()` calls it, so the app died on launch. It has been rewritten correctly
(receiver `p0` for `getRootfs`, a fresh `new-instance` for every `File` and
`FileOutputStream`) and the bytecode re-checked with `dexdump`.

While investigating, the dex version was also normalized: `classes3.dex` is now
assembled with `smali a --api 23`, so all four dex files are `dex\n035` like the
shipped APK (`smali a --api 34` would emit 039). Same package, versionCode and
signing key, so it updates over the broken build.

    for f in classes*.dex; do xxd -l 8 $f; done   # -> 6465 780a 3033 3500  (dex\n035)

Fixed sha256: `ef1e41bc9b83bc7d38af7f6c7bd215dfe8a73c92336ad8804fb18998d8959a78`

## 5. Crash on Terminal / Packages (0.2.2 follow-up)

Symptom: app opens, rootfs pack starts loading, tapping Terminal or Packages kills the app.

Three independent defects, all fixed in `classes3.dex`:

* **UI updates from background threads.** `MainActivity.setStatus()` called
  `TextView.setText()` directly. It is invoked from the extraction thread
  (`onExtractProgress` / `onExtractDone`), the server reader thread
  (`onServerOutput`) and the exec reader thread (`onExecExit`). After the first
  layout pass `setText` throws `CalledFromWrongThreadException`:
  - `onExtractProgress` aborted the rootfs extraction after the first frame
    (the marker is never written, so every launch re-extracts and the rootfs is
    always incomplete);
  - `onExecExit` threw on the reader thread *outside* any try/catch, so the
    process died ("Packages" crash);
  - `onServerOutput` killed the server reader thread.
  Fix: `setStatus` now posts a `MainActivity$StatusRunnable` to the TextView,
  so it is always executed on the UI thread.

* **`jsQuote()` built invalid JavaScript.** It did
  `JSONObject{"v":s}.toString().substring(2, len-2)`, which yields `v":"...`
  instead of a quoted literal. Every `window.onTermData(...)` /
  `window.onExecData(...)` call was a syntax error, so terminal and apt output
  never rendered. Fixed to `substring(5, len-1)` (the quoted JSON value).

* **proot could not load on Android.** The bundled `libproot.so` is Termux
  proot 5.1.107.102 (arm); its `DT_NEEDED` is `libtalloc.so.2` and
  `libandroid-shmem.so`, neither of which is in the APK or the Debian rootfs.
  Every `proot` exec (Terminal, Packages, Editor server) failed in the dynamic
  linker. Fix: bundle `lib/armeabi-v7a/libtalloc.so.2` and
  `libandroid-shmem.so` (extracted from the matching Termux packages) and set
  `LD_LIBRARY_PATH` to `ApplicationInfo.nativeLibraryDir` in `prootEnv()`.

New build: `VSCodeShell-0.2.2-armv7-fixed.apk`
SHA-256: `573be938129951151a274194cc0ecd93e731ab5d6500869f10dddbcc8c1121a2`

Signing: the original `vscode-debug.keystore` is not in the repo, so this build
is signed with a new key `vscode-debug-fixed.keystore` (store/key password
`android`, alias `androiddebugkey`, `CN=Android Debug,O=Android,C=US`).
Uninstall 0.2.2 before installing it. If the original keystore is still
available, `VSCodeShell-0.2.2-armv7-fixed-unsigned.apk` is zip-aligned and
unsigned, ready to be signed with the old key for an in-place update.

Verification done here:

* `classes3.dex` re-assembled with `smali a --api 23` (`dex\n035`), class list
  identical to the shipped dex plus `MainActivity$StatusRunnable`.
* `apksigner verify` and `zipalign -c -p 4` pass; `assets/rootfs.tar` is
  byte-identical (`a2b402ed…`).
* Not verified: on-device behaviour (no Android device here).

### 5.1 Launch crash in the first fixed build (fixed)

The first follow-up build (sha `454c17fa…`) installed but died before the
activity opened. Cause: `prootEnv()` called the **private** `getNativeLibDir()`
with `invoke-virtual`. Private methods must be invoked with `invoke-direct`;
ART rejects the class with a `VerifyError` at load, so the process never
started. The call is now `invoke-direct` and the class verifies.


