import asyncio

from aiogram import Dispatcher
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from utils.db import init_db
from handlers.start import router as start_router
from handlers.settings import router as settings_router
from handlers.video_generation import router as video_router

from config import BOT_TOKEN

async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()

    dp.include_router(start_router)
    dp.include_router(settings_router)
    dp.include_router(video_router)

    await bot.set_my_commands([BotCommand(command="settings", description="Открыть настройки")])
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
