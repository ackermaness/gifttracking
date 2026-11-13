"""Generate a Telethon StringSession locally. NEVER commit/share its output."""
from __future__ import annotations

from getpass import getpass
from telethon import TelegramClient
from telethon.sessions import StringSession


def main() -> None:
    api_id = int(input("TG_API_ID: ").strip())
    api_hash = getpass("TG_API_HASH (hidden): ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nTG_SESSION_STRING (SECRET - copy ONLY into GitHub Actions Secrets):")
        print(client.session.save())
        print("\nDo not paste it into chat, issues, code or screenshots.")


if __name__ == "__main__":
    main()
