"""
Долгие операции с паками (перекраска, копирование).

Что делает Job:
  * показывает прогресс и примерное оставшееся время;
  * после каждых BATCH_SIZE добавленных эмодзи делает паузу BATCH_PAUSE секунд
    (Telegram ограничивает частоту addStickerToSet, без пауз летят flood wait на минуты);
  * если Telegram всё равно просит подождать, показывает обратный отсчёт вместо тишины;
  * принимает остановку кнопкой «Остановить».

Время оценивается по среднему времени на один эмодзи (без пауз и ожиданий Telegram)
плюс все ещё предстоящие паузы, поэтому цифра примерная, но честная.
"""
import asyncio
import contextlib
import logging
import time

from hydrogram import enums

import config
import keyboards
import states
import texts as T
from tg import JobCancelled, flood_hook

log = logging.getLogger("fatheremoji")
HTML = enums.ParseMode.HTML

# Читаются при каждом обращении, поэтому в тестах и на лету их можно менять
BATCH_SIZE = int(getattr(config, "BATCH_SIZE", 8))
BATCH_PAUSE = float(getattr(config, "BATCH_PAUSE", 280))

FIRST_GUESS = 2.5          # секунд на эмодзи, пока нет замеров
EDIT_EVERY = 4.0           # как часто обновлять сообщение в работе
EDIT_EVERY_WAIT = 15.0     # как часто обновлять отсчёт во время паузы


def fmt_duration(sec: float) -> str:
    """72 -> '1 мин 12 с', 4000 -> '1 ч 7 мин'."""
    sec = max(0, int(round(sec)))
    if sec < 60:
        return f"{sec} с"
    m, s = divmod(sec, 60)
    if m < 10:
        return f"{m} мин {s} с" if s else f"{m} мин"
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = int(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


class Job:
    def __init__(self, uid: int, status, total: int, head: str, extra: str = "", pacing: bool = False):
        self.uid = uid
        self.status = status          # сообщение, которое показывает прогресс
        self.total = total
        self.head = head              # заголовок (HTML)
        self.extra = extra            # строка под полосой прогресса (HTML)
        self.pacing = pacing          # делать ли паузы между пачками
        self.cancel = False
        self.done = 0                 # обработано (с ошибками)
        self.added = 0                # реально добавлено
        self.since_pause = 0          # сколько добавлено с прошлой паузы
        self.proc_time = 0.0          # секунды чистой работы без пауз и ожиданий Telegram
        self.proc_n = 0
        self.flood_time = 0.0         # сколько всего Telegram заставил ждать
        self._last_edit = 0.0

    # ── оценка времени ──────────────────────────────────────────
    def avg(self) -> float:
        return self.proc_time / self.proc_n if self.proc_n else FIRST_GUESS

    def future_pauses(self) -> int:
        """Сколько пауз ещё впереди (пауза после самого последнего эмодзи не нужна)."""
        remaining = self.total - self.done
        if not self.pacing or remaining <= 0:
            return 0
        return (self.since_pause + remaining - 1) // BATCH_SIZE

    def eta(self, wait_left: float = 0.0) -> float:
        remaining = max(0, self.total - self.done)
        return remaining * self.avg() + self.future_pauses() * BATCH_PAUSE + wait_left

    # ── учёт результата ─────────────────────────────────────────
    def record(self, seconds: float, ok: bool):
        self.done += 1
        self.proc_time += max(0.0, seconds)
        self.proc_n += 1
        if ok:
            self.added += 1
            self.since_pause += 1

    def needs_pause(self) -> bool:
        return self.pacing and self.since_pause >= BATCH_SIZE and self.done < self.total

    # ── сообщение с прогрессом ──────────────────────────────────
    def render(self, state: str = "run", wait_left: float = 0.0) -> str:
        text = f"{self.head}\n\n[{progress_bar(self.done, self.total)}] {self.done} / {self.total}"
        if self.extra:
            text += f"\n{self.extra}"
        eta = fmt_duration(self.eta(wait_left))
        if state == "pause":
            return text + "\n\n" + T.JOB_PAUSE.format(left=fmt_duration(wait_left), batch=BATCH_SIZE, eta=eta)
        if state == "flood":
            return text + "\n\n" + T.JOB_FLOOD.format(left=fmt_duration(wait_left), eta=eta)
        if self.pacing and self.total > BATCH_SIZE:
            text += T.JOB_PACE_NOTE.format(batch=BATCH_SIZE, pause=fmt_duration(BATCH_PAUSE))
        return text + "\n\n" + T.JOB_ETA.format(eta=eta)

    async def update(self, state: str = "run", wait_left: float = 0.0, every: float = EDIT_EVERY):
        now = time.monotonic()
        if now - self._last_edit < every:
            return
        self._last_edit = now
        try:
            await self.status.edit_text(self.render(state, wait_left), reply_markup=keyboards.kb_job_stop(),
                                        parse_mode=HTML, disable_web_page_preview=True)
        except Exception as e:                      # MESSAGE_NOT_MODIFIED, flood на правках и т.п.
            log.debug(f"не удалось обновить прогресс: {e}")

    async def finish(self, text: str):
        try:
            await self.status.edit_text(text, parse_mode=HTML, disable_web_page_preview=True, reply_markup=None)
        except Exception:
            try:
                await self.status.reply(text, parse_mode=HTML, disable_web_page_preview=True)
            except Exception as e:
                log.warning(f"не удалось показать итог: {e}")

    # ── ожидание с обратным отсчётом ────────────────────────────
    async def wait(self, seconds: float, state: str):
        """Ждёт, обновляя отсчёт. Каждую секунду-другую проверяет остановку."""
        end = time.monotonic() + seconds
        first = True
        while True:
            if self.cancel:
                raise JobCancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            await self.update(state, left, every=0 if first else EDIT_EVERY_WAIT)
            first = False
            await asyncio.sleep(min(2.0, left))

    async def pause(self):
        self.since_pause = 0
        await self.wait(BATCH_PAUSE, "pause")

    async def on_flood(self, seconds: float):
        """Вызывается из tg_api_retry, когда Telegram просит подождать."""
        self.flood_time += seconds
        if seconds >= 20:
            self.since_pause = 0          # Telegram и так заставил переждать, лишняя пауза не нужна
        await self.wait(seconds, "flood")


def busy(uid: int) -> bool:
    return uid in states.jobs


@contextlib.asynccontextmanager
async def running(job: Job):
    """Регистрирует задачу (одна на пользователя) и подключает показ ожидания Telegram."""
    states.jobs[job.uid] = job
    token = flood_hook.set(job.on_flood)
    try:
        yield job
    finally:
        flood_hook.reset(token)
        states.jobs.pop(job.uid, None)
