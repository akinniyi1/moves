import os
import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.getenv("TELETHON_API_ID"))
api_hash = os.getenv("TELETHON_API_HASH")

async def main():
    print("Starting login...")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start()
        print("Login successful.")
        print("Copy and save the following session string:")
        print(client.session.save())

asyncio.run(main())
