import asyncio

from court_bot.bot import CourtBot
from court_bot.config import load_config


async def main() -> None:
    config = load_config()
    bot = CourtBot(config=config)

    async with bot:
        await bot.start(config.token)


if __name__ == "__main__":
    asyncio.run(main())
