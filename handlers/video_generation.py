import os
from typing import Optional
from io import BytesIO

from aiogram import Router, F
from aiogram.types import Message

from utils.db import enqueue_generation_job, get_user_settings, set_active_generation
from utils.generation_queue import get_generation_queue

from config import ADMIN_ID

router = Router(name="video_generation")


async def _start_generation(message: Message, prompt: str, image_bytes: Optional[bytes] = None) -> None:
    user_id = message.from_user.id

    is_vertical, duration_sec, active, size = get_user_settings(user_id)

    if user_id != ADMIN_ID:
        if int(active) == 1:
            await message.answer("❗️У вас уже есть активная генерация. Дождитесь завершения.")
            return

    set_active_generation(user_id, 1)
    wait_msg: Optional[Message] = None
    try:
        wait_msg = await message.reply("⏳")

        orientation = "portrait" if int(is_vertical) == 1 else "landscape"
        duration_i = int(duration_sec)
        frames = duration_i * 30

        enqueue_generation_job(
            user_id=user_id,
            chat_id=message.chat.id,
            prompt=prompt,
            orientation=None if image_bytes is not None else orientation,
            frames=frames,
            size=str(size),
            image_bytes=image_bytes,
            wait_message_id=wait_msg.message_id if wait_msg else None,
            poll_interval=3.0,
            timeout_sec=900.0,
        )

        try:
            queue = get_generation_queue()
            queue.notify_new_job()
        except Exception:
            # If the queue is not initialized we will rely on the next startup to pick the job up
            pass

        if wait_msg:
            try:
                await wait_msg.edit_text(
                    "⏳ Ваша генерация поставлена в очередь. Я пришлю обновления сюда.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    except Exception:
        set_active_generation(user_id, 0)
        if wait_msg:
            try:
                await wait_msg.delete()
            except Exception:
                pass
        await message.reply("❗️Не удалось поставить задачу в очередь. Попробуйте позже.")


@router.message(F.text & ~F.media_group_id)
async def on_text(message: Message) -> None:
    # Start generation from raw text
    text = message.text or ""
    await _start_generation(message, text)


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    # Ignore media groups entirely
    if message.media_group_id:
        return

    caption = message.caption or ""
    if not caption.strip():
        await message.reply("❗️Пожалуйста, добавьте подпись к фото — это будет промпт.")
        return

    # Скачать фото в память (без сохранения на диск) и передать байты
    try:
        buf = BytesIO()
        await message.bot.download(message.photo[-1], destination=buf)
        image_bytes = buf.getvalue()
    except Exception:
        await message.reply("Не удалось получить фото для генерации.")
        return

    await _start_generation(message, caption, image_bytes=image_bytes)
