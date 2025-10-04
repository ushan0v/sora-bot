import os
from typing import Optional
from io import BytesIO

from aiogram import Router, F
from aiogram.types import Message
from aiogram.types.input_file import URLInputFile

from utils.db import get_user_settings, set_active_generation
from utils.sora_client import generate_video

from config import PROXY_URL

router = Router(name="video_generation")


async def _start_generation(message: Message, prompt: str, image_bytes: Optional[bytes] = None) -> None:
    user_id = message.from_user.id

    is_vertical, duration_sec, active, size = get_user_settings(user_id)

    if user_id != 793840080:
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

        async for evt in generate_video(
            prompt,
            orientation=orientation,
            image=image_bytes,
            frames=frames,
            size=str(size),
            poll_interval_sec=3.0,
            timeout_sec=900.0,
            proxy=PROXY_URL,
        ):
            et = str(evt.get("event"))

            if et == "queued" or (et == "progress" and evt.get("status") == "queued"):
                if wait_msg:
                    try:
                        await wait_msg.edit_text("⏳ Генерация скоро начнется...")
                    except Exception:
                        pass
                continue

            if et == "progress" and evt.get("status") == "rendering":
                pct = evt.get("progress_pct")
                if isinstance(pct, (int, float)):
                    pct_i = int(round(float(pct) * 100))
                    if wait_msg:
                        try:
                            await wait_msg.edit_text(f"🚀 Видео создается. Прогресс: <b>{pct_i}%</b>")
                        except Exception:
                            pass
                continue

            if et == "error":
                if wait_msg:
                    try:
                        await wait_msg.delete()
                    except Exception:
                        pass
                err_msg = evt.get("message") or evt.get("code") or "Неизвестная ошибка"
                await message.reply(f"<b>🚫 Ошибка генерации:</b>\n<pre>{err_msg}</pre>")
                return

            if et == "finished":
                if wait_msg:
                    try:
                        await wait_msg.delete()
                    except Exception:
                        pass

                url = evt.get("downloadable_url") or evt.get("url")
                if url:
                    try:
                        await message.reply_video(
                            video=URLInputFile(url),
                            caption="<b>✅ Видео успешно создано</b>",
                        )
                    except Exception as e:
                        # Fallback: send as a plain link
                        await message.reply("<b>✅ Видео успешно создано</b>\n\n" + url)
                else:
                    await message.reply("❗️ Видео успешно создано, но файл не найден в ответе")
                return
        # If loop exits without finished or error, treat as unknown failure
        if wait_msg:
            try:
                await wait_msg.delete()
            except Exception:
                pass
        await message.reply("🚫 Ошибка генерации: неизвестное состояние")
    finally:
        set_active_generation(user_id, 0)


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
        await message.reply("Пожалуйста, добавьте подпись к фото — это будет промпт.")
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
