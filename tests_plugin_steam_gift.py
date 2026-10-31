"""
Функциональные тесты плагина steam_gift.py на моках.

Проверяют: настройки и их повреждение, поиск ссылки «добавить в друзья» и региона,
привязки лотов, выбор издания/региона, клиент DesslyHub (ретраи, бизнес-ошибки, битый JSON),
полный цикл выдачи (DRY-RUN и боевой), все денежные защиты, идемпотентность, черный список,
ожидание ссылки от покупателя, опрос транзакций, восстановление после перезапуска,
весь Telegram-UI и удаление плагина.

Все запросы к DesslyHub и Telegram подменены заглушками - тест не тратит деньги и не пишет
в ваши чаты. Настройки плагина создаются во временной папке, поэтому ваши реальные
storage/plugins/steam_gift/ НЕ затрагиваются.

Запуск из корня FunPayCardinal:
    cp tests_plugin_steam_gift.py /path/to/FunPayCardinal/
    cd /path/to/FunPayCardinal && pip install -r requirements.txt && python3 tests_plugin_steam_gift.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

spec = importlib.util.spec_from_file_location("plugins.steam_gift", "plugins/steam_gift.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.steam_gift"] = plugin
spec.loader.exec_module(plugin)

from FunPayAPI import types  # noqa: E402

PASS = []
FAIL = []

INVITE = "https://s.team/f/AbCdEf123"
APP_ID = 1091500
EDITIONS = [
    {"edition": "Cyberpunk 2077", "package_id": 456432,
     "regions_info": [{"region": "RU", "discount": "94", "price": "3.6290", "price_original": "33.6290"},
                      {"region": "US", "discount": "10", "price": "42.6550", "price_original": "47.0000"}]},
    {"edition": "Cyberpunk 2077 Ultimate", "package_id": 456999,
     "regions_info": [{"region": "RU", "discount": "50", "price": "15.0000", "price_original": "30.0000"}]},
]


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


# ══════════════════════════════════════ МОКИ ══════════════════════════════════════
class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeSession:
    """Заменяет requests.Session: отвечает заготовками и пишет все запросы в self.calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.fail_times = 0

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "json": json, "params": params, "timeout": timeout})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("connection reset by peer")
        path = url.split("/api/v1")[-1].split("?")[0]
        for key in sorted(self.routes, key=len, reverse=True):
            if path == key or path.startswith(key):
                value = self.routes[key]
                if callable(value):
                    value = value(method=method, path=path, json=json, params=params)
                return FakeResponse(value)
        return FakeResponse({"error_code": -1}, 200)

    def posts(self):
        return [i for i in self.calls if i["method"] == "POST"]


def base_routes(balance="12.5000", gift_response=None, tx_status="success"):
    return {
        "/merchants/balance": {"balance": balance},
        "/service/steamgift/games": {"games": [{"name": "Cyberpunk 2077", "appid": APP_ID},
                                               {"name": "Cyberpunk 2077 Phantom Liberty", "appid": 1495710},
                                               {"name": "Elden Ring", "appid": 1245620}]},
        f"/service/steamgift/games/{APP_ID}": {"game": EDITIONS},
        "/service/steamgift/sendgames": gift_response if gift_response is not None
        else {"transaction_id": "tx-1", "status": "pending", "error_code": 0},
        "/merchants/transaction/tx-1/status": {"status": tx_status},
        "/exchange_rate/steam/5": {"exchange_rate": 90.0},
    }


class FakeBot:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.answers = []
        self.deleted = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=len(self.messages), text=text)

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=message_id, text=text)

    def answer_callback_query(self, callback_id, text=None, **kwargs):
        self.answers.append(text)

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeTelegram:
    def __init__(self):
        self.bot = FakeBot()
        self.authorized_users = {1: {}}
        self.notifications = []
        self.msg_handlers = []
        self.cbq_handlers = []
        self.user_states = {}
        self.commands = {}

    def send_notification(self, text, keyboard=None, notification_type="10", photo=None, pin=False):
        self.notifications.append((text, notification_type))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def add_command_to_menu(self, command, help_text):
        self.commands[command] = help_text

    # состояния - как в tg_bot.bot.TGBot
    def get_state(self, chat_id, user_id):
        try:
            return self.user_states[chat_id][user_id]
        except KeyError:
            return None

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states.setdefault(chat_id, {})[user_id] = {"state": state, "mid": message_id,
                                                            "data": data or {}}

    def clear_state(self, chat_id, user_id, del_msg=False):
        try:
            mid = self.user_states[chat_id][user_id].get("mid")
            del self.user_states[chat_id][user_id]
            if del_msg:
                self.bot.delete_message(chat_id, mid)
            return mid
        except KeyError:
            return None

    def check_state(self, chat_id, user_id, state):
        try:
            return self.user_states[chat_id][user_id]["state"] == state
        except KeyError:
            return False


class FakeAccount:
    def __init__(self):
        self.id = 111
        self.username = "seller"
        self.orders = {}
        self.chats = {}

    def get_order(self, order_id, **kwargs):
        return self.orders.get(str(order_id))

    def get_chat_by_name(self, name, make_request=False):
        return self.chats.get(name)


class FakeCardinal:
    def __init__(self):
        self.telegram = FakeTelegram()
        self.account = FakeAccount()
        self.blacklist = ["badguy"]
        self.run_id = 1
        self.sent = []
        self.registered_commands = []
        self.old_mode_enabled = False

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands.append((uuid, commands))

    def send_message(self, chat_id, message_text, chat_name=None, interlocutor_id=None, attempts=3,
                     watermark=True):
        self.sent.append({"chat_id": chat_id, "text": message_text, "chat_name": chat_name,
                          "watermark": watermark})
        return [SimpleNamespace(id=1, text=message_text, chat_id=chat_id)]


class SyncThread:
    """Заменяет threading.Thread: выполняет функцию сразу, синхронно (детерминированные тесты)."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args or (), kwargs or {}
        self.started = False

    def start(self):
        self.started = True
        if self.target:
            self.target(*self.args, **self.kwargs)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class FakeTgMessage:
    def __init__(self, text, chat_id=1, user_id=1):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.from_user = SimpleNamespace(id=user_id, username="tester", language_code="ru")
        self.id = 1
        self.content_type = "text"


import telebot.types as tt  # noqa: E402


def FakeCallback(data, chat_id=1, user_id=1):
    """Настоящий telebot.types.CallbackQuery - плагин проверяет тип через isinstance."""
    return tt.CallbackQuery.de_json({
        "id": f"cbq-{abs(hash(data)) % 10 ** 9}",
        "from": {"id": user_id, "is_bot": False, "first_name": "Tester", "username": "tester"},
        "chat_instance": "1",
        "data": data,
        "message": {"message_id": 5, "date": 1700000000,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": 111, "is_bot": True, "first_name": "Bot"},
                    "text": "/gift"},
    })


def make_order_full(order_id="900001", invite=INVITE, region="RU", price=1500.0,
                    status=types.OrderStatuses.PAID):
    """Настоящий объект FunPayAPI.types.Order с заполненными полями покупателя."""
    fields = {}
    if invite is not None:
        fields["f1"] = types.LotField("f1", invite, "Ссылка на Steam")
    if region is not None:
        fields["f2"] = types.LotField("f2", region, "Регион")
    return types.Order(order_id, status, None, None, None, fields, 1, price, types.Currency.RUB, None,
                       999, "buyer1", 111, "seller", 888, None, [], "ru")


def make_order_short(order_id="900001", description="Steam Gift Cyberpunk 2077", price=1500.0,
                     buyer="buyer1", status=types.OrderStatuses.PAID):
    return types.OrderShortcut(order_id, description, price, types.Currency.RUB, buyer, 999, 888,
                               status, __import__("datetime").datetime.now(), "Steam, Игры", None,
                               "<html>")


def make_event(order_id="900001", description="Steam Gift Cyberpunk 2077", lot_name=None,
               lot_id=None, price=1500.0, buyer="buyer1"):
    from FunPayAPI.updater.events import NewOrderEvent
    event = NewOrderEvent("tag", make_order_short(order_id, description, price, buyer))
    event.config_section_name = lot_name
    event.config_section_obj = None
    event.lot_id = lot_id
    event.lot_shortcut = None
    event.delivered = False
    return event


# ══════════════════════════════ ПЕСОЧНИЦА ══════════════════════════════
TMP = tempfile.mkdtemp(prefix="steamgift_tests_")


def sandbox(settings=None, routes=None, orders=None, sync_threads=True):
    """
    Готовит чистое окружение: временные файлы настроек, кардинал с моками,
    синхронные потоки и (опционально) маршруты DesslyHub.
    """
    plugin.PLUGIN_DIR = TMP
    plugin.SETTINGS_PATH = os.path.join(TMP, "settings.json")
    plugin.ORDERS_PATH = os.path.join(TMP, "orders.json")
    plugin.GAMES_CACHE_PATH = os.path.join(TMP, "games.json")
    for path in (plugin.SETTINGS_PATH, plugin.ORDERS_PATH, plugin.GAMES_CACHE_PATH):
        if os.path.exists(path):
            os.remove(path)
    plugin._settings = {}
    plugin._orders = {}
    plugin._last_low_balance_alert = 0.0
    if sync_threads:
        plugin.threading = SimpleNamespace(Thread=SyncThread)
    current = plugin.load_settings()
    current.update({"enabled": True, "dry_run": False, "apikey": "test-key",
                    "mappings": {"Steam Gift Cyberpunk 2077": {"app_id": APP_ID,
                                                               "edition": "Cyberpunk 2077",
                                                               "region": "", "package_id": 0}},
                    "poll_interval": 3})
    if settings:
        current.update(settings)
    plugin.save_settings(current)
    cardinal = FakeCardinal()
    session = FakeSession(base_routes() if routes is None else routes)
    plugin.make_client = lambda s=None: plugin.DesslyClient((s or plugin.load_settings()).get("apikey"),
                                                            (s or plugin.load_settings()).get("base_url"),
                                                            20, 0, session=session)
    for order_id, order in (orders or {"900001": make_order_full()}).items():
        cardinal.account.orders[order_id] = order
    return cardinal, session


def notifications(cardinal, needle=None):
    texts = [t for t, _ in cardinal.telegram.notifications]
    if needle is None:
        return texts
    return [t for t in texts if needle in t]


# ══════════════════════════════ ТЕСТЫ ══════════════════════════════
def test_metadata():
    check("NAME на английском", plugin.NAME == "SteamGift (DesslyHub Auto Gift Delivery)")
    check("CREDITS = yungspider", plugin.CREDITS == "yungspider")
    check("SETTINGS_PAGE = True (меню в Telegram)", plugin.SETTINGS_PAGE is True)
    check("UUID валиден", len(str(plugin.UUID)) == 36 and plugin.UUID.count("-") == 4)
    check("DESCRIPTION на русском и упоминает команду", "Команда: /gift" in plugin.DESCRIPTION)
    for name in ("BIND_TO_PRE_INIT", "BIND_TO_POST_START", "BIND_TO_NEW_ORDER", "BIND_TO_NEW_MESSAGE",
                 "BIND_TO_LAST_CHAT_MESSAGE_CHANGED", "BIND_TO_DELETE"):
        value = getattr(plugin, name, None)
        check(f"{name} объявлен", (isinstance(value, list) and all(callable(i) for i in value))
              if name != "BIND_TO_DELETE" else callable(value))
    check("по умолчанию плагин ВЫКЛЮЧЕН", plugin.DEFAULT_SETTINGS["enabled"] is False)
    check("по умолчанию включен DRY-RUN", plugin.DEFAULT_SETTINGS["dry_run"] is True)


def test_settings():
    sandbox()
    settings = plugin.load_settings()
    check("создан файл настроек", os.path.exists(plugin.SETTINGS_PATH))
    check("значения по умолчанию загружены", settings["default_region"] == "RU"
          and settings["poll_interval"] == 3)
    settings["custom_key"] = "value"
    plugin.save_settings(settings)
    plugin._settings = {}
    reloaded = plugin.load_settings()
    check("неизвестные ключи сохраняются", reloaded.get("custom_key") == "value")
    reloaded["poll_interval"] = "не число"
    plugin.save_settings(reloaded)
    plugin._settings = {}
    check("битый тип настройки откатывается к дефолту", plugin.load_settings()["poll_interval"] == 10)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write("{ это не json")
    plugin._settings = {}
    check("поврежденный файл настроек не роняет плагин",
          isinstance(plugin.load_settings(), dict) and plugin.load_settings()["default_region"] == "RU")
    plugin.update_setting("default_region", "KZ")
    plugin._settings = {}
    check("update_setting сохраняет значение", plugin.load_settings()["default_region"] == "KZ")


def test_extract():
    check("ссылка s.team", plugin.find_invite_url(INVITE) == INVITE)
    check("трейд-ссылка внутри текста",
          plugin.find_invite_url(f"привет {INVITE.replace('s.team/f/AbCdEf123', 'steamcommunity.com/tradeoffer/new/?partner=5&token=a-b')} спасибо")
          == "https://steamcommunity.com/tradeoffer/new/?partner=5&token=a-b")
    check("ссылка без https:// дополняется схемой",
          plugin.find_invite_url("steamcommunity.com/profiles/7656119800") ==
          "https://steamcommunity.com/profiles/7656119800")
    check("обычный текст не принимается за ссылку", plugin.find_invite_url("здравствуйте, когда гифт?") is None)
    check("пустой текст", plugin.find_invite_url("") is None and plugin.find_invite_url(None) is None)

    sandbox()
    settings = plugin.load_settings()
    order = make_order_full(invite=INVITE, region="KZ")
    check("ссылка найдена в поле заказа", plugin.extract_invite_url(order, settings) == INVITE)
    check("регион найден в поле заказа", plugin.extract_region(order, settings) == "KZ")
    order2 = make_order_full(invite=None, region=None)
    order2.fields["f3"] = types.LotField("f3", INVITE, "Комментарий")
    check("ссылка найдена в произвольном поле", plugin.extract_invite_url(order2, settings) == INVITE)
    check("нет ссылки -> None", plugin.extract_invite_url(make_order_full(invite=None, region=None), settings) is None)
    check("регион из сообщения: 'регион: UA'", plugin.extract_region_from_text("мой регион: UA") == "UA")
    check("регион из сообщения не найден", plugin.extract_region_from_text("привет") is None)


def test_mappings():
    sandbox()
    settings = plugin.load_settings()
    check("точное совпадение с названием лота",
          plugin.find_mapping(settings, make_order_short(), make_event())[0] == "Steam Gift Cyberpunk 2077")
    check("поиск по подстроке описания заказа",
          plugin.find_mapping(settings, make_order_short(description="Купил Steam Gift Cyberpunk 2077 срочно"),
                              make_event(lot_name=None))[0] == "Steam Gift Cyberpunk 2077")
    check("чужой лот не матчится",
          plugin.find_mapping(settings, make_order_short(description="Аккаунт Steam"),
                              make_event(description="Аккаунт Steam")) is None)
    settings["mappings"]["12345"] = {"app_id": 1}
    check("привязка по ID лота", plugin.find_mapping(settings, make_order_short(),
                                                     make_event(lot_id=12345))[0] == "12345")
    settings["mappings"] = {}
    check("без привязок -> None", plugin.find_mapping(settings, make_order_short(), make_event()) is None)

    single = [{"edition": "Game", "package_id": 1,
               "regions_info": [{"region": "RU", "price": "5.0", "discount": "0"}]}]
    edition, info = plugin.resolve_edition(single, {}, "RU")
    check("одно издание выбирается автоматически", edition["package_id"] == 1 and info["price"] == "5.0")
    edition, info = plugin.resolve_edition(EDITIONS, {"package_id": 456999}, "RU")
    check("выбор по package_id", edition["edition"] == "Cyberpunk 2077 Ultimate")
    edition, info = plugin.resolve_edition(EDITIONS, {"edition": "cyberpunk 2077 ultimate"}, "RU")
    check("выбор по названию издания (регистронезависимо)", edition["package_id"] == 456999)
    edition, info = plugin.resolve_edition(EDITIONS, {"package_id": 456432}, "US")
    check("цена берётся для нужного региона", info["region"] == "US" and info["price"] == "42.6550")
    try:
        plugin.resolve_edition(EDITIONS, {}, "RU")
        check("несколько изданий без указания -> ошибка", False)
    except plugin.GiftConfigError as e:
        check("несколько изданий без указания -> ошибка", "несколько изданий" in str(e))
    try:
        plugin.resolve_edition(EDITIONS, {"package_id": 456432}, "ZZ")
        check("недоступный регион -> ошибка", False)
    except plugin.GiftConfigError as e:
        check("недоступный регион -> ошибка", "недоступен" in str(e) and "RU" in str(e))
    try:
        plugin.resolve_edition([], {}, "RU")
        check("пустой ответ API -> ошибка", False)
    except plugin.GiftConfigError:
        check("пустой ответ API -> ошибка", True)


def test_client():
    sandbox()
    session = FakeSession(base_routes())
    client = plugin.DesslyClient("key", plugin.BASE_URL, 20, 0, session=session)
    check("баланс парсится из строки", client.balance() == 12.5)
    check("apikey передается в заголовке", session.calls[-1]["headers"]["apikey"] == "key")
    games = client.games(use_cache=False)
    check("каталог игр получен", len(games) == 3 and games[0]["appid"] == APP_ID)
    check("кэш каталога записан", os.path.exists(plugin.GAMES_CACHE_PATH))
    cached_games = client.games(use_cache=True)
    check("кэш каталога читается", len(cached_games) == 3)
    editions = client.game(APP_ID)
    check("издания игры получены", editions[0]["package_id"] == 456432)
    result = client.send_gift(INVITE, 456432, "RU", reference="900001")
    check("sendgames вернул transaction_id", result.get("transaction_id") == "tx-1")
    payload = session.posts()[-1]["json"]
    check("payload гифта корректен",
          payload == {"invite_url": INVITE, "package_id": "456432", "region": "RU", "reference": "900001"})
    check("статус транзакции получен", client.transaction_status("tx-1") == "success")
    check("курс валюты получен", client.exchange_rate(5) == 90.0)

    try:
        plugin.DesslyClient("", plugin.BASE_URL, 20, 0, session=FakeSession()).balance()
        check("без apikey -> ошибка -5", False)
    except plugin.DesslyError as e:
        check("без apikey -> ошибка -5", e.code == -5)

    error_session = FakeSession({"/merchants/balance": {"error_code": -2}})
    try:
        plugin.DesslyClient("k", plugin.BASE_URL, 20, 3, session=error_session).balance()
        check("бизнес-ошибка не ретраится", False)
    except plugin.DesslyError as e:
        check("бизнес-ошибка (-2) не ретраится", e.code == -2 and len(error_session.calls) == 1)
        check("код -2 переводится на русский", "недостаточно средств" in e.message)

    flaky = FakeSession(base_routes())
    flaky.fail_times = 2
    retries_client = plugin.DesslyClient("k", plugin.BASE_URL, 20, 3, session=flaky)
    check("сетевой сбой ретраится и запрос проходит", retries_client.balance() == 12.5
          and len(flaky.calls) == 3)
    dead = FakeSession(base_routes())
    dead.fail_times = 99
    try:
        plugin.DesslyClient("k", plugin.BASE_URL, 20, 2, session=dead).balance()
        check("после исчерпания ретраев -> ошибка", False)
    except plugin.DesslyError as e:
        check("после исчерпания ретраев -> ошибка", e.code == -1 and len(dead.calls) == 3)

    broken = FakeSession({"/merchants/balance": ValueError("no json")})
    try:
        plugin.DesslyClient("k", plugin.BASE_URL, 20, 0, session=broken).balance()
        check("ответ не JSON -> ошибка", False)
    except plugin.DesslyError as e:
        check("ответ не JSON -> ошибка", e.code == -1 and "JSON" in e.message)

    check("ошибка -55 описана по-русски", "уже есть эта игра" in plugin.error_text(-55))
    check("неизвестный код ошибки", "неизвестная ошибка" in plugin.error_text(-999))
    check("маскирование apikey", plugin.mask_key("1234567890") == "12******90")


def test_dry_run_flow():
    cardinal, session = sandbox({"dry_run": True})
    plugin.handle_new_order(cardinal, make_event())
    record = plugin.get_record("900001")
    check("DRY-RUN: запись создана", record is not None)
    check("DRY-RUN: статус dry_run", record.get("status") == "dry_run")
    check("DRY-RUN: покупка НЕ совершена", len(session.posts()) == 0)
    check("DRY-RUN: издание и цена определены", record.get("package_id") == 456432
          and float(record.get("price_usd", 0)) == 3.629)
    check("DRY-RUN: уведомление в Telegram", bool(notifications(cardinal, "DRY-RUN")))
    check("DRY-RUN: покупателю ничего не отправлено", len(cardinal.sent) == 0)


def test_real_flow():
    cardinal, session = sandbox()
    plugin.handle_new_order(cardinal, make_event())
    record = plugin.get_record("900001")
    check("боевой режим: транзакция записана", record.get("tx") == "tx-1")
    check("боевой режим: API вернул pending", record.get("api_status") == "pending")
    check("боевой режим: sendgames вызван один раз", len(session.posts()) == 1)
    check("боевой режим: reference = номер заказа FunPay",
          session.posts()[0]["json"]["reference"] == "900001")
    check("боевой режим: уведомление о отправке", bool(notifications(cardinal, "гифт отправлен")))
    check("боевой режим: статус сразу проверен", plugin.get_record("900001").get("status") == "success")
    check("боевой режим: покупатель получил сообщение", len(cardinal.sent) == 1)
    # $username подставляется через safe_text() Кардинала (с невидимыми разделителями)
    sent_text = cardinal.sent[0]["text"].replace("\u2063", "")
    check("боевой режим: в сообщении есть имя покупателя", "buyer1" in sent_text, )
    check("боевой режим: сообщение про подарок", "подарок" in sent_text.lower())
    check("боевой режим: уведомление об успехе", bool(notifications(cardinal, "успешно отправлен")))


def test_guards():
    cardinal, session = sandbox({"max_order_cost_usd": 1.0})
    plugin.handle_new_order(cardinal, make_event())
    check("лимит цены: покупка отменена", len(session.posts()) == 0)
    check("лимит цены: статус error", plugin.get_record("900001").get("status") == "error")
    check("лимит цены: критический алерт",
          bool([1 for t, nt in cardinal.telegram.notifications if nt == "13"]))

    cardinal, session = sandbox({"min_balance_usd": 100.0})
    plugin.handle_new_order(cardinal, make_event())
    check("низкий баланс: покупка отменена", len(session.posts()) == 0)
    check("низкий баланс: алерт продавцу", bool(notifications(cardinal, "недостаточно средств")))

    # цена гифта 3.629 $, заказ 1500 RUB при курсе 90 = 16.67 $ -> маржа ~359%
    cardinal, session = sandbox({"min_margin_percent": 500.0})
    plugin.handle_new_order(cardinal, make_event())
    check("маржа ниже порога: покупка отменена", len(session.posts()) == 0)
    check("маржа: посчитан доход в USD",
          abs(float(plugin.get_record("900001").get("income_usd", 0)) - 1500 / 90) < 0.01)
    check("маржа: алерт продавцу", bool(notifications(cardinal, "маржа")))

    cardinal, session = sandbox({"min_margin_percent": 10.0})
    plugin.handle_new_order(cardinal, make_event())
    check("маржа выше порога: покупка совершена", len(session.posts()) == 1)

    cardinal, session = sandbox({"apikey": ""})
    plugin.handle_new_order(cardinal, make_event())
    check("без API-ключа покупка не совершается", len(session.posts()) == 0)
    check("без API-ключа понятная ошибка", "API-ключ" in str(plugin.get_record("900001").get("error_text")))

    cardinal, session = sandbox({"mappings": {"Steam Gift Cyberpunk 2077": {"app_id": APP_ID,
                                                                          "edition": "", "region": "ZZ",
                                                                          "package_id": 0}}})
    plugin.handle_new_order(cardinal, make_event())
    check("недоступный регион из привязки: покупка отменена", len(session.posts()) == 0)
    check("недоступный регион: ошибка записана",
          plugin.get_record("900001").get("status") == "error")


def test_idempotency():
    cardinal, session = sandbox()
    plugin.handle_new_order(cardinal, make_event())
    first_calls = len(session.posts())
    plugin.handle_new_order(cardinal, make_event())
    plugin.handle_new_order(cardinal, make_event())
    check("повторные события по тому же заказу не дублируют покупку", len(session.posts()) == first_calls)
    plugin.start_order(cardinal, "900001")
    check("повторный вызов start_order не покупает второй гифт", len(session.posts()) == first_calls)

    cardinal, session = sandbox()
    plugin.on_new_order(cardinal, make_event(order_id="ADTEST", description="Steam Gift Cyberpunk 2077"))
    check("тест автовыдачи (ADTEST) игнорируется", plugin.get_record("ADTEST") is None)
    cardinal, session = sandbox({"enabled": False})
    plugin.handle_new_order(cardinal, make_event(order_id="900002"))
    check("выключенный плагин не обрабатывает заказы", plugin.get_record("900002") is None
          and len(session.posts()) == 0)


def test_no_link_and_blacklist():
    cardinal, session = sandbox(orders={"900001": make_order_full(invite=None)})
    plugin.handle_new_order(cardinal, make_event())
    record = plugin.get_record("900001")
    check("нет ссылки: статус waiting_link", record.get("status") == "waiting_link")
    check("нет ссылки: покупателю отправлен запрос", len(cardinal.sent) == 1
          and "ссылку" in cardinal.sent[0]["text"].lower())
    check("нет ссылки: покупка не совершена", len(session.posts()) == 0)

    # покупатель присылает ссылку в чат
    from FunPayAPI.updater.events import NewMessageEvent

    class FakeMessage:
        chat_id = 888
        author_id = 999
        by_bot = False
        type = None
        text = f"держите {INVITE} регион RU"

        def __str__(self):
            return self.text

    plugin.on_new_message(cardinal, NewMessageEvent("tag", FakeMessage()))
    record = plugin.get_record("900001")
    check("ссылка из чата: покупка совершена", len(session.posts()) == 1)
    check("ссылка из чата: статус success", record.get("status") == "success")
    check("ссылка из чата: region подхватился из сообщения", record.get("region") == "RU")

    cardinal, session = sandbox({"ask_for_link": False}, orders={"900001": make_order_full(invite=None)})
    plugin.handle_new_order(cardinal, make_event())
    check("нет ссылки и запрос выключен: статус error",
          plugin.get_record("900001").get("status") == "error")
    check("нет ссылки и запрос выключен: покупателю не пишем", len(cardinal.sent) == 0)

    cardinal, session = sandbox(orders={"900001": make_order_full()})
    cardinal.account.orders["900001"] = make_order_full()
    cardinal.blacklist = ["buyer1"]
    plugin.handle_new_order(cardinal, make_event())
    check("покупатель в ЧС: пропущен", plugin.get_record("900001").get("status") == "skipped")
    check("покупатель в ЧС: покупка не совершена", len(session.posts()) == 0)

    cardinal, session = sandbox(orders={})
    cardinal.account.get_order = lambda order_id, **kw: (_ for _ in ()).throw(Exception("нет сети"))
    plugin.handle_new_order(cardinal, make_event(order_id="900003"))
    check("ошибка получения заказа: статус error",
          plugin.get_record("900003").get("status") == "error")
    check("ошибка получения заказа: алерт продавцу", bool(notifications(cardinal, "вручную")))


def test_api_failures():
    cardinal, session = sandbox(routes=base_routes(
        gift_response={"status": "failed", "error_code": -55}))
    plugin.handle_new_order(cardinal, make_event())
    record = plugin.get_record("900001")
    check("ошибка -55: статус failed", record.get("status") == "failed")
    check("ошибка -55: код сохранен", record.get("error_code") == -55)
    check("ошибка -55: критический алерт", bool(notifications(cardinal, "уже есть эта игра")))
    check("ошибка -55: покупателю не пишем (failed_text пуст)", len(cardinal.sent) == 0)

    cardinal, session = sandbox({"failed_text": "Извините, не смогли отправить гифт."},
                                routes=base_routes(gift_response={"error_code": -51}))
    plugin.handle_new_order(cardinal, make_event())
    check("ошибка -51: статус failed", plugin.get_record("900001").get("status") == "failed")
    check("ошибка -51: покупателю отправлен текст", len(cardinal.sent) == 1)

    cardinal, session = sandbox(routes=base_routes(tx_status="failed"))
    plugin.handle_new_order(cardinal, make_event())
    check("транзакция failed: статус записан", plugin.get_record("900001").get("status") == "failed")
    check("транзакция failed: алерт продавцу", bool(notifications(cardinal, "ошибкой")))

    cardinal, session = sandbox(routes=base_routes(tx_status="cancelled"))
    plugin.handle_new_order(cardinal, make_event())
    check("транзакция cancelled: статус записан",
          plugin.get_record("900001").get("status") == "cancelled")
    check("транзакция cancelled: упомянут возврат денег",
          bool(notifications(cardinal, "возвращены")))

    cardinal, session = sandbox()
    plugin.handle_new_order(cardinal, make_event())
    check("успех: транзакция помечена success", plugin.get_record("900001").get("status") == "success")


def test_recovery_and_poller():
    cardinal, session = sandbox()
    plugin.set_record("900009", status="sending", buyer="buyer1", chat_id=888,
                      lot="Steam Gift Cyberpunk 2077")
    plugin.recover_interrupted(cardinal)
    check("прерванная отправка помечена как error",
          plugin.get_record("900009").get("status") == "error")
    check("прерванная отправка: алерт продавцу", bool(notifications(cardinal, "оборвался")))
    check("прерванная отправка: повторной покупки нет", len(session.posts()) == 0)

    # поллер доводит pending -> success
    cardinal, session = sandbox()
    plugin.set_record("900010", status="pending", tx="tx-1", buyer="buyer1",
                      chat_id=888, created=int(time.time()))
    cardinal.account.orders["900010"] = make_order_full("900010")
    runner = RunIdController(cardinal, allow=8)
    plugin.poller_loop(runner, 0)
    check("поллер: транзакция завершена success", plugin.get_record("900010").get("status") == "success")

    # поллер снимает зависший pending по таймауту
    cardinal, session = sandbox({"poll_max_minutes": 0})
    plugin.set_record("900011", status="pending", tx="tx-1", buyer="buyer1",
                      chat_id=888, created=int(time.time()) - 120)
    plugin.poller_loop(RunIdController(cardinal, allow=8), 0)
    check("поллер: зависшая транзакция -> timeout",
          plugin.get_record("900011").get("status") == "timeout")

    # поллер снимает ожидание ссылки по таймауту
    cardinal, session = sandbox({"wait_link_minutes": 0})
    plugin.set_record("900012", status="waiting_link", buyer="buyer1", chat_id=888,
                      asked_at=int(time.time()) - 120, created=int(time.time()) - 120)
    plugin.poller_loop(RunIdController(cardinal, allow=8), 0)
    check("поллер: ожидание ссылки -> timeout_link",
          plugin.get_record("900012").get("status") == "timeout_link")

    # проверка низкого баланса
    cardinal, session = sandbox({"min_balance_usd": 50.0, "low_balance_cooldown": 1})
    cardinal.telegram.notifications.clear()
    plugin.check_low_balance(cardinal)
    check("низкий баланс: критический алерт",
          bool([1 for t, nt in cardinal.telegram.notifications if nt == "13"]))
    cardinal.telegram.notifications.clear()
    plugin.check_low_balance(cardinal)
    check("низкий баланс: повтор подавлен cooldown", not cardinal.telegram.notifications)


class RunIdController:
    """Кардинал, у которого run_id «меняется» после N обращений - так останавливается поллер."""

    def __init__(self, cardinal, allow=8):
        self.telegram = cardinal.telegram
        self.account = cardinal.account
        self.blacklist = cardinal.blacklist
        self.sent = cardinal.sent
        self.old_mode_enabled = False
        self._allow = allow
        self._calls = 0

    @property
    def run_id(self):
        self._calls += 1
        return 0 if self._calls <= self._allow else 1

    @run_id.setter
    def run_id(self, value):
        pass


def test_telegram_ui():
    cardinal, session = sandbox()
    plugin.pre_init(cardinal)
    tg = cardinal.telegram
    check("/gift зарегистрирован", any(kw.get("commands") == ["gift"] for _, kw in tg.msg_handlers))
    check("callback-хэндлеры зарегистрированы", len(tg.cbq_handlers) >= 2)
    check("команда добавлена в меню бота", bool(cardinal.registered_commands))
    check("перевод команды добавлен", f"{plugin.UUID}_cmd_gift" in tg.commands.values() or
          bool(cardinal.registered_commands))

    plugin.on_gift_command(FakeTgMessage("/gift"))
    check("/gift открывает меню", bool(tg.bot.messages) and "SteamGift" in tg.bot.messages[-1])
    check("в меню виден боевой режим", "БОЕВОЙ РЕЖИМ" in tg.bot.messages[-1])

    tg.bot.messages.clear()
    plugin.on_gift_command(FakeTgMessage("/gift", user_id=4242))
    check("чужой пользователь не получает меню", not tg.bot.messages)

    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:toggle_enabled"))
    check("кнопка выключить работает", plugin.load_settings()["enabled"] is False)
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:toggle_enabled"))
    check("кнопка включить работает", plugin.load_settings()["enabled"] is True)
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:toggle_dry"))
    check("DRY-RUN переключается", plugin.load_settings()["dry_run"] is True)
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:toggle_dry"))

    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:balance"))
    check("баланс показан", any("12.50" in i for i in tg.bot.edits + tg.bot.messages))
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:test"))
    check("тест подключения: баланс OK", any("OK" in i for i in tg.bot.edits))
    check("тест подключения: каталог игр OK", any("Каталог игр" in i for i in tg.bot.edits))

    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:mappings"))
    check("меню привязок открывается", any("Привязки лотов" in i for i in tg.bot.edits))
    plugin.apply_mapping("Steam Gift Elden Ring | 1245620 |  | US")
    mappings = plugin.load_settings()["mappings"]
    check("привязка добавлена", mappings.get("Steam Gift Elden Ring", {}).get("app_id") == 1245620)
    check("регион в привязке сохранен", mappings["Steam Gift Elden Ring"]["region"] == "US")
    try:
        plugin.apply_mapping("без appid")
        check("неверный формат привязки -> ошибка", False)
    except ValueError:
        check("неверный формат привязки -> ошибка", True)
    try:
        plugin.apply_mapping("Лот | не-число")
        check("не-числовой app_id -> ошибка", False)
    except ValueError:
        check("не-числовой app_id -> ошибка", True)
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:mapping_del:Steam Gift Elden Ring"))
    check("привязка удалена", "Steam Gift Elden Ring" not in plugin.load_settings()["mappings"])

    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:input:games_search"))
    check("запрошен ввод для поиска игры", len(tg.bot.messages) > 0)
    state = tg.get_state(1, 1)
    check("состояние ввода установлено", state and state["state"] == plugin.INPUT_STATE
          and state["data"]["field"] == "games_search")
    plugin.handle_input(FakeTgMessage("cyberpunk"))
    check("поиск игры вернул результаты",
          any("1495710" in i for i in tg.bot.messages))

    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:transactions"))
    check("меню транзакций открывается", any("Транзакции" in i for i in tg.bot.edits))
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:limits"))
    check("меню лимитов открывается", any("Лимиты и защита" in i for i in tg.bot.edits))
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:texts"))
    check("меню текстов открывается", any("Тексты сообщений" in i for i in tg.bot.edits))

    # ввод настроек через состояние
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:input:apikey"))
    plugin.handle_input(FakeTgMessage("secret-key-123"))
    check("apikey сохранен", plugin.load_settings()["apikey"] == "secret-key-123")
    check("apikey в меню маскируется", "secret" not in plugin.menu_text(cardinal))
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:input:default_region"))
    plugin.handle_input(FakeTgMessage("kz"))
    check("регион сохранен в верхнем регистре", plugin.load_settings()["default_region"] == "KZ")
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:input:default_region"))
    plugin.handle_input(FakeTgMessage("Russia"))
    check("неверный регион отклонен", plugin.load_settings()["default_region"] == "KZ")
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:input:max_order_cost_usd"))
    plugin.handle_input(FakeTgMessage("25.5"))
    check("лимит цены сохранен как float", plugin.load_settings()["max_order_cost_usd"] == 25.5)
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:input:max_order_cost_usd"))
    plugin.handle_input(FakeTgMessage("abc"))
    check("нечисловой лимит отклонен", plugin.load_settings()["max_order_cost_usd"] == 25.5)
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:input:success_text"))
    plugin.handle_input(FakeTgMessage("Готово, $username! Первая строка\\nВторая строка"))
    check("перенос строки в тексте применен", "\n" in plugin.load_settings()["success_text"])
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:toggle:notify_success"))
    check("переключатель уведомлений работает", plugin.load_settings()["notify_success"] is False)

    plugin.on_settings_callback(cardinal, FakeCallback("47:" + plugin.UUID + ":0"))
    check("открытие настроек из ПУ Кардинала работает", len(tg.bot.edits) > 0)
    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:unknown_action"))
    check("неизвестный callback не роняет плагин", True)

    plugin.on_settings_callback(cardinal, FakeCallback("DSGift:check_now"))
    check("кнопка «проверить транзакции» работает",
          any("Активных транзакций нет" in i for i in tg.bot.edits + tg.bot.messages))


def test_delete():
    cardinal, session = sandbox()
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    open(os.path.join(plugin.PLUGIN_DIR, "orders.json"), "w").write("{}")
    plugin.on_delete(cardinal, FakeCallback("DSGift:menu"))
    check("on_delete удалил папку плагина", not os.path.exists(plugin.PLUGIN_DIR))
    check("on_delete ответил на callback", bool(cardinal.telegram.bot.answers))


def main():
    for func in (test_metadata, test_settings, test_extract, test_mappings, test_client,
                 test_dry_run_flow, test_real_flow, test_guards, test_idempotency,
                 test_no_link_and_blacklist, test_api_failures, test_recovery_and_poller,
                 test_telegram_ui, test_delete):
        print(f"\n--- {func.__name__} ---")
        try:
            func()
        except Exception:
            import traceback
            traceback.print_exc()
            FAIL.append(f"{func.__name__}: исключение")
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 70)
    print(f"Пройдено: {len(PASS)}  Провалено: {len(FAIL)}  Всего: {len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  ✗ {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
