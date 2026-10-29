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
# ⚠️ ЗАПОЛНИТЕ ПЕРЕМЕННЫЕ ⚠️
# =====================================================================
VK_TOKEN = "vk1.a.puFDEZeb25347jnBEOwzhWKmW3y9tDNwk1gelv-RXOjSSXQcq-tVXE2YFqo-P5RWvCbr7thfQH4sDBNVZp1cuMQkrWMxRJRTQhFm_7aYdCt9bBoZktKTYcTk-uBI_C8c_-qjPdHY86CCwEH1I2EciPKroPeFhVihv17ldAvMSaw5qybW8_deMOdKW4oXhTvPZWlv__6e3KoX454m3qBFOA"
TEAM_ID = "pHf9ZSwg"

# 👇 Впишите ники руководителей и админов клуба (регистр не важен)
LEADER_USERNAMES = ["PavelMart", "R-23", "Kurerov", "Cheburahka_CHM"]
# =====================================================================

# =====================================================================
# 🏆 НАСТРОЙКИ МИНИ-ЧЕМПИОНАТА
# =====================================================================
CHAMP_KEYWORDS = ["PolyStation"]
# =====================================================================

DB_PATH = "bot.db"
tournament_cache = {}
waiting_for_nick = set()
waiting_for_token = set()
waiting_for_member_nick = set()
pending_member_target = {}
champ_overall_cache = {"data": [], "timestamp": 0}


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
                 (vk_id INTEGER PRIMARY KEY, lichess_username TEXT, lichess_token TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS token_cache
                 (username TEXT PRIMARY KEY, token TEXT)''')
    c.execute("PRAGMA table_info(users)")
    columns = [info[1] for info in c.fetchall()]
    if 'lichess_token' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN lichess_token TEXT")
    conn.commit()
    conn.close()


def get_user_lichess(vk_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT lichess_username FROM users WHERE vk_id=?", (vk_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def get_user_token(vk_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT lichess_token FROM users WHERE vk_id=?", (vk_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def save_user(vk_id, lichess_username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT vk_id FROM users WHERE vk_id=?", (vk_id,))
    if c.fetchone():
        c.execute("UPDATE users SET lichess_username=? WHERE vk_id=?", (lichess_username, vk_id))
    else:
        c.execute("INSERT INTO users (vk_id, lichess_username) VALUES (?, ?)", (vk_id, lichess_username))
    conn.commit()
    conn.close()


def save_user_token(vk_id, token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET lichess_token=? WHERE vk_id=?", (token, vk_id))
    conn.commit()
    conn.close()
    username = get_user_lichess(vk_id)
    if username:
        save_token_cache(username, token)


def save_token_cache(username, token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO token_cache (username, token) VALUES (?, ?)", (username.lower(), token))
    conn.commit()
    conn.close()


def get_token_from_cache(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token FROM token_cache WHERE username=?", (username.lower(),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


# --- LICHESS API ---
async def fetch_lichess_json(url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def fetch_lichess_ndjson(url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
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


async def check_token_valid(token, expected_username):
    url = "https://lichess.org/api/account"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("username", "").lower() == expected_username.lower()
            return False
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


# --- ЛОГИКА МИНИ-ЧЕМПИОНАТА (не используется в текущем меню) ---
def is_champ_stage(tournament):
    name = tournament.get('fullName', tournament.get('name', '')).lower()
    return any(keyword.lower() in name for keyword in CHAMP_KEYWORDS)


def get_stage_number(tournament):
    name = tournament.get('fullName', tournament.get('name', ''))
    match = re.search(r'(\d+\.\d+)', name)
    if match:
        return match.group(1)
    match = re.search(r'(\d+)(?:st|nd|rd|th)', name, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def get_sorted_champ_stages(all_tournaments):
    finished = [t for t in all_tournaments if t.get('status') == 30 and is_champ_stage(t)]
    finished.sort(key=lambda x: x.get('startsAt', 0))
    return finished


def get_points(rank):
    if rank == 1:
        return 25
    elif rank == 2:
        return 18
    elif rank == 3:
        return 15
    elif rank == 4:
        return 12
    elif rank == 5:
        return 11
    elif rank == 6:
        return 10
    elif rank == 7:
        return 9
    elif rank == 8:
        return 8
    elif rank == 9:
        return 7
    elif rank == 10:
        return 6
    elif 11 <= rank <= 15:
        return 3
    elif 16 <= rank <= 20:
        return 2
    elif rank >= 21:
        return 1
    return 0


# --- РАСЧЕТ ПЕРИОДОВ ---
def get_period_dates(period_type):
    current_year = datetime.now().year
    if period_type == "autumn":
        start = date(current_year, 9, 1)
        end = date(current_year, 12, 15)
    else:
        start = date(current_year, 2, 1)
        end = date(current_year, 5, 15)
    return start, end


# --- ПРОВЕРКА ПРАВ РУКОВОДИТЕЛЯ (ПО СПИСКУ) ---
async def is_club_leader(username):
    return username.lower() in [name.lower() for name in LEADER_USERNAMES]


# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("👤 Статистика игрока", payload={"cmd": "menu_player"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🧩 Статистика задач", payload={"cmd": "menu_puzzles"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📅 Статистика за периоды", payload={"cmd": "menu_periods"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🏆 Статистика клуба", payload={"cmd": "menu_club"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("👥 Статистика участника", payload={"cmd": "view_member_stats"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🎯 Задача дня", payload={"cmd": "daily_puzzle"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔄 Сменить ник", payload={"cmd": "change_nick"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("🔑 Мой токен Lichess", payload={"cmd": "set_token"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_periods_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🍂 Осенний период (1 сен - 15 дек)", payload={"cmd": "period_stats", "type": "autumn"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🌸 Весенний период (1 фев - 15 мая)", payload={"cmd": "period_stats", "type": "spring"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_member_period_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🍂 Осенний период", payload={"cmd": "member_period", "type": "autumn"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🌸 Весенний период", payload={"cmd": "member_period", "type": "spring"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_champ_menu_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("📊 Общая таблица", payload={"cmd": "champ_overall", "page": 0}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📅 Список этапов", payload={"cmd": "champ_stages", "page": 0}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_overall_keyboard(page, has_next):
    keyboard = Keyboard(one_time=False, inline=False)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(Text("⬅️ Назад", payload={"cmd": "champ_overall", "page": page - 1}))
    if has_next:
        nav_buttons.append(Text("Вперед ➡️", payload={"cmd": "champ_overall", "page": page + 1}))

    if nav_buttons:
        for btn in nav_buttons:
            keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_stages_keyboard(stages, page):
    keyboard = Keyboard(one_time=False, inline=False)
    per_page = 5
    start_idx = page * per_page
    end_idx = start_idx + per_page

    for i, s in enumerate(stages[start_idx:end_idx]):
        global_idx = start_idx + i + 1
        stage_num = get_stage_number(s)
        prefix = f"Этап {stage_num}: " if stage_num else f"{global_idx}. "
        short_name = s['name'][:20] + "..." if len(s['name']) > 20 else s['name']

        keyboard.add(Text(f"{prefix}{short_name}",
                          payload={"cmd": "champ_view_stage", "idx": start_idx + i, "page": page, "res_page": 0}),
                     color=KeyboardButtonColor.SECONDARY)
        keyboard.row()

    nav_buttons = []
    if page > 0:
        nav_buttons.append(Text("⬅️ Назад", payload={"cmd": "champ_stages", "page": page - 1}))
    if end_idx < len(stages):
        nav_buttons.append(Text("Вперед ➡️", payload={"cmd": "champ_stages", "page": page + 1}))

    if nav_buttons:
        for btn in nav_buttons:
            keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_stage_results_keyboard(idx, page, res_page, has_next):
    keyboard = Keyboard(one_time=False, inline=False)
    nav_buttons = []
    if res_page > 0:
        nav_buttons.append(
            Text("⬅️ Назад", payload={"cmd": "champ_view_stage", "idx": idx, "page": page, "res_page": res_page - 1}))
    if has_next:
        nav_buttons.append(
            Text("Вперед ➡️", payload={"cmd": "champ_view_stage", "idx": idx, "page": page, "res_page": res_page + 1}))

    if nav_buttons:
        for btn in nav_buttons:
            keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 К списку этапов", payload={"cmd": "champ_stages", "page": page}),
                 color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
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


# --- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
def register_handlers(bot: Bot):
    @bot.on.message(text=["Начать", "start", "/start"])
    async def start_handler(message: Message):
        waiting_for_nick.discard(message.from_id)
        waiting_for_token.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        user = get_user_lichess(message.from_id)
        if user:
            await message.answer(f"👋 Привет! Ты привязан как {user}.\n\nВыбери нужный раздел в меню ниже:",
                                 keyboard=get_main_keyboard())
        else:
            waiting_for_nick.add(message.from_id)
            await message.answer("👋 Привет! Это бот клуба.\n\nПожалуйста, отправьте ваш ник на Lichess для привязки:",
                                 keyboard=Keyboard(one_time=True).get_json())

    @bot.on.message(payload_contains={"cmd": "set_token"})
    async def set_token_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user:
            await message.answer("Сначала привяжите ник (напишите 'Начать').")
            return
        waiting_for_nick.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        waiting_for_token.add(message.from_id)
        await message.answer(
            f"🔑 Отправьте ваш персональный токен Lichess.\n\n"
            f"Он должен принадлежать вашему аккаунту: {user}.\n"
            f"Его можно создать здесь: https://lichess.org/account/oauth/token/create\n"
,
            keyboard=Keyboard(one_time=True).get_json()
        )

    @bot.on.message(payload_contains={"cmd": "view_member_stats"})
    async def view_member_stats_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user:
            await message.answer("Сначала привяжите аккаунт (напишите 'Начать').")
            return
        is_leader = await is_club_leader(user)
        if not is_leader:
            await message.answer("❌ Эта функция доступна только руководителям и администраторам клуба.")
            return
        waiting_for_nick.discard(message.from_id)
        waiting_for_token.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        waiting_for_member_nick.add(message.from_id)
        await message.answer(
            "Введите ник участника клуба на Lichess, чтобы посмотреть его статистику за выбранный период:",
            keyboard=Keyboard(one_time=True).get_json())

    @bot.on.message(payload_contains={"cmd": "member_period"})
    async def member_period_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        period_type = payload_data.get("type", "autumn")

        target_username = pending_member_target.get(message.from_id)
        if not target_username:
            await message.answer("Сначала выберите участника через «👥 Статистика участника» в главном меню.")
            return

        start_date, end_date = get_period_dates(period_type)
        period_name = "Осенний период" if period_type == "autumn" else "Весенний период"

        await message.answer(
            f"⏳ Собираю статистику для {target_username} за {period_name} ({start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')})...")

        try:
            user_token = get_token_from_cache(target_username)

            user_data = await fetch_lichess_json(f"https://lichess.org/api/user/{target_username.lower()}")
            played_tournaments = await fetch_lichess_ndjson(
                f"https://lichess.org/api/user/{target_username.lower()}/tournament/played")

            period_tourn_count = 0
            period_tourn_wins = 0
            for t in played_tournaments:
                starts_at = t.get('tournament', {}).get('startsAt', 0)
                t_date = datetime.fromtimestamp(starts_at / 1000).date()
                if start_date <= t_date <= end_date:
                    period_tourn_count += 1
                    if t.get('player', {}).get('rank') == 1:
                        period_tourn_wins += 1

            puzzles_solved = 0
            puzzles_won = 0
            start_rating = None
            end_rating = None
            if user_token:
                try:
                    puzzles_data = await fetch_lichess_ndjson("https://lichess.org/api/puzzle/activity",
                                                              token=user_token)
                    for entry in puzzles_data:
                        try:
                            event_ts = entry.get("date", 0)
                            event_date = datetime.fromtimestamp(event_ts / 1000).date()
                            if start_date <= event_date <= end_date:
                                is_win = entry.get("win", False)
                                if is_win:
                                    puzzles_won += 1
                                    puzzles_solved += 1
                                else:
                                    puzzles_solved += 1
                                rating = entry.get("puzzle", {}).get("rating")
                                if rating:
                                    if start_rating is None:
                                        start_rating = rating
                                    end_rating = rating
                        except Exception:
                            continue
                except Exception:
                    pass

            perfs = user_data.get('perfs', {})
            rating_lines = []
            for mode in ['bullet', 'blitz', 'rapid', 'classical', 'puzzle']:
                if mode in perfs and 'rating' in perfs[mode]:
                    prov = " (provisional)" if perfs[mode].get('prov') else ""
                    rating_lines.append(f"• {mode.capitalize()}: {perfs[mode]['rating']}{prov}")

            text = f"👤 Статистика игрока {target_username}\n"
            text += f"📅 Период: {period_name} ({start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')})\n\n"

            text += f"🏆 Турниры за период:\n"
            text += f"• Участий: {period_tourn_count}\n"
            text += f"• Побед (1 место): {period_tourn_wins}\n\n"

            if puzzles_solved > 0:
                text += f"🧩 Задачи за период:\n"
                text += f"• Правильно: {puzzles_won}\n"
                text += f"• Неправильно: {puzzles_solved - puzzles_won}\n"
                if start_rating and end_rating:
                    text += f"• Рейтинг: {start_rating} ➡️ {end_rating}\n"
                text += "\n"
            else:
                text += f"🧩 Задачи за период: нет данных (токен не найден)\n\n"

            if rating_lines:
                text += f"📈 Текущие рейтинги:\n" + "\n".join(rating_lines) + "\n"

            await message.answer(text, keyboard=get_member_period_keyboard())

        except Exception as e:
            await message.answer(f"⚠️ Ошибка при сборе статистики: {e}", keyboard=get_member_period_keyboard())

    @bot.on.message(payload_contains={"cmd": "menu_periods"})
    async def menu_periods_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user:
            await message.answer("Сначала привяжите аккаунт (напишите 'Начать').")
            return
        await message.answer("📅 Выберите период, за который хотите посмотреть статистику:",
                             keyboard=get_periods_keyboard())

    @bot.on.message(payload_contains={"cmd": "period_stats"})
    async def period_stats_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        period_type = payload_data.get("type", "autumn")
        user = get_user_lichess(message.from_id)

        if not user:
            await message.answer("Сначала привяжите аккаунт.")
            return

        user_token = get_token_from_cache(user)
        if not user_token:
            await message.answer(
                f"Для статистики за периоды нужен токен Lichess для ника {user}. Нажмите «🔑 Мой токен Lichess» в главном меню.",
                keyboard=get_periods_keyboard())
            return

        start_date, end_date = get_period_dates(period_type)
        period_name = "Осенний период" if period_type == "autumn" else "Весенний период"

        await message.answer(
            f"⏳ Собираю статистику за {period_name} ({start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')})...")

        try:
            puzzles_data = await fetch_lichess_ndjson(
                "https://lichess.org/api/puzzle/activity",
                token=user_token
            )

            puzzles_solved = 0
            puzzles_won = 0
            start_rating = None
            end_rating = None

            for entry in puzzles_data:
                try:
                    event_ts = entry.get("date", 0)
                    event_date = datetime.fromtimestamp(event_ts / 1000).date()

                    if start_date <= event_date <= end_date:
                        is_win = entry.get("win", False)
                        if is_win:
                            puzzles_won += 1
                            puzzles_solved += 1
                        else:
                            puzzles_solved += 1

                        rating = entry.get("puzzle", {}).get("rating")
                        if rating:
                            if start_rating is None:
                                start_rating = rating
                            end_rating = rating
                except Exception:
                    continue

            played_tournaments = await fetch_lichess_ndjson(
                f"https://lichess.org/api/user/{user.lower()}/tournament/played")
            tourn_count = 0
            tourn_wins = 0

            for t in played_tournaments:
                starts_at = t.get('tournament', {}).get('startsAt', 0)
                t_date = datetime.fromtimestamp(starts_at / 1000).date()

                if start_date <= t_date <= end_date:
                    tourn_count += 1
                    if t.get('player', {}).get('rank') == 1:
                        tourn_wins += 1

            if puzzles_solved == 0 and tourn_count == 0:
                await message.answer(
                    f"За {period_name.lower()} ({start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}) у вас нет активности.",
                    keyboard=get_periods_keyboard())
                return

            text = f"📊 Статистика за {period_name} ({start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')})\n\n"

            if puzzles_solved > 0:
                text += f"🧩 Задачи:\n"
                text += f"• Правильно решено: {puzzles_won}\n"
                text += f"• Неправильно решено: {puzzles_solved - puzzles_won}\n"
                if start_rating and end_rating:
                    text += f"• Рейтинг: {start_rating} ➡️ {end_rating}\n"
                text += "\n"

            text += f"🏆 Турниры:\n"
            text += f"• Сыграно турниров: {tourn_count}\n"
            text += f"• Побед (1 место): {tourn_wins}\n"

            await message.answer(text, keyboard=get_periods_keyboard())

        except Exception as e:
            await message.answer(f"⚠️ Ошибка при сборе статистики: {e}", keyboard=get_periods_keyboard())

    @bot.on.message(payload_contains={"cmd": "back_main"})
    async def back_main_handler(message: Message):
        pending_member_target.pop(message.from_id, None)
        waiting_for_member_nick.discard(message.from_id)
        user = get_user_lichess(message.from_id)
        await message.answer(f"👋 Главное меню\n\nВы привязаны как: {user}\nВыберите, что хотите посмотреть:",
                             keyboard=get_main_keyboard())

    @bot.on.message(payload_contains={"cmd": "change_nick"})
    async def change_nick_handler(message: Message):
        waiting_for_nick.add(message.from_id)
        waiting_for_token.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
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

        user_token = get_token_from_cache(lichess_user)
        await message.answer("⏳ Загружаю статистику задач...")
        try:
            user_data = await fetch_lichess_json(f"https://lichess.org/api/user/{lichess_user}", token=user_token)
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

        text = f"{title} (стр. {page + 1}):\nНажмите на кнопку, чтобы увидеть результаты."
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

        back_kb = get_tournament_results_keyboard(t_format, tid, list_type, list_page, res_page, False, status,
                                                  date_str, players)

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

        if message.from_id in waiting_for_token:
            token = text.strip()
            if not token.startswith("lip_"):
                await message.answer(
                    "❌ Токен должен начинаться с `lip_`. Попробуйте еще раз или напишите /start для отмены.")
                return

            user = get_user_lichess(message.from_id)
            await message.answer("⏳ Проверяю токен...")
            is_valid = await check_token_valid(token, user)
            if not is_valid:
                await message.answer(
                    f"❌ Токен недействителен или не принадлежит аккаунту {user}. Попробуйте другой токен или напишите /start для отмены.")
                return

            save_user_token(message.from_id, token)
            waiting_for_token.discard(message.from_id)
            await message.answer(f"✅ Токен успешно привязан! Теперь статистика задач будет доступна в полном объеме.",
                                 keyboard=get_main_keyboard())
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

            old_token = get_user_token(message.from_id)
            save_user(message.from_id, username)
            waiting_for_nick.discard(message.from_id)

            if old_token:
                is_valid_for_new_nick = await check_token_valid(old_token, username)
                if not is_valid_for_new_nick:
                    await message.answer(
                        f"✅ Успешно! Вы привязаны как {username}.\n⚠",
                        keyboard=get_main_keyboard())
                    return
                else:
                    await message.answer(
                        f"✅ Успешно! Вы привязаны как {username}. Ваш токен привязан к этому аккаунту.",
                        keyboard=get_main_keyboard())
                    return

            await message.answer(f"✅ Успешно! Вы привязаны как {username}.", keyboard=get_main_keyboard())
            return

        if message.from_id in waiting_for_member_nick:
            target_username = text.strip()
            if not target_username or len(target_username) > 30 or " " in target_username:
                await message.answer("❌ Некорректный ник. Попробуйте еще раз (без пробелов):")
                return

            await message.answer("⏳ Проверяю ник на Lichess...")
            if not await check_user_exists(target_username):
                await message.answer(f"❌ Игрок {target_username} не найден на Lichess.\nПопробуйте другой ник:")
                return

            pending_member_target[message.from_id] = target_username
            waiting_for_member_nick.discard(message.from_id)

            await message.answer(
                f"Участник: {target_username}\nВыберите период для просмотра статистики:",
                keyboard=get_member_period_keyboard()
            )
            return

        await message.answer("Пожалуйста, пользуйтесь кнопками в меню ниже, или напишите 'Начать'.",
                             keyboard=get_main_keyboard())


# --- АСИНХРОННЫЙ ЗАПУСК ---
async def main():
    init_db()
    print("Бот ВК запущен...")
   
    connector = TCPConnector(ssl=False)
    http_client = SingleAiohttpClient(connector=connector)
    api = API(token=VK_TOKEN, http_client=http_client)
    bot = Bot(api=api)

    register_handlers(bot)

    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())