"""CLI for sending cross-agent messages: python -m halos.msg <recipient> \"<message>\""""

import asyncio
import sys
from pathlib import Path

from .config import load_config
from .db import Database


async def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: python -m halos.msg <recipient> \"<message>\"")
        print("Example: python -m halos.msg Alpha \"Can you check the calendar?\"")
        sys.exit(1)

    recipient = args[0]
    content = " ".join(args[1:])

    config = load_config()
    db = Database(config.db_path)
    await db.connect()

    # Infer sender from parent process or default to "cli"
    sender = "cli"

    msg_id = await db.enqueue_agent_message(sender, recipient, content)
    print(f"Message queued (id={msg_id}): {sender} -> {recipient}")

    await db.close()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
