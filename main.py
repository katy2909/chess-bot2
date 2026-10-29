import sqlite3
import json
import httpx
import asyncio
import re
import time
from datetime import datetime, date
from aiohttp import TCPConnector

# --- ИСПРАВЛЕНИЕ БАГА VKBOTTLE ---
from vkbottle.dispatch.rules.base import PayloadContainsRule, PayloadRule

_original_payload_contains_check = PayloadContainsRule.check


async def _safe_payload_contains_check(self, event):
    try:
        return await _original_payload_contains_check(self, event)
    except TypeError:
        return False


PayloadContainsRule.check = _safe_payload_contains_check
_original_payload_check = PayloadRule.check


async def _safe_payload_check(self, event):
    try:
        return await _original_payload_check(self, event)
    except TypeError:
        return False


PayloadRule.check = _safe_payload_check
# -------------------------------------------------------------------------

import os

os.environ["VKBOTTLE_SKIP_LOGGING_PRESET"] = "1"

from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, Text, KeyboardButtonColor
from vkbottle.http import SingleAiohttpClient
from vkbottle.api import API

# =====================================================================
# ⚠️ ЗАПОЛНИТЕ ДВЕ ПЕРЕМЕННЫЕ ⚠️
# =====================================================================
VK_TOKEN = "vk1.a.puFDEZeb25347jnBEOwzhWKmW3y9tDNwk1gelv-RXOjSSXQcq-tVXE2YFqo-P5RWvCbr7thfQH4sDBNVZp1cuMQkrWMxRJRTQhFm_7aYdCt9bBoZktKTYcTk-uBI_C8c_-qjPdHY86CCwEH1I2EciPKroPeFhVihv17ldAvMSaw5qybW8_deMOdKW4oXhTvPZWlv__6e3KoX454m3qBFOA"
TEAM_ID = "pHf9ZSwg"
# =====================================================================

DB_PATH = "bot.db"
tournament_cache = {}
waiting_for_nick = set()


# --- ФОРМАТИРОВАНИЕ ДАТЫ ---
def format_date(ts):
    if not ts: return "Неизвестно"
    try:
        return datetime.fromtimestamp(ts / 1000).strftime('%d.%m.%Y %H:%M')
    except:
        return "Неизвестно"


# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (vk_id INTEGER PRIMARY KEY, lichess_username TEXT)''')
    conn.commit()
    conn.close()


def get_user_lichess(vk_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT lichess_username FROM users WHERE vk_id=?", (vk_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def save_user(vk_id, lichess_username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users VALUES (?, ?)", (vk_id, lichess_username))
    conn.commit()
    conn.close()


# --- LICHESS API ---
async def fetch_lichess_json(url):
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def fetch_lichess_ndjson(url):
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        results = []
        for line in resp.text.strip().split('\n'):
            if line.strip():
                results.append(json.loads(line))
        return results


async def check_user_exists(username):
    url = f"https://lichess.org/api/user/{username}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url)
            return resp.status_code == 200
        except Exception:
            return False


async def get_club_members(team_id):
    url = f"https://lichess.org/api/team/{team_id}/users"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            users = []
            for line in resp.text.strip().split('\n'):
                if line.strip():
                    users.append(json.loads(line))
            return users
        except Exception:
            return []


async def get_users_profiles(usernames):
    if not usernames:
        return []
    ids_str = ",".join(usernames[:300])
    url = "https://lichess.org/api/users"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(url, data=ids_str)
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception:
            return []


async def get_all_team_tournaments():
    tournaments = []
    try:
        arenas = await fetch_lichess_ndjson(f"https://lichess.org/api/team/{TEAM_ID}/arena")
        tournaments.extend(arenas)
    except Exception:
        pass
    try:
        swisses = await fetch_lichess_ndjson(f"https://lichess.org/api/team/{TEAM_ID}/swiss")
        tournaments.extend(swisses)
    except Exception:
        pass
    return tournaments


# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("👤 Статистика игрока", payload={"cmd": "menu_player"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🧩 Статистика задач", payload={"cmd": "menu_puzzles"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🏆 Статистика клуба", payload={"cmd": "menu_club"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🎯 Задача дня", payload={"cmd": "daily_puzzle"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔄 Сменить ник", payload={"cmd": "change_nick"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_back_to_main_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_club_keyboard(has_tournaments):
    keyboard = Keyboard(one_time=False, inline=False)
    if has_tournaments:
        keyboard.add(Text("📜 Посмотреть все турниры", payload={"cmd": "club_tournaments_menu"}),
                     color=KeyboardButtonColor.PRIMARY)
        keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_tournaments_menu_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("📅 Предстоящие", payload={"cmd": "tournaments_list", "type": "upcoming"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.add(Text("🏁 Завершённые", payload={"cmd": "tournaments_list", "type": "finished"}),
                 color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("🔙 К статистике клуба", payload={"cmd": "menu_club"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_paginated_tournaments_keyboard(page_tournaments, t_type, page, has_next):
    keyboard = Keyboard(one_time=False, inline=False)
    for t in page_tournaments:
        name = t.get('fullName', t.get('name', 'Турнир'))
        short_name = name[:12] + "..." if len(name) > 12 else name
        t_format = "tournament" if "clock" in t else "swiss"
        players = t.get('nbPlayers', 0)
        date_str = format_date(t.get('startsAt'))

        short_date = date_str[:5] if date_str != "Неизвестно" else ""

        btn_text = f"🏆 {short_name} ({players}ч., {short_date})"

        keyboard.add(Text(btn_text, payload={
            "cmd": "view", "format": t_format, "id": t['id'], "status": t.get('status'),
            "list_type": t_type, "list_page": page, "res_page": 0, "d": date_str, "p": players
        }), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()

    nav_buttons = []
    if page > 0:
        nav_buttons.append(Text("⬅️ Назад", payload={"cmd": "tournaments_list", "type": t_type, "page": page - 1}))
    if has_next:
        nav_buttons.append(Text("Вперед ➡️", payload={"cmd": "tournaments_list", "type": t_type, "page": page + 1}))
    if nav_buttons:
        for btn in nav_buttons:
            keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 К статистике клуба", payload={"cmd": "menu_club"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_tournament_results_keyboard(t_format, tid, list_type, list_page, res_page, has_next, status, date_str, players):
    keyboard = Keyboard(one_time=False, inline=False)
    nav_buttons = []
    if res_page > 0:
        nav_buttons.append(Text("⬅️ Назад", payload={
            "cmd": "view", "format": t_format, "id": tid, "status": status,
            "list_type": list_type, "list_page": list_page, "res_page": res_page - 1, "d": date_str, "p": players
        }))
    if has_next:
        nav_buttons.append(Text("Вперед ➡️", payload={
            "cmd": "view", "format": t_format, "id": tid, "status": status,
            "list_type": list_type, "list_page": list_page, "res_page": res_page + 1, "d": date_str, "p": players
        }))

    if nav_buttons:
        for btn in nav_buttons:
            keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 К списку турниров", payload={"cmd": "tournaments_list", "type": list_type, "page": list_page}),
                 color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 К статистике клуба", payload={"cmd": "menu_club"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


# --- СОЗДАНИЕ БОТА ---
connector = TCPConnector(ssl=False)
http_client = SingleAiohttpClient(connector=connector)
api = API(token=VK_TOKEN, http_client=http_client)
bot = Bot(api=api)


# --- ОБРАБОТЧИКИ ---
@bot.on.message(text=["Начать", "start", "/start"])
async def start_handler(message: Message):
    waiting_for_nick.discard(message.from_id)
    user = get_user_lichess(message.from_id)
    if user:
        await message.answer(f"👋 Привет! Ты привязан как {user}.\n\nВыбери нужный раздел в меню ниже:",
                             keyboard=get_main_keyboard())
    else:
        waiting_for_nick.add(message.from_id)
        await message.answer("👋 Привет! Это бот клуба.\n\nПожалуйста, отправьте ваш ник на Lichess для привязки:",
                             keyboard=Keyboard(one_time=True).get_json())


@bot.on.message(payload_contains={"cmd": "back_main"})
async def back_main_handler(message: Message):
    user = get_user_lichess(message.from_id)
    await message.answer(f"👋 Главное меню\n\nВы привязаны как: {user}\nВыберите, что хотите посмотреть:",
                         keyboard=get_main_keyboard())


@bot.on.message(payload_contains={"cmd": "change_nick"})
async def change_nick_handler(message: Message):
    waiting_for_nick.add(message.from_id)
    await message.answer("🔗 Регистрация\n\nОтправьте ваш новый ник на Lichess:",
                         keyboard=Keyboard(one_time=True).get_json())


@bot.on.message(payload_contains={"cmd": "menu_player"})
async def menu_player_handler(message: Message):
    lichess_user = get_user_lichess(message.from_id)
    if not lichess_user:
        await message.answer("Сначала привяжите аккаунт (напишите 'Начать').")
        return
    await message.answer("⏳ Загружаю вашу статистику...")
    try:
        user_data = await fetch_lichess_json(f"https://lichess.org/api/user/{lichess_user}")
        played_tournaments = await fetch_lichess_ndjson(
            f"https://lichess.org/api/user/{lichess_user}/tournament/played")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")
        return
    total_tournaments = len(played_tournaments)
    wins = sum(1 for item in played_tournaments if item.get('player', {}).get('rank') == 1)
    lines = [
        f"👤 Статистика игрока {lichess_user}\n",
        f"🏆 Всего сыграно турниров: {total_tournaments}",
        f"🥇 Побед (1 место): {wins}\n",
        f"📈 Текущие рейтинги Lichess:"
    ]
    perfs = user_data.get('perfs', {})
    for mode in ['bullet', 'blitz', 'rapid', 'classical']:
        if mode in perfs and 'rating' in perfs[mode]:
            prov = " (provisional)" if perfs[mode].get('prov') else ""
            lines.append(f"• {mode.capitalize()}: {perfs[mode]['rating']}{prov}")
    await message.answer("\n".join(lines), keyboard=get_back_to_main_keyboard())


@bot.on.message(payload_contains={"cmd": "menu_puzzles"})
async def menu_puzzles_handler(message: Message):
    lichess_user = get_user_lichess(message.from_id)
    if not lichess_user:
        await message.answer("Сначала привяжите аккаунт (напишите 'Начать').")
        return
    await message.answer("⏳ Загружаю статистику задач...")
    try:
        user_data = await fetch_lichess_json(f"https://lichess.org/api/user/{lichess_user}")
        puzzle_perf = user_data.get('perfs', {}).get('puzzle', {})
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")
        return
    if not puzzle_perf:
        await message.answer("Вы пока не решали задачи на Lichess.", keyboard=get_back_to_main_keyboard())
        return
    lines = [f"🧩 Статистика задач {lichess_user}\n", f"📈 Рейтинг задач: {puzzle_perf.get('rating', '?')}",
             f"🎯 Всего решено: {puzzle_perf.get('games', 0)}\n"]
    await message.answer("\n".join(lines), keyboard=get_back_to_main_keyboard())


@bot.on.message(payload_contains={"cmd": "daily_puzzle"})
async def daily_puzzle_handler(message: Message):
    await message.answer("⏳ Загружаю задачу дня...")
    try:
        data = await fetch_lichess_json("https://lichess.org/api/puzzle/daily")
        rating = data.get('puzzle', {}).get('rating', '?')
        themes = ", ".join(data.get('puzzle', {}).get('themes', []))
        text = f"🎯 Ежедневная задача Lichess\n\n📈 Рейтинг задачи: {rating}\n🏷️ Темы: {themes}\n\nРешить: https://lichess.org/training/daily"
        await message.answer(text, keyboard=get_back_to_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка загрузки задачи: {e}", keyboard=get_back_to_main_keyboard())


@bot.on.message(payload_contains={"cmd": "menu_club"})
async def menu_club_handler(message: Message):
    await message.answer("⏳ Собираю статистику клуба...")
    try:
        team_info = await fetch_lichess_json(f"https://lichess.org/api/team/{TEAM_ID}")
        total_members = team_info.get('nbMembers', 0)
        team_name = team_info.get('name', 'Наш клуб')
        tournaments = await get_all_team_tournaments()

        upcoming = [t for t in tournaments if t.get('status') == 10]
        ongoing = [t for t in tournaments if t.get('status') == 20]
        total_tournaments = len(tournaments)
        finished_count = total_tournaments - len(upcoming) - len(ongoing)

        top_players_text = ""
        members = await get_club_members(TEAM_ID)
        if members:
            usernames = [m.get('id') for m in members if m.get('id')]
            profiles = await get_users_profiles(usernames)

            modes = [
                ('blitz', '⚡ Блиц'),
                ('rapid', '🐢 Рапид'),
                ('bullet', '🚀 Пуля')
            ]

            for mode_key, mode_name in modes:
                def get_mode_rating(p):
                    perfs = p.get('perfs', {})
                    r = perfs.get(mode_key, {}).get('rating', 0)
                    return r if isinstance(r, int) else 0

                mode_profiles = [p for p in profiles if get_mode_rating(p) > 0]
                mode_profiles.sort(key=get_mode_rating, reverse=True)

                if mode_profiles:
                    top_players_text += f"\n👑 Топ-5 ({mode_name}):\n"
                    for i, p in enumerate(mode_profiles[:5], 1):
                        top_players_text += f"{i}. {p.get('username', 'Unknown')} — {get_mode_rating(p)}\n"

    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}", keyboard=get_back_to_main_keyboard())
        return

    text = (
        f"🏆 Статистика клуба: {team_name}\n\n"
        f"👥 Участников: {total_members}\n"
        f"📊 Всего турниров: {total_tournaments}\n"
        f"⏳ Идущих: {len(ongoing)}\n"
        f"📅 Предстоящих: {len(upcoming)}\n"
        f"🏁 Завершённых: {finished_count}\n"
        f"{top_players_text}\n"
    )
    await message.answer(text, keyboard=get_club_keyboard(len(tournaments) > 0))


@bot.on.message(payload_contains={"cmd": "club_tournaments_menu"})
async def club_tournaments_menu_handler(message: Message):
    await message.answer("Выберите, какие турниры вы хотите посмотреть:", keyboard=get_tournaments_menu_keyboard())


@bot.on.message(payload_contains={"cmd": "tournaments_list"})
async def tournaments_list_handler(message: Message):
    payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
    t_type = payload_data.get("type", "upcoming")
    page = payload_data.get("page", 0)
    tournaments = await get_all_team_tournaments()
    if t_type == "upcoming":
        filtered = [t for t in tournaments if t.get('status') == 10]
        filtered.sort(key=lambda x: x.get('startsAt', 0))
        title = "📅 Предстоящие турниры"
    else:
        filtered = [t for t in tournaments if t.get('status') == 30]
        filtered.sort(key=lambda x: x.get('startsAt', 0), reverse=True)
        title = "🏁 Завершённые турниры"
    if not filtered:
        await message.answer(f"{title} не найдены.", keyboard=get_tournaments_menu_keyboard())
        return
    per_page = 5
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_tournaments = filtered[start_idx:end_idx]

    text = f"{title} (стр. {page + 1}):\nНажмите на кнопку, чтобы увидеть результаты.\nВ скобках указано: (участники, дата)."
    keyboard = get_paginated_tournaments_keyboard(page_tournaments, t_type, page, len(filtered) > end_idx)
    await message.answer(text, keyboard=keyboard)


@bot.on.message(payload_contains={"cmd": "view"})
async def view_tournament_handler(message: Message):
    payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
    t_format = payload_data.get("format", "tournament")
    tid = payload_data.get("id")
    status = payload_data.get("status")
    list_type = payload_data.get("list_type", "finished")
    list_page = payload_data.get("list_page", 0)
    res_page = payload_data.get("res_page", 0)
    date_str = payload_data.get("d", "?")
    players = payload_data.get("p", "?")

    back_kb = get_tournament_results_keyboard(t_format, tid, list_type, list_page, res_page, False, status, date_str,
                                              players)

    if status == 10:
        await message.answer(
            f"📅 Турнир ещё не начался!\n\nДата: {date_str}\nУчастников: {players}\nСсылка: https://lichess.org/{'tournament' if t_format == 'tournament' else 'swiss'}/{tid}",
            keyboard=back_kb)
        return

    await message.answer("⏳ Загружаю результаты турнира...")
    url = f"https://lichess.org/api/{t_format}/{tid}/results"
    try:
        results = await fetch_lichess_ndjson(url)
        results.sort(key=lambda x: x.get('rank', 9999))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await message.answer(
                f"⚠️ Результаты недоступны.\nСсылка: https://lichess.org/{'tournament' if t_format == 'tournament' else 'swiss'}/{tid}",
                keyboard=back_kb)
        else:
            await message.answer(f"⚠️ Ошибка загрузки: {e}", keyboard=back_kb)
        return
    except Exception as e:
        await message.answer(f"⚠️ Ошибка загрузки: {e}", keyboard=back_kb)
        return

    if not results:
        await message.answer("Результатов пока нет.", keyboard=back_kb)
        return

    per_page = 20
    start_idx = res_page * per_page
    end_idx = start_idx + per_page
    page_results = results[start_idx:end_idx]

    if not page_results:
        await message.answer("На этой странице нет результатов.", keyboard=back_kb)
        return

    link = f"https://lichess.org/{'tournament' if t_format == 'tournament' else 'swiss'}/{tid}"

    lines = [
        f"🏆 Результаты турнира",
        f"📅 Дата: {date_str}",
        f"👥 Участников: {players}",
        f"🔗 Ссылка: {link}\n",
        f"Таблица (места {start_idx + 1}-{min(end_idx, len(results))}):"
    ]
    for r in page_results:
        lines.append(f"{r.get('rank', '?')}. {r.get('username', 'Unknown')} — {r.get('score', '?')} очк.")

    has_next = len(results) > end_idx
    final_kb = get_tournament_results_keyboard(t_format, tid, list_type, list_page, res_page, has_next, status,
                                               date_str, players)

    await message.answer("\n".join(lines), keyboard=final_kb)


@bot.on.message(text="<text>")
async def any_text_handler(message: Message, text: str):
    if message.payload:
        return

    if message.from_id in waiting_for_nick:
        username = text.strip()
        if not username or len(username) > 30 or " " in username:
            await message.answer("❌ Некорректный ник. Попробуйте еще раз (без пробелов):")
            return

        await message.answer("⏳ Проверяю ник на Lichess...")
        exists = await check_user_exists(username)
        if not exists:
            await message.answer(f"❌ Игрок {username} не найден на Lichess.\nПопробуйте другой ник:")
            return

        save_user(message.from_id, username)
        waiting_for_nick.discard(message.from_id)
        await message.answer(f"✅ Успешно! Вы привязаны как {username}.", keyboard=get_main_keyboard())
        return

    await message.answer("Пожалуйста, пользуйтесь кнопками в меню ниже, или напишите 'Начать'.",
                         keyboard=get_main_keyboard())


if __name__ == "__main__":
    init_db()
    print("Бот ВК запущен...")
    bot.run_forever()