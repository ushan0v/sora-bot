import asyncio

from aiogram import Dispatcher

from bot import create_bot, setup_bot_commands
from utils.db import init_db
from utils.generation_queue import init_generation_queue
from handlers.start import router as start_router
from handlers.settings import router as settings_router
from handlers.video_generation import router as video_router
from handlers.add_account import router as add_account_router


async def main() -> None:
    init_db()
    bot = create_bot()
    dp = Dispatcher()
    queue = init_generation_queue(bot)

    # Routers
    dp.include_router(start_router)
    dp.include_router(settings_router)
    dp.include_router(add_account_router)
    dp.include_router(video_router)

    await setup_bot_commands(bot)
    await queue.start()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await queue.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
