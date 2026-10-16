"""One-off: register the bot's command menu and description (0.15).

Usage:  BOT_TOKEN=... python3 utilities/set_commands.py
        COMMUNITY_ENABLED=true BOT_TOKEN=... python3 utilities/set_commands.py
Use the same COMMUNITY_ENABLED value as the deployed function.
"""

import asyncio
import os

from telegram import Bot, BotCommand

COMMANDS = [
    BotCommand("start", "Register (new users only)"),
    BotCommand("help", "How to use the bot"),
]
if os.getenv("COMMUNITY_ENABLED", "false").lower() == "true":
    COMMANDS.append(BotCommand("leave", "Leave your current Community"))

DESCRIPTION = (
    "Look up hymn lyrics, chords, sheet music, recordings and ready-made slides "
    "from Then Sings My Soul and 10 other hymnbooks. For members with a "
    "Singapore, Malaysia or Australia phone number."
)

SHORT_DESCRIPTION = "Hymn lyrics, chords, scores, recordings and slides from 11 hymnbooks."


async def main():
    async with Bot(os.environ["BOT_TOKEN"]) as bot:
        await bot.set_my_commands(COMMANDS)
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
        print("Commands:", [c.command for c in await bot.get_my_commands()])


if __name__ == "__main__":
    asyncio.run(main())
