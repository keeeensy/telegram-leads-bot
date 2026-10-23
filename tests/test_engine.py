import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.engine import Answered, EditButtons, Engine, Lead, Notify, Session, Toast, View  # noqa: E402
from bot.scenario import BACK, ScenarioError, parse_workbook  # noqa: E402
from tools.build_scenario import build  # noqa: E402


@pytest.fixture(scope="session")
def scenario_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("sc") / "scenario.xlsx"
    build(path)
    return path


@pytest.fixture(scope="session")
def sc(scenario_path):
    return parse_workbook(str(scenario_path))


class Chat:
    """Имитация клиента: жмёт кнопки по тексту, пишет сообщения, копит всё, что прислал бот."""

    def __init__(self, sc):
        self.engine = Engine(sc)
        self.s = Session()
        self.outs = []
        self.view = None
        self.leads, self.notifies, self.toasts = [], [], []

    def _apply(self, outs):
        self.outs = outs
        for o in outs:
            if isinstance(o, View) and o.step:
                self.view = o
            elif isinstance(o, EditButtons):
                self.view.buttons = o.buttons
            elif isinstance(o, Lead):
                self.leads.append(o)
            elif isinstance(o, Notify):
                self.notifies.append(o)
            elif isinstance(o, Toast):
                self.toasts.append(o.text)
        return self

    def start(self):
        return self._apply(self.engine.start(self.s))

    def buttons(self):
        return [b for row in self.view.buttons for b in row]

    def click(self, *labels):
        for label in labels:
            found = [b for b in self.buttons() if label in b.text and b.data]
            assert found, f"нет кнопки \"{label}\" на шаге {self.s.step}: {[b.text for b in self.buttons()]}"
            self._apply(self.engine.callback(self.s, found[0].data))
        return self

    def say(self, text):
        return self._apply(self.engine.text(self.s, text))

    def file(self, name="tz.pdf"):
        return self._apply(self.engine.file(self.s, {"kind": "document", "file_id": "F1", "name": name}))

    def at(self, step):
        assert self.s.step == step, f"ожидали шаг {step}, сейчас {self.s.step}"
        return self

    def contacts(self, name="Иван"):
        self.at("имя").say(name).at("телефон").say("8 (999) 123-45-67").at("мессенджер")
        self.click("Telegram", "Продолжить").at("время").click("Вечер")
        return self.at("спасибо")

    def lead(self):
        assert len(self.leads) == 1
        return dict(self.leads[0].fields)


def sections(chat):
    chat.click("Основные страницы", "Контакты", "Акции", "Сохранить")
    chat.click("Услуги и товары", "Сертификаты", "Сохранить")
    chat.click("Правовая", "Договор-оферта", "Сохранить")
    chat.click("Иное").say("Калькулятор доставки")
    assert any(b.text.startswith("✓ ") and "Основные" in b.text for b in chat.buttons())
    chat.click("Готово")


def test_scenario_valid(sc):
    assert len(sc.steps) > 50 and "финал" in sc.routes


def test_every_step_reachable(sc):
    seen, todo = set(), [sc.start]
    while todo:
        t = todo.pop()
        if t in seen or t == BACK:
            continue
        seen.add(t)
        if t in sc.routes:
            todo += sc.routes[t]
            continue
        st = sc.steps[t]
        todo += [o.goto for o in st.options if o.goto] + ([st.next] if st.next else [])
    assert set(sc.steps) - seen == set()


def test_privacy_then_menu(sc):
    c = Chat(sc).start().at("приватность")
    policy = [b for b in c.buttons() if "Политика" in b.text][0]
    assert policy.url.startswith("https://")
    c.click("Начать").at("главное_меню")
    assert "Назад" not in [b.text for b in c.buttons()][-1]


def test_products_full_branch(sc):
    c = Chat(sc).start().click("Начать", "Заказать новый сайт").at("новый_сайт").click("Товары").at("оплата")
    c.click("Да").at("доставка").click("Нет").at("склад").click("Да").at("интеграции").click("Нет")
    c.at("функционал").click("Поиск", "Квиз", "Другое", "Готово")
    c.say("Сравнение товаров").at("вариации")
    c.click("Цвет", "Нет")  # "Нет" снимает остальные отметки
    assert [b.text for b in c.buttons() if b.text.startswith("✓ ") and b.data.startswith("o")] == ["✓ Нет"]
    c.click("Размер", "Иное", "Готово").say("Вкус").at("кол_товаров").click("100-1 000")
    c.at("инфо_товары").click("Заполню Excel").at("разделы")
    sections(c)
    c.at("дизайн").click("Шаблон").at("пример_сайта").click("Есть подробное ТЗ").at("тз_файл")
    c.say("вот ссылка").at("тз_файл")  # текст вместо файла - просим файл
    c.file("ТЗ.pdf").at("имя")
    c.contacts()
    lead = c.lead()
    assert lead["Что на сайте"] == "Товары"
    assert lead["Функционал"] == "Поиск, Квиз, Другое (ввести вручную): Сравнение товаров"
    assert lead["Вариации товаров"] == "Размер, Иное (введу): Вкус"
    assert lead["Основные страницы"] == "Контакты, Акции" and lead["Услуги и товары"] == "Сертификаты"
    assert lead["Правовая информация"] == "Договор-оферта"
    assert lead["Другие страницы"] == "Калькулятор доставки"
    assert lead["Телефон"] == "+79991234567"
    assert lead["Файл ТЗ"] == "ТЗ.pdf" and c.leads[0].files[0]["file_id"] == "F1"
    assert "Иван" in c.view.text
    # после заявки диалог сброшен, кнопки финала работают
    assert not c.s.active and c.s.answers == {}
    c.click("главное меню").at("главное_меню")


def test_services_branch(sc):
    c = Chat(sc).start().click("Начать", "Заказать новый сайт", "Услуги").at("сфера")
    c.click("Иное").say("Ветеринария").at("структура").click("Одностраничный")
    c.at("наполнение").click("уникальные тексты", "Иное", "Готово").say("Есть фото в Google Диске")
    c.at("оплата").click("Нет").at("интеграции").click("Да").at("попапы")
    c.click("текстами/ценами").say("3 окна с ценами").at("разделы_у")
    labels = [b.text for b in c.buttons()]
    assert "Услуги" in labels and not any("товары" in x.lower() for x in labels)
    c.click("Услуги", "Вакансии", "Сохранить", "Готово").at("пример_сайта")
    c.click("Да, есть").say("site.ru, нравятся цвета").at("имя").contacts()
    lead = c.lead()
    assert lead["Сфера"] == "Иное: Ветеринария"
    assert lead["Всплывающие окна"].endswith(": 3 окна с ценами")
    assert lead["Услуги"] == "Вакансии"
    assert lead["Пример сайта"] == "Да, есть: site.ru, нравятся цвета"
    assert "Расчёт доставки" not in lead


def test_products_and_services_branch(sc):
    c = Chat(sc).start().click("Начать", "Заказать новый сайт", "Товары и услуги").at("приоритет")
    c.click("поровну").at("сфера").click("Автомобили").at("оплата").click("Да").at("доставка").click("Да")
    c.at("склад").click("Нет").at("интеграции").click("Нет").at("функционал").click("Личный", "Готово")
    c.at("вариации").click("Нет", "Готово").at("кол_товаров").click("> 10 000").at("инфо_товары")
    c.click("Нужно парсить").at("разделы").click("Готово").at("дизайн").click("Полный").at("потоки")
    c.click("две разные").at("пример_сайта").click("Нет, нужна помощь").contacts()
    assert c.lead()["Разные потоки"] == "Да, две разные страницы"


def test_other_branch(sc):
    c = Chat(sc).start().click("Начать", "Заказать новый сайт", "Иное").say("Сайт-визитка для фотографа")
    c.at("структура")
    c.click("Многостраничный").at("наполнение").click("Есть все", "Готово").at("оплата")


def test_back_navigation(sc):
    c = Chat(sc).start().click("Начать", "Заказать новый сайт", "Товары").at("оплата")
    c.click("Назад").at("новый_сайт").click("Назад").at("главное_меню")
    c.click("Заказать новый сайт", "Услуги", "Ремонт").at("структура").click("Назад").at("сфера")
    # "Назад" во время ввода "Иное" возвращает к вопросу
    c.click("Иное").click("Назад").at("сфера")
    assert c.s.pending == []
    # мультивыбор помнит отметки после возврата
    c.click("Ремонт", "Многостраничный").click("Нужны уникальные", "Готово").at("оплата").click("Назад")
    c.at("наполнение")
    assert any(b.text == "✓ Нужны уникальные тексты" for b in c.buttons())


def test_back_from_contacts_returns_to_branch(sc):
    c = Chat(sc).start().click("Начать", "Доработать текущий сайт").at("др_платформа")
    c.click("Tilda", "Улучшить визуал", "Готово").at("др_ссылка").say("my-site.ru").at("др_пересборка")
    c.click("только доработки").at("др_продвижение").click("Нет, пока", "Готово").at("др_доступы")
    c.click("все доступы").at("имя").click("Назад").at("др_доступы").click("все доступы").at("имя")
    assert c.s.branch == "финал"


def test_link_validation(sc):
    c = Chat(sc).start().click("Начать", "Доработать текущий сайт", "WordPress", "Исправить баги", "Готово")
    c.at("др_ссылка").say("не знаю").at("др_ссылка")
    assert "не ссылка" in c.outs[-1].text
    c.say("https://example.com/page").at("др_пересборка")
    c.click("Назад").click("Не помню ссылку").at("др_пересборка")


def test_phone_validation(sc):
    c = Chat(sc).start().click("Начать", "Доработать текущий сайт", "Joomla", "Перенос", "Готово")
    c.click("Не помню ссылку", "Да, можно", "Да, SEO", "Готово", "Нет никаких").at("имя").say("Оля")
    c.say("123").at("телефон")
    assert "номер" in c.outs[-1].text
    assert c.view.contact
    c.say("Назад").at("имя")
    c.say("Оля").at("телефон")._apply(c.engine.contact(c.s, "79001112233")).at("мессенджер")


def test_promotion_never_skips_to_final(sc):
    c = Chat(sc).start().click("Начать", "Продвижение сайта").at("пр_что")
    c.click("Сайт", "Соцсети", "Готово").say("site.ru").say("vk.com/club").at("пр_типы")
    c.click("SEO", "Готово").at("пр_сейчас").click("Никогда не продвигали", "Готово").at("имя")
    c.contacts()
    lead = c.lead()
    assert lead["Что продвигать"] == "Сайт: site.ru, Соцсети: vk.com/club"
    assert "Кто помогал" not in lead


def test_promotion_full(sc):
    c = Chat(sc).start().click("Начать", "Продвижение сайта", "Пока нет сайта", "Готово")
    c.click("Не знаю", "Готово").at("пр_сейчас")
    c.click("платную рекламу", "соцсети", "Готово").say("Директ").at("пр_помощь")
    c.click("сотрудники Яндекса", "Готово").at("пр_формат").click("Только разовая").at("пр_гео")
    c.click("Вся Россия").at("пр_цели").click("Звонки").at("пр_ссылка").click("Сайта пока нет").at("имя")
    c.contacts()
    lead = c.lead()
    assert lead["Текущее продвижение"] == "Да, через платную рекламу: Директ, Да, через соцсети"
    assert lead["Сайт для анализа"] == "Сайта пока нет"


def test_mixed_multi_choice_follows_route(sc):
    """Отмечено и "платная реклама", и вариант с переходом в финал → вопрос "кто помогал" показывается."""
    c = Chat(sc).start().click("Начать", "Продвижение сайта", "Сайт", "Готово").say("a.ru")
    c.click("SEO", "Готово").click("SEO-продвижение", "Затрудняюсь")
    # "Затрудняюсь" снимает другие → только он → финал
    c.click("Готово").at("имя")


def test_examples_and_order(sc):
    for sphere in ("Строительство", "Ремонтные", "Производство", "Автомобили", "Специалисты", "Еда", "Спорт"):
        c = Chat(sc).start().click("Начать", "Примеры наших работ", sphere)
        assert "Сайт №1" in c.view.text
        c.click("Назад к списку сфер").at("примеры").click(sphere).click("Заказать услугу").at("заказ_выбор")
        c.click("Продвижение сайта").at("пр_что")
    sport = Chat(sc).start().click("Начать", "Примеры наших работ", "Спорт")
    assert "спорта" in sport.view.text and "кулинар" not in sport.view.text


def test_examples_other_notifies_manager(sc):
    c = Chat(sc).start().click("Начать", "Примеры наших работ", "Иное").say("Сайт для ветклиники")
    c.at("пример_иное_ок")
    assert c.notifies and "ветклиники" in c.notifies[0].text


def test_contacts_question(sc):
    c = Chat(sc).start().click("Начать", "Связаться с нами").at("контакты")
    assert "site-it@mail.ru" in c.view.text
    c.click("Задать вопрос").say("Сколько стоит лендинг?").at("вопрос_ок")
    assert c.notifies[0].text == "Сколько стоит лендинг?"
    c.click("главное меню").at("главное_меню")


def test_text_after_final_goes_to_manager(sc):
    c = Chat(sc).start().click("Начать", "Продвижение сайта", "Не знаю", "Готово", "SEO", "Готово")
    c.click("Затрудняюсь", "Готово").contacts()
    c.say("А сколько по времени?")
    assert c.notifies[-1].text == "А сколько по времени?"


def test_typing_on_button_step_hints(sc):
    c = Chat(sc).start().click("Начать").say("привет").at("главное_меню")
    assert "кнопок" in c.outs[-1].text


def test_stale_button(sc):
    c = Chat(sc).start().click("Начать")
    old = c.view
    c.click("Заказать новый сайт")
    c._apply(c.engine.callback(c.s, old.buttons[1][0].data))
    assert c.toasts and c.s.step == "новый_сайт"


def test_multi_requires_choice(sc):
    c = Chat(sc).start().click("Начать", "Продвижение сайта", "Готово")
    assert c.toasts and c.s.step == "пр_что"


def test_reminder_resume(sc):
    c = Chat(sc).start().click("Начать", "Заказать новый сайт", "Товары", "Да")
    assert c.s.active
    v = c.engine.reminder(c.s)
    c.view = v
    c.click("Продолжить").at("доставка")


def test_session_roundtrip(sc):
    c = Chat(sc).start().click("Начать", "Заказать новый сайт", "Товары", "Да")
    s2 = Session.from_dict(c.s.to_dict())
    assert s2 == c.s


def test_random_walks_never_break(sc):
    """"Обезьяний" тест: тысячи случайных действий не должны ронять бота или заводить в тупик."""
    rnd = random.Random(42)
    finished = 0
    for _ in range(300):
        c = Chat(sc).start()
        for _ in range(120):
            r = rnd.random()
            btns = [b for b in c.buttons() if b.data]
            if r < 0.8 and btns:
                c._apply(c.engine.callback(c.s, rnd.choice(btns).data))
            elif r < 0.9:
                c.say(rnd.choice(["site.ru", "Иван", "+7 900 000-00-00", "что-то", "Назад"]))
            else:
                c.file()
            assert c.s.step in sc.steps
            assert c.view is not None and (c.view.buttons or c.view.contact or c.s.pending
                                           or sc.steps[c.s.step].type in ("текст", "файл"))
        finished += len(c.leads)
    assert finished > 0


def test_broken_table_reports_rows(tmp_path, scenario_path):
    import openpyxl
    wb = openpyxl.load_workbook(scenario_path)
    ws = wb["Кнопки"]
    ws.cell(row=3, column=3, value="несуществующий_шаг")
    wb["Шаги"].cell(row=5, column=2, value="кнопочки")
    bad = tmp_path / "bad.xlsx"
    wb.save(bad)
    with pytest.raises(ScenarioError) as e:
        parse_workbook(str(bad))
    text = "\n".join(e.value.errors)
    assert "Кнопки, строка 3" in text and "несуществующий_шаг" in text
    assert "Шаги, строка 5" in text and "кнопочки" in text


def test_edit_text_in_table_changes_bot(tmp_path, scenario_path):
    import openpyxl
    wb = openpyxl.load_workbook(scenario_path)
    ws = wb["Шаги"]
    for row in ws.iter_rows(min_row=2):
        if row[0].value == "главное_меню":
            row[2].value = "Добрый день! Новый текст приветствия"
    path = tmp_path / "edited.xlsx"
    wb.save(path)
    c = Chat(parse_workbook(str(path))).start().click("Начать")
    assert "Новый текст приветствия" in c.view.text


def test_button_colors(sc):
    c = Chat(sc).start()
    assert [b.style for b in c.buttons() if b.text == "Начать"] == ["success"]
    c.click("Начать")
    assert [b.style for b in c.buttons() if b.text == "Заказать новый сайт"] == ["primary"]
    c.click("Продвижение сайта")
    assert [b.style for b in c.buttons() if b.text == "Готово"] == ["success"]


def test_no_emoji_left(sc):
    import re
    emoji = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
    texts = [st.text for st in sc.steps.values()] + [o.text for st in sc.steps.values() for o in st.options]
    assert [t for t in texts if emoji.search(t)] == []
