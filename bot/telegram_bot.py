"""Telegram-слой: превращает действия клиента в вызовы движка и рисует ответы."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)

from . import scenario as scenario_mod
from .engine import Answered, EditButtons, Engine, Lead, Notify, Session, Toast, View, touch
from .storage import Storage, leads_xlsx

log = logging.getLogger("bot")


# Кастомные иконки на кнопках Telegram разрешает только при Premium у владельца бота.
# Если Telegram их отклонил, дальше отправляем кнопки без иконок.
ICONS = {"enabled": True}


def inline(rows) -> InlineKeyboardMarkup | None:
    if not rows:
        return None

    def button(b):
        extra = {"style": b.style or None,
                 "icon_custom_emoji_id": (b.icon or None) if ICONS["enabled"] else None}
        if b.url:
            return InlineKeyboardButton(text=b.text, url=b.url, **extra)
        return InlineKeyboardButton(text=b.text, callback_data=b.data, **extra)

    return InlineKeyboardMarkup(inline_keyboard=[[button(b) for b in row] for row in rows])


def has_icons(rows) -> bool:
    return any(b.icon for row in rows for b in row)


def user_title(user) -> str:
    name = html.escape(user.full_name or str(user.id))
    tail = f" (@{user.username})" if user.username else ""
    return f'<a href="tg://user?id={user.id}">{name}</a>{html.escape(tail)}'


class App:
    def __init__(self, token: str, source: str, db_path: str, lead_chat: str = "", proxy: str = ""):
        session = AiohttpSession(proxy=proxy) if proxy else None
        self.bot = Bot(token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                                           link_preview_is_disabled=True))
        self.source = source
        self.lead_chat_env = lead_chat
        self.store = Storage(db_path)
        self.engine: Engine | None = None
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_error = ""
        self.loaded_at = 0.0
        self.router = Router()
        self._routes()

    # ---------- сценарий ----------

    @property
    def sc(self):
        return self.engine.sc

    def lead_chat(self) -> int | None:
        ids = self.sc.setting_ids("Чат для заявок (ID)") or [int(x) for x in self.lead_chat_env.split(",") if x.strip()]
        return ids[0] if ids else None

    def is_admin(self, message: Message) -> bool:
        return message.from_user.id in self.sc.setting_ids("Администраторы (ID через запятую)") \
            or message.chat.id == self.lead_chat()

    async def reload(self) -> list[str]:
        """Перечитать таблицу. При ошибках бот остаётся на прошлой версии."""
        try:
            sc = await scenario_mod.load(self.source)
        except scenario_mod.ScenarioError as e:
            return e.errors
        if self.engine:
            self.engine.sc = sc
        else:
            self.engine = Engine(sc)
        self.loaded_at = time.time()
        return []

    # ---------- вывод ----------

    async def show(self, chat_id: int, s: Session, outs: list, cq: CallbackQuery | None = None, user=None):
        note = None
        toasted = False
        for out in outs:
            try:
                if isinstance(out, Answered):
                    note = out.text
                elif isinstance(out, Toast):
                    if cq:
                        await cq.answer(out.text, show_alert=True)
                        toasted = True
                    else:
                        await self.bot.send_message(chat_id, html.escape(out.text))
                elif isinstance(out, EditButtons):
                    if s.ui.get("msg_id"):
                        await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=s.ui["msg_id"],
                                                                 reply_markup=inline(out.buttons))
                elif isinstance(out, View):
                    await self._send_view(chat_id, s, out, note)
                    if out.step:
                        note = None
                elif isinstance(out, Lead):
                    await self._send_lead(out, user)
                elif isinstance(out, Notify):
                    await self._send_notify(out, user)
            except TelegramBadRequest as e:
                log.warning("telegram: %s", e)
        if cq and not toasted:
            await cq.answer()

    async def _send_view(self, chat_id: int, s: Session, view: View, note: str | None):
        if view.step:
            # закрываем прошлый шаг: убираем кнопки и подписываем выбор клиента
            if s.ui.get("reply_kb"):
                await self.bot.send_message(chat_id, html.escape(note or "Принято"), reply_markup=ReplyKeyboardRemove())
                s.ui["reply_kb"] = False
            elif s.ui.get("msg_id"):
                try:
                    if note:
                        await self.bot.edit_message_text(
                            chat_id=chat_id, message_id=s.ui["msg_id"],
                            text=s.ui.get("msg_text", "") + f"\n\n<i>✓ {html.escape(note)}</i>", reply_markup=None)
                    else:
                        await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=s.ui["msg_id"],
                                                                 reply_markup=None)
                except TelegramBadRequest:
                    pass
            s.ui["msg_id"] = None

        if len(view.images) == 1:
            await self.bot.send_photo(chat_id, view.images[0])
        elif view.images:
            await self.bot.send_media_group(chat_id, [InputMediaPhoto(media=u) for u in view.images[:10]])

        if view.contact:
            keyboard = [[KeyboardButton(text=self.sc.setting('Кнопка "Отправить номер"'), request_contact=True)]]
            if view.back_label:
                keyboard.append([KeyboardButton(text=view.back_label)])
            markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)
        else:
            markup = inline(view.buttons)
        try:
            msg = await self.bot.send_message(chat_id, view.text, reply_markup=markup)
        except TelegramBadRequest:
            if view.contact or not ICONS["enabled"] or not has_icons(view.buttons):
                raise
            log.warning("Telegram отклонил иконки на кнопках (нужен Premium у владельца бота), отключаю их")
            ICONS["enabled"] = False
            msg = await self.bot.send_message(chat_id, view.text, reply_markup=inline(view.buttons))
        if view.step:
            s.ui["msg_id"] = None if view.contact else msg.message_id
            s.ui["msg_text"] = view.text
            s.ui["reply_kb"] = view.contact

    async def _send_lead(self, lead: Lead, user):
        lead_id = self.store.add_lead(user.id, user.username or "", lead.fields)
        chat = self.lead_chat()
        if not chat:
            log.warning('Заявка №%s сохранена, но "Чат для заявок" не указан в настройках', lead_id)
            return
        body = "\n".join(f"<b>{html.escape(k)}:</b> {html.escape(v)}" for k, v in lead.fields)
        text = f"<b>Новая заявка №{lead_id}</b>\nКлиент: {user_title(user)}\n\n{body}"
        text += "\n\n<i>Ответьте на это сообщение, чтобы написать клиенту через бота.</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Взять в работу",
                                                                         callback_data=f"take:{lead_id}")]])
        msg = await self.bot.send_message(chat, text[:4096], reply_markup=kb)
        self.store.link(chat, msg.message_id, user.id)
        for f in lead.files:
            sent = await (self.bot.send_photo(chat, f["file_id"], caption=f"К заявке №{lead_id}")
                          if f["kind"] == "photo" else
                          self.bot.send_document(chat, f["file_id"], caption=f"К заявке №{lead_id}"))
            self.store.link(chat, sent.message_id, user.id)

    async def _send_notify(self, n: Notify, user):
        chat = self.lead_chat()
        if not chat:
            return
        text = f"{html.escape(n.title)}\nОт: {user_title(user)}\n\n{html.escape(n.text)}" \
               "\n\n<i>Ответьте на это сообщение, чтобы написать клиенту.</i>"
        if n.file:
            send = self.bot.send_photo if n.file["kind"] == "photo" else self.bot.send_document
            msg = await send(chat, n.file["file_id"], caption=text[:1024])
        else:
            msg = await self.bot.send_message(chat, text[:4096])
        self.store.link(chat, msg.message_id, user.id)

    # ---------- обработчики ----------

    async def handle(self, user, chat_id: int, action, cq: CallbackQuery | None = None):
        async with self.locks[user.id]:
            s = self.store.session(user.id)
            outs = action(s)
            touch(s)
            await self.show(chat_id, s, outs, cq, user)
            self.store.save(user.id, s)

    def _routes(self):
        r = self.router

        @r.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
        async def start(m: Message):
            await self.handle(m.from_user, m.chat.id, self.engine.start)

        @r.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
        async def menu(m: Message):
            await self.handle(m.from_user, m.chat.id, self.engine.main_menu)

        @r.message(Command("id"))
        async def my_id(m: Message):
            await m.answer(f"ID этого чата: <code>{m.chat.id}</code>\nВаш ID: <code>{m.from_user.id}</code>\n\n"
                           'Вставьте нужный ID в лист "Настройки" таблицы.')

        @r.message(Command("reload"))
        async def reload(m: Message):
            if not self.is_admin(m):
                return
            errors = await self.reload()
            if errors:
                await m.answer("Таблица не загружена, бот работает по прошлой версии.\n\n"
                               + html.escape("\n".join("• " + e for e in errors[:25])))
            else:
                await m.answer(f"Сценарий обновлён: шагов {len(self.sc.steps)}, веток {len(self.sc.routes)}")

        @r.message(Command("leads"))
        async def leads(m: Message):
            if not self.is_admin(m):
                return
            rows = self.store.leads()
            if not rows:
                await m.answer("Заявок пока нет")
                return
            path = leads_xlsx(rows, f"data/заявки_{time.strftime('%Y-%m-%d')}.xlsx")
            await m.answer_document(FSInputFile(path), caption=f"Всего заявок: {len(rows)}")

        @r.callback_query(F.data.startswith("take:"))
        async def take(cq: CallbackQuery):
            lead_id = int(cq.data.split(":")[1])
            who = cq.from_user.full_name
            if self.store.take_lead(lead_id, who):
                await cq.message.edit_text(cq.message.html_text + f"\n\n<b>В работе:</b> {html.escape(who)}, "
                                           f"{time.strftime('%d.%m %H:%M')}", reply_markup=None)
                await cq.answer("Заявка ваша")
            else:
                await cq.answer("Заявку уже взяли", show_alert=True)

        @r.callback_query(F.message.chat.type == ChatType.PRIVATE)
        async def button(cq: CallbackQuery):
            await self.handle(cq.from_user, cq.message.chat.id, lambda s: self.engine.callback(s, cq.data), cq)

        @r.message(F.reply_to_message)
        async def manager_reply(m: Message, *rest):
            """Ответ менеджера на заявку или вопрос → клиенту."""
            user_id = self.store.linked_user(m.chat.id, m.reply_to_message.message_id)
            if m.chat.id != self.lead_chat() or not user_id:
                return await on_message(m)
            try:
                header = f"<b>{html.escape(self.sc.setting('Подпись к ответу специалиста'))}</b>"
                if m.text:
                    await self.bot.send_message(user_id, f"{header}\n{m.html_text}")
                elif m.caption is not None or m.photo or m.document or m.video:
                    await m.copy_to(user_id, caption=f"{header}\n{m.html_text}" if m.caption else header)
                else:  # стикер, голосовое и т.п. без подписи
                    await self.bot.send_message(user_id, header)
                    await m.copy_to(user_id)
                await m.reply("Отправлено клиенту")
            except TelegramForbiddenError:
                await m.reply("Клиент заблокировал бота, сообщение не доставлено")

        @r.message(F.chat.type == ChatType.PRIVATE)
        async def on_message(m: Message):
            if m.chat.type != ChatType.PRIVATE:
                return
            icons = [e.custom_emoji_id for e in (m.entities or []) if e.type == "custom_emoji"]
            if icons and self.is_admin(m):  # админ прислал кастомный эмодзи → подсказываем ID для колонки "Иконка"
                await m.answer("ID для колонки \"Иконка\":\n" + "\n".join(f"<code>{i}</code>" for i in icons))
                return
            if m.contact:
                action = lambda s: self.engine.contact(s, m.contact.phone_number)  # noqa: E731
            elif m.document:
                f = {"kind": "document", "file_id": m.document.file_id, "name": m.document.file_name or "файл"}
                action = lambda s: self.engine.file(s, f)  # noqa: E731
            elif m.photo:
                f = {"kind": "photo", "file_id": m.photo[-1].file_id, "name": "фото"}
                action = lambda s: self.engine.file(s, f)  # noqa: E731
            elif m.text:
                action = lambda s: self.engine.text(s, m.text)  # noqa: E731
            else:
                return
            await self.handle(m.from_user, m.chat.id, action)

    # ---------- фоновые задачи ----------

    async def remind_loop(self):
        while True:
            await asyncio.sleep(60)
            minutes = self.sc.setting_int("Напоминание через, минут (0 = выключено)")
            if minutes <= 0:
                continue
            for user_id in self.store.to_remind(time.time() - minutes * 60):
                async with self.locks[user_id]:
                    s = self.store.session(user_id)
                    if not s.active or s.reminded:
                        continue
                    s.reminded = True
                    try:
                        await self._send_view(user_id, s, self.engine.reminder(s), None)
                    except (TelegramForbiddenError, TelegramBadRequest) as e:
                        log.info("напоминание %s не доставлено: %s", user_id, e)
                    self.store.save(user_id, s)

    async def reload_loop(self):
        while True:
            await asyncio.sleep(max(1, self.sc.setting_int("Обновлять сценарий каждые, минут")) * 60)
            errors = await self.reload()
            text = "\n".join(errors)
            if errors and text != self.last_error and self.lead_chat():
                try:
                    await self.bot.send_message(
                        self.lead_chat(), "В таблице сценария ошибки, бот работает по прошлой версии:\n\n"
                        + html.escape("\n".join("• " + e for e in errors[:25])))
                except TelegramBadRequest:
                    pass
            self.last_error = text

    async def run(self):
        errors = await self.reload()
        if errors:
            raise SystemExit("Сценарий не загружен:\n" + "\n".join(errors))
        log.info("Сценарий загружен: %d шагов", len(self.sc.steps))
        dp = Dispatcher()
        dp.include_router(self.router)
        await self.bot.set_my_commands([BotCommand(command="start", description="Начать заново"),
                                        BotCommand(command="menu", description="Главное меню")])
        asyncio.create_task(self.remind_loop())
        asyncio.create_task(self.reload_loop())
        await dp.start_polling(self.bot)
