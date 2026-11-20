#!/usr/bin/env python3
"""Tests for telegram_phone.py - no Telegram, no phone: the executor and
allowlist are exercised with a stubbed _run()."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_phone import Executor, RateLimiter, is_allowed


def captured(allow_shell=True):
    ex = Executor(allow_shell=allow_shell)
    calls = []

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        return "OUT"

    ex._run = fake_run
    return ex, calls


class TestExecutor(unittest.TestCase):
    def test_ping(self):
        self.assertEqual(Executor().execute("ping"), "pong")

    def test_battery(self):
        ex, calls = captured()
        self.assertEqual(ex.execute("battery"), "OUT")
        self.assertEqual(calls, [["termux-battery-status"]])

    def test_shell_enabled(self):
        ex, calls = captured(allow_shell=True)
        self.assertEqual(ex.execute("shell echo hi"), "OUT")
        self.assertEqual(calls, [["sh", "-c", "echo hi"]])

    def test_shell_disabled_by_default(self):
        ex, _ = captured(allow_shell=False)
        self.assertTrue(ex.execute("shell echo hi").startswith("shell is disabled"))

    def test_sms(self):
        ex, calls = captured()
        ex.execute("sms +15551234567 hello there")
        self.assertEqual(calls, [["termux-sms-send", "+15551234567", "hello there"]])

    def test_notify_single_arg(self):
        ex, calls = captured()
        ex.execute("notify hi")
        self.assertEqual(calls, [["termux-notification", "--title", "agent",
                                  "--content", "hi"]])

    def test_notify_title_content(self):
        ex, calls = captured()
        ex.execute("notify Title | body here")
        self.assertEqual(calls, [["termux-notification", "--title", "Title",
                                  "--content", "body here"]])

    def test_toast(self):
        ex, calls = captured()
        ex.execute("toast hello world")
        self.assertEqual(calls, [["termux-toast", "hello world"]])

    def test_photo_default_path(self):
        ex, calls = captured()
        ex.execute("photo")
        self.assertEqual(calls, [["termux-camera-photo",
                                  "/sdcard/DCIM/agent_photo.jpg"]])

    def test_unknown_command_refused(self):
        self.assertIn("unknown command", Executor().execute("rm -rf /"))

    def test_bad_quoting(self):
        self.assertIn("bad quoting", Executor().execute("toast 'unclosed"))


class TestRateLimiter(unittest.TestCase):
    def test_limit(self):
        rl = RateLimiter(limit=3, window=60)
        self.assertTrue(rl.allow(now=0))
        self.assertTrue(rl.allow(now=1))
        self.assertTrue(rl.allow(now=2))
        self.assertFalse(rl.allow(now=3))

    def test_window_expiry(self):
        rl = RateLimiter(limit=1, window=10)
        self.assertTrue(rl.allow(now=0))
        self.assertFalse(rl.allow(now=5))
        self.assertTrue(rl.allow(now=11))


class TestAllowed(unittest.TestCase):
    def test_me(self):
        self.assertTrue(is_allowed("me", "me", None, "me"))

    def test_user_allow(self):
        self.assertTrue(is_allowed(42, "me", {42}, "me"))
        self.assertFalse(is_allowed(43, "me", {42}, "me"))

    def test_chat_denied(self):
        self.assertFalse(is_allowed(42, "other", {42}, "me"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
