from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from utils.db import add_user_if_not_exists


router = Router(name="start")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    add_user_if_not_exists(message.from_user.id)
    await message.answer(
        "Привет! Я бот для генерации видео с помощью Sora 2.\n\n"
        "/settings — выбрать формат, длительность и качество.\n\n"
        "Отправь текст или фото с подписью — и я начну генерацию."
    )
