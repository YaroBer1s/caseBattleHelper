#!/usr/bin/env python3

#daum342
"""Слушает живую ленту дропов сайта по WebSocket, печатает короткие строки и пишет JSONL.

Установка:
    pip install requests "websockets>=14"

Запуск:
    python parse_drops.py https://site.example
    python parse_drops.py https://site.example --ws wss://ws.site.example/path
    python parse_drops.py https://site.example --raw     # показать полный JSON первых сообщений

Если адрес WebSocket со страницы не нашёлся, возьмите его вручную:
DevTools -> Network -> фильтр WS -> обновить страницу -> скопировать URL.
"""
import argparse
import asyncio
import base64
import json
import os
from os import system
import html as html_lib
import re
import sys
import time
from collections import deque
from datetime import datetime

import requests
import websockets

# Консоль Windows часто не в UTF-8, а в названиях есть кириллица и "★": без этого будет UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Расшифровка числовых кодов из enums-1177b647.js самого сайта.
SOURCE_NAMES = {
    1: "кейс", 2: "апгрейд", 3: "контракт", 4: "розыгрыш", 5: "коинфлип",
    6: "игровой сервер", 7: "замена", 8: "событие", 9: "подарок", 10: "скин-бинго",
}
RARITY_NAMES = {
    0: "?", 10: "Ширпотреб", 11: "Базовое", 20: "Промышленное", 30: "Армейское",
    40: "Запрещённое", 50: "Засекреченное", 60: "Тайное", 90: "Контрабанда",
    32: "Distinguished", 42: "Exceptional", 52: "Superior", 62: "Master",
    70: "Remarkable", 75: "Exotic", 85: "Extraordinary", 100: "Exceedingly rare",
}
TYPE_CONTAINER = 65  # asset.type: предмет сам является кейсом/контейнером

# Цвета названия по редкости (номера 256-цветной палитры ANSI).
# Группировка повторяет схему самого сайта (enums-*.js), цвета как в CS. Меняйте числа под себя.
_C = {"base": 250, "industrial": 117, "milspec": 33, "restricted": 99,
      "classified": 205, "covert": 196, "rare": 220}
RARITY_COLORS = {
    10: _C["base"], 11: _C["base"], 20: _C["industrial"],
    30: _C["milspec"], 32: _C["milspec"], 100: _C["milspec"],
    40: _C["restricted"], 42: _C["restricted"], 70: _C["restricted"], 90: _C["restricted"],
    50: _C["classified"], 52: _C["classified"], 75: _C["classified"],
    60: _C["covert"], 62: _C["covert"],
    85: _C["rare"],
}
USE_COLOR = False  # включается в main(), если вывод идёт в настоящую консоль


def setup_colors(disable: bool) -> bool:
    """Цвета только в живой консоли: не в файле/пайпе, не при NO_COLOR и не при --no-color."""
    if disable or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        pass
    if os.name == "nt":
        system("")  # включает обработку ANSI-последовательностей в консоли Windows 10+
    return True


def paint(text: str, color) -> str:
    if not USE_COLOR or color is None:
        return text
    return f"\x1b[38;5;{color}m{text}\x1b[0m"

# Если у кейса есть поле с ценой, оно будет показано. Если нет, показывается порог (case.threshold).
CASE_PRICE_KEYS = ("price", "cost", "amount")

# Заполняется автоматически из каталога кейсов на странице сайта при запуске.
# Ключ — название кейса, значение — его цена в валюте сайта.
CASE_PRICES = {}
last_rois = []
pos_neg = []

def get_case_reference_price(case: dict):
    """Ищет цену кейса по названию из каталога, включая slug из live-дропа."""
    title = str(case.get("title") or "").strip().casefold()
    slug = str(case.get("slug") or "").strip().casefold()
    for name, price in CASE_PRICES.items():
        if title == name.strip().casefold() or (slug and slug == CASE_SLUGS.get(name, "").casefold()):
            try:
                price = int(price)
            except (TypeError, ValueError):
                return None
            return price if price > 0 else None
    return None


def return_percent(item_price, case: dict):
    """Чистый процент относительно цены кейса: (предмет - кейс) / кейс * 100."""
    case_price = get_case_reference_price(case)
    if item_price is None or case_price is None:
        return None
    try:
        item_price = float(item_price)
    except (TypeError, ValueError):
        return None
    return (item_price - case_price) / case_price * 100

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def decode_live_drop_data(b64: str) -> dict:
    """Повторяет то, что делает JS на сайте: atob -> байты -> UTF-8 -> JSON."""
    return json.loads(base64.b64decode(b64).decode("utf-8"))


def normalize_ws_url(url):
    """Браузер принимает в new WebSocket() и https://..., а библиотека websockets требует ws:// или wss://."""
    if not url:
        return url
    return re.sub(r"^http(s?)://", lambda m: "ws" + m.group(1) + "://", url, flags=re.I)


CASE_SLUGS = {}


def extract_case_catalog(page_html: str) -> dict:
    """Извлекает названия, slug и цены кейсов из карточек каталога на странице."""
    catalog = {}
    slugs = {}
    # На сайте карточка выглядит как <a data-title="..." href="/case/slug"> ... price-box ...
    anchor_re = re.compile(r"<a\b(?=[^>]*\bdata-title\s*=)([^>]*)>(.*?)(?=<a\b(?=[^>]*\bdata-title\s*=)|\Z)", re.I | re.S)
    attr_title = re.compile(r"\bdata-title\s*=\s*([\"'])(.*?)\1", re.I | re.S)
    attr_href = re.compile(r"\bhref\s*=\s*([\"'])(.*?)\1", re.I | re.S)
    currency_re = re.compile(r"<span[^>]*class\s*=\s*([\"'])([^\"']*__currency[^\"']*)\1[^>]*>(.*?)</span>", re.I | re.S)
    for match in anchor_re.finditer(page_html):
        attrs, body = match.group(1), match.group(2)
        title_match, href_match = attr_title.search(attrs), attr_href.search(attrs)
        if not title_match or not href_match or not href_match.group(2).startswith("/case/"):
            continue
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", title_match.group(2))).strip()
        # Если старая цена зачёркнута, берём последнюю __currency в карточке — текущую цену.
        price_matches = [m for m in currency_re.finditer(body) if "price-old" not in m.group(2)]
        if not title or not price_matches:
            continue
        raw_price = html_lib.unescape(re.sub(r"<[^>]+>", "", price_matches[-1].group(3))).strip()
        raw_price = raw_price.replace("\u00a0", " ").replace(" ", "").replace(",", ".")
        try:
            price = float(raw_price)
        except ValueError:
            continue
        if price <= 0:
            continue
        catalog[title] = price
        slugs[title] = href_match.group(2).rstrip("/").rsplit("/", 1)[-1]
    CASE_SLUGS.clear()
    CASE_SLUGS.update(slugs)
    return catalog


def read_page(site: str):
    """Возвращает (стартовые дропы, WebSocket URL, цены кейсов из каталога)."""
    html = requests.get(site, headers={"User-Agent": UA}, timeout=15).text
    case_catalog = extract_case_catalog(html)

    m = re.search(r"""__liveDropData\s*[=:]\s*(["'])([A-Za-z0-9+/=]+)\1""", html)
    initial = []
    if m:
        try:
            initial = decode_live_drop_data(m.group(2)).get("lastDrop", [])
        except (ValueError, UnicodeDecodeError) as e:
            print(f"[warn] не удалось разобрать __liveDropData: {e!r}", file=sys.stderr)

    # в __userData адрес лежит в host.ws (ключ может быть как в JSON, так и в JS-литерале)
    # (на case-battle.moe там стоит именно https://ws6..., браузер сам превращает его в wss://)
    w = re.search(r"""["']?\bws["']?\s*:\s*["']((?:wss?|https?):(?:\\?/){2}[^"']+)["']""", html)
    ws_url = normalize_ws_url(w.group(1).replace("\\/", "/")) if w else None
    return initial, ws_url, case_catalog


class Dedup:
    """Ограниченный по размеру набор уже виденных id."""

    def __init__(self, limit: int = 5000):
        self._set, self._order, self._limit = set(), deque(), limit

    def seen(self, key) -> bool:
        # Без ID невозможно надёжно определить, является ли дроп повтором.
        if key is None or key == "":
            return False

        if key in self._set:
            return True

        self._set.add(key)
        self._order.append(key)

        if len(self._order) > self._limit:
            self._set.discard(self._order.popleft())

        return False


def case_part(d: dict, case: dict) -> str:
    """Откуда выпало: кейс (с ценой или порогом) либо другой источник."""
    if case:
        title = case.get("title") or case.get("slug") or "?"
        extra = None
        for key in CASE_PRICE_KEYS:
            if case.get(key) is not None:
                extra = f"цена {case[key]}"
                break
        return f"Кейс «{title}» ({get_case_reference_price(case)} RUB)" + (f" ({extra})" if extra else "")
    src = d.get("source")
    return SOURCE_NAMES.get(src, f"source={src}")


def summarize(d: dict, online) -> str:
    asset = d.get("asset") or {}
    #user = d.get("user") or {}
    case = d.get("case") or {}
    case_price = get_case_reference_price(case)
    price = d.get("price") if d.get("price") is not None else asset.get("price")
    rarity = asset.get("rarity")
    color = RARITY_COLORS.get(rarity)

    roi = return_percent(price, case)

    if roi is not None:
        last_rois.append(roi)

    if price is not None and case_price is not None:
        try:
            if price*0.9 > case_price:
                arrow = paint(f'↑ ({roi:+.1f}%)', 46)
                pos_neg.append(1)
            elif price < case_price:
                arrow = paint(f'↓ ({roi:+.1f}%)', 196)
                pos_neg.append(0)
            else:
                arrow = paint(f'- ({roi:+.1f}%)', 226)

        except (TypeError, ValueError):
            arrow = paint("?", 250)
    else:
        arrow = paint("?", 250)
    item_name = str(asset.get("name") or asset.get("title") or "?") + (
        " [контейнер]" if asset.get("type") == TYPE_CONTAINER else ""
    )
    # Окупаемость показываем только для кейсов, чья цена есть в CASE_PRICES.

    parts = [
        arrow,
        datetime.now().strftime("%H:%M:%S"),
        #f"онлайн {online if online is not None else '?'}",
        paint(item_name, color),
        f"{price if price is not None else '?'}",
        case_part(d, case)
    ]
    return " · ".join(parts)


def store(drops, out, dedup: Dedup, online) -> int:
    new = 0

    if drops:
        os.system("cls" if os.name == "nt" else "clear")
        for d in drops:
            if d.get("source") in (2, 3):
                continue

            if dedup.seen(d.get("id")):
                continue
            new += 1
            out.write(json.dumps({"ts": time.time(), "drop": d}, ensure_ascii=False) + "\n")
            print(summarize(d, online), flush=True)

        if len(last_rois) > 0:
            sum_roi = 0
            for roi in last_rois:
                if roi:
                    sum_roi += roi
            avg_roi = sum_roi / len(last_rois)

            print(f"\n>>> AVG ROI: {avg_roi:3.2f}% AVG +/-: {sum(pos_neg)/len(pos_neg)*100:3.1f}%")

    pos_neg.clear()
    last_rois.clear()
    out.flush()
    return new


async def listen(ws_url: str, site: str, out, dedup: Dedup, state: dict):
    delay = 1
    while True:
        try:
            async with websockets.connect(
                ws_url,
                origin=site.rstrip("/"),
                additional_headers={"User-Agent": UA},
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                #print(f"[ws] подключено: {ws_url}", file=sys.stderr)
                delay = 1
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if state["raw"] > 0:
                        state["raw"] -= 1
                        print("[raw]", json.dumps(data, ensure_ascii=False, indent=2), file=sys.stderr)
                    # data["ol"] - онлайн сайта; data["ld"] - список дропов
                    if data.get("ol") is not None:
                        state["online"] = data["ol"]
                    store(data.get("ld") or [], out, dedup, state["online"])
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            print(f"[ws] обрыв: {e!r}, переподключение через {delay} с", file=sys.stderr)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", help="адрес сайта, например https://site.example")
    ap.add_argument("--ws", help="адрес WebSocket, если авто-поиск не сработал")
    ap.add_argument("--out", default="drops.jsonl")
    ap.add_argument("--raw", action="store_true", help="показать полный JSON первых двух сообщений")
    ap.add_argument("--no-color", action="store_true", help="не раскрашивать названия по редкости")
    args = ap.parse_args()
    global USE_COLOR
    USE_COLOR = setup_colors(args.no_color)

    initial, ws_url = [], None
    try:
        initial, ws_url, case_catalog = read_page(args.site)
        CASE_PRICES.update(case_catalog)
        #print(f"[init] кейсов в каталоге: {len(case_catalog)}", file=sys.stderr)
    except requests.RequestException as e:
        #print(f"[warn] страница недоступна: {e!r}", file=sys.stderr)
        pass
    ws_url = normalize_ws_url(args.ws) or ws_url
    if not ws_url:
        sys.exit("Адрес WebSocket не найден. Передайте его через --ws (см. шапку файла).")

    dedup = Dedup()
    state = {"online": None, "raw": 2 if args.raw else 0}
    with open(args.out, "a", encoding="utf-8") as out:
        if initial:
            #print(f"[init] стартовых дропов со страницы: {len(initial)}", file=sys.stderr)
            store(initial, out, dedup, state["online"])
        await listen(ws_url, args.site, out, dedup, state)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass