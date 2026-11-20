# telegram_phone

An allowlisted remote-control agent that runs on **your own Android phone**
(Termux) and executes commands it receives over Telegram. For a device you own,
with consent. It never does anything outside the allowlist.

## Pieces

- `telegram_phone.py` — runs on the phone. Logs into your Telegram account with
  Telethon, watches one chat (default: Saved Messages), runs allowlisted
  Termux:API commands, replies with the result. Logs everything.
- `telegram_send.py` — runs on the agent machine. Same Telegram account (a
  second logged-in session) and sends a command into the watched chat.
- `tests/test_telegram_phone.py` — 16 tests for the executor, allowlist and
  rate limiter (no Telegram, no phone needed).

## Requirements (phone)

- Android + [Termux](https://f-droid.org/packages/com.termux/) and the
  **Termux:API** app (grant its permissions in Android settings).
- In Termux: `pkg install python && pip install telethon`
- API ID/hash from https://my.telegram.org (Telegram → API development tools).

## Run the phone agent

```bash
export TG_API_ID=1234567
export TG_API_HASH=yourapihash
python3 telegram_phone.py            # first run: enter phone number + code
```

## Send a command

From the agent machine (same account):

```bash
export TG_API_ID=1234567
export TG_API_HASH=yourapihash
python3 telegram_send.py battery
python3 telegram_send.py notify Title | hello there
python3 telegram_send.py sms +15551234567 "running late"
python3 telegram_send.py photo
python3 telegram_send.py location
```

Or just message yourself in Telegram → Saved Messages.

## Commands

```
ping                 pong
battery              termux-battery-status
location             termux-location
clipboard            termux-clipboard-get
toast <text>         termux-toast
notify <t> | <c>     termux-notification
photo [path]         termux-camera-photo (default /sdcard/DCIM/agent_photo.jpg)
sms <number> <text>  termux-sms-send
contacts             termux-contact-list
info                 termux-telephony-deviceinfo
shell <command>      DISABLED unless TG_ALLOW_SHELL=1
```

## Configuration

```
TG_API_ID, TG_API_HASH   Telegram API credentials (required)
TG_PHONE                 account phone number (first login only)
TG_SESSION               session file on the phone (default phone.session)
TG_CHAT                  chat to watch (default: me = Saved Messages)
TG_ALLOWED_USERS         comma-separated user ids, or me (default me)
TG_ALLOW_SHELL           1 to enable arbitrary shell (default off)
TG_LOG                   command log file (default phone_agent.log)
```

On the agent machine use `TG_SESSION_AGENT` (default `agent.session`).

## Verify

```bash
python3 telegram_phone.py --selftest
python3 -m unittest tests.test_telegram_phone -v
```

## Safety / trust

- Only the allowlisted commands run; anything else is refused.
- Only messages from `TG_ALLOWED_USERS` in `TG_CHAT` are accepted.
- `shell` is off by default and runs arbitrary commands when enabled.
- The session files and API hash are **full access to your Telegram account** —
  keep them private, never commit them.
- This uses a second logged-in session of your personal account on the agent
  machine. If you would rather not do that, the alternative is a Telegram **bot**
  that posts commands into a private group the phone reads; the trade-off is more
  setup (group id, bot privacy mode) for not sharing your account session.
- Only control a phone you own; don't install this on someone else's device.
