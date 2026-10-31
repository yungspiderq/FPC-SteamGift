"""
SteamGift — автовыдача Steam-гифтов через DesslyHub (плагин для FunPay Cardinal v0.1.17+)

🎁 Покупатель оплатил лот «Steam Gift: <игра>» — плагин сам берёт его ссылку
«добавить в друзья», покупает игру-гифт через API DesslyHub и следит за
транзакцией до статуса success / failed, сообщая покупателю результат.

Возможности:
 • Полный цикл по API DesslyHub: каталог игр -> издание (package_id) и цена
   региона -> отправка гифта -> опрос статуса транзакции.
 • Привязка лотов FunPay к играм Steam (appid / издание / регион) через меню.
 • Ссылку «добавить в друзья» ищет в полях заказа и в описании; если её нет -
   вежливо просит покупателя в чате и ждёт N минут (настраивается).
 • 🧪 DRY-RUN по умолчанию: весь цикл проходит, но покупка НЕ совершается -
   можно спокойно настроить привязки и проверить сценарий без трат.
 • Защита денег: проверка баланса, лимит стоимости одной покупки, минимальная
   маржа, ровно одна отправка на заказ, состояние переживает рестарт Кардинала.
 • Ошибки DesslyHub переводятся на русский (-51 битая ссылка, -55 игра уже есть,
   -58 регион недоступен, -2 недостаточно средств и т.д.).
 • Меню в Telegram: /gift — баланс, привязки лотов, транзакции, лимиты, DRY-RUN.

Команды Telegram:
 • /gift - меню плагина (то же, что «Настройки» на странице плагина).

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
ВАЖНО: лоты с гифтами НЕ должны быть в auto_delivery.cfg - иначе Кардинал
выдаст товар сам. API-ключ выдаёт менеджер DesslyHub (https://t.me/DesslyHub_Manager),
документация API: https://desslyhub.readme.io/reference/introduction
Настройки: storage/plugins/steam_gift/settings.json (или меню /gift).
Автор: yungspider
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import time
import uuid as uuid_lib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cardinal import Cardinal

import requests
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message

from FunPayAPI import types
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent, LastChatMessageChangedEvent

from tg_bot import utils as tg_utils, CBT
from locales.localizer import Localizer

logger = logging.getLogger("FPC.SteamGift")
localizer = Localizer()
_ = localizer.translate

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "SteamGift (DesslyHub Auto Gift Delivery)"
VERSION = "1.0.0"
DESCRIPTION = ("Автовыдача Steam-гифтов через API DesslyHub: покупатель оплатил лот - плагин берёт его "
               "ссылку «добавить в друзья», покупает игру-гифт, следит за транзакцией до success/failed "
               "и пишет покупателю результат. DRY-RUN режим, лимиты и маржа, привязки лотов в меню. "
               "Настройки: storage/plugins/steam_gift/settings.json. Команда: /gift")
CREDITS = "yungspider"
UUID = "e3be06f8-aa4c-4d8f-992f-fa6ed151b865"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "steam_gift")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")
ORDERS_PATH = os.path.join(PLUGIN_DIR, "orders.json")
GAMES_CACHE_PATH = os.path.join(PLUGIN_DIR, "games.json")

BASE_URL = "https://desslyhub.com/api/v1"
DOC_URL = "https://desslyhub.readme.io/reference/introduction"

DEFAULT_SETTINGS: dict[str, Any] = {
    # Безопасность: по умолчанию плагин ВЫКЛЮЧЕН и работает в DRY-RUN (деньги не тратятся).
    "enabled": False,
    "dry_run": True,

    # API DesslyHub
    "apikey": "",
    "base_url": BASE_URL,
    "timeout": 20,
    "retries": 2,

    # Как находим данные покупателя
    "default_region": "RU",
    "invite_field_names": ["ссылка на steam", "steam ссылка", "ссылка steam", "invite", "friend link",
                           "трейд-ссылка", "trade link", "steam"],
    "region_field_names": ["регион", "region", "страна", "country"],
    "ask_for_link": True,
    "ask_link_text": ("Здравствуйте, $username! Спасибо за покупку 🎁\n"
                      "Чтобы отправить игру-гифт, пришлите, пожалуйста:\n"
                      "1) ссылку «добавить в друзья» из вашего профиля Steam "
                      "(Steam -> Профиль -> Друзья -> Добавить друга -> отправить ссылку-приглашение);\n"
                      "2) регион вашего аккаунта Steam (например: RU, KZ, UA, US)."),
    "wait_link_minutes": 60,

    # Сообщения покупателю
    "success_text": ("🎁 Игра отправлена вам в подарок! Примите подарок в Steam в течение 24 часов "
                     "(Steam -> Уведомления или Инвентарь -> «Принять подарок»).\n"
                     "Спасибо за покупку, $username! Будем рады вашему отзыву ⭐"),
    "failed_text": "",   # пусто = покупателю об ошибке не пишем, разбирается продавец

    # Опрос транзакций
    "poll_interval": 10,
    "poll_max_minutes": 90,

    # Защита денег
    "min_balance_usd": 5.0,
    "max_order_cost_usd": 100.0,
    "min_margin_percent": 0.0,          # 0 = проверка маржи выключена
    "funpay_currency_code": 5,          # код валюты цены заказа в API DesslyHub (5 = RUB, 1 = USD, 37 = KZT)

    # Уведомления
    "notify_success": True,
    "notify_failed": True,
    "low_balance_cooldown": 120,        # минут между повторными алертами о низком балансе

    # Привязки лотов FunPay -> игры Steam:
    # {"<название лота или ID лота>": {"app_id": 123, "edition": "", "region": "", "package_id": 0}}
    "mappings": {},

    # Кэш каталога игр, минут
    "games_cache_minutes": 720,
}

CURRENCY_CODES = {"USD": 1, "GBP": 2, "EUR": 3, "CHF": 4, "RUB": 5, "PLN": 6, "BRL": 7, "JPY": 8,
                  "UAH": 18, "KZT": 37, "INR": 24, "CNY": 23}

ERROR_CODES = {
    -1: "внутренняя ошибка сервера DesslyHub",
    -2: "недостаточно средств на балансе DesslyHub",
    -3: "некорректная сумма",
    -4: "некорректное тело запроса",
    -5: "доступ запрещен (проверьте API-ключ)",
    -51: "неверная ссылка для добавления в друзья",
    -52: "некорректный app_id игры",
    -53: "информация об игре не найдена",
    -54: "у покупателя нет основной игры (это DLC)",
    -55: "у покупателя уже есть эта игра",
    -56: "не удалось добавить покупателя в друзья",
    -57: "некорректно указан регион покупателя",
    -58: "регион получателя недоступен для гифта",
    -59: "покупатель не добавил / удалил бота из друзей",
    -100: "некорректный логин Steam",
    -120: "некорректное значение валюты",
    -121: "валюта не поддерживается",
    -151: "неверный ID транзакции",
    -152: "транзакция не найдена",
    -153: "не указан номер страницы",
}
"""Коды ошибок DesslyHub -> понятное описание (по документации error-codes)."""

BUSINESS_ERRORS = {-2, -3, -4, -5, -51, -52, -53, -54, -55, -56, -57, -58, -59, -100, -120, -121,
                   -151, -152, -153}
"""Ошибки, которые нет смысла повторять (ретраить)."""

FINAL_STATUSES = ("success", "failed", "cancelled")

# Ссылка «добавить в друзья» / трейд-ссылка / ссылка на профиль Steam
INVITE_PATTERNS = (
    re.compile(r"https?://s\.team/[A-Za-z0-9/\-?=&_.]+", re.I),
    re.compile(r"https?://steamcommunity\.com/tradeoffer/new/?\?partner=\d+(?:&token=[A-Za-z0-9_\-]+)?", re.I),
    re.compile(r"https?://steamcommunity\.com/(?:id|profiles)/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-/?=&.]+)?", re.I),
    re.compile(r"https?://steam\.community\.com/[A-Za-z0-9_\-/?=&.]+", re.I),
    # покупатели часто вставляют ссылку без схемы - ловим и её
    re.compile(r"(?:www\.)?(?:steamcommunity\.com|s\.team)/[A-Za-z0-9_\-/?=&.]+", re.I),
)

_lock = threading.RLock()
_settings: dict = {}
_orders: dict = {}
_cardinal: Cardinal | None = None
_thread_started = False
_last_low_balance_alert = 0.0


class DesslyError(Exception):
    """Ошибка API DesslyHub с кодом из документации."""

    def __init__(self, code: int, message: str | None = None):
        self.code = int(code)
        self.message = message or ERROR_CODES.get(self.code, f"неизвестная ошибка (код {self.code})")
        super().__init__(f"[{self.code}] {self.message}")


class GiftConfigError(Exception):
    """Ошибка настроек плагина (нет привязки лота, не выбрано издание и т.п.)."""


# ══════════════════════════════ ХРАНИЛИЩЕ ══════════════════════════════
def _atomic_write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path: str) -> Any:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.error(f"[SteamGift] Не удалось прочитать $YELLOW{path}$RESET - файл поврежден?")
        logger.debug("TRACEBACK", exc_info=True)
        return None


def load_settings(force: bool = False) -> dict:
    """
    Загружает настройки (создает файл с настройками по умолчанию при первом запуске).
    Значения, которых нет в файле, дополняются из DEFAULT_SETTINGS - поэтому добавление
    новых опций в следующих версиях плагина не ломает существующие настройки.
    """
    global _settings
    with _lock:
        if _settings and not force:
            return _settings
        raw = _read_json(SETTINGS_PATH)
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in DEFAULT_SETTINGS and isinstance(DEFAULT_SETTINGS[key], type(value)):
                    settings[key] = value
                elif key in DEFAULT_SETTINGS:
                    # тип не совпал - пробуем привести, иначе берем значение по умолчанию
                    try:
                        settings[key] = type(DEFAULT_SETTINGS[key])(value)
                    except Exception:
                        logger.warning(f"[SteamGift] Настройка {key} имеет неверный тип - беру значение "
                                       f"по умолчанию.")
                else:
                    settings[key] = value
            if not isinstance(settings.get("mappings"), dict):
                settings["mappings"] = {}
        else:
            save_settings(settings)
            logger.info(f"[SteamGift] Создан файл настроек $YELLOW{SETTINGS_PATH}$RESET")
        _settings = settings
        return _settings


def save_settings(settings: dict | None = None) -> None:
    """Сохраняет настройки на диск."""
    global _settings
    with _lock:
        if settings is not None:
            _settings = settings
        if not _settings:
            return
        try:
            _atomic_write(SETTINGS_PATH, _settings)
        except Exception:
            logger.error("[SteamGift] Не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def update_setting(key: str, value: Any) -> None:
    """Меняет одну настройку и сохраняет файл."""
    settings = load_settings()
    with _lock:
        settings[key] = value
    save_settings(settings)


def load_orders() -> dict:
    """Загружает журнал заказов/транзакций."""
    global _orders
    with _lock:
        if _orders:
            return _orders
        raw = _read_json(ORDERS_PATH)
        _orders = raw if isinstance(raw, dict) else {}
        return _orders


def save_orders() -> None:
    """Сохраняет журнал заказов/транзакций."""
    with _lock:
        if not _orders:
            return
        try:
            _atomic_write(ORDERS_PATH, _orders)
        except Exception:
            logger.error("[SteamGift] Не удалось сохранить журнал транзакций.")
            logger.debug("TRACEBACK", exc_info=True)


def get_record(order_id: str) -> dict | None:
    return load_orders().get(str(order_id))


def set_record(order_id: str, **fields) -> dict:
    """Создает/обновляет запись заказа в журнале."""
    orders = load_orders()
    with _lock:
        record = orders.setdefault(str(order_id), {})
        record["order_id"] = str(order_id)
        record.update(fields)
        record["updated"] = int(time.time())
        if "created" not in record:
            record["created"] = int(time.time())
        # журнал не должен расти бесконечно: старые завершенные заказы удаляются
        if len(orders) > 500:
            finished = [i for i in orders
                        if orders[i].get("status") in (*FINAL_STATUSES, "skipped", "error", "timeout",
                                                       "timeout_link", "dry_run")]
            for key in sorted(finished, key=lambda k: int(orders[k].get("updated", 0)))[:100]:
                orders.pop(key, None)
    save_orders()
    return orders[str(order_id)]


def load_games_cache(settings: dict | None = None) -> tuple[list, float]:
    """Возвращает (список игр, время загрузки кэша)."""
    raw = _read_json(GAMES_CACHE_PATH)
    if isinstance(raw, dict) and isinstance(raw.get("games"), list):
        return raw["games"], float(raw.get("time", 0))
    return [], 0.0


def save_games_cache(games: list) -> None:
    try:
        _atomic_write(GAMES_CACHE_PATH, {"time": time.time(), "games": games})
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ КЛИЕНТ API DESSLYHUB ══════════════════════════════
class DesslyClient:
    """
    Минимальный клиент API DesslyHub (https://desslyhub.readme.io).

    Все методы возвращают распарсенный JSON; ошибки API поднимаются как :class:`DesslyError`.
    Сетевые сбои повторяются `retries` раз, бизнес-ошибки - не повторяются.
    """

    def __init__(self, apikey: str, base_url: str = BASE_URL, timeout: int = 20, retries: int = 2,
                 session=None):
        self.apikey = apikey or ""
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.timeout = max(5, int(timeout or 20))
        self.retries = max(0, int(retries or 0))
        self.session = session if session is not None else requests.Session()

    def _request(self, method: str, path: str, payload: dict | None = None, params: dict | None = None,
                 raise_on_error: bool = True) -> dict:
        if not self.apikey:
            raise DesslyError(-5, "API-ключ DesslyHub не задан (меню /gift -> 🔑 API-ключ)")
        url = f"{self.base_url}{path}"
        headers = {"apikey": self.apikey, "accept": "application/json"}
        last_error: DesslyError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.request(method, url, headers=headers, json=payload,
                                                params=params, timeout=self.timeout)
                try:
                    data = response.json()
                except Exception:
                    raise DesslyError(-1, f"ответ не похож на JSON (HTTP {response.status_code})")
                code = data.get("error_code") if isinstance(data, dict) else None
                if code not in (None, 0):
                    error = DesslyError(int(code))
                    if raise_on_error:
                        raise error
                    return data
                if response.status_code >= 400:
                    raise DesslyError(-1, f"HTTP {response.status_code}")
                return data if isinstance(data, dict) else {"result": data}
            except DesslyError as e:
                if e.code in BUSINESS_ERRORS:
                    raise
                last_error = e
            except Exception as e:
                last_error = DesslyError(-1, f"{type(e).__name__}: {e}")
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        raise last_error or DesslyError(-1)

    def balance(self) -> float:
        """GET /merchants/balance -> баланс мерчанта (USD)."""
        return float(self._request("GET", "/merchants/balance").get("balance", 0) or 0)

    def games(self, use_cache: bool = True) -> list:
        """GET /service/steamgift/games -> каталог игр [{name, appid}]."""
        settings = load_settings()
        if use_cache:
            cached, cached_at = load_games_cache(settings)
            if cached and time.time() - cached_at < int(settings.get("games_cache_minutes", 720)) * 60:
                return cached
        games = self._request("GET", "/service/steamgift/games").get("games") or []
        if isinstance(games, list) and games:
            save_games_cache(games)
        return games if isinstance(games, list) else []

    def game(self, app_id: int | str) -> list:
        """GET /service/steamgift/games/{app_id} -> издания игры с ценами по регионам."""
        editions = self._request("GET", f"/service/steamgift/games/{app_id}").get("game") or []
        return editions if isinstance(editions, list) else []

    def send_gift(self, invite_url: str, package_id: int | str, region: str,
                  reference: str | None = None) -> dict:
        """POST /service/steamgift/sendgames -> {transaction_id, status, error_code}."""
        payload = {"invite_url": invite_url, "package_id": str(package_id), "region": str(region)}
        if reference:
            payload["reference"] = str(reference)
        return self._request("POST", "/service/steamgift/sendgames", payload=payload,
                             raise_on_error=False)

    def transaction_status(self, transaction_id: str) -> str:
        """GET /merchants/transaction/{id}/status -> pending / success / failed / cancelled."""
        return str(self._request("GET", f"/merchants/transaction/{transaction_id}/status")
                   .get("status", "") or "")

    def transactions(self, page: int = 1) -> list:
        """GET /merchants/transactions?page=N -> список транзакций (по 100, новые сверху)."""
        data = self._request("GET", "/merchants/transactions", params={"page": int(page)})
        result = data.get("transactions") or []
        return result if isinstance(result, list) else []

    def exchange_rate(self, currency_code: int) -> float:
        """GET /exchange_rate/steam/{code} -> курс (например 87.87 для RUB)."""
        return float(self._request("GET", f"/exchange_rate/steam/{int(currency_code)}")
                   .get("exchange_rate", 0) or 0)


def make_client(settings: dict | None = None) -> DesslyClient:
    settings = settings or load_settings()
    return DesslyClient(settings.get("apikey", ""), settings.get("base_url", BASE_URL),
                        int(settings.get("timeout", 20)), int(settings.get("retries", 2)))


# ══════════════════════════════ ПОИСК ДАННЫХ В ЗАКАЗЕ ══════════════════════════════
def find_invite_url(text: str | None) -> str | None:
    """
    Ищет ссылку «добавить в друзья» / трейд-ссылку / ссылку на профиль Steam в тексте.

    :param text: любой текст (значение поля заказа, описание, сообщение покупателя).
    """
    if not text:
        return None
    for pattern in INVITE_PATTERNS:
        match = pattern.search(str(text))
        if match:
            url = match.group(0).strip().rstrip(".,;)")
            if not url.lower().startswith(("http://", "https://")):
                url = f"https://{url}"   # DesslyHub ждет полную ссылку
            return url
    return None


def order_field_texts(order) -> list[tuple[str, str]]:
    """
    Возвращает список (название поля, значение) из заполненных полей заказа.

    :param order: объект :class:`FunPayAPI.types.Order`.
    """
    result = []
    fields = getattr(order, "fields", None) or {}
    for key, field in fields.items():
        if key in ("payment_msg", "desc", "summary"):
            continue
        try:
            value = order.get_field_value_any(key)
        except Exception:
            value = getattr(field, "value", None)
            if isinstance(value, dict):
                value = " ".join(str(i) for i in value.values() if i)
        name = getattr(field, "name", None) or str(key)
        if value:
            result.append((str(name), str(value)))
    return result


def extract_invite_url(order, settings: dict) -> str | None:
    """
    Ищет ссылку покупателя: сначала в полях с подходящим названием, затем в любом поле,
    затем в параметрах/описании заказа.

    :param order: объект заказа (:class:`FunPayAPI.types.Order` или OrderShortcut).
    :param settings: настройки плагина.
    """
    field_names = [str(i).strip().lower() for i in settings.get("invite_field_names", []) if str(i).strip()]
    fields = order_field_texts(order)

    for name, value in fields:
        if any(marker in name.lower() for marker in field_names):
            found = find_invite_url(value) or (value.strip() if "steam" in value.lower() else None)
            if found:
                return found
    for _name, value in fields:
        found = find_invite_url(value)
        if found:
            return found
    for text in (getattr(order, "description", None), getattr(order, "short_description", None),
                 getattr(order, "lot_params_text", None)):
        found = find_invite_url(text)
        if found:
            return found
    return None


def extract_region(order, settings: dict) -> str | None:
    """Ищет регион покупателя в полях заказа (2 буквы)."""
    markers = [str(i).strip().lower() for i in settings.get("region_field_names", []) if str(i).strip()]
    for name, value in order_field_texts(order):
        if not any(marker in name.lower() for marker in markers):
            continue
        match = re.search(r"\b([A-Za-z]{2})\b", value)
        if match:
            return match.group(1).upper()
    return None


# ══════════════════════════════ ПРИВЯЗКИ ЛОТОВ ══════════════════════════════
def find_mapping(settings: dict, order, event=None) -> tuple[str, dict] | None:
    """
    Ищет привязку лота к игре Steam.

    Порядок: точное совпадение с названием лота из auto_delivery.cfg, затем ID лота,
    затем самое длинное вхождение ключа привязки в описание заказа.

    :param settings: настройки плагина.
    :param order: объект заказа.
    :param event: событие нового заказа (опционально).

    :return: (ключ привязки, словарь привязки) или None.
    """
    mappings = settings.get("mappings") or {}
    if not mappings:
        return None

    lot_name = str(getattr(event, "config_section_name", "") or "")
    if lot_name and lot_name in mappings:
        return lot_name, mappings[lot_name]

    lot_id = getattr(event, "lot_id", None)
    if lot_id is not None and str(lot_id) in mappings:
        return str(lot_id), mappings[str(lot_id)]

    description = str(getattr(order, "description", "") or "")
    candidates = [key for key in mappings if key and key in description]
    if candidates:
        key = max(candidates, key=len)
        return key, mappings[key]
    if lot_name:
        candidates = [key for key in mappings if key and key in lot_name]
        if candidates:
            key = max(candidates, key=len)
            return key, mappings[key]
    return None


def resolve_edition(editions: list, mapping: dict, region: str) -> tuple[dict, dict]:
    """
    Выбирает издание игры (package_id) и цену для региона.

    :param editions: ответ Get Game By App ID (список изданий).
    :param mapping: привязка лота (может содержать package_id или название издания).
    :param region: регион получателя.

    :return: (издание, информация о регионе с ценой).
    """
    if not editions:
        raise GiftConfigError("DesslyHub не вернул ни одного издания игры - проверьте app_id")

    package_id = mapping.get("package_id")
    wanted = str(mapping.get("edition") or "").strip().lower()
    chosen = None
    if package_id:
        for edition in editions:
            if str(edition.get("package_id")) == str(package_id):
                chosen = edition
                break
        if chosen is None:
            raise GiftConfigError(f"издание с package_id={package_id} не найдено в ответе DesslyHub")
    elif wanted:
        for edition in editions:
            if str(edition.get("edition", "")).strip().lower() == wanted:
                chosen = edition
                break
        if chosen is None:
            for edition in editions:
                if wanted in str(edition.get("edition", "")).lower():
                    chosen = edition
                    break
        if chosen is None:
            available = ", ".join(str(i.get("edition")) for i in editions[:8])
            raise GiftConfigError(f"издание «{mapping.get('edition')}» не найдено. Доступны: {available}")
    elif len(editions) == 1:
        chosen = editions[0]
    else:
        available = ", ".join(str(i.get("edition")) for i in editions[:8])
        raise GiftConfigError(f"у игры несколько изданий - укажите нужное в привязке лота. Доступны: {available}")

    for info in chosen.get("regions_info") or []:
        if str(info.get("region", "")).upper() == str(region).upper():
            return chosen, info
    available_regions = ", ".join(sorted({str(i.get("region", "?")).upper()
                                          for i in chosen.get("regions_info") or []})[:20])
    raise GiftConfigError(f"регион {region} недоступен для этого издания. Доступны: {available_regions}")


# ══════════════════════════════ УТИЛИТЫ ══════════════════════════════
def error_text(code: int | None, extra: str = "") -> str:
    """Понятное описание ошибки DesslyHub."""
    if code is None:
        return extra or "неизвестная ошибка"
    text = ERROR_CODES.get(int(code), f"неизвестная ошибка (код {code})")
    return f"{text} (код {code}){f': {extra}' if extra else ''}"


def format_price(value: Any) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def notify(c: Cardinal, text: str, critical: bool = False) -> None:
    """Отправляет уведомление в Telegram (в отдельном потоке)."""
    if not c.telegram:
        return
    notification_type = tg_utils.NotificationTypes.critical if critical else tg_utils.NotificationTypes.other
    threading.Thread(target=c.telegram.send_notification, args=(text,),
                     kwargs={"notification_type": notification_type}, daemon=True).start()


def mask_key(apikey: str) -> str:
    """Маскирует API-ключ для вывода в меню/логи."""
    apikey = str(apikey or "")
    if not apikey:
        return "не задан"
    if len(apikey) <= 4:
        return "*" * len(apikey)
    return f"{apikey[:2]}{'*' * 6}{apikey[-2:]}"


def to_usd(amount: float, currency_code: int, client: DesslyClient) -> float | None:
    """
    Переводит сумму из валюты заказа в доллары (валюта баланса DesslyHub).

    :return: сумму в USD или None, если курс получить не удалось.
    """
    if currency_code == CURRENCY_CODES["USD"]:
        return float(amount)
    try:
        rate = client.exchange_rate(currency_code)
    except DesslyError as e:
        logger.warning(f"[SteamGift] Не удалось получить курс валюты {currency_code}: {e}")
        return None
    if not rate:
        return None
    return float(amount) / float(rate)


def send_buyer_message(c: Cardinal, record: dict, template: str) -> bool:
    """
    Отправляет сообщение покупателю по шаблону (переменные Кардинала + $order / $game).

    :return: True, если сообщение отправлено.
    """
    if not template or not template.strip():
        return False
    chat_id = record.get("chat_id")
    buyer = record.get("buyer")
    if not chat_id:
        return False
    try:
        order = c.account.get_order(str(record.get("order_id")))
        text = cardinal_tools_format(template, order) if order is not None else template
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        text = template
    text = text.replace("$order", str(record.get("order_id", ""))).replace("$game",
                                                                           str(record.get("edition", "")))
    result = c.send_message(chat_id, text, buyer)
    return bool(result)


def cardinal_tools_format(template: str, order) -> str:
    """Форматирует текст штатной функцией Кардинала (с запасным вариантом)."""
    try:
        from Utils import cardinal_tools
        return cardinal_tools.format_order_text(template, order)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
        return template.replace("$username", str(getattr(order, "buyer_username", "") or ""))


# ══════════════════════════════ ЛОГИКА ВЫДАЧИ ══════════════════════════════
def resolve_chat_id(c: Cardinal, order) -> int | str | None:
    """Определяет ID чата покупателя (как это делает ядро Кардинала)."""
    try:
        chat = c.account.get_chat_by_name(order.buyer_username)
        if chat:
            return chat.id
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
    return getattr(order, "chat_id", None)


def handle_new_order(c: Cardinal, e: NewOrderEvent) -> None:
    """Хэндлер нового заказа: если лот привязан к игре - запускаем сценарий выдачи гифта."""
    settings = load_settings()
    if not settings.get("enabled"):
        return
    order = getattr(e, "order", None)
    if order is None or order.id == "ADTEST":
        return
    if get_record(order.id):
        return  # заказ уже обработан (или обрабатывается)

    found = find_mapping(settings, order, e)
    if not found:
        return
    lot_key, mapping = found
    if not mapping.get("app_id"):
        set_record(order.id, status="error", error_text=f"в привязке лота «{lot_key}» не указан app_id")
        notify(c, f"❌ <b>SteamGift</b>: в привязке лота <code>{tg_utils.escape(lot_key)}</code> "
                  f"не указан app_id игры. Заказ <code>{tg_utils.escape(order.id)}</code> не обработан.",
               True)
        return

    set_record(order.id, status="new", buyer=order.buyer_username,
               buyer_id=order.buyer_id, lot=lot_key, mapping=dict(mapping),
               funpay_price=float(order.price or 0), funpay_currency=str(order.currency),
               amount=int(order.amount or 1), dry_run=bool(settings.get("dry_run")),
               chat_id=order.chat_id)
    logger.info(f"[SteamGift] Заказ $YELLOW#{order.id}$RESET подходит под привязку «{lot_key}» "
                f"(app_id={mapping.get('app_id')}) - запускаю выдачу гифта.")
    threading.Thread(target=start_order, args=(c, str(order.id)), daemon=True,
                     name=f"SteamGift-{order.id}").start()


def start_order(c: Cardinal, order_id: str) -> None:
    """
    Основной сценарий: получить заказ -> найти ссылку/регион -> проверить лимиты ->
    купить гифт -> записать транзакцию. Выполняется в отдельном потоке.
    """
    settings = load_settings()
    if not settings.get("enabled"):
        return
    record = get_record(order_id)
    if not record:
        return
    if record.get("tx") or record.get("status") in FINAL_STATUSES:
        logger.info(f"[SteamGift] Заказ #{order_id} уже обработан (статус {record.get('status')}) - "
                    f"повторная покупка отменена.")
        return

    # черный список
    if record.get("buyer") and record.get("buyer") in (getattr(c, "blacklist", None) or []):
        set_record(order_id, status="skipped", error_text="покупатель в черном списке")
        notify(c, f"⛔ <b>SteamGift</b>: покупатель <code>{tg_utils.escape(str(record.get('buyer')))}</code> "
                  f"в черном списке - гифт по заказу <code>{tg_utils.escape(order_id)}</code> не отправлен.",
               True)
        return

    # полный объект заказа (нужны поля, заполненные покупателем)
    order = None
    for attempt in range(2, -1, -1):
        try:
            order = c.account.get_order(order_id)
            break
        except Exception:
            logger.warning(f"[SteamGift] Не удалось получить заказ #{order_id}. Осталось попыток: {attempt}")
            logger.debug("TRACEBACK", exc_info=True)
            time.sleep(1)
    if order is None:
        set_record(order_id, status="error", error_text="не удалось получить заказ из FunPay")
        notify(c, f"❌ <b>SteamGift</b>: не удалось получить заказ <code>{tg_utils.escape(order_id)}</code> "
                  f"из FunPay. Обработайте заказ вручную.", True)
        return

    chat_id = record.get("chat_id") or resolve_chat_id(c, order)
    mapping = record.get("mapping") or {}
    region = (str(mapping.get("region") or "").strip().upper() or extract_region(order, settings)
              or str(settings.get("default_region", "RU")).strip().upper())
    invite_url = record.get("invite_url") or extract_invite_url(order, settings)

    if chat_id:
        set_record(order_id, chat_id=chat_id)

    if not invite_url:
        if settings.get("ask_for_link") and not record.get("asked_link"):
            text = cardinal_tools_format(str(settings.get("ask_link_text", "")), order)
            sent = c.send_message(chat_id, text, record.get("buyer")) if chat_id else None
            set_record(order_id, status="waiting_link", asked_link=True, asked_at=int(time.time()),
                       region=region, ask_sent=bool(sent))
            logger.info(f"[SteamGift] Заказ #{order_id}: ссылка не найдена - попросил покупателя в чате.")
            notify(c, f"🔗 <b>SteamGift</b>: по заказу <code>{tg_utils.escape(order_id)}</code> нет ссылки "
                      f"«добавить в друзья» - покупателю отправлен запрос. Жду {settings.get('wait_link_minutes', 60)} мин.")
        else:
            set_record(order_id, status="error", region=region,
                       error_text="не найдена ссылка «добавить в друзья»")
            notify(c, f"❌ <b>SteamGift</b>: по заказу <code>{tg_utils.escape(order_id)}</code> нет ссылки "
                      f"«добавить в друзья» (поля заказа пусты). Отправьте гифт вручную.", True)
        return

    set_record(order_id, invite_url=invite_url, region=region, status="sending")

    client = make_client(settings)

    # баланс
    try:
        balance = client.balance()
    except DesslyError as e:
        fail_order(c, order_id, e.code, e.message)
        return
    min_balance = float(settings.get("min_balance_usd", 0) or 0)
    if min_balance and balance < min_balance:
        set_record(order_id, status="error", error_code=-2, balance=balance,
                   error_text=f"баланс DesslyHub {format_price(balance)} $ ниже порога "
                              f"{format_price(min_balance)} $")
        notify(c, f"💸 <b>SteamGift</b>: недостаточно средств на балансе DesslyHub "
                  f"({format_price(balance)} $ при пороге {format_price(min_balance)} $). "
                  f"Заказ <code>{tg_utils.escape(order_id)}</code> НЕ отправлен - пополните баланс.", True)
        return

    # издание, регион и цена
    try:
        editions = client.game(mapping.get("app_id"))
        edition, region_info = resolve_edition(editions, mapping, region)
    except DesslyError as e:
        fail_order(c, order_id, e.code, e.message)
        return
    except GiftConfigError as e:
        set_record(order_id, status="error", error_text=str(e))
        notify(c, f"❌ <b>SteamGift</b>: не удалось выбрать издание для заказа "
                  f"<code>{tg_utils.escape(order_id)}</code> (лот <code>{tg_utils.escape(str(record.get('lot')))}</code>, "
                  f"регион {tg_utils.escape(region)}):\n{tg_utils.escape(str(e))}", True)
        return

    try:
        price = float(region_info.get("price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    set_record(order_id, app_id=mapping.get("app_id"), edition=edition.get("edition"),
               package_id=edition.get("package_id"), price_usd=price, balance_before=balance,
               discount=region_info.get("discount"))

    # лимит стоимости одной покупки
    max_cost = float(settings.get("max_order_cost_usd", 0) or 0)
    if max_cost and price > max_cost:
        set_record(order_id, status="error",
                   error_text=f"цена гифта {format_price(price)} $ больше лимита {format_price(max_cost)} $")
        notify(c, f"🛡 <b>SteamGift</b>: цена гифта {format_price(price)} $ превышает лимит "
                  f"{format_price(max_cost)} $ - заказ <code>{tg_utils.escape(order_id)}</code> НЕ отправлен.",
               True)
        return

    # минимальная маржа
    margin = float(settings.get("min_margin_percent", 0) or 0)
    if margin > 0 and price > 0:
        income_usd = to_usd(float(record.get("funpay_price", 0) or 0),
                            int(settings.get("funpay_currency_code", 5)), client)
        if income_usd is None:
            set_record(order_id, status="error", error_text="не удалось получить курс валюты для проверки маржи")
            notify(c, f"⚠️ <b>SteamGift</b>: не удалось проверить маржу по заказу "
                      f"<code>{tg_utils.escape(order_id)}</code> (нет курса валюты) - покупка отменена.", True)
            return
        profit = (income_usd - price) / price * 100
        set_record(order_id, income_usd=round(income_usd, 4), margin_percent=round(profit, 2))
        if profit < margin:
            set_record(order_id, status="error",
                       error_text=f"маржа {profit:.1f}% ниже минимальной {margin:.1f}%")
            notify(c, f"🛡 <b>SteamGift</b>: маржа {profit:.1f}% ниже порога {margin:.1f}% "
                      f"(цена гифта {format_price(price)} $, доход {format_price(income_usd)} $) - "
                      f"заказ <code>{tg_utils.escape(order_id)}</code> НЕ отправлен.", True)
            return

    # DRY-RUN: покупаем «понарошку»
    if settings.get("dry_run"):
        fake_tx = f"DRYRUN-{uuid_lib.uuid4()}"
        set_record(order_id, status="dry_run", tx=fake_tx, finished=int(time.time()))
        logger.info(f"[SteamGift] DRY-RUN заказ #{order_id}: гифт «{edition.get('edition')}» "
                    f"(package_id={edition.get('package_id')}, регион {region}, цена {price} $) "
                    f"НЕ отправлен - включен тестовый режим.")
        notify(c, f"🧪 <b>SteamGift · DRY-RUN</b>\n"
                  f"Заказ: <code>{tg_utils.escape(order_id)}</code> "
                  f"({tg_utils.escape(str(record.get('buyer') or ''))})\n"
                  f"Лот: <code>{tg_utils.escape(str(record.get('lot') or ''))}</code>\n"
                  f"Игра: <b>{tg_utils.escape(str(edition.get('edition')))}</b> "
                  f"(app_id {mapping.get('app_id')}, package_id {edition.get('package_id')})\n"
                  f"Регион: <code>{tg_utils.escape(region)}</code>  |  Цена: <b>{format_price(price)} $</b>\n"
                  f"Баланс DesslyHub: {format_price(balance)} $\n"
                  f"Ссылка: <code>{tg_utils.escape(invite_url[:80])}</code>\n\n"
                  f"<i>Покупка НЕ совершена. Выключите DRY-RUN в меню /gift, когда всё верно.</i>")
        return

    # боевая отправка
    try:
        response = client.send_gift(invite_url, edition.get("package_id"), region, reference=order_id)
    except DesslyError as e:
        fail_order(c, order_id, e.code, e.message)
        return

    code = int(response.get("error_code") or 0)
    transaction_id = response.get("transaction_id")
    api_status = str(response.get("status") or "").lower()

    if transaction_id and code == 0:
        set_record(order_id, status="pending", tx=str(transaction_id), api_status=api_status,
                   reference=str(order_id))
        logger.info(f"[SteamGift] Заказ #{order_id}: гифт отправлен, транзакция "
                    f"$YELLOW{transaction_id}$RESET (статус {api_status or 'pending'}).")
        notify(c, f"🎁 <b>SteamGift</b>: гифт отправлен по заказу <code>{tg_utils.escape(order_id)}</code>\n"
                  f"Игра: <b>{tg_utils.escape(str(edition.get('edition')))}</b>  |  "
                  f"Регион: <code>{tg_utils.escape(region)}</code>  |  Цена: {format_price(price)} $\n"
                  f"Транзакция: <code>{tg_utils.escape(str(transaction_id))}</code> — слежу за статусом.")
        check_transaction(c, order_id)
    elif api_status in FINAL_STATUSES:
        finalize_transaction(c, order_id, api_status, code)
    else:
        fail_order(c, order_id, code or -1, error_text(code or -1))


def fail_order(c: Cardinal, order_id: str, code: int | None, message: str = "") -> None:
    """Фиксирует ошибку отправки и уведомляет продавца (и покупателя - если задан текст)."""
    settings = load_settings()
    record = set_record(order_id, status="failed", error_code=code, error_text=message or error_text(code))
    logger.error(f"[SteamGift] Заказ #{order_id}: не удалось отправить гифт - {message or error_text(code)}")
    if settings.get("notify_failed", True):
        notify(c, f"🚨 <b>SteamGift</b>: гифт по заказу <code>{tg_utils.escape(order_id)}</code> НЕ отправлен.\n"
                  f"Покупатель: <code>{tg_utils.escape(str(record.get('buyer') or ''))}</code>\n"
                  f"Лот: <code>{tg_utils.escape(str(record.get('lot') or ''))}</code>\n"
                  f"Ошибка: {tg_utils.escape(message or error_text(code))}\n"
                  f"<i>Деньги за гифт не списаны (или возвращены). Обработайте заказ вручную.</i>", True)
    if settings.get("failed_text"):
        try:
            send_buyer_message(c, record, str(settings["failed_text"]))
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def check_transaction(c: Cardinal, order_id: str) -> str | None:
    """
    Однократно проверяет статус транзакции и завершает заказ, если статус финальный.

    :return: статус транзакции или None.
    """
    record = get_record(order_id)
    if not record or not record.get("tx") or str(record.get("tx", "")).startswith("DRYRUN-"):
        return record.get("status") if record else None
    if record.get("status") in FINAL_STATUSES:
        return record.get("status")

    settings = load_settings()
    client = make_client(settings)
    try:
        status = client.transaction_status(record["tx"])
    except DesslyError as e:
        logger.warning(f"[SteamGift] Заказ #{order_id}: не удалось проверить статус транзакции - {e}")
        set_record(order_id, last_error=str(e.message))
        return None
    if not status:
        return None
    if status in FINAL_STATUSES:
        finalize_transaction(c, order_id, status)
    else:
        set_record(order_id, status="pending", api_status=status)
    return status


def finalize_transaction(c: Cardinal, order_id: str, status: str, code: int | None = None) -> None:
    """Завершает заказ по финальному статусу транзакции."""
    settings = load_settings()
    extra = {"error_code": code, "error_text": error_text(code)} if code else {}
    record = set_record(order_id, status=status, finished=int(time.time()), **extra)
    logger.info(f"[SteamGift] Заказ #{order_id}: статус транзакции -> $YELLOW{status}$RESET"
                + (f" ({extra.get('error_text')})" if code else ""))

    if status == "success":
        if settings.get("notify_success", True):
            notify(c, f"✅ <b>SteamGift</b>: гифт по заказу <code>{tg_utils.escape(order_id)}</code> "
                      f"успешно отправлен!\n"
                      f"Игра: <b>{tg_utils.escape(str(record.get('edition') or ''))}</b>  |  "
                      f"Цена: {format_price(record.get('price_usd', 0))} $\n"
                      f"Покупатель: <code>{tg_utils.escape(str(record.get('buyer') or ''))}</code>")
        if not record.get("dry_run") and settings.get("success_text"):
            try:
                send_buyer_message(c, record, str(settings["success_text"]))
            except Exception:
                logger.error(f"[SteamGift] Заказ #{order_id}: не удалось отправить сообщение покупателю.")
                logger.debug("TRACEBACK", exc_info=True)
    else:
        text = "отменена, деньги возвращены на баланс" if status == "cancelled" else "завершилась ошибкой"
        if settings.get("notify_failed", True):
            notify(c, f"🚨 <b>SteamGift</b>: транзакция по заказу <code>{tg_utils.escape(order_id)}</code> "
                      f"{text}.\n"
                      f"Игра: <b>{tg_utils.escape(str(record.get('edition') or ''))}</b>  |  "
                      f"Покупатель: <code>{tg_utils.escape(str(record.get('buyer') or ''))}</code>\n"
                      f"Транзакция: <code>{tg_utils.escape(str(record.get('tx') or ''))}</code>\n"
                      + (f"Причина: {tg_utils.escape(error_text(code))}\n" if code else "")
                      + f"<i>Обработайте заказ вручную (возможно, покупатель не принял гифт или "
                        f"игра у него уже есть).</i>", True)
        if settings.get("failed_text"):
            try:
                send_buyer_message(c, record, str(settings["failed_text"]))
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)


def check_low_balance(c: Cardinal) -> None:
    """Периодическая проверка баланса DesslyHub (алерт при низком остатке)."""
    settings = load_settings()
    global _last_low_balance_alert
    min_balance = float(settings.get("min_balance_usd", 0) or 0)
    if not settings.get("enabled") or not settings.get("apikey") or not min_balance:
        return
    cooldown = int(settings.get("low_balance_cooldown", 120)) * 60
    if time.time() - _last_low_balance_alert < cooldown:
        return
    try:
        balance = make_client(settings).balance()
    except DesslyError as e:
        logger.warning(f"[SteamGift] Не удалось проверить баланс DesslyHub: {e}")
        return
    if balance < min_balance:
        _last_low_balance_alert = time.time()
        notify(c, f"💸 <b>SteamGift</b>: низкий баланс DesslyHub — {format_price(balance)} $ "
                  f"(порог {format_price(min_balance)} $). Пополните, иначе гифты не отправятся.", True)


def poller_loop(c: Cardinal, run_id: int) -> None:
    """Фоновый поток: опрос статусов транзакций, контроль ожидания ссылки и баланса."""
    logger.info("[SteamGift] Поток опроса транзакций запущен.")
    tick = 0
    while True:
        if c.run_id != run_id:
            logger.info("[SteamGift] Поток опроса транзакций остановлен.")
            return
        try:
            settings = load_settings()
            interval = max(3, int(settings.get("poll_interval", 10)))
            orders = load_orders()
            now = int(time.time())

            for order_id, record in list(orders.items()):
                if c.run_id != run_id:
                    return
                status = record.get("status")
                if status == "pending" and record.get("tx"):
                    age = now - int(record.get("created", now))
                    if age > int(settings.get("poll_max_minutes", 90)) * 60:
                        set_record(order_id, status="timeout",
                                   error_text=f"статус не получен за {settings.get('poll_max_minutes')} мин.")
                        notify(c, f"⏳ <b>SteamGift</b>: транзакция <code>"
                                  f"{tg_utils.escape(str(record.get('tx')))}</code> по заказу "
                                  f"<code>{tg_utils.escape(order_id)}</code> всё ещё не завершилась. "
                                  f"Проверьте статус вручную: /gift -> 🧾 Транзакции.", True)
                        continue
                    check_transaction(c, order_id)
                elif status == "waiting_link":
                    asked_at = int(record.get("asked_at", record.get("created", now)))
                    if now - asked_at > int(settings.get("wait_link_minutes", 60)) * 60:
                        set_record(order_id, status="timeout_link",
                                   error_text="покупатель не прислал ссылку вовремя")
                        notify(c, f"⌛ <b>SteamGift</b>: покупатель по заказу "
                                  f"<code>{tg_utils.escape(order_id)}</code> не прислал ссылку "
                                  f"«добавить в друзья» за {settings.get('wait_link_minutes')} мин. "
                                  f"Обработайте заказ вручную.", True)

            tick += 1
            if settings.get("enabled") and tick % 30 == 0:  # примерно раз в 5 минут при интервале 10 сек
                check_low_balance(c)
        except Exception:
            logger.error("[SteamGift] Ошибка в потоке опроса транзакций.")
            logger.debug("TRACEBACK", exc_info=True)

        for _ in range(max(3, int(load_settings().get("poll_interval", 10)))):
            if c.run_id != run_id:
                return
            time.sleep(1)


# ══════════════════════════════ ХЭНДЛЕРЫ СОБЫТИЙ ══════════════════════════════
def on_new_order(c: Cardinal, e: NewOrderEvent) -> None:
    """Обертка над handle_new_order (чтобы исключения не ломали обработку событий)."""
    try:
        handle_new_order(c, e)
    except Exception:
        logger.error("[SteamGift] Ошибка при обработке нового заказа.")
        logger.debug("TRACEBACK", exc_info=True)


def on_new_message(c: Cardinal, e: NewMessageEvent | LastChatMessageChangedEvent) -> None:
    """
    Следит за чатами, где ждём ссылку «добавить в друзья»:
    как только покупатель прислал ссылку - продолжаем выдачу.
    """
    try:
        settings = load_settings()
        if not settings.get("enabled"):
            return
        if isinstance(e, NewMessageEvent):
            message = e.message
            chat_id, text, author_id = message.chat_id, str(message), getattr(message, "author_id", None)
            by_me = bool(getattr(message, "by_bot", False)) or author_id == getattr(c.account, "id", None)
        else:
            if not c.old_mode_enabled:
                return
            chat = e.chat
            chat_id, text, by_me = chat.id, str(chat), bool(getattr(chat, "last_by_bot", False))
        if by_me:
            return

        waiting = [oid for oid, rec in load_orders().items()
                   if rec.get("status") == "waiting_link" and str(rec.get("chat_id")) == str(chat_id)]
        if not waiting:
            return
        invite_url = find_invite_url(text)
        if not invite_url:
            return
        order_id = waiting[0]
        region = get_record(order_id).get("region") or extract_region_from_text(text) or \
            str(settings.get("default_region", "RU"))
        set_record(order_id, invite_url=invite_url, region=region, status="sending")
        logger.info(f"[SteamGift] Заказ #{order_id}: покупатель прислал ссылку - продолжаю выдачу.")
        threading.Thread(target=start_order, args=(c, order_id), daemon=True,
                         name=f"SteamGift-{order_id}").start()
    except Exception:
        logger.error("[SteamGift] Ошибка при обработке сообщения покупателя.")
        logger.debug("TRACEBACK", exc_info=True)


def extract_region_from_text(text: str | None) -> str | None:
    """Ищет регион (2 буквы) в сообщении покупателя рядом со словами «регион»/«region»."""
    if not text:
        return None
    match = re.search(r"(?:регион|region|страна|country)[^\w]{0,5}\b([A-Za-z]{2})\b", str(text), re.I)
    if match:
        return match.group(1).upper()
    return None


# ══════════════════════════════ TELEGRAM: МЕНЮ ПЛАГИНА ══════════════════════════════
CB = "DSGift"
"""Префикс callback'ов плагина (не пересекается с числовыми CBT Кардинала)."""

INPUT_STATE = f"{CB}_input"
"""User-state для ввода значений с клавиатуры."""

INPUT_FIELDS = {
    "apikey": ("🔑 API-ключ DesslyHub", "ключ", str,
               "Выдаёт менеджер DesslyHub: https://t.me/DesslyHub_Manager"),
    "default_region": ("🌍 Регион по умолчанию", "регион (2 буквы, например RU)", str,
                       "Используется, если регион не указан в привязке лота и не найден в заказе."),
    "max_order_cost_usd": ("🛡 Лимит цены гифта, $", "число (0 = без лимита)", float,
                           "Защита от дорогой покупки при ошибке в привязке."),
    "min_balance_usd": ("💸 Порог баланса, $", "число (0 = не проверять)", float,
                        "При балансе ниже порога гифты не отправляются, приходит алерт."),
    "min_margin_percent": ("📉 Минимальная маржа, %", "число (0 = не проверять)", float,
                           "Сравнивает цену гифта (USD) с ценой заказа FunPay, переведенной по курсу "
                           "DesslyHub. Курс берётся для валюты из настройки funpay_currency_code."),
    "funpay_currency_code": ("💱 Код валюты цены заказа", "число (5=RUB, 1=USD, 37=KZT, 18=UAH, 3=EUR)",
                             int, "Коды валют DesslyHub: 1 USD, 3 EUR, 5 RUB, 18 UAH, 37 KZT."),
    "wait_link_minutes": ("⌛ Ожидание ссылки, минут", "число", int,
                          "Сколько ждать ссылку «добавить в друзья» от покупателя."),
    "ask_link_text": ("🔗 Текст запроса ссылки", "текст (доступен $username)", str,
                      "Отправляется покупателю, если в заказе нет ссылки."),
    "success_text": ("✅ Текст после успешной отправки", "текст ($username, $order, $game)", str,
                     "Пустой текст = ничего не отправлять покупателю."),
    "failed_text": ("🚨 Текст при ошибке", "текст ($username, $order, $game)", str,
                    "Пустой текст (по умолчанию) = покупателю не пишем, разбирается продавец."),
    "mapping": ("🎮 Привязка лота", "формат: название лота | app_id | издание | регион", str,
                "Пример: Steam Gift Cyberpunk 2077 | 1091500 |  | RU\n"
                "Издание и регион можно не указывать. Название лота = как в описании заказа "
                "(или секция auto_delivery.cfg, или ID лота)."),
    "games_search": ("🔎 Поиск игры в каталоге DesslyHub", "название игры (часть названия)", str,
                     "Покажет appid найденных игр - его нужно указать в привязке лота."),
}


def is_authorized(c: Cardinal, user_id: int | None) -> bool:
    """Проверяет, авторизован ли пользователь в ПУ Telegram."""
    if not c.telegram or user_id is None:
        return False
    return user_id in c.telegram.authorized_users


def stats_line() -> str:
    """Короткая статистика по журналу заказов."""
    orders = load_orders()
    counts: dict[str, int] = {}
    for record in orders.values():
        key = str(record.get("status", "?"))
        counts[key] = counts.get(key, 0) + 1
    return (f"🧾 В обработке: <b>{counts.get('pending', 0)}</b>  |  "
            f"ждём ссылку: <b>{counts.get('waiting_link', 0)}</b>\n"
            f"✅ Успешно: <b>{counts.get('success', 0)}</b>  |  "
            f"🚨 Ошибок: <b>{counts.get('failed', 0) + counts.get('error', 0)}</b>  |  "
            f"🧪 DRY-RUN: <b>{counts.get('dry_run', 0)}</b>")


def menu_text(c: Cardinal) -> str:
    settings = load_settings()
    balance_text = "не проверялся"
    balance = settings.get("_last_balance")
    if balance is not None:
        balance_text = f"{format_price(balance)} $"
    state = "🟢 включен" if settings.get("enabled") else "🔴 ВЫКЛЮЧЕН"
    mode = "🧪 DRY-RUN (деньги не тратятся)" if settings.get("dry_run") else "💸 БОЕВОЙ РЕЖИМ"
    return f"""🎁 <b>SteamGift</b> · автовыдача Steam-гифтов через DesslyHub <i>v{VERSION}</i>

Состояние: {state}
Режим: {mode}
🔑 API-ключ: <code>{tg_utils.escape(mask_key(settings.get('apikey', '')))}</code>
💳 Баланс DesslyHub: <b>{balance_text}</b>
🌍 Регион по умолчанию: <code>{tg_utils.escape(str(settings.get('default_region', '')))}</code>
🎮 Привязок лотов: <b>{len(settings.get('mappings') or {})}</b>
🛡 Лимит цены: <b>{format_price(settings.get('max_order_cost_usd', 0))} $</b>  |  порог баланса: <b>{format_price(settings.get('min_balance_usd', 0))} $</b>
📉 Мин. маржа: <b>{settings.get('min_margin_percent', 0)}%</b>

{stats_line()}

<i>Лоты с гифтами НЕ должны быть в auto_delivery.cfg. Документация API: desslyhub.readme.io</i>"""


def menu_keyboard(c: Cardinal) -> K:
    settings = load_settings()
    kb = K()
    kb.row(B("🟢 Выключить" if settings.get("enabled") else "🔴 Включить", None, f"{CB}:toggle_enabled"),
           B("🧪 DRY-RUN: ВКЛ" if settings.get("dry_run") else "💸 DRY-RUN: ВЫКЛ", None, f"{CB}:toggle_dry"))
    kb.row(B("🔑 API-ключ", None, f"{CB}:input:apikey"),
           B("💳 Баланс", None, f"{CB}:balance"))
    kb.row(B("🧪 Тест подключения", None, f"{CB}:test"),
           B("🔄 Проверить транзакции", None, f"{CB}:check_now"))
    kb.add(B(f"🎮 Привязки лотов ({len(settings.get('mappings') or {})})", None, f"{CB}:mappings"))
    kb.add(B("🧾 Транзакции", None, f"{CB}:transactions"))
    kb.add(B("🛡 Лимиты и защита", None, f"{CB}:limits"))
    kb.add(B("💬 Тексты сообщений", None, f"{CB}:texts"))
    kb.add(B("🔙 Назад", None, f"{CBT.PLUGINS_LIST}:0"))
    return kb


def answer(c: Cardinal, obj: Message | CallbackQuery, text: str, kb: K | None = None) -> None:
    """Отправляет или обновляет сообщение меню."""
    tg = c.telegram
    if isinstance(obj, CallbackQuery):
        try:
            tg.bot.edit_message_text(text, obj.message.chat.id, obj.message.id, reply_markup=kb,
                                     parse_mode="HTML")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
        try:
            tg.bot.answer_callback_query(obj.id)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
    else:
        tg.bot.send_message(obj.chat.id, text, reply_markup=kb, parse_mode="HTML")


def open_menu(c: Cardinal, obj: Message | CallbackQuery) -> None:
    if not is_authorized(c, obj.from_user.id):
        return
    answer(c, obj, menu_text(c), menu_keyboard(c))


def toggle_setting(c: Cardinal, obj: CallbackQuery, key: str) -> None:
    settings = load_settings()
    update_setting(key, not bool(settings.get(key)))
    settings = load_settings()
    if key == "enabled" and settings.get("enabled") and settings.get("dry_run"):
        logger.info("[SteamGift] Плагин включен в режиме DRY-RUN - покупки не совершаются.")
    open_menu(c, obj)


def do_balance(c: Cardinal, obj: CallbackQuery) -> None:
    """Проверяет баланс DesslyHub (в отдельном потоке)."""
    if not is_authorized(c, obj.from_user.id):
        return

    def worker():
        settings = load_settings()
        try:
            balance = make_client(settings).balance()
        except DesslyError as e:
            answer(c, obj, f"❌ Не удалось получить баланс: {tg_utils.escape(e.message)}",
                   menu_keyboard(c))
            return
        settings["_last_balance"] = balance
        save_settings(settings)
        answer(c, obj, f"💳 Баланс DesslyHub: <b>{format_price(balance)} $</b>\n"
                       f"Порог плагина: {format_price(settings.get('min_balance_usd', 0))} $\n"
                       f"Обновлено: {datetime.datetime.now().strftime('%H:%M:%S')}", menu_keyboard(c))

    threading.Thread(target=worker, daemon=True, name="SteamGiftBalance").start()


def do_test(c: Cardinal, obj: CallbackQuery) -> None:
    """Тест подключения: баланс + каталог игр (в отдельном потоке)."""
    if not is_authorized(c, obj.from_user.id):
        return

    def worker():
        settings = load_settings()
        client = make_client(settings)
        lines = ["🧪 <b>Тест подключения к DesslyHub</b>", ""]
        try:
            balance = client.balance()
            settings["_last_balance"] = balance
            save_settings(settings)
            lines.append(f"💳 Баланс: <b>{format_price(balance)} $</b> — ✅ OK")
        except DesslyError as e:
            lines.append(f"💳 Баланс: ❌ {tg_utils.escape(e.message)}")
        try:
            games = client.games(use_cache=False)
            lines.append(f"🎮 Каталог игр: <b>{len(games)}</b> позиций — ✅ OK")
        except DesslyError as e:
            lines.append(f"🎮 Каталог игр: ❌ {tg_utils.escape(e.message)}")
        mappings = settings.get("mappings") or {}
        if mappings:
            lines.append("")
            lines.append("<b>Проверка привязок:</b>")
            for lot, mapping in list(mappings.items())[:10]:
                region = str(mapping.get("region") or settings.get("default_region") or "").upper()
                try:
                    editions = client.game(mapping.get("app_id"))
                    edition, info = resolve_edition(editions, mapping, region)
                    lines.append(f"✅ <code>{tg_utils.escape(lot)}</code> → "
                                 f"{tg_utils.escape(str(edition.get('edition')))} · {region} · "
                                 f"{format_price(info.get('price', 0))} $")
                except (DesslyError, GiftConfigError) as e:
                    lines.append(f"❌ <code>{tg_utils.escape(lot)}</code> → {tg_utils.escape(str(getattr(e, 'message', e)))}")
        else:
            lines.append("")
            lines.append("<i>Привязок лотов пока нет — добавьте: 🎮 Привязки лотов.</i>")
        lines.append("")
        lines.append(f"<i>Режим: {'🧪 DRY-RUN' if settings.get('dry_run') else '💸 боевой'}</i>")
        answer(c, obj, "\n".join(lines), menu_keyboard(c))

    threading.Thread(target=worker, daemon=True, name="SteamGiftTest").start()


def open_mappings(c: Cardinal, obj: Message | CallbackQuery) -> None:
    if not is_authorized(c, obj.from_user.id):
        return
    settings = load_settings()
    mappings = settings.get("mappings") or {}
    lines = ["🎮 <b>Привязки лотов FunPay → игры Steam</b>", ""]
    if not mappings:
        lines.append("<i>Привязок нет. Добавьте первую — без них плагин не знает, "
                     "какую игру покупать.</i>")
    for lot, mapping in mappings.items():
        region = str(mapping.get("region") or "").upper() or f"по умолчанию ({settings.get('default_region')})"
        edition = str(mapping.get("edition") or "")
        edition_text = f" · издание «{tg_utils.escape(edition)}»" if edition else ""
        lines.append(f"• <code>{tg_utils.escape(lot)}</code>\n"
                     f"  app_id <b>{mapping.get('app_id')}</b>{edition_text} · регион {tg_utils.escape(region)}")
    kb = K()
    kb.add(B("➕ Добавить привязку", None, f"{CB}:input:mapping"))
    kb.add(B("🔎 Найти игру в каталоге", None, f"{CB}:input:games_search"))
    for lot in list(mappings)[:20]:
        kb.add(B(f"🗑 {lot[:40]}", None, f"{CB}:mapping_del:{lot}"))
    kb.add(B("🔙 Назад", None, f"{CB}:menu"))
    answer(c, obj, "\n".join(lines), kb)


def delete_mapping(c: Cardinal, obj: CallbackQuery) -> None:
    if not is_authorized(c, obj.from_user.id):
        return
    key = obj.data.split(":", 2)[2]
    settings = load_settings()
    mappings = settings.get("mappings") or {}
    if mappings.pop(key, None) is not None:
        update_setting("mappings", mappings)
        logger.info(f"[SteamGift] Удалена привязка лота $YELLOW{key}$RESET")
    open_mappings(c, obj)


def open_transactions(c: Cardinal, obj: Message | CallbackQuery) -> None:
    if not is_authorized(c, obj.from_user.id):
        return
    orders = load_orders()
    lines = ["🧾 <b>Транзакции SteamGift</b>", "", stats_line(), ""]
    if not orders:
        lines.append("<i>Заказов пока не было.</i>")
    sorted_orders = sorted(orders.items(), key=lambda i: int(i[1].get("updated", 0)), reverse=True)
    marks = {"success": "✅", "pending": "⏳", "failed": "🚨", "error": "❌", "dry_run": "🧪",
             "waiting_link": "🔗", "timeout": "⌛", "timeout_link": "⌛", "cancelled": "↩️",
             "skipped": "⛔", "sending": "📤", "new": "🆕"}
    for order_id, record in sorted_orders[:15]:
        mark = marks.get(str(record.get("status")), "•")
        when = datetime.datetime.fromtimestamp(int(record.get("updated", 0))).strftime("%d.%m %H:%M")
        price = f" · {format_price(record.get('price_usd', 0))} $" if record.get("price_usd") else ""
        lines.append(f"{mark} <code>{tg_utils.escape(order_id)}</code> {when} — "
                     f"{tg_utils.escape(str(record.get('edition') or record.get('lot') or '?'))}{price}")
        if record.get("status") in ("failed", "error", "timeout", "timeout_link"):
            lines.append(f"   <i>{tg_utils.escape(str(record.get('error_text') or ''))[:120]}</i>")
    kb = K()
    kb.add(B("🔄 Проверить статусы сейчас", None, f"{CB}:check_now"))
    kb.add(B("🔙 Назад", None, f"{CB}:menu"))
    answer(c, obj, "\n".join(lines), kb)


def do_check_now(c: Cardinal, obj: CallbackQuery) -> None:
    """Проверяет статусы всех активных транзакций (в отдельном потоке)."""
    if not is_authorized(c, obj.from_user.id):
        return

    def worker():
        orders = load_orders()
        active = [oid for oid, rec in orders.items() if rec.get("status") == "pending" and rec.get("tx")]
        result = []
        for order_id in active:
            try:
                status = check_transaction(c, order_id)
                result.append(f"#{order_id}: {status or 'нет ответа'}")
            except Exception:
                result.append(f"#{order_id}: ошибка")
                logger.debug("TRACEBACK", exc_info=True)
        text = "🔄 <b>Проверка статусов</b>\n\n" + ("\n".join(result) if result else
                                                   "<i>Активных транзакций нет.</i>")
        answer(c, obj, text, menu_keyboard(c))

    threading.Thread(target=worker, daemon=True, name="SteamGiftCheck").start()


def open_limits(c: Cardinal, obj: Message | CallbackQuery) -> None:
    if not is_authorized(c, obj.from_user.id):
        return
    settings = load_settings()
    text = f"""🛡 <b>Лимиты и защита</b>

🛡 Лимит цены одного гифта: <b>{format_price(settings.get('max_order_cost_usd', 0))} $</b>
💸 Порог баланса DesslyHub: <b>{format_price(settings.get('min_balance_usd', 0))} $</b>
📉 Минимальная маржа: <b>{settings.get('min_margin_percent', 0)}%</b>
💱 Код валюты цены заказа: <b>{settings.get('funpay_currency_code', 5)}</b>
⌛ Ожидание ссылки от покупателя: <b>{settings.get('wait_link_minutes', 60)} мин.</b>
🔔 Уведомлять об успехе: {'да' if settings.get('notify_success') else 'нет'}  |  об ошибках: {'да' if settings.get('notify_failed') else 'нет'}
🔗 Просить ссылку, если её нет в заказе: {'да' if settings.get('ask_for_link') else 'нет'}

<i>Лимиты срабатывают ДО покупки: деньги не тратятся, приходит алерт.</i>"""
    kb = K()
    kb.row(B("🛡 Лимит цены", None, f"{CB}:input:max_order_cost_usd"),
           B("💸 Порог баланса", None, f"{CB}:input:min_balance_usd"))
    kb.row(B("📉 Мин. маржа", None, f"{CB}:input:min_margin_percent"),
           B("💱 Код валюты", None, f"{CB}:input:funpay_currency_code"))
    kb.row(B("⌛ Ожидание ссылки", None, f"{CB}:input:wait_link_minutes"),
           B("🌍 Регион по умолч.", None, f"{CB}:input:default_region"))
    kb.row(B(f"🔔 Об успехе: {'вкл' if settings.get('notify_success') else 'выкл'}", None,
             f"{CB}:toggle:notify_success"),
           B(f"🔔 Об ошибках: {'вкл' if settings.get('notify_failed') else 'выкл'}", None,
             f"{CB}:toggle:notify_failed"))
    kb.row(B(f"🔗 Просить ссылку: {'вкл' if settings.get('ask_for_link') else 'выкл'}", None,
             f"{CB}:toggle:ask_for_link"))
    kb.add(B("🔙 Назад", None, f"{CB}:menu"))
    answer(c, obj, text, kb)


def open_texts(c: Cardinal, obj: Message | CallbackQuery) -> None:
    if not is_authorized(c, obj.from_user.id):
        return
    settings = load_settings()
    text = f"""💬 <b>Тексты сообщений покупателю</b>

🔗 Запрос ссылки (если её нет в заказе):
<tg-spoiler>{tg_utils.escape(str(settings.get('ask_link_text', ''))[:600])}</tg-spoiler>

✅ После успешной отправки:
<tg-spoiler>{tg_utils.escape(str(settings.get('success_text', ''))[:600]) or 'не отправляется'}</tg-spoiler>

🚨 При ошибке (по умолчанию пусто = покупателю не пишем):
<tg-spoiler>{tg_utils.escape(str(settings.get('failed_text', ''))[:600]) or 'не отправляется'}</tg-spoiler>

<i>Доступны переменные Кардинала ($username, $order_desc, $full_date_text...) и $order, $game.</i>"""
    kb = K()
    kb.add(B("🔗 Текст запроса ссылки", None, f"{CB}:input:ask_link_text"))
    kb.add(B("✅ Текст после успеха", None, f"{CB}:input:success_text"))
    kb.add(B("🚨 Текст при ошибке", None, f"{CB}:input:failed_text"))
    kb.add(B("🔙 Назад", None, f"{CB}:menu"))
    answer(c, obj, text, kb)


def ask_input(c: Cardinal, obj: CallbackQuery, field: str) -> None:
    """Просит ввести значение настройки."""
    if not is_authorized(c, obj.from_user.id):
        return
    if field not in INPUT_FIELDS:
        return
    title, placeholder, _converter, hint = INPUT_FIELDS[field]
    tg = c.telegram
    text = f"{title}\n\nВведите: <i>{tg_utils.escape(placeholder)}</i>\n\n{tg_utils.escape(hint)}\n\n" \
           f"<i>Отмена — /cancel или кнопка ниже.</i>"
    kb = K().add(B("🔙 Отмена", None, f"{CB}:menu"))
    message = tg.bot.send_message(obj.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    tg.set_state(obj.message.chat.id, message.id, obj.from_user.id, INPUT_STATE, {"field": field})
    try:
        tg.bot.answer_callback_query(obj.id)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


def handle_input(m: Message) -> None:
    """Обрабатывает введенное значение настройки."""
    c = _cardinal
    if c is None or not c.telegram or not is_authorized(c, m.from_user.id):
        return
    tg = c.telegram
    state = tg.get_state(m.chat.id, m.from_user.id)
    if not state or state.get("state") != INPUT_STATE:
        return
    field = (state.get("data") or {}).get("field")
    tg.clear_state(m.chat.id, m.from_user.id, True)
    if field not in INPUT_FIELDS:
        return

    title, _placeholder, converter, _hint = INPUT_FIELDS[field]
    value = (m.text or "").strip()
    if not value:
        tg.bot.send_message(m.chat.id, "❌ Пустое значение - ничего не изменил.", parse_mode="HTML")
        return

    try:
        if field == "mapping":
            apply_mapping(value)
            result = f"✅ Привязка добавлена:\n<code>{tg_utils.escape(value)}</code>"
            open_mappings_after_edit(c, m, result)
            return
        if field == "games_search":
            search_games(c, m, value)
            return
        converted = converter(value)
        if field in ("max_order_cost_usd", "min_balance_usd", "min_margin_percent") and converted < 0:
            raise ValueError("значение не может быть отрицательным")
        if field == "default_region":
            converted = str(value).strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", converted):
                raise ValueError("нужны 2 латинские буквы, например RU")
        if field in ("ask_link_text", "success_text", "failed_text"):
            converted = value.replace("\\n", "\n")
    except (ValueError, TypeError) as e:
        tg.bot.send_message(m.chat.id, f"❌ Не удалось сохранить {title}: {tg_utils.escape(str(e))}",
                            parse_mode="HTML")
        return

    update_setting(field, converted)
    shown = mask_key(converted) if field == "apikey" else (
        f"{converted}" if field not in ("ask_link_text", "success_text", "failed_text") else "сохранен")
    logger.info(f"[SteamGift] Настройка $YELLOW{field}$RESET изменена пользователем "
                f"$MAGENTA{m.from_user.id}$RESET")
    tg.bot.send_message(m.chat.id, f"✅ {title}: <code>{tg_utils.escape(str(shown))}</code>",
                        parse_mode="HTML", reply_markup=menu_keyboard(c))


def open_mappings_after_edit(c: Cardinal, m: Message, result: str) -> None:
    c.telegram.bot.send_message(m.chat.id, result, parse_mode="HTML")
    open_mappings(c, m)


def apply_mapping(value: str) -> None:
    """
    Разбирает строку «название лота | app_id | издание | регион» и сохраняет привязку.

    :raises ValueError: если формат неверный.
    """
    parts = [i.strip() for i in value.split("|")]
    if len(parts) < 2 or not parts[0]:
        raise ValueError("формат: название лота | app_id | издание | регион")
    lot = parts[0]
    try:
        app_id = int(parts[1])
    except ValueError:
        raise ValueError("app_id должен быть числом (например 1091500)")
    edition = parts[2] if len(parts) > 2 else ""
    region = parts[3].upper() if len(parts) > 3 and parts[3] else ""
    if region and not re.fullmatch(r"[A-Z]{2}", region):
        raise ValueError("регион - 2 латинские буквы (например RU)")
    settings = load_settings()
    mappings = dict(settings.get("mappings") or {})
    mappings[lot] = {"app_id": app_id, "edition": edition, "region": region, "package_id": 0}
    update_setting("mappings", mappings)
    logger.info(f"[SteamGift] Добавлена привязка: $YELLOW{lot}$RESET -> app_id {app_id} "
                f"(издание «{edition or 'любое'}», регион {region or 'по умолчанию'})")


def search_games(c: Cardinal, m: Message, query: str) -> None:
    """Ищет игру в каталоге DesslyHub и показывает appid (в отдельном потоке)."""
    def worker():
        settings = load_settings()
        try:
            games = make_client(settings).games()
        except DesslyError as e:
            c.telegram.bot.send_message(m.chat.id, f"❌ Не удалось получить каталог: "
                                                   f"{tg_utils.escape(e.message)}", parse_mode="HTML")
            return
        needle = query.lower()
        found = [i for i in games if needle in str(i.get("name", "")).lower()][:20]
        if not found:
            c.telegram.bot.send_message(m.chat.id, f"🔎 Ничего не нашлось по запросу "
                                                   f"<code>{tg_utils.escape(query)}</code> "
                                                   f"(в каталоге {len(games)} игр).", parse_mode="HTML")
            return
        lines = [f"🔎 Результаты по «{tg_utils.escape(query)}» (показано {len(found)}):", ""]
        for game in found:
            lines.append(f"• {tg_utils.escape(str(game.get('name')))} — "
                         f"app_id <code>{game.get('appid')}</code>")
        lines.append("")
        lines.append("<i>Добавьте привязку: 🎮 Привязки лотов → ➕ Добавить</i>")
        try:
            c.telegram.bot.send_message(m.chat.id, "\n".join(lines), parse_mode="HTML",
                                        reply_markup=menu_keyboard(c))
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

    threading.Thread(target=worker, daemon=True, name="SteamGiftSearch").start()


def toggle_option(c: Cardinal, obj: CallbackQuery, key: str) -> None:
    if not is_authorized(c, obj.from_user.id):
        return
    settings = load_settings()
    update_setting(key, not bool(settings.get(key)))
    open_limits(c, obj)


def on_settings_callback(c: Cardinal, obj: CallbackQuery) -> None:
    """Роутер callback'ов плагина."""
    if not is_authorized(c, obj.from_user.id):
        return
    parts = obj.data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"
    try:
        if action in ("menu", ""):
            open_menu(c, obj)
        elif action == "toggle_enabled":
            toggle_setting(c, obj, "enabled")
        elif action == "toggle_dry":
            toggle_setting(c, obj, "dry_run")
        elif action == "balance":
            do_balance(c, obj)
        elif action == "test":
            do_test(c, obj)
        elif action == "check_now":
            do_check_now(c, obj)
        elif action == "mappings":
            open_mappings(c, obj)
        elif action == "transactions":
            open_transactions(c, obj)
        elif action == "limits":
            open_limits(c, obj)
        elif action == "texts":
            open_texts(c, obj)
        elif action == "mapping_del":
            delete_mapping(c, obj)
        elif action == "input" and len(parts) > 2:
            ask_input(c, obj, parts[2])
        elif action == "toggle" and len(parts) > 2:
            toggle_option(c, obj, parts[2])
        else:
            open_menu(c, obj)
    except Exception:
        logger.error("[SteamGift] Ошибка при обработке callback'а меню.")
        logger.debug("TRACEBACK", exc_info=True)
        try:
            c.telegram.bot.answer_callback_query(obj.id, "Произошла ошибка. Подробности в логах бота.",
                                                 show_alert=True)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def on_gift_command(m: Message) -> None:
    """Обрабатывает команду /gift."""
    c = _cardinal
    if c is None or not c.telegram or not is_authorized(c, m.from_user.id):
        return
    open_menu(c, m)


# ══════════════════════════════ ИНИЦИАЛИЗАЦИЯ ══════════════════════════════
def recover_interrupted(c: Cardinal) -> None:
    """
    Помечает заказы, прерванные перезапуском Кардинала в момент отправки.
    Повторно покупать гифт НЕЛЬЗЯ (возможен двойной расход), поэтому заказ
    передается продавцу вручную.
    """
    orders = load_orders()
    for order_id, record in list(orders.items()):
        if record.get("status") == "sending" and not record.get("tx"):
            set_record(order_id, status="error",
                       error_text="отправка прервана перезапуском бота - проверьте транзакции DesslyHub")
            notify(c, f"⚠️ <b>SteamGift</b>: заказ <code>{tg_utils.escape(order_id)}</code> оборвался "
                      f"во время отправки гифта (перезапуск бота). Повторно покупать не стал - "
                      f"проверьте список транзакций DesslyHub и дообработайте заказ вручную.", True)


def pre_init(c: Cardinal) -> None:
    """Регистрирует команду /gift, страницу настроек и переводы."""
    global _cardinal
    _cardinal = c
    load_settings()
    load_orders()

    for language, text in (("ru", "Автовыдача Steam-гифтов через DesslyHub"),
                           ("en", "Steam gift auto-delivery via DesslyHub"),
                           ("uk", "Автовидача Steam-гіфтів через DesslyHub")):
        try:
            localizer.add_translation(UUID, "cmd_gift", text, language)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

    if not c.telegram:
        logger.info("[SteamGift] Telegram отключен - меню плагина недоступно.")
        return
    c.telegram.msg_handler(on_gift_command, commands=["gift"])
    c.telegram.cbq_handler(lambda obj: on_settings_callback(c, obj),
                           lambda call: call.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    c.telegram.cbq_handler(lambda obj: on_settings_callback(c, obj),
                           lambda call: call.data.startswith(f"{CB}:"))
    c.telegram.msg_handler(handle_input,
                           func=lambda msg: c.telegram.check_state(msg.chat.id, msg.from_user.id, INPUT_STATE))
    c.add_telegram_commands(UUID, [("gift", f"{UUID}_cmd_gift", True)])
    settings = load_settings()
    logger.info(f"[SteamGift] Команда $YELLOW/gift$RESET зарегистрирована. "
                f"enabled={settings.get('enabled')}, dry_run={settings.get('dry_run')}, "
                f"привязок лотов: {len(settings.get('mappings') or {})}")
    if settings.get("enabled") and not settings.get("apikey"):
        logger.warning("[SteamGift] Плагин включен, но API-ключ DesslyHub не задан - выдача не сработает.")


def post_start(c: Cardinal) -> None:
    """Восстанавливает прерванные заказы и запускает поток опроса транзакций."""
    global _thread_started
    recover_interrupted(c)
    if _thread_started:
        return
    _thread_started = True
    threading.Thread(target=poller_loop, args=(c, c.run_id), daemon=True, name="SteamGiftPoller").start()


def on_delete(c: Cardinal, call) -> None:
    """Удаляет файлы плагина при его удалении через ПУ Telegram."""
    import shutil
    try:
        if os.path.exists(PLUGIN_DIR):
            shutil.rmtree(PLUGIN_DIR, ignore_errors=True)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)
    try:
        c.telegram.bot.answer_callback_query(call.id, "Файлы плагина SteamGift удалены.")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


BIND_TO_PRE_INIT = [pre_init]
BIND_TO_POST_START = [post_start]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [on_new_message]
BIND_TO_DELETE = on_delete
