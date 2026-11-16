#!/usr/bin/env python3
"""
apkbuild.py - scrape a web folder/site (or a GitHub repo) and build an Android APK.

Usage:
    python3 apkbuild.py URL [options]

Examples:
    python3 apkbuild.py https://example.com/ --out myapp.apk
    python3 apkbuild.py https://github.com/user/repo --out repo.apk
    python3 apkbuild.py https://example.com/app/ --name "My App" --package com.me.myapp

Only the URL is required. The pipeline is fully automatic:

    mirror  ->  generate Android project  ->  aapt2  ->  javac
            ->  d8 (dex)  ->  zip  ->  zipalign  ->  apksigner  ->  APK

What gets mirrored: HTML/CSS/JS, source files, icons, images, fonts, video and
audio that the start page (and the pages it links to, same origin) reference.
A generated index page is added if the folder has no index.html, so the result
is always navigable offline.

Requirements (auto-detected, no arguments needed):
    * Android SDK with platforms/android-<api>/android.jar and
      build-tools/<ver>/{aapt2,d8,zipalign,apksigner}
      (ANDROID_HOME / ANDROID_SDK_ROOT, or /usr/lib/android-sdk, or ./android-sdk)
    * JDK: javac, jar, keytool  (Java 11+)

Note: only scrape/build content you have the rights to use.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

VERSION = "1.0"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

# Extensions we mirror. Text/source is kept as-is; binaries are copied verbatim.
TEXT_EXT = {
    ".html", ".htm", ".xhtml", ".css", ".js", ".mjs", ".cjs", ".json",
    ".xml", ".txt", ".md", ".markdown", ".svg", ".webmanifest", ".map",
    ".py", ".java", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".rb", ".php", ".pl", ".sh", ".bat", ".ps1", ".sql", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".gitignore",
    ".gradle", ".properties", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".asp", ".aspx", ".jsp", ".vue", ".scss", ".sass", ".less", ".styl",
}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".avif", ".tiff", ".tif"}
MEDIA_EXT = {".mp4", ".webm", ".ogv", ".ogg", ".mp3", ".wav", ".m4a", ".m4v", ".mov", ".mkv", ".avi", ".flac", ".aac"}
FONT_EXT = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
SKIP_EXT = {".exe", ".dll", ".so", ".dmg", ".iso", ".bin", ".deb", ".rpm", ".msi", ".apk", ".aab"}

MAX_FILE_BYTES = 8 * 1024 * 1024


def log(msg: str) -> None:
    print("[apkbuild] " + msg, flush=True)


def die(msg: str) -> "None":
    print("[apkbuild] ERROR: " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, cwd=None, capture=True, check=True):
    """Run a command; return stdout text. Raises SystemExit on failure."""
    log("$ " + " ".join(str(c) for c in cmd))
    try:
        p = subprocess.run(
            [str(c) for c in cmd], cwd=cwd, check=check,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
        )
    except FileNotFoundError:
        die("command not found: %s" % cmd[0])
    except subprocess.CalledProcessError as e:
        out = (e.stdout or "").strip()
        if out:
            print(out, file=sys.stderr)
        die("command failed (%d): %s" % (e.returncode, " ".join(str(c) for c in cmd)))
    return (p.stdout or "") if capture else ""


# --------------------------------------------------------------------------
# PNG icon generation (no PIL needed)
# --------------------------------------------------------------------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def make_png(size: int, rgb: tuple) -> bytes:
    """Solid-colour PNG with a lighter rounded-ish border, generated in pure Python."""
    r, g, b = rgb
    rows = []
    for y in range(size):
        row = bytearray()
        row.append(0)  # filter type
        for x in range(size):
            # simple border shading
            edge = min(x, y, size - 1 - x, size - 1 - y)
            if edge < max(1, size // 12):
                row += bytes((min(255, r + 40), min(255, g + 40), min(255, b + 40), 255))
            else:
                row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------
def normalize(url: str, base: str | None = None) -> str | None:
    if base:
        url = urllib.parse.urljoin(base, url)
    url = url.strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("data:") or url.startswith("javascript:") or url.startswith("mailto:") \
            or url.startswith("tel:") or url.startswith("about:") or url.startswith("#"):
        return None
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    # drop the fragment, keep the query (some sites need it)
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def ext_of(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def safe_rel_path(path: str) -> str:
    path = path.lstrip("/")
    path = posixpath.normpath(path)
    if path in (".", "..") or path.startswith("../"):
        path = "index.html"
    return path


def decode_text(data: bytes) -> str:
    m = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', data[:4096], re.I)
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return data.decode(enc, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# HTML/CSS reference extraction
# --------------------------------------------------------------------------
URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")


try:
    from html.parser import HTMLParser

    class RefCollector(HTMLParser):  # type: ignore[misc]
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.refs: list[str] = []
            self.base: str | None = None
            self.title= ""
            self._in_title = False

        def handle_starttag(self, tag, attrs):
            d = {k: (v or "") for k, v in attrs}
            if tag == "base" and d.get("href"):
                self.base = d["href"]
            for k in ("href", "src", "poster", "data-src", "data-href", "action", "formaction"):
                if d.get(k):
                    self.refs.append(d[k])
            for k in ("srcset", "imagesrcset"):
                if d.get(k):
                    for part in d[k].split(","):
                        u = part.strip().split(" ")[0]
                        if u:
                            self.refs.append(u)
            if d.get("style"):
                self.refs.extend(m.group(2) for m in URL_RE.finditer(d["style"]))
            if tag == "title":
                self._in_title = True

        def handle_endtag(self, tag):
            if tag == "title":
                self._in_title = False

        def handle_data(self, data):
            if self._in_title:
                self.title += data
            self.refs.extend(m.group(2) for m in URL_RE.finditer(data))
except Exception:  # pragma: no cover
    class RefCollector:  # type: ignore[no-redef]
        def __init__(self):
            self.refs = []
            self.base = None
            self.title = ""

        def feed(self, text):
            self.refs.extend(m.group(2) for m in URL_RE.finditer(text))
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            if m:
                self.title = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def css_refs(text: str) -> list[str]:
    out = []
    for m in URL_RE.finditer(text):
        out.append(m.group(2))
    for m in re.finditer(r'@import\s+(?:url\()?\s*["\']?([^"\')]+)', text, re.I):
        out.append(m.group(1))
    return out


# --------------------------------------------------------------------------
# Scraper
# --------------------------------------------------------------------------
class Scraper:
    def __init__(self, start_url, max_pages, max_mb, timeout, respect_robots, insecure):
        self.start_url = start_url
        self.max_pages = max_pages
        self.max_bytes = max_mb * 1024 * 1024
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.insecure = insecure
        self.total = 0
        self.fetched = 0
        self.local_map: dict[str, str] = {}
        self.refs_by_file: dict[str, list[str]] = {}
        self.title = ""
        self.icon_candidates: list[tuple[int, str]] = []
        self._robots_disallow: list[str] = []

    # -- fetching ---------------------------------------------------------
    def _fetch(self, url: str):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        ctx = None
        if self.insecure:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read()
        return ctype, data

    def _load_robots(self):
        p = urllib.parse.urlsplit(self.start_url)
        robots = "%s://%s/robots.txt" % (p.scheme, p.netloc)
        try:
            _, data = self._fetch(robots)
            text = decode_text(data)
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line.lower().startswith("disallow:"):
                    path = line.split(":", 1)[1].strip()
                    if path:
                        self._robots_disallow.append(path)
            log("robots.txt: %d disallow rule(s)" % len(self._robots_disallow))
        except Exception:
            log("robots.txt not available (continuing)")

    def _robots_ok(self, path: str) -> bool:
        for d in self._robots_disallow:
            if path.startswith(d):
                return False
        return True

    # -- saving -----------------------------------------------------------
    def _save(self, url: str, data: bytes, ctype: str) -> str:
        p = urllib.parse.urlsplit(url)
        raw = p.path or "/"
        is_html = "html" in ctype.lower()
        if raw.endswith("/"):
            d = safe_rel_path(raw.rstrip("/"))
            path = "index.html" if d in (".", "index.html") else d + "/index.html"
        else:
            path = safe_rel_path(raw)
            if is_html and ext_of(path) == "":
                path = path + ".html"
        dest = os.path.join(self.mirror, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        rel = path.replace(os.sep, "/")
        self.local_map[url] = rel
        if ext_of(path) in (".png", ".jpg", ".jpeg", ".webp"):
            name = posixpath.basename(path).lower()
            score = 0
            for key in ("apple-touch", "icon", "logo", "favicon", "launcher"):
                if key in name:
                    score += 1
            self.icon_candidates.append((score, rel))
        return rel

    # -- rewriting --------------------------------------------------------
    def _rewrite(self):
        for url, rel in self.local_map.items():
            refs = self.refs_by_file.get(rel)
            if not refs:
                continue
            full = os.path.join(self.mirror, rel)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            base_dir = posixpath.dirname(rel)
            for ref in refs:
                nref = normalize(ref, url)
                if not nref:
                    continue
                target = self.local_map.get(nref)
                if not target:
                    continue
                relpath = posixpath.relpath(target, base_dir or ".")
                if ref.startswith("data:") or ref.startswith("javascript:"):
                    continue
                text = text.replace(ref, relpath)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)

    # -- main -------------------------------------------------------------
    def scrape(self, mirror: str) -> dict:
        self.mirror = mirror
        os.makedirs(mirror, exist_ok=True)
        if self.respect_robots:
            self._load_robots()

        # Directory prefix: keep the crawl inside the folder that was given.
        p = urllib.parse.urlsplit(self.start_url)
        prefix = p.path or "/"
        last = posixpath.basename(prefix.rstrip("/"))
        if "." in last:                       # looks like a file -> use its folder
            prefix = posixpath.dirname(prefix.rstrip("/")) + "/"
        self.prefix = prefix

        queue: list[str] = [self.start_url]
        visited: set[str] = set()
        while queue and self.fetched < self.max_pages and self.total < self.max_bytes:
            url = queue.pop(0)
            norm = normalize(url)
            if not norm or norm in visited:
                continue
            if not same_origin(norm, self.start_url):
                continue
            path = urllib.parse.urlsplit(norm).path or "/"
            if not path.startswith(self.prefix):
                continue
            if self.respect_robots and not self._robots_ok(path):
                log("robots: skip %s" % path)
                continue
            visited.add(norm)
            try:
                ctype, data = self._fetch(norm)
            except Exception as e:
                log("skip %s (%s)" % (path, e))
                continue
            if len(data) > MAX_FILE_BYTES:
                log("skip large %s (%.1f MB)" % (path, len(data) / 1e6))
                continue
            self.total += len(data)
            self.fetched += 1
            rel = self._save(norm, data, ctype)
            ext = ext_of(rel)
            if "html" in ctype.lower() or ext in (".html", ".htm", ".xhtml"):
                text = decode_text(data)
                rc = RefCollector()
                try:
                    rc.feed(text)
                except Exception:
                    pass
                if rc.title.strip() and not self.title:
                    self.title = html_mod.unescape(rc.title).strip()
                self.refs_by_file[rel] = list(rc.refs)
                for ref in rc.refs:
                    nref = normalize(ref, rc.base or norm)
                    if nref and nref not in visited:
                        queue.append(nref)
            elif ext == ".css":
                text = decode_text(data)
                refs = css_refs(text)
                self.refs_by_file[rel] = refs
                for ref in refs:
                    nref = normalize(ref, norm)
                    if nref and nref not in visited:
                        queue.append(nref)
            if self.fetched % 25 == 0:
                log("mirrored %d files (%.1f MB)" % (self.fetched, self.total / 1e6))

        self._rewrite()
        log("mirrored %d files, %.1f MB" % (self.fetched, self.total / 1e6))
        return {
            "files": self.fetched,
            "bytes": self.total,
            "title": self.title,
            "icon": (sorted(self.icon_candidates, reverse=True)[0][1] if self.icon_candidates else None),
            "map": self.local_map,
            "start_url": self.start_url,
        }


# --------------------------------------------------------------------------
# GitHub repo mode
# --------------------------------------------------------------------------
def github_info(url: str):
    p = urllib.parse.urlsplit(url)
    if p.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    branch, subpath = None, ""
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        branch = parts[3]
        subpath = "/".join(parts[4:])
    return {"owner": owner, "repo": repo, "branch": branch, "subpath": subpath}


def fetch_github(gh: dict, mirror: str, timeout: int) -> dict:
    branches = [gh["branch"]] if gh["branch"] else ["main", "master"]
    last_err = None
    tar_path = os.path.join(mirror, ".repo.tar.gz")
    for br in branches:
        url = "https://codeload.github.com/%s/%s/tar.gz/refs/heads/%s" % (gh["owner"], gh["repo"], br)
        log("github: %s" % url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            with open(tar_path, "wb") as f:
                f.write(data)
            break
        except Exception as e:
            last_err = e
            log("branch %s failed (%s)" % (br, e))
    else:
        die("could not download github repo: %s" % last_err)

    extract_root = os.path.join(mirror, "_repo")
    os.makedirs(extract_root, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(extract_root, filter="data")
    os.remove(tar_path)
    tops = [d for d in os.listdir(extract_root) if os.path.isdir(os.path.join(extract_root, d))]
    if not tops:
        die("github tarball was empty")
    root = os.path.join(extract_root, tops[0])
    # move contents up
    for name in os.listdir(root):
        shutil.move(os.path.join(root, name), os.path.join(mirror, name))
    os.rmdir(root)
    os.rmdir(extract_root)

    # start page
    start = ""
    if gh["subpath"]:
        cand = os.path.join(mirror, gh["subpath"])
        if os.path.isfile(cand):
            start = safe_rel_path(gh["subpath"])
    if not start:
        for cand in ("index.html", "index.htm", "README.md"):
            if os.path.isfile(os.path.join(mirror, cand)):
                start = cand
                break
    title = "%s/%s" % (gh["owner"], gh["repo"])
    readme = os.path.join(mirror, "README.md")
    if os.path.isfile(readme):
        try:
            with open(readme, "r", encoding="utf-8", errors="replace") as f:
                first = f.readline().strip().lstrip("# ").strip()
            if first:
                title = first
        except OSError:
            pass
    files = sum(len(fs) for _, _, fs in os.walk(mirror))
    log("github: %d files mirrored" % files)
    return {"files": files, "bytes": 0, "title": title, "icon": None,
            "map": {}, "start_url": url, "start": start}


# --------------------------------------------------------------------------
# Fallback index page
# --------------------------------------------------------------------------
def generate_index(mirror: str, title: str) -> str:
    rows = []
    for dirpath, _, filenames in os.walk(mirror):
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, mirror).replace(os.sep, "/")
            if rel.startswith("."):
                continue
            size = os.path.getsize(full)
            rows.append('      <li><a href="%s">%s</a> <span class="n">(%s)</span></li>'
                        % (rel, rel, human_size(size)))
    doc = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>%s</title>
<style>
 body{margin:0;background:#0c0e12;color:#e6e6e6;font:14px/1.5 -apple-system,Roboto,sans-serif}
 header{padding:16px;border-bottom:1px solid #2a2f37}h1{margin:0;font-size:16px}
 main{padding:16px}ul{list-style:none;padding:0}li{padding:4px 0;border-bottom:1px solid #1b1e24}
 a{color:#3c8dff;text-decoration:none}.n{color:#6e7681;font-size:11px}
</style>
</head>
<body>
<header><h1>%s &mdash; offline mirror</h1></header>
<main><ul>
%s
</ul></main>
</body>
</html>
""" % (html_mod.escape(title), html_mod.escape(title), "\n".join(rows))
    with open(os.path.join(mirror, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    return "index.html"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


# --------------------------------------------------------------------------
# Android project generation
# --------------------------------------------------------------------------
MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="__PACKAGE__"
    android:versionCode="__VERSION_CODE__"
    android:versionName="__VERSION_NAME__">
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <application
        android:label="@string/app_name"
        android:icon="@drawable/ic_launcher"
        android:usesCleartextTraffic="true"
        android:hardwareAccelerated="true"
        android:theme="@style/AppTheme">
        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:configChanges="keyboardHidden|orientation|screenLayout|screenSize|smallestScreenSize|uiMode"
            android:windowSoftInputMode="adjustResize">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""

ACTIVITY = """package __PACKAGE__;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public class MainActivity extends Activity {

    private WebView webView;
    private String startUrl;

    /** Named nested class: d8/R8 8.2 crashes on anonymous WebViewClient subclasses. */
    static class AppWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return false;
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            if (url != null && (url.startsWith("tel:") || url.startsWith("mailto:"))) {
                try {
                    view.getContext().startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception e) {
                }
                return true;
            }
            return false;
        }
    }

    static class AppWebChromeClient extends WebChromeClient {
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        startUrl = getIntent().getDataString();
        if (startUrl == null) {
            startUrl = "__START_URL__";
        }
        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        webView.setWebViewClient(new AppWebViewClient());
        webView.setWebChromeClient(new AppWebChromeClient());
        setContentView(webView);
        if (startUrl.startsWith("http") && !startUrl.startsWith("file")) {
            webView.loadUrl(startUrl);
        } else {
            webView.loadUrl("file:///android_asset/www/" + startUrl);
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView != null && webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (webView != null) {
            webView.onPause();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }
}
"""

STRINGS = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">__APP_NAME__</string>
</resources>
"""

STYLES = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="AppTheme" parent="@android:style/Theme.DeviceDefault.NoActionBar">
        <item name="android:windowBackground">@android:color/white</item>
    </style>
</resources>
"""


def derive_package(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    host = re.sub(r"^www\.", "", host)
    parts = [re.sub(r"[^a-z0-9]", "", seg) for seg in host.split(".")]
    parts = [p for p in parts if p]
    if not parts:
        return "com.apkbuild.app"
    if len(parts[-1]) in ("com", "org", "net", "io", "dev", "app"):
        # example.com -> com.example
        segs = list(reversed(parts))
    else:
        segs = list(reversed(parts))
    segs = [s for s in segs if s and not s[0].isdigit()]
    if not segs:
        return "com.apkbuild.app"
    pkg = ".".join(segs[:3])
    if len(pkg.split(".")) < 2:
        pkg += ".app"
    return pkg


def write_project(work: str, args, info: dict, mirror: str) -> str:
    pkg = args.package or derive_package(args.url)
    app_name = args.name or info.get("title") or urllib.parse.urlsplit(args.url).netloc or "App"
    app_name = re.sub(r"\s+", " ", app_name).strip()[:60] or "App"

    pkg_path = pkg.replace(".", os.sep)
    src_dir = os.path.join(work, "src", pkg_path)
    res_dir = os.path.join(work, "res")
    gen_dir = os.path.join(work, "gen")
    assets_dir = os.path.join(work, "assets", "www")
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(os.path.join(res_dir, "values"), exist_ok=True)
    os.makedirs(os.path.join(res_dir, "drawable"), exist_ok=True)
    os.makedirs(gen_dir, exist_ok=True)

    # assets = mirror
    if os.path.isdir(assets_dir):
        shutil.rmtree(assets_dir)
    shutil.copytree(mirror, assets_dir)

    online = bool(info.get("online"))
    if online:
        start = ""
        start_url = info.get("start_url") or args.url
    else:
        start = info.get("start") or ""
        if not start:
            for cand in ("index.html", "index.htm"):
                if os.path.isfile(os.path.join(mirror, cand)):
                    start = cand
                    break
        if not start:
            start = generate_index(mirror, app_name)
        start = start.lstrip("/")
        # make sure the asset copy has it
        if not os.path.isfile(os.path.join(assets_dir, start)):
            start = generate_index(assets_dir, app_name)
        start_url = "file:///android_asset/www/" + start

    # icon
    icon_src = info.get("icon")
    icon_dst = os.path.join(res_dir, "drawable", "ic_launcher.png")
    used_default = False
    if icon_src:
        src = os.path.join(mirror, icon_src)
        if os.path.isfile(src) and ext_of(src) in (".png", ".jpg", ".jpeg", ".webp"):
            shutil.copyfile(src, icon_dst)
    if not os.path.isfile(icon_dst):
        h = int(hashlib.md5((pkg + app_name).encode()).hexdigest()[:6], 16)
        rgb = (30 + h % 180, 60 + (h >> 8) % 160, 120 + (h >> 16) % 120)
        with open(icon_dst, "wb") as f:
            f.write(make_png(192, rgb))
        used_default = True

    def render(t: str) -> str:
        return (t.replace("__PACKAGE__", pkg)
                 .replace("__APP_NAME__", html_mod.escape(app_name))
                 .replace("__VERSION_CODE__", str(args.version_code))
                 .replace("__VERSION_NAME__", args.version_name)
                 .replace("__START_URL__", start_url))

    with open(os.path.join(work, "AndroidManifest.xml"), "w", encoding="utf-8") as f:
        f.write(render(MANIFEST))
    with open(os.path.join(src_dir, "MainActivity.java"), "w", encoding="utf-8") as f:
        f.write(render(ACTIVITY))
    with open(os.path.join(res_dir, "values", "strings.xml"), "w", encoding="utf-8") as f:
        f.write(render(STRINGS))
    with open(os.path.join(res_dir, "values", "styles.xml"), "w", encoding="utf-8") as f:
        f.write(STYLES)

    log("project: package=%s name=%r start=%s icon=%s"
        % (pkg, app_name, start, "generated" if used_default else str(icon_src)))
    return pkg


# --------------------------------------------------------------------------
# SDK discovery
# --------------------------------------------------------------------------
def find_tools(args):
    cands = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            cands.append(os.environ[env])
    if args.sdk_root:
        cands.insert(0, args.sdk_root)
    cands += [os.path.join(os.path.expanduser("~"), "Android", "Sdk"),
              "/usr/lib/android-sdk", "/opt/android-sdk",
              os.path.join(os.getcwd(), "android-sdk")]
    sdk = None
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "platforms")) and os.path.isdir(os.path.join(c, "build-tools")):
            sdk = os.path.abspath(c)
            break
    if not sdk:
        die("Android SDK not found. Set ANDROID_HOME or --sdk-root (need platforms/ and build-tools/).")

    platforms = sorted(os.listdir(os.path.join(sdk, "platforms")))
    if not platforms:
        die("no platforms/ in %s" % sdk)
    api = args.api
    chosen = None
    for name in platforms:
        m = re.search(r"(\d+)$", name)
        if m and (api is None or int(m.group(1)) == api):
            chosen = name
    if chosen is None:
        chosen = platforms[-1]
    android_jar = os.path.join(sdk, "platforms", chosen, "android.jar")
    if not os.path.isfile(android_jar):
        die("missing %s" % android_jar)

    bts = [d for d in os.listdir(os.path.join(sdk, "build-tools"))
           if re.match(r"^\d+(\.\d+)*$", d)]
    if not bts:
        die("no build-tools/ in %s" % sdk)
    bts.sort(key=lambda s: tuple(int(x) for x in s.split(".")))
    bt = bts[-1]
    bt_dir = os.path.join(sdk, "build-tools", bt)

    def tool(name):
        p = os.path.join(bt_dir, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
        w = shutil.which(name)
        return w or p

    tools = {
        "sdk": sdk,
        "android_jar": android_jar,
        "aapt2": tool("aapt2"),
        "d8": tool("d8"),
        "zipalign": tool("zipalign"),
        "apksigner": tool("apksigner"),
        "javac": shutil.which("javac"),
        "jar": shutil.which("jar"),
        "keytool": shutil.which("keytool"),
    }
    missing = [k for k, v in tools.items() if v and not os.path.exists(str(v))]
    if missing:
        die("missing build tools: %s" % ", ".join(missing))
    for k in ("javac", "jar", "keytool"):
        if not tools[k]:
            die("missing JDK tool: %s" % k)
    log("sdk=%s platform=%s build-tools=%s" % (sdk, chosen, bt))
    return tools


# --------------------------------------------------------------------------
# APK build
# --------------------------------------------------------------------------
def build_apk(work, tools, args, pkg, out_path):
    aapt2, d8, zipalign, apksigner = tools["aapt2"], tools["d8"], tools["zipalign"], tools["apksigner"]
    android_jar = tools["android_jar"]

    res_zip = os.path.join(work, "res.zip")
    run([aapt2, "compile", "--dir", os.path.join(work, "res"), "-o", res_zip])

    base_apk = os.path.join(work, "base.apk")
    cmd = [aapt2, "link", "-o", base_apk, "-I", android_jar,
           "--manifest", os.path.join(work, "AndroidManifest.xml"),
           "-R", res_zip, "--java", os.path.join(work, "gen"), "--auto-add-overlay",
           "--min-sdk-version", "23", "--target-sdk-version", str(args.target_sdk),
           "--version-code", str(args.version_code), "--version-name", args.version_name]
    if args.aapt2_extra:
        import shlex
        cmd += shlex.split(args.aapt2_extra)
    run(cmd)

    obj = os.path.join(work, "obj")
    os.makedirs(obj, exist_ok=True)
    sources = []
    for root in (os.path.join(work, "gen"), os.path.join(work, "src")):
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if fn.endswith(".java"):
                    sources.append(os.path.join(dirpath, fn))
    javac_base = [tools["javac"], "-encoding", "UTF-8", "-classpath", android_jar, "-d", obj]
    javac_attempts = (["--release", "8"], ["-source", "8", "-target", "8"], [])
    compiled = False
    for extra in javac_attempts:
        shutil.rmtree(obj, ignore_errors=True)
        os.makedirs(obj, exist_ok=True)
        try:
            run(javac_base + extra + sources)
            compiled = True
            break
        except SystemExit:
            log("javac %s failed, trying another target setting" % (" ".join(extra) or "(default)"))
    if not compiled:
        die("javac could not compile the generated sources")

    jar_path = os.path.join(work, "classes.jar")
    run([tools["jar"], "--create", "--file", jar_path, "-C", obj, "."])

    dex_dir = os.path.join(work, "dex")
    os.makedirs(dex_dir, exist_ok=True)
    run([d8, "--lib", android_jar, "--min-api", "23", "--release",
         "--output", dex_dir, jar_path])

    app_apk = os.path.join(work, "app-unsigned.apk")
    shutil.copyfile(base_apk, app_apk)
    assets_root = os.path.join(work, "assets")
    with zipfile.ZipFile(app_apk, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(dex_dir, "classes.dex"), "classes.dex")
        for dirpath, _, filenames in os.walk(assets_root):
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                zf.write(full, os.path.relpath(full, work).replace(os.sep, "/"))

    aligned = os.path.join(work, "app-aligned.apk")
    run([zipalign, "-f", "4", app_apk, aligned])

    ks = args.keystore
    if not ks:
        ks = os.path.join(os.path.expanduser("~"), ".apkbuild", "debug.keystore")
    if not os.path.isfile(ks):
        os.makedirs(os.path.dirname(ks), exist_ok=True)
        run([tools["keytool"], "-genkeypair", "-v", "-keystore", ks,
             "-storepass", args.ks_pass, "-alias", args.ks_alias,
             "-keypass", args.ks_pass, "-keyalg", "RSA", "-keysize", "2048",
             "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"])
    final = out_path
    os.makedirs(os.path.dirname(os.path.abspath(final)), exist_ok=True)
    run([apksigner, "sign", "--ks", ks, "--ks-pass", "pass:" + args.ks_pass,
         "--key-pass", "pass:" + args.ks_pass, "--out", final, aligned])
    log("signed: %s" % final)
    return final


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="apkbuild.py",
        description="Scrape a URL/folder and build an Android APK (aapt2 + javac + d8 + zipalign + apksigner).",
        epilog="Only URL is required:  python3 apkbuild.py https://example.com/ --out app.apk",
    )
    ap.add_argument("url", help="start URL (a page, a folder, or a github.com repo/folder)")
    ap.add_argument("--out", default="app.apk", help="output APK path (default: app.apk)")
    ap.add_argument("--name", default=None, help="app label (default: page <title> or host)")
    ap.add_argument("--package", default=None, help="Android package (default: derived from the host)")
    ap.add_argument("--version-code", type=int, default=1)
    ap.add_argument("--version-name", default="1.0")
    ap.add_argument("--mode", choices=["auto", "offline", "online"], default="auto",
                    help="auto: offline if anything was mirrored, else online")
    ap.add_argument("--max-pages", type=int, default=300)
    ap.add_argument("--max-mb", type=int, default=200)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--api", type=int, default=None, help="compile against this android.jar API level")
    ap.add_argument("--target-sdk", type=int, default=34)
    ap.add_argument("--sdk-root", default=None)
    ap.add_argument("--keystore", default=None)
    ap.add_argument("--ks-pass", default="android")
    ap.add_argument("--ks-alias", default="androiddebugkey")
    ap.add_argument("--respect-robots", action="store_true")
    ap.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    ap.add_argument("--no-build", action="store_true", help="mirror + generate project only")
    ap.add_argument("--keep-work", action="store_true", help="keep the temporary project directory")
    ap.add_argument("--aapt2-extra", default=None, help="extra args for aapt2 link (quoted)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    url = normalize(args.url)
    if not url:
        die("invalid URL: %r" % args.url)

    out_abs = os.path.abspath(args.out)
    work = tempfile.mkdtemp(prefix="apkbuild-")
    mirror = os.path.join(work, "mirror")
    os.makedirs(mirror, exist_ok=True)

    gh = github_info(url)
    t0 = time.time()
    if gh:
        info = fetch_github(gh, mirror, args.timeout)
    else:
        log("scraping %s (prefix: folder of the URL, same origin)" % url)
        sc = Scraper(url, args.max_pages, args.max_mb, args.timeout, args.respect_robots, args.insecure)
        info = sc.scrape(mirror)

    # decide offline vs online
    if args.mode == "online":
        info["start"] = ""
    elif args.mode == "auto":
        if not info.get("start") and info.get("files", 0) == 0:
            info["start"] = ""
    online = args.mode == "online" or (not info.get("start") and info.get("files", 0) == 0)
    if online:
        # wrap the live site instead
        info["start"] = ""
        info["start_url"] = url
        info["online"] = True
        log("mode: online (load %s in the WebView)" % url)
    else:
        log("mode: offline (%s)" % info.get("start"))

    pkg = write_project(work, args, info, mirror)
    log("project generated in %s" % work)

    if args.no_build:
        log("(--no-build) done; project kept at %s" % work)
        return 0

    tools = find_tools(args)
    apk = build_apk(work, tools, args, pkg, out_abs)

    # verify
    try:
        run([tools["apksigner"], "verify", apk], check=True)
        log("apksigner verify: OK")
    except SystemExit:
        log("WARNING: apksigner verification failed")

    size = os.path.getsize(apk)
    log("done: %s (%.1f MB) in %.1fs" % (apk, size / 1e6, time.time() - t0))
    if args.keep_work:
        log("work dir kept: %s" % work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
