#!/usr/bin/env python3
"""telegram_phone.py - an allowlisted remote-control agent that runs on your phone.

For a phone YOU own, with consent. It logs into your Telegram account with
Telethon (like any other device session), watches one chat (default: Saved
Messages), and runs a small allowlist of Termux:API commands. Every command is
logged; only the allowed sender/chat is accepted; `shell` is off unless you
explicitly enable it.

Setup (Android):
  1. Install Termux and the Termux:API app (F-Droid); grant the API permissions.
  2. In Termux:  pkg install python && pip install telethon
  3. Get API ID/hash at https://my.telegram.org  ->  TG_API_ID / TG_API_HASH
  4. Run:  TG_API_ID=... TG_API_HASH=... python3 telegram_phone.py
     (first run asks for the phone number + one-time code)
  5. From the agent machine run telegram_send.py with the same account to send
     a command, or just message yourself in Saved Messages.

Safety:
  - Only commands on the allowlist run; anything else is refused.
  - Only messages from TG_ALLOWED_USERS in TG_ALLOWED_CHAT are accepted.
  - `shell` is disabled unless TG_ALLOW_SHELL=1. It runs arbitrary commands.
  - Keep API hashes, sessions and logs private; they are full account access.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

log = None  # set in main


def _setup_log(path):
    global log
    import logging
    log = logging.getLogger("telegram_phone")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.FileHandler(path)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(fh)
    return log


class RateLimiter:
    """Simple fixed-window limiter: max `limit` hits per `window` seconds."""

    def __init__(self, limit=20, window=60.0):
        self.limit = limit
        self.window = window
        self.hits: list[float] = []

    def allow(self, now=None) -> bool:
        now = time.time() if now is None else now
        self.hits = [t for t in self.hits if now - t < self.window]
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True


class Executor:
    """Turn a command string into an allowlisted Termux:API call."""

    def __init__(self, allow_shell: bool = False, timeout: int = 30):
        self.allow_shell = allow_shell
        self.timeout = timeout

    # -- handlers ---------------------------------------------------------- #

    def _handlers(self):
        return {
            "ping": self._ping,
            "battery": self._battery,
            "location": self._location,
            "clipboard": self._clipboard,
            "toast": self._toast,
            "notify": self._notify,
            "photo": self._photo,
            "sms": self._sms,
            "contacts": self._contacts,
            "info": self._info,
            "shell": self._shell,
        }

    def execute(self, text: str) -> str:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            return f"error: bad quoting: {exc}"
        if not parts:
            return ""
        cmd = parts[0].lower()
        args = parts[1:]
        fn = self._handlers().get(cmd)
        if fn is None:
            return f"unknown command {cmd!r}. Try 'help'."
        if cmd == "shell" and not self.allow_shell:
            return "shell is disabled (set TG_ALLOW_SHELL=1 to enable)"
        try:
            return fn(args)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"

    def help(self) -> str:
        return ("commands: ping, battery, location, clipboard, toast, notify, "
                "photo, sms, contacts, info, shell (disabled unless enabled)")

    # -- individual commands ---------------------------------------------- #

    def _ping(self, args):
        return "pong"

    def _battery(self, args):
        return self._run(["termux-battery-status"])

    def _location(self, args):
        return self._run(["termux-location"])

    def _clipboard(self, args):
        return self._run(["termux-clipboard-get"])

    def _toast(self, args):
        if not args:
            return "usage: toast <text>"
        return self._run(["termux-toast", " ".join(args)])

    def _notify(self, args):
        if not args:
            return "usage: notify <text>  |  notify <title> | <content>"
        if "|" in args:
            i = args.index("|")
            title = " ".join(args[:i]) or "agent"
            content = " ".join(args[i + 1:])
        elif len(args) == 1:
            title, content = "agent", args[0]
        else:
            title, content = args[0], " ".join(args[1:])
        return self._run(["termux-notification", "--title", title,
                          "--content", content])

    def _photo(self, args):
        path = args[0] if args else "/sdcard/DCIM/agent_photo.jpg"
        return self._run(["termux-camera-photo", path])

    def _sms(self, args):
        if len(args) < 2:
            return "usage: sms <number> <text>"
        to, text = args[0], " ".join(args[1:])
        return self._run(["termux-sms-send", to, text])

    def _contacts(self, args):
        return self._run(["termux-contact-list"])

    def _info(self, args):
        return self._run(["termux-telephony-deviceinfo"])

    def _shell(self, args):
        if not args:
            return "usage: shell <command>"
        return self._run(["sh", "-c", " ".join(args)])

    # -- runner ------------------------------------------------------------ #

    def _run(self, argv, timeout=None) -> str:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout or self.timeout)
        except FileNotFoundError:
            return f"error: {argv[0]!r} not found (is Termux:API installed?)"
        except subprocess.TimeoutExpired:
            return f"error: {argv[0]!r} timed out"
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not out:
            return f"error: {err or f'exit {proc.returncode}'}"
        return out or err or f"ok (exit {proc.returncode})"


# --------------------------------------------------------------------------- #
# authorization
# --------------------------------------------------------------------------- #

def is_allowed(sender_id, chat_id, allowed_users, allowed_chat) -> bool:
    """Pure check so it can be unit-tested without Telegram."""
    users_ok = (allowed_users is None or sender_id in allowed_users
                or sender_id in ("me",))
    chat_ok = (allowed_chat is None or chat_id == allowed_chat
               or chat_id in ("me",))
    return bool(users_ok and chat_ok)


# --------------------------------------------------------------------------- #
# Telegram listener
# --------------------------------------------------------------------------- #

def run_listener(args) -> int:
    try:
        from telethon import TelegramClient, events
    except ImportError:
        print("error: telethon is required (pip install telethon)", file=sys.stderr)
        return 2

    executor = Executor(allow_shell=args.allow_shell)
    limiter = RateLimiter(limit=args.rate_limit, window=60.0)
    allowed_users = None if args.allow_user in (None, "", "me") else {
        int(u) for u in args.allow_user.split(",") if u.strip()}

    client = TelegramClient(args.session, args.api_id, args.api_hash)

    @client.on(events.NewMessage(chats=args.chat, from_users=allowed_users))
    async def handler(event):
        sender = event.sender_id
        chat = event.chat_id
        if not is_allowed(sender, chat, allowed_users, args.chat):
            return
        if not limiter.allow():
            await event.respond("rate limited; try again later")
            return
        text = (event.raw_text or "").strip()
        log.info("cmd from=%s chat=%s text=%r", sender, chat, text)
        result = executor.execute(text)
        log.info("reply=%r", result)
        await event.respond(result[:4000])

    print(f"listening on chat={args.chat!r}; allowed users={allowed_users or 'me'}; "
          f"shell={'on' if args.allow_shell else 'off'}")
    client.start(phone=args.phone)
    client.run_until_disconnected()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="telegram_phone",
        description="Allowlisted Telegram remote control for your own phone.")
    ap.add_argument("--api-id", type=int, default=int(os.environ.get("TG_API_ID", 0)))
    ap.add_argument("--api-hash", default=os.environ.get("TG_API_HASH", ""))
    ap.add_argument("--phone", default=os.environ.get("TG_PHONE", ""),
                    help="account phone number, used only at first login")
    ap.add_argument("--session", default=os.environ.get("TG_SESSION", "phone.session"))
    ap.add_argument("--chat", default=os.environ.get("TG_CHAT", "me"),
                    help="chat to watch (default: me = Saved Messages)")
    ap.add_argument("--allow-user", default=os.environ.get("TG_ALLOWED_USERS", "me"),
                    help="comma-separated Telegram user ids, or 'me'")
    ap.add_argument("--allow-shell", action="store_true",
                    default=os.environ.get("TG_ALLOW_SHELL", "") in ("1", "true", "yes"))
    ap.add_argument("--rate-limit", type=int, default=20,
                    help="max commands per minute (default 20)")
    ap.add_argument("--log", default=os.environ.get("TG_LOG", "phone_agent.log"))
    ap.add_argument("--selftest", action="store_true",
                    help="run the executor/allowlist checks and exit")
    return ap


def _selftest() -> int:
    ex = Executor(allow_shell=True)
    checks = [
        ("ping", "pong"),
        ("battery", None),
        ("toast hello", None),
        ("notify hi there", None),
        ("sms +15551234567 hello", None),
        ("shell echo hi", "hi"),
    ]
    ok = True
    for text, expect in checks:
        out = ex.execute(text)
        if expect is not None and expect not in out:
            ok = False
            print(f"FAIL {text!r} -> {out!r}")
        else:
            print(f"ok   {text!r} -> {out!r}")
    # shell gated off by default
    if Executor(allow_shell=False).execute("shell echo hi").startswith("shell is disabled"):
        print("ok   shell disabled by default")
    else:
        ok = False
        print("FAIL shell should be disabled by default")
    # unknown command
    if "unknown command" in ex.execute("rm -rf /"):
        print("ok   unknown command refused")
    else:
        ok = False
        print("FAIL unknown command not refused")
    # authorization
    if is_allowed("me", "me", None, "me") and not is_allowed(123, "me", {999}, "me"):
        print("ok   allowlist")
    else:
        ok = False
        print("FAIL allowlist")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_log(args.log)
    if args.selftest:
        return _selftest()
    if not args.api_id or not args.api_hash:
        print("error: set TG_API_ID and TG_API_HASH (https://my.telegram.org)",
              file=sys.stderr)
        return 2
    return run_listener(args)


if __name__ == "__main__":
    sys.exit(main())
