from io import BytesIO

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from utils.accounts import add_account as add_account_into_pool, DuplicateAccountError


router = Router(name="add_account")


class AddAccountState(StatesGroup):
    waiting_for_json = State()


@router.message(Command("add_account"))
async def cmd_add_account(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccountState.waiting_for_json)
    await message.answer(
        "Подключая свой аккаунт, вы увеличиваете лимиты для себя и всех участников и даёте согласие на использование его ботом для генерации видео. Аккаунт обязательно должен иметь доступ к Sora 2.\n\n"
        "<i>Рекомендуется использовать неосновной (вторичный) аккаунт.</i>\n\n"
        "<b>Что делать?</b>\n\n"
        "1) Установите расширение для экспорта cookies <a href=\"https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm\">Chrome</a> / <a href=\"https://addons.mozilla.org/ru/firefox/addon/cookie-editor\">Firefox</a>\n"
        "2) Откройте <a href=\"https://sora.chatgpt.com\">Sora</a> и войдите в аккаунт.\n"
        "3) Нажмите на иконку расширения на этой странице → выберите <b>Export</b> → формат <b>JSON</b> → сохраните файл как <b>cookies.json</b>.\n\n"
        "Отправьте файл <b>cookies.json</b> этому боту.\n\n",
        disable_web_page_preview=True
    )



@router.message(AddAccountState.waiting_for_json, F.document)
async def on_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    file_name = (doc.file_name or "").lower()
    if not file_name.endswith(".json"):
        await state.clear()
        await message.answer("❗️Необходимо отправить документ с расширнием .json\n\nПопробуйте еще раз — /add_account")
        return

    # Download file into memory and validate via accounts.add_account
    try:
        buf = BytesIO()
        await message.bot.download(doc, destination=buf)
        cookies_json = buf.getvalue().decode("utf-8", errors="ignore")
    except Exception:
        await state.clear()
        await message.answer("❗️Некорректные файлы cookies\n\nПопробуйте еще раз — /add_account")
        return

    # Try to add account into the pool (includes validation)
    try:
        add_account_into_pool(cookies_json)
    except DuplicateAccountError:
        await state.clear()
        await message.answer("❗️Этот аккаунт уже добавлен в базу.\n\nПопробуйте другой аккаунт — /add_account")
        return
    except Exception:
        await state.clear()
        await message.answer("❗️Некорректные файлы cookies\nПопробуйте еще раз — /add_account")
        return

    await state.clear()
    await message.answer("✅ Аккаунт успешно добавлен и будет использоваться для генерации видео.")


@router.message(AddAccountState.waiting_for_json)
async def on_any_other(message: Message, state: FSMContext) -> None:
    # Any non-document while waiting should cancel the state and instruct the user
    await state.clear()
    await message.answer("❗️Необходимо отправить файл\n\nПопробуйте еще раз — /add_account", disable_web_page_preview=True)
