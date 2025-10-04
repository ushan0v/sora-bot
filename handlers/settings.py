from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from utils.db import (
    get_user_settings,
    update_orientation,
    update_duration,
    add_user_if_not_exists,
    update_size,
)
from keyboard.settings_menu import build_settings_keyboard


router = Router(name="settings")


@router.message(Command("settings"))
@router.message(Command("settigs"))
async def cmd_settings(message: Message) -> None:
    add_user_if_not_exists(message.from_user.id)
    is_vertical, duration_sec, _, size = get_user_settings(message.from_user.id)
    await message.answer(
        "Выберите ориентацию, длительность и качество:",
        reply_markup=build_settings_keyboard(bool(is_vertical), int(duration_sec), str(size)),
    )


@router.callback_query(F.data.startswith("set:"))
async def on_settings_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    is_vertical, duration_sec, _, size = get_user_settings(user_id)
    data = callback.data or ""
    if data.startswith("set:orient:"):
        value = data.split(":", 2)[2]
        new_is_vertical = 1 if value == "portrait" else 0
        if new_is_vertical == int(is_vertical):
            await callback.answer()
            return
        update_orientation(user_id, new_is_vertical)
        is_vertical = new_is_vertical
        await callback.message.edit_reply_markup(
            reply_markup=build_settings_keyboard(bool(is_vertical), int(duration_sec), str(size))
        )
        return

    if data.startswith("set:dur:" ):
        value = int(data.split(":", 2)[2])
        if value == int(duration_sec):
            await callback.answer()
            return
        update_duration(user_id, value)
        duration_sec = value
        await callback.message.edit_reply_markup(
            reply_markup=build_settings_keyboard(bool(is_vertical), int(duration_sec), str(size))
        )
        return

    if data.startswith("set:size:"):
        value = data.split(":", 2)[2]
        value_norm = value.lower()
        if value_norm not in ("small", "large"):
            await callback.answer()
            return
        if value_norm == str(size).lower():
            await callback.answer()
            return
        update_size(user_id, value_norm)
        size = value_norm
        await callback.message.edit_reply_markup(
            reply_markup=build_settings_keyboard(bool(is_vertical), int(duration_sec), str(size))
        )
