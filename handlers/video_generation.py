from typing import Optional
from io import BytesIO

from aiogram import Router, F
from aiogram.types import Message
from aiogram.types.input_file import URLInputFile

from utils.db import get_user_settings
from utils.sora import SoraClient

from config import PROXY_URL, COOKIES

router = Router(name="video_generation")

client = SoraClient(cookies=COOKIES, proxy=PROXY_URL)

async def _start_generation(message: Message, prompt: str, image_bytes: Optional[bytes] = None) -> None:
    user_id = message.from_user.id

    is_vertical, duration_sec, size = get_user_settings(user_id)

    wait_msg: Optional[Message] = None

    wait_msg = await message.reply("⏳")

    orientation = "portrait" if int(is_vertical) == 1 else "landscape"
    duration_i = int(duration_sec)
    frames = duration_i * 30

    async for evt in client.generate_video(
        prompt=prompt,
        orientation=orientation,
        start_image=image_bytes,
        frames=frames,
        size=str(size),
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

            url = evt.get("url")
            if url:
                try:
                    await message.reply_video(
                        video=URLInputFile(url),
                        caption="<b>✅ Видео успешно создано</b>",
                    )
                except Exception as e:
                    await message.reply("<b>✅ Видео успешно создано</b>\n\n" + url)
            else:
                await message.reply("❗️Видео успешно создано, но файл не найден в ответе")
            return
    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass
    await message.reply("<b>🚫 Ошибка генерации:</b>\n<pre>Неизвестное состояние</pre>")



@router.message(F.text & ~F.media_group_id)
async def on_text(message: Message) -> None:
    text = message.text or ""
    await _start_generation(message, text)


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    if message.media_group_id:
        return

    caption = message.caption or ""
    if not caption.strip():
        await message.reply("❗️Пожалуйста, добавьте подпись к фото — это будет промпт.")
        return

    try:
        buf = BytesIO()
        await message.bot.download(message.photo[-1], destination=buf)
        image_bytes = buf.getvalue()
    except Exception:
        await message.reply("Не удалось получить фото для генерации.")
        return

    await _start_generation(message, caption, image_bytes=image_bytes)
