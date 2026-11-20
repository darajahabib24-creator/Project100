#!/usr/bin/env python3
"""telegram_send.py - send one command to the phone agent over Telegram.

Run this on the agent machine. It uses the SAME Telegram account as the phone
agent (a second logged-in session), so a command lands in the watched chat
(default: Saved Messages) and the phone executes it.

    TG_API_ID=... TG_API_HASH=... python3 telegram_send.py battery
    TG_API_ID=... TG_API_HASH=... python3 telegram_send.py sms +15551234567 "hi"

The first run asks for the account phone number and the one-time login code.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="telegram_send",
        description="Send a command to your phone agent over Telegram.")
    ap.add_argument("command", nargs="+", help="command text to send")
    ap.add_argument("--api-id", type=int,
                    default=int(os.environ.get("TG_API_ID", 0)))
    ap.add_argument("--api-hash", default=os.environ.get("TG_API_HASH", ""))
    ap.add_argument("--phone", default=os.environ.get("TG_PHONE", ""),
                    help="account phone number (only needed at first login)")
    ap.add_argument("--session",
                    default=os.environ.get("TG_SESSION_AGENT", "agent.session"))
    ap.add_argument("--chat", default=os.environ.get("TG_CHAT", "me"),
                    help="chat to send to (default: me = Saved Messages)")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.api_id or not args.api_hash:
        print("error: set TG_API_ID and TG_API_HASH (https://my.telegram.org)",
              file=sys.stderr)
        return 2
    try:
        from telethon import TelegramClient
    except ImportError:
        print("error: telethon is required (pip install telethon)", file=sys.stderr)
        return 2

    async def run() -> None:
        client = TelegramClient(args.session, args.api_id, args.api_hash)
        if args.phone:
            await client.start(phone=args.phone)
        else:
            await client.start()
        await client.send_message(args.chat, " ".join(args.command))
        await client.disconnect()

    asyncio.run(run())
    print("sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
