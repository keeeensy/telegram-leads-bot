"""Движок диалога: принимает действия клиента и возвращает, что показать.

Ничего не знает о Telegram, поэтому все ветки сценария проверяются тестами.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import asdict, dataclass, field

from .scenario import (BACK, CHOICE, COLORS, FILE, FINAL, MENU, MESSAGE, MULTI, PHONE, TEXT, WITH_OPTIONS, Scenario,
                       Step)


# ---------- что движок просит показать ----------

@dataclass
class Button:
    text: str
    data: str = ""   # callback
    url: str = ""
    style: str = ""  # цвет: success / primary / danger
    icon: str = ""   # ID кастомного эмодзи перед текстом


@dataclass
class View:
    """Новое сообщение бота."""
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    contact: bool = False        # клавиатура с кнопкой "Отправить номер"
    back_label: str = ""         # для клавиатуры телефона
    step: bool = False           # это шаг сценария (а не подсказка): у прошлого шага убираем кнопки


@dataclass
class EditButtons:
    """Обновить кнопки текущего сообщения (отметки в мультивыборе)."""
    buttons: list[list[Button]]


@dataclass
class Answered:
    """Подпись к предыдущему сообщению: что выбрал клиент."""
    text: str


@dataclass
class Toast:
    """Короткое уведомление (всплывашка при нажатии кнопки)."""
    text: str


@dataclass
class Lead:
    fields: list[tuple[str, str]]
    files: list[dict]


@dataclass
class Notify:
    """Сообщение менеджеру (вопрос клиента, ответ с пометкой "сразу сообщить")."""
    title: str
    text: str
    file: dict | None = None


# ---------- состояние клиента ----------

@dataclass
class Session:
    step: str = ""
    branch: str = ""
    history: list = field(default_factory=list)   # [[шаг, ветка], ...], последний = текущий
    answers: dict = field(default_factory=dict)
    draft: list = field(default_factory=list)     # отмеченные варианты в мультивыборе
    pending: list = field(default_factory=list)   # варианты, для которых ждём ввод текста
    seq: int = 0                                  # номер показа, защищает от нажатий на старые кнопки
    active: bool = False                          # клиент в процессе заполнения заявки
    reminded: bool = False
    updated: float = 0.0
    ui: dict = field(default_factory=dict)        # данные Telegram-слоя (id последнего сообщения и т.п.)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def fmt(text: str) -> str:
    """Текст из таблицы → HTML для Telegram: экранирование и **жирный**."""
    text = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)


PHONE_RE = re.compile(r"\D")
URL_RE = re.compile(r"^(https?://)?[\w.-]+\.[a-zа-яё]{2,}(/\S*)?$", re.I)
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def normalize_phone(raw: str) -> str | None:
    digits = PHONE_RE.sub("", raw)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else None


class Engine:
    def __init__(self, scenario: Scenario):
        self.sc = scenario

    # ---------- публичные действия ----------

    def start(self, s: Session) -> list:
        self._reset(s)
        return self._goto(s, self.sc.start)

    def main_menu(self, s: Session) -> list:
        self._reset(s)
        return self._goto(s, self.sc.main)

    def resume(self, s: Session) -> list:
        """Показать текущий шаг заново (после напоминания или перезапуска)."""
        step = self.sc.steps.get(s.step)
        if not step:
            return self.main_menu(s)
        if s.pending:
            return [self._prompt_view(s, step)]
        return [self._render(s, step)]

    def callback(self, s: Session, data: str) -> list:
        m = re.fullmatch(r"([obdcm])(\d+)(?::(\d+))?", data)
        if not m:
            return []
        kind, seq, idx = m.group(1), int(m.group(2)), m.group(3)
        if seq != s.seq:
            return [Toast("Эта кнопка уже неактуальна, используйте кнопки последнего сообщения")]
        step = self.sc.steps.get(s.step)
        if kind == "m" or not step:
            return self.main_menu(s)
        if kind == "c":
            return self.resume(s)
        if kind == "b":
            return self.back(s)
        if kind == "d":
            return self._done(s, step)
        idx = int(idx)
        if idx >= len(step.options) or s.pending:
            return []
        if step.type == MULTI:
            return self._toggle(s, step, idx)
        return self._choose(s, step, idx)

    def back(self, s: Session) -> list:
        step = self.sc.steps.get(s.step)
        if step and s.pending:  # отмена ввода "Иное" - снова показываем вопрос
            s.pending = []
            return [self._render(s, step)]
        if len(s.history) < 2:
            return self.main_menu(s)
        s.history.pop()
        s.step, s.branch = s.history[-1]
        s.pending = []
        prev = self.sc.steps.get(s.step)
        if not prev:
            return self.main_menu(s)
        s.draft = self._saved_choices(s, prev)
        return [self._render(s, prev)]

    def text(self, s: Session, text: str) -> list:
        text = text.strip()
        step = self.sc.steps.get(s.step)
        if not step:
            return self.start(s)
        if text == self.sc.setting('Кнопка "Назад"'):
            return self.back(s)

        if s.pending:
            opt = step.options[s.pending[0]]
            s.answers.setdefault(step.id, {"choices": [opt.text]}).setdefault("extra", {})[opt.text] = text
            s.pending.pop(0)
            if s.pending:
                return [self._prompt_view(s, step)]
            return self._notify(s, step) + self._after_choice(s, step)

        if step.type == TEXT:
            error = self._check(step, text)
            if error:
                return [View(fmt(error))]
            s.answers[step.id] = {"text": text}
            return self._notify(s, step) + self._goto(s, self._next(s, step))

        if step.type == PHONE:
            phone = normalize_phone(text)
            if not phone:
                return [View(fmt(self.sc.setting('Сообщение "неверный телефон"')))]
            return self._phone(s, step, phone)

        if step.type == FINAL:
            return [Notify("Вопрос от клиента", text), View(fmt(self.sc.setting("Ответ на вопрос клиента")))]

        if step.type == FILE:
            return [View(fmt(self.sc.setting('Сообщение "нужен файл"')))]
        return [View(fmt(self.sc.setting('Сообщение "выберите кнопку"')))]

    def contact(self, s: Session, phone: str) -> list:
        step = self.sc.steps.get(s.step)
        if step and step.type == PHONE and not s.pending:
            return self._phone(s, step, normalize_phone(phone) or phone)
        return self.text(s, phone)

    def file(self, s: Session, file: dict) -> list:
        """file = {"kind": "document"|"photo", "file_id": ..., "name": ...}"""
        step = self.sc.steps.get(s.step)
        if not step:
            return self.start(s)
        if step.type == FILE and not s.pending:
            s.answers[step.id] = {"text": file.get("name") or "файл", "file": file}
            return [Answered(file.get("name") or "файл получен")] + self._goto(s, self._next(s, step))
        if step.type == FINAL:
            return [Notify("Файл от клиента", file.get("name", ""), file),
                    View(fmt(self.sc.setting("Ответ на вопрос клиента")))]
        return [View(fmt(self.sc.setting('Сообщение "файл не нужен"')))]

    def reminder(self, s: Session) -> View:
        s.seq += 1
        return View(fmt(self.sc.setting("Текст напоминания")), [
            [Button(self.sc.setting('Кнопка "Продолжить" в напоминании'), f"c{s.seq}")],
            [Button(self.sc.setting('Кнопка "В главное меню"'), f"m{s.seq}")],
        ], step=True)

    # ---------- переходы ----------

    def _reset(self, s: Session):
        s.step, s.branch, s.history, s.answers = "", "", [], {}
        s.draft, s.pending, s.active, s.reminded = [], [], False, False

    def _next(self, s: Session, step: Step) -> str:
        """Куда идти после шага: явный "Следующий шаг" или следующий шаг текущей ветки."""
        if step.next:
            return step.next
        route = self.sc.routes.get(s.branch, [])
        # Шаги вне маршрута (файл ТЗ, "Спасибо" в разделах) продолжают ветку от последнего шага маршрута
        for sid, _ in reversed(s.history):
            if sid in route:
                i = route.index(sid)
                return route[i + 1] if i + 1 < len(route) else ""
        return ""

    def _goto(self, s: Session, target: str, outs: list | None = None) -> list:
        outs = outs if outs is not None else []
        for _ in range(30):  # защита от зацикливания сообщений в кривой таблице
            if target == BACK:
                if len(s.history) >= 2:
                    target, s.branch = s.history[-2]
                else:
                    target = self.sc.main
            if target in self.sc.routes:
                s.branch = target
                target = self.sc.routes[target][0]
                continue
            step = self.sc.steps.get(target) or self.sc.steps[self.sc.main]
            if step.type == MESSAGE:
                images = step.images
                outs.append(View(self._text(s, step), images=images))
                target = self._next(s, step) or self.sc.main
                continue
            break
        else:
            step = self.sc.steps[self.sc.main]

        ids = [h[0] for h in s.history]
        if step.id in ids:  # уже были на этом шаге (возврат в меню разделов) - без петель в истории
            i = ids.index(step.id)
            del s.history[i + 1:]
            s.branch = s.history[i][1]
        else:
            s.history.append([step.id, s.branch])
        s.step = step.id
        s.pending = []
        s.draft = self._saved_choices(s, step)
        s.active = bool(s.branch) and step.type != FINAL

        if step.type == FINAL:
            text = self._text(s, step)
            outs.append(self._lead(s))
            self._reset(s)
            s.step, s.history = step.id, [[step.id, ""]]
            outs.append(self._render(s, step, text))
        else:
            outs.append(self._render(s, step))
        return outs

    def _choose(self, s: Session, step: Step, idx: int) -> list:
        opt = step.options[idx]
        if step.type in (CHOICE, TEXT) or (step.type == MENU and not opt.goto):
            s.answers[step.id] = {"choices": [opt.text]}
        if step.type == FINAL:
            return self._goto(s, opt.goto or self.sc.main)
        if opt.prompt:
            s.pending = [idx]
            return [Answered(opt.text), self._prompt_view(s, step)]
        if step.type == MENU:
            target = self.sc.steps.get(opt.goto)
            if target and target.next == BACK:  # вход в раздел меню - без подписи, вернёмся сюда же
                return self._goto(s, opt.goto)
            return [Answered(opt.text)] + self._goto(s, opt.goto or self._next(s, step) or self.sc.main)
        notify = self._notify(s, step) if step.type != TEXT else []
        return [Answered(opt.text)] + notify + self._after_choice(s, step)

    def _toggle(self, s: Session, step: Step, idx: int) -> list:
        if idx in s.draft:
            s.draft.remove(idx)
        elif step.options[idx].exclusive:
            s.draft = [idx]
        else:
            s.draft = [i for i in s.draft if not step.options[i].exclusive] + [idx]
        return [EditButtons(self._buttons(s, step))]

    def _done(self, s: Session, step: Step) -> list:
        if step.type != MULTI or s.pending:
            return []
        if not s.draft:
            return [Toast(self.sc.setting('Сообщение "выберите хотя бы один"'))]
        chosen = sorted(s.draft)
        s.answers[step.id] = {"choices": [step.options[i].text for i in chosen]}
        s.pending = [i for i in chosen if step.options[i].prompt]
        outs = [Answered(", ".join(step.options[i].text for i in chosen))]
        if s.pending:
            return outs + [self._prompt_view(s, step)]
        return outs + self._notify(s, step) + self._after_choice(s, step)

    def _after_choice(self, s: Session, step: Step) -> list:
        """Переход после ответа. В мультивыборе кнопка с переходом срабатывает,
        только если все отмеченные варианты имеют переход (иначе - дальше по маршруту)."""
        chosen = [o for o in step.options if o.text in s.answers.get(step.id, {}).get("choices", [])]
        gotos = [o.goto for o in chosen]
        target = gotos[0] if gotos and all(gotos) else self._next(s, step)
        return self._goto(s, target or self.sc.main)

    def _phone(self, s: Session, step: Step, phone: str) -> list:
        s.answers[step.id] = {"text": phone}
        return [Answered(phone)] + self._goto(s, self._next(s, step))

    def _check(self, step: Step, text: str) -> str:
        if step.check == "ссылка" and not URL_RE.match(text):
            return self.sc.setting('Сообщение "неверная ссылка"')
        if step.check == "email" and not EMAIL_RE.match(text):
            return self.sc.setting('Сообщение "неверный email"')
        return ""

    def _notify(self, s: Session, step: Step) -> list:
        if not step.notify:
            return []
        return [Notify(step.label or "Сообщение", self._answer_text(s, step))]

    # ---------- отображение ----------

    def _text(self, s: Session, step: Step) -> str:
        def sub(m):
            ref = self.sc.steps.get(m.group(1))
            return self._answer_text(s, ref) if ref and s.answers.get(ref.id) else ""
        return fmt(re.sub(r"\{([^{}\s]+)\}", sub, step.text)).replace(", !", "!")

    def _render(self, s: Session, step: Step, text: str | None = None) -> View:
        s.seq += 1
        view = View(text or self._text(s, step), self._buttons(s, step), images=step.images, step=True)
        if step.type == PHONE:
            view.contact = True
            view.back_label = self.sc.setting('Кнопка "Назад"') if self._can_back(s, step) else ""
        return view

    def _prompt_view(self, s: Session, step: Step) -> View:
        s.seq += 1
        opt = step.options[s.pending[0]]
        return View(fmt(opt.prompt), [[Button(self.sc.setting('Кнопка "Назад"'), f"b{s.seq}")]], step=True)

    def _can_back(self, s: Session, step: Step) -> bool:
        return step.back and len(s.history) >= 2

    def _buttons(self, s: Session, step: Step) -> list[list[Button]]:
        rows: list[list[Button]] = []
        if step.type == PHONE:
            return rows
        for i, opt in enumerate(step.options):
            label = opt.text
            if step.type == MULTI and i in s.draft:
                label = "✓ " + label
            if step.type == MENU and opt.goto in self.sc.steps and s.answers.get(opt.goto):
                label = "✓ " + label
            data = "" if opt.url else f"o{s.seq}:{i}"
            rows.append([Button(label, data, opt.url, COLORS.get(opt.color, ""), opt.icon)])
        rows = self._pack(rows)
        if step.type == MULTI:
            rows.append([Button(step.done_label or self.sc.setting('Кнопка "Готово" по умолчанию'), f"d{s.seq}",
                                style=COLORS.get(self.sc.setting('Цвет кнопки "Готово"').lower(), ""))])
        if self._can_back(s, step):
            rows.append([Button(self.sc.setting('Кнопка "Назад"'), f"b{s.seq}")])
        return rows

    @staticmethod
    def _pack(rows):
        """Короткие кнопки (Да/Нет, Цвет/Размер) - по две в ряд."""
        if rows and all(len(r[0].text) <= 14 for r in rows):
            return [sum(rows[i:i + 2], []) for i in range(0, len(rows), 2)]
        return rows

    def _saved_choices(self, s: Session, step: Step) -> list:
        if step.type != MULTI:
            return []
        saved = s.answers.get(step.id, {}).get("choices", [])
        return [i for i, o in enumerate(step.options) if o.text in saved]

    # ---------- заявка ----------

    def _answer_text(self, s: Session, step: Step) -> str:
        a = s.answers.get(step.id) or {}
        if "choices" in a:
            extra = a.get("extra", {})
            return ", ".join(f"{c}: {extra[c]}" if c in extra else c for c in a["choices"])
        return a.get("text", "")

    def _lead(self, s: Session) -> Lead:
        fields, files, seen = [], [], set()

        def add(step: Step):
            if step.id in seen or not s.answers.get(step.id):
                return
            seen.add(step.id)
            a = s.answers[step.id]
            if a.get("file"):
                files.append(a["file"])
            if step.label:
                fields.append((step.label, self._answer_text(s, step)))

        for sid, _ in s.history:
            step = self.sc.steps.get(sid)
            if not step:
                continue
            add(step)
            if step.type == MENU:  # ответы внутри разделов меню
                for opt in step.options:
                    if opt.goto in self.sc.steps:
                        add(self.sc.steps[opt.goto])
        return Lead(fields, files)


def touch(s: Session):
    s.updated = time.time()
    s.reminded = False
