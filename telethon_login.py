import os
import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.getenv("25469867"))
api_hash = os.getenv("029a35f93bf8c618e67c995b9b94d26b"))

async def main():
    print("Starting login...")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("Login successful.")
        print("Copy and save the following session string:")
        print(client.session.save())

asyncio.run(main())
