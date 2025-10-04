from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from config import BOT_TOKEN


def create_bot() -> Bot:
    if not BOT_TOKEN or BOT_TOKEN.startswith("PUT_"):
        raise RuntimeError("BOT_TOKEN is not set in environment or config.py")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML")
    )
    return bot


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="settings", description="Открыть настройки"),
            BotCommand(command="add_account", description="Добавить аккаунт"),
        ]
    )
