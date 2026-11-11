"""Generate a Telethon StringSession for your own account.

Run locally:  python gen_session.py
Get API_ID / API_HASH from https://my.telegram.org -> API development tools.
Keep the printed session string secret: it gives full access to your account.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nTG_SESSION =\n")
    print(client.session.save())
    client.send_message("me", "TG_SESSION:\n" + client.session.save())
    print("\n(also sent to your Saved Messages)")
