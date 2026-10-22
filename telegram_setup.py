#!/usr/bin/env python
"""One-time Telegram setup check.

1. Create a bot with @BotFather in Telegram (/newbot), copy the token it gives you and
   put it in .env as TELEGRAM_BOT_TOKEN=...  (never paste the token anywhere else).
2. Open a chat with your new bot in Telegram and press Start (or send any message).
3. Run this script. It verifies the token, finds your chat id from that message, saves it
   to .env with --save, and sends a test message in the same format as the daily reports.

    python telegram_setup.py --save
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from premarket_scanner import config
from premarket_scanner import telegram_notify as tg


def discover_chat_id(token: str, kinds: tuple[str, ...] | None = None) -> tuple[str, str] | None:
    """Newest chat that has messaged (or added) the bot, as (chat_id, description).

    ``kinds`` limits the chat types, e.g. ("group", "supergroup", "channel").
    """
    updates = tg.api_call("getUpdates", token, limit=100)
    found: tuple[str, str] | None = None
    for upd in updates or []:
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or (upd.get("my_chat_member") or {}).get("chat") or {}
        if chat.get("id") is None:
            continue
        if kinds and chat.get("type") not in kinds:
            continue
        name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
            or chat.get("username") or "?"
        found = (str(chat["id"]), f"{chat.get('type', 'chat')} '{name}'")
    return found


def add_extra_chat(token: str, send_test: bool = True) -> int:
    """Find the newest group/channel the bot was added to and append it to TELEGRAM_EXTRA_CHAT_IDS."""
    found = discover_chat_id(token, kinds=("group", "supergroup", "channel"))
    if not found:
        print("No group or channel found yet. Add the bot to the group (or as an admin of the channel), "
              "send one message there, then run again.")
        return 1
    chat_id, desc = found
    existing = tg.extra_chat_ids()
    if chat_id in existing or chat_id == tg.credentials()[1]:
        print(f"{desc} (id {chat_id}) is already receiving reports.")
    else:
        save_env_value("TELEGRAM_EXTRA_CHAT_IDS", ",".join(existing + [chat_id]))
        print(f"Added {desc} (id {chat_id}) to TELEGRAM_EXTRA_CHAT_IDS in .env")
    if send_test:
        text = ("Signal Bot เชื่อมต่อกับแชทนี้แล้ว จะได้รับรายงานสแกนและสัญญาณซื้อเหมือนแชทหลัก"
                if tg.is_thai() else "Signal Bot connected: this chat now receives the scan reports and signals.")
        try:
            tg.api_call("sendMessage", token, chat_id=chat_id, text=text)
            print("Test message sent to that chat.")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not post there: {exc}. For a channel the bot must be an administrator with 'Post messages'.")
            return 1
    return 0


def save_env_value(key: str, value: str) -> None:
    """Set ``key=value`` in .env, replacing an existing line or appending one."""
    env_path = config.PROJECT_ROOT / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, text, flags=re.M):
        text = re.sub(pattern, lambda _m: f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    env_path.write_text(text, encoding="utf-8")


def save_chat_id(chat_id: str) -> None:
    save_env_value("TELEGRAM_CHAT_ID", chat_id)


TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


def read_clipboard() -> str:
    if sys.platform.startswith("win"):
        import subprocess

        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=20,
        )
        return (out.stdout or "").strip()
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text.strip()
    except Exception:  # noqa: BLE001
        return ""


def token_from_clipboard() -> str | None:
    """Take the token from the clipboard (never printed) and store it in .env."""
    token = read_clipboard().strip().strip('"').strip("'")
    if not token:
        print("Clipboard is empty. Copy the token from @BotFather, then run again.")
        return None
    if not TOKEN_RE.match(token):
        shape = f"{len(token)} chars, {'with' if ':' in token else 'no'} ':'"
        print(f"Clipboard does not look like a bot token ({shape}). "
              "Copy the token from @BotFather (digits:letters), then run again.")
        return None
    save_env_value("TELEGRAM_BOT_TOKEN", token)
    print("Saved TELEGRAM_BOT_TOKEN from the clipboard to .env (not displayed).")
    return token


def prompt_for_token() -> str | None:
    """Ask for the token in this terminal with hidden input and store it in .env."""
    import getpass

    try:
        token = getpass.getpass("Paste the bot token from @BotFather (typing is hidden), then press Enter: ").strip()
    except (EOFError, OSError):
        print("No interactive terminal available; run this command in a terminal window.")
        return None
    token = token.strip('"').strip("'")
    if not token:
        print("No token entered.")
        return None
    if not re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", token):
        print("Warning: that does not look like a BotFather token (expected digits:letters, e.g. 123456789:AAxxxx). Saving anyway.")
    save_env_value("TELEGRAM_BOT_TOKEN", token)
    print("Saved TELEGRAM_BOT_TOKEN to .env")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Telegram bot and send a test message")
    parser.add_argument("--save", action="store_true", help="write the discovered chat id into .env")
    parser.add_argument("--set-token", action="store_true",
                        help="ask for the bot token in this terminal (hidden input) and save it to .env first")
    parser.add_argument("--token-from-clipboard", action="store_true",
                        help="take the bot token from the clipboard (copied from @BotFather) and save it to .env")
    parser.add_argument("--add-chat", action="store_true",
                        help="add the newest group/channel the bot was added to as an extra report recipient")
    parser.add_argument("--no-test", action="store_true", help="do not send the test message")
    args = parser.parse_args(argv)

    if args.token_from_clipboard and token_from_clipboard() is None:
        return 1
    if args.set_token and prompt_for_token() is None:
        return 1

    token, chat_id = tg.credentials()
    if not token:
        print("TELEGRAM_BOT_TOKEN is empty in .env.")
        print("Create a bot with @BotFather (/newbot), then put the token in .env as TELEGRAM_BOT_TOKEN=...")
        print("Or run:  python telegram_setup.py --set-token --save   and paste it when asked.")
        return 1
    if args.add_chat:
        return add_extra_chat(token, send_test=not args.no_test)
    try:
        me = tg.api_call("getMe", token)
    except Exception as exc:  # noqa: BLE001
        print(f"Token check failed: {exc}")
        return 1
    username = me.get("username", "?")
    print(f"Bot OK: @{username}")

    if not chat_id:
        try:
            found = discover_chat_id(token)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not read updates: {exc}")
            return 1
        if not found:
            print(f"No chat found yet. In Telegram open @{username}, press Start (or send any message), then run again.")
            return 1
        chat_id, desc = found
        print(f"Found chat id {chat_id} ({desc})")
        if args.save:
            save_chat_id(chat_id)
            print("Saved TELEGRAM_CHAT_ID to .env")
        else:
            print("Re-run with --save to store it in .env, or add TELEGRAM_CHAT_ID=... yourself.")
    else:
        print(f"Using TELEGRAM_CHAT_ID={chat_id} from .env")

    if args.no_test:
        return 0
    now = datetime.now(ZoneInfo(config.MARKET_TZ)).strftime("%Y-%m-%d %H:%M ET")
    if tg.is_thai():
        text = "\n".join([
            f"เชื่อมต่อ Signal Bot สำเร็จ ({now})",
            "",
            tg.HEADER_A_TH,
            "(ทดสอบ) รายชื่อหุ้นที่พุ่งขึ้นก่อนตลาดเปิดจะแสดงตรงนี้",
            "",
            tg.HEADER_B_TH,
            "(ทดสอบ) หุ้นที่เข้าเงื่อนไขซื้อจะแสดงตรงนี้",
        ])
    else:
        text = "\n".join([
            f"Signal Bot connected ({now})",
            "",
            tg.HEADER_A,
            "(test) the pre-market list will appear here",
            "",
            tg.HEADER_B,
            "(test) the trade setups will appear here",
        ])
    try:
        tg.api_call("sendMessage", token, chat_id=chat_id, text=text)
    except Exception as exc:  # noqa: BLE001
        print(f"Test message failed: {exc}")
        return 1
    print("Test message sent. Check Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
