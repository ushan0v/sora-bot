from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def _checkmark(selected: bool) -> str:
    return "✅ " if selected else ""


def build_settings_keyboard(is_vertical: bool, duration_sec: int, size: str) -> InlineKeyboardMarkup:
    h_selected = not is_vertical
    v_selected = is_vertical
    d5 = duration_sec == 5
    d10 = duration_sec == 10
    d15 = duration_sec == 15
    sz_small = (size or '').lower() == 'small'
    sz_large = (size or '').lower() != 'small'

    row1 = [
        InlineKeyboardButton(
            text=f"{_checkmark(h_selected)}Горизонтальный",
            callback_data="set:orient:landscape",
        ),
        InlineKeyboardButton(
            text=f"{_checkmark(v_selected)}Вертикальный",
            callback_data="set:orient:portrait",
        ),
    ]

    row2 = [
        InlineKeyboardButton(text=f"{_checkmark(d5)}5 сек.", callback_data="set:dur:5"),
        InlineKeyboardButton(text=f"{_checkmark(d10)}10 сек.", callback_data="set:dur:10"),
        InlineKeyboardButton(text=f"{_checkmark(d15)}15 сек.", callback_data="set:dur:15"),
    ]

    row3 = [
        InlineKeyboardButton(text=f"{_checkmark(sz_small)}720p", callback_data="set:size:small"),
        InlineKeyboardButton(text=f"{_checkmark(sz_large)}1080p", callback_data="set:size:large"),
    ]

    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])
