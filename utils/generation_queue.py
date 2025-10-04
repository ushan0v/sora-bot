import asyncio
import logging
from typing import Any, Dict, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types.input_file import URLInputFile

from config import PROXY_URL
from utils.db import (
    fetch_next_queued_job,
    list_jobs_by_status,
    set_active_generation,
    update_job,
)
from utils.sora_client import generate_video, resume_generation


_logger = logging.getLogger("generation_queue")


class GenerationQueue:
    def __init__(self, bot: Bot, *, max_workers: int = 5) -> None:
        self.bot = bot
        self.max_workers = max_workers
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._main_task: Optional[asyncio.Task[Any]] = None
        self._inflight: dict[int, asyncio.Task[Any]] = {}

    async def start(self) -> None:
        if self._main_task:
            return
        await self._recover_running_jobs()
        self._main_task = asyncio.create_task(self._run_loop(), name="generation-queue")

    async def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        tasks = []
        for task in self._inflight.values():
            task.cancel()
            tasks.append(task)
        if self._main_task:
            self._main_task.cancel()
            tasks.append(self._main_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._main_task = None

    def notify_new_job(self) -> None:
        self._wake_event.set()

    async def _recover_running_jobs(self) -> None:
        jobs = list_jobs_by_status(["running"])
        for job in jobs:
            task_id = job.get("task_id")
            account_id = job.get("account_id")
            if task_id and account_id:
                _logger.info("Resuming job %s (task %s)", job["id"], task_id)
                self._start_job(job, resume=True)
            else:
                # Without task/account data we cannot resume safely; requeue it
                update_job(
                    job["id"],
                    status="queued",
                    task_id=None,
                    account_id=None,
                    progress=None,
                    last_event="requeued",
                )
                self.notify_new_job()

    def _start_job(self, job: Dict[str, Any], *, resume: bool) -> None:
        job_id = int(job["id"])
        task = asyncio.create_task(self._process_job(job, resume=resume), name=f"generation-job-{job_id}")
        self._inflight[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: self._on_task_done(jid, t))

    def _on_task_done(self, job_id: int, task: asyncio.Task[Any]) -> None:
        self._inflight.pop(job_id, None)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None
        except Exception as e:  # pragma: no cover - defensive
            exc = e
        if exc:
            _logger.exception("Generation job %s failed", job_id, exc_info=exc)
        self._wake_event.set()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            started = False
            while not self._stop_event.is_set() and len(self._inflight) < self.max_workers:
                job = fetch_next_queued_job()
                if not job:
                    break
                _logger.info("Starting queued job %s", job["id"])
                self._start_job(job, resume=False)
                started = True
            if self._stop_event.is_set():
                break
            if not started and not self._inflight:
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                finally:
                    self._wake_event.clear()
            else:
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                finally:
                    self._wake_event.clear()

    async def _process_job(self, job: Dict[str, Any], *, resume: bool) -> None:
        job_id = int(job["id"])
        prompt = job.get("prompt") or ""
        orientation = job.get("orientation")
        frames = int(job.get("frames") or 0)
        size = (job.get("size") or "large").lower()
        wait_message_id = job.get("wait_message_id")
        chat_id = job.get("chat_id")
        user_id = job.get("user_id")
        poll_interval = float(job.get("poll_interval") or 3.0)
        timeout_sec = float(job.get("timeout_sec") or 900.0)
        image_bytes = job.get("image")
        if isinstance(image_bytes, memoryview):
            image_bytes = image_bytes.tobytes()
        elif image_bytes is not None and not isinstance(image_bytes, (bytes, bytearray)):
            image_bytes = bytes(image_bytes)

        generator: Optional[Any]
        if resume:
            task_id = job.get("task_id")
            account_id = job.get("account_id")
            if not task_id or not account_id:
                _logger.warning("Job %s missing resume data, marking as failed", job_id)
                await self._handle_failure(job, "Не удалось возобновить генерацию")
                return
            generator = resume_generation(
                task_id=str(task_id),
                account_id=int(account_id),
                poll_interval_sec=poll_interval,
                timeout_sec=timeout_sec,
                proxy=PROXY_URL,
            )
            await self._edit_wait_message(job, "♻️ Возобновляю отслеживание генерации...")
        else:
            generator = generate_video(
                prompt,
                orientation=orientation,
                image=image_bytes,
                frames=frames,
                size=size,
                poll_interval_sec=poll_interval,
                timeout_sec=timeout_sec,
                proxy=PROXY_URL,
            )
            await self._edit_wait_message(job, "⏳ Генерация скоро начнётся...")

        try:
            async for event in generator:  # type: ignore[assignment]
                should_continue = await self._handle_event(job, event)
                if not should_continue:
                    return
            await self._handle_failure(job, "Неизвестное состояние генерации")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("Job %s raised exception", job_id, exc_info=exc)
            await self._handle_failure(job, f"Внутренняя ошибка: {exc}")

    async def _handle_event(self, job: Dict[str, Any], event: Dict[str, Any]) -> bool:
        job_id = int(job["id"])
        event_name = str(event.get("event"))
        update_job(job_id, last_event=event_name)
        if event_name == "account":
            account_id = event.get("account_id")
            if account_id is not None:
                update_job(job_id, account_id=int(account_id))
                job["account_id"] = int(account_id)
            return True
        if event_name == "auth":
            await self._edit_wait_message(job, "🔐 Авторизация...")
            return True
        if event_name == "uploaded":
            await self._edit_wait_message(job, "📤 Изображение загружено, готовим генерацию...")
            return True
        if event_name == "queued":
            task_id = event.get("task_id")
            if task_id:
                update_job(job_id, task_id=str(task_id))
                job["task_id"] = str(task_id)
            await self._edit_wait_message(job, "⏳ Генерация скоро начнётся...")
            return True
        if event_name == "progress":
            status = event.get("status")
            if status == "queued":
                await self._edit_wait_message(job, "⏳ Генерация скоро начнётся...")
                return True
            if status == "rendering":
                pct = event.get("progress_pct")
                if isinstance(pct, (int, float)):
                    pct_int = int(round(float(pct) * 100))
                    update_job(job_id, progress=float(pct))
                    await self._edit_wait_message(
                        job,
                        f"🚀 Видео создаётся. Прогресс: <b>{pct_int}%</b>",
                    )
            return True
        if event_name == "draft_found":
            await self._edit_wait_message(job, "🔄 Обрабатываем черновик...")
            return True
        if event_name == "error":
            message = event.get("message") or event.get("code") or "Неизвестная ошибка"
            update_job(job_id, status="failed", error_message=str(message))
            await self._handle_failure(job, message)
            return False
        if event_name == "finished":
            url = event.get("downloadable_url") or event.get("url")
            update_job(job_id, status="completed", result_url=url, progress=1.0)
            await self._handle_success(job, url)
            return False
        return True

    async def _edit_wait_message(self, job: Dict[str, Any], text: str) -> None:
        message_id = job.get("wait_message_id")
        chat_id = job.get("chat_id")
        if not message_id or not chat_id:
            return
        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=int(chat_id),
                message_id=int(message_id),
                parse_mode="HTML",
            )
        except TelegramAPIError:
            job["wait_message_id"] = None
            update_job(int(job["id"]), wait_message_id=None)

    async def _delete_wait_message(self, job: Dict[str, Any]) -> None:
        message_id = job.get("wait_message_id")
        chat_id = job.get("chat_id")
        if not message_id or not chat_id:
            return
        try:
            await self.bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
        except TelegramAPIError:
            pass
        finally:
            job["wait_message_id"] = None
            update_job(int(job["id"]), wait_message_id=None)

    async def _handle_failure(self, job: Dict[str, Any], message: str) -> None:
        await self._delete_wait_message(job)
        chat_id = job.get("chat_id")
        user_id = job.get("user_id")
        if chat_id:
            try:
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"<b>🚫 Ошибка генерации:</b>\n<pre>{message}</pre>",
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                pass
        if user_id is not None:
            set_active_generation(int(user_id), 0)
        update_job(int(job["id"]), status="failed", error_message=str(message), image=None)

    async def _handle_success(self, job: Dict[str, Any], url: Optional[str]) -> None:
        await self._delete_wait_message(job)
        chat_id = job.get("chat_id")
        user_id = job.get("user_id")
        if chat_id:
            if url:
                try:
                    await self.bot.send_video(
                        chat_id=int(chat_id),
                        video=URLInputFile(url),
                        caption="<b>✅ Видео успешно создано</b>",
                        parse_mode="HTML",
                    )
                except TelegramAPIError:
                    try:
                        await self.bot.send_message(
                            chat_id=int(chat_id),
                            text=f"<b>✅ Видео успешно создано</b>\n\n{url}",
                            parse_mode="HTML",
                        )
                    except TelegramAPIError:
                        pass
            else:
                try:
                    await self.bot.send_message(
                        chat_id=int(chat_id),
                        text="❗️Видео успешно создано, но ссылка отсутствует",
                    )
                except TelegramAPIError:
                    pass
        if user_id is not None:
            set_active_generation(int(user_id), 0)
        update_job(int(job["id"]), status="completed", progress=1.0, image=None)


_generation_queue: Optional[GenerationQueue] = None


def init_generation_queue(bot: Bot, *, max_workers: int = 5) -> GenerationQueue:
    global _generation_queue
    if _generation_queue is None:
        _generation_queue = GenerationQueue(bot, max_workers=max_workers)
    return _generation_queue


def get_generation_queue() -> GenerationQueue:
    if _generation_queue is None:
        raise RuntimeError("GenerationQueue is not initialized")
    return _generation_queue


__all__ = [
    "GenerationQueue",
    "init_generation_queue",
    "get_generation_queue",
]
