import sqlite3
import json
import httpx
import asyncio
import re
import time
import aiohttp
import os
import sys
import openpyxl
from openpyxl.styles import Font, Alignment
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from aiohttp import TCPConnector
from collections import defaultdict

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

os.environ["VKBOTTLE_SKIP_LOGGING_PRESET"] = "1"

from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, Text, KeyboardButtonColor, DocMessagesUploader
from vkbottle.http import SingleAiohttpClient
from vkbottle.api import API

# =====================================================================
# ⚠️ НАСТРОЙКИ ⚠️
# =====================================================================
SECRET_CODE = "chess-bot"
VK_TOKEN = os.getenv("BOT_TOKEN")
TEAM_ID = "pHf9ZSwg"

LEADER_USERNAMES = ["PavelMart", "R-23", "Kurerov", "Cheburahka_CHM", "Kate_pankov"]

# Ключевые слова для чемпионата
CHAMP_KEYWORDS = ["Under Pressure"]

# =====================================================================
# 📂 НАСТРОЙКА ПОСТОЯННОГО ХРАНИЛИЩА (ДЛЯ BOTHOST)
# =====================================================================
if os.name != 'nt':
    data_dir = Path("/app/data")
else:
    data_dir = Path("data")

data_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = str(data_dir / "bot.db")
CACHE_FILE = str(data_dir / "table_cache.json")
CLUB_CACHE_FILE = str(data_dir / "club_cache.json")

print(f"=== БАЗА ДАННЫХ ИНИЦИАЛИЗИРУЕТСЯ ПО ПУТИ: {DB_PATH} ===")

# =====================================================================
# ПЕРЕМЕННЫЕ СОСТОЯНИЯ И КЭШИ
# =====================================================================
waiting_for_code = set()
waiting_for_nick = set()
waiting_for_token = set()
waiting_for_member_nick = set()
waiting_for_table = set()
waiting_for_surname = set()
pending_member_target = {}

# Глобальные кэши
table_cache = {}
last_table_message = {}
daily_puzzle_cache = {"data": None, "timestamp": 0}
tournaments_cache = {"data": [], "timestamp": 0}
tournament_results_cache = {}
club_stats_cache = {"text": "", "timestamp": 0}
api_cache = {}
champ_cache = {}  # Кэш для чемпионата


# --- ФОРМАТИРОВАНИЕ ДАТЫ ---
def format_date(ts):
    if not ts: return "Неизвестно"
    try:
        # Устанавливаем часовой пояс МСК (UTC+3)
        msk_tz = timezone(timedelta(hours=3))
        # Переводим время из UTC в МСК
        dt = datetime.fromtimestamp(ts / 1000, tz=msk_tz)
        return dt.strftime('%d.%m.%Y %H:%M')
    except Exception:
        return "Неизвестно"


# --- СИСТЕМА ОЧКОВ ДЛЯ ЧЕМПИОНАТА ---
def get_champ_points(rank):
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


def get_period_dates(period_type):
    if period_type == "autumn":
        start = date(2026, 9, 1)
        end = date(2026, 12, 15)
    else:
        start = date(2027, 2, 1)
        end = date(2027, 5, 15)
    return start, end


# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (vk_id INTEGER PRIMARY KEY, lichess_username TEXT, lichess_token TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS surnames
                 (username TEXT PRIMARY KEY, surname TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS token_cache
                 (username TEXT PRIMARY KEY, token TEXT)''')
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
    table_cache.clear()
    api_cache.clear()


def save_user_surname(vk_id, surname):
    username = get_user_lichess(vk_id)
    if username:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO surnames (username, surname) VALUES (?, ?)",
                  (username.lower(), surname.strip()))
        conn.commit()
        conn.close()
        table_cache.clear()


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
    headers = {"User-Agent": "VK-ChessClubBot/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        for attempt in range(3):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 429:
                    await asyncio.sleep(5)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    await asyncio.sleep(5)
                    continue
                raise e
            except Exception:
                await asyncio.sleep(2)
        return {}


async def fetch_lichess_ndjson(url, token=None):
    headers = {"User-Agent": "VK-ChessClubBot/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        for attempt in range(3):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 429:
                    await asyncio.sleep(5)
                    continue
                resp.raise_for_status()
                results = []
                for line in resp.text.strip().split('\n'):
                    if line.strip():
                        results.append(json.loads(line))
                return results
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    await asyncio.sleep(5)
                    continue
                raise e
            except Exception:
                await asyncio.sleep(2)
        return []


async def check_user_exists(username):
    url = f"https://lichess.org/api/user/{username}"
    headers = {"User-Agent": "VK-ChessClubBot/1.0"}
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        try:
            resp = await client.get(url, headers=headers)
            return resp.status_code == 200
        except Exception:
            return False


async def check_token_valid(token, expected_username):
    url = "https://lichess.org/api/account"
    headers = {
        "User-Agent": "VK-ChessClubBot/1.0",
        "Authorization": f"Bearer {token}"
    }
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("username", "").lower() == expected_username.lower()
            return False
        except Exception:
            return False


async def fetch_users_profiles(usernames):
    if not usernames:
        return []
    ids_str = ",".join(usernames[:300])
    url = "https://lichess.org/api/users"
    headers = {"User-Agent": "VK-ChessClubBot/1.0"}
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        try:
            resp = await client.post(url, data=ids_str, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception:
            return []


async def get_all_team_tournaments():
    if time.time() - tournaments_cache["timestamp"] < 3600 and tournaments_cache["data"]:
        return tournaments_cache["data"]

    tournaments = []
    try:
        arenas = await fetch_lichess_ndjson(f"https://lichess.org/api/team/{TEAM_ID}/arena")
        for a in arenas: a['type'] = 'arena'
        tournaments.extend(arenas)
    except Exception:
        pass
    try:
        swisses = await fetch_lichess_ndjson(f"https://lichess.org/api/team/{TEAM_ID}/swiss")
        for s in swisses: s['type'] = 'swiss'
        tournaments.extend(swisses)
    except Exception:
        pass

    if not tournaments and tournaments_cache["data"]:
        return tournaments_cache["data"]

    tournaments_cache["data"] = tournaments
    tournaments_cache["timestamp"] = time.time()
    return tournaments


# --- ЗАГРУЗКА ФАЙЛОВ В VK ---
async def upload_doc_to_vk(api, peer_id, file_path, title):
    uploader = DocMessagesUploader(api)
    attachment = await uploader.upload(file_source=file_path, peer_id=peer_id, title=title)
    return attachment


# --- СБОР ДАННЫХ ЧЕМПИОНАТА ---
async def fetch_championship_data():
    if "data" in champ_cache and time.time() - champ_cache["timestamp"] < 3600:
        return champ_cache["data"]

    print("=== [ЧЕМПИОНАТ] Загрузка данных... ===", flush=True)
    tournaments = await get_all_team_tournaments()

    # Фильтруем по ключевым словам (берем ВСЕ турниры: и завершенные, и предстоящие)
    champ_tours = []
    for t in tournaments:
        # ИСПРАВЛЕНИЕ: Берем fullName (как отдает Lichess), а не name
        t_name = t.get('fullName', t.get('name', ''))
        if any(kw.lower() in t_name.lower() for kw in CHAMP_KEYWORDS):
            champ_tours.append(t)

    # Сортируем по дате (от старых к новым)
    champ_tours.sort(key=lambda x: x.get('startsAt', 0))

    overall_points = defaultdict(int)
    stages = []

    for t in champ_tours:
        t_id = t['id']
        t_type = t.get('type', 'arena')
        t_name = t.get('fullName', t.get('name', 'Без названия'))
        t_date = format_date(t.get('startsAt'))
        t_status = t.get('status', 0)

        url_link = f"https://lichess.org/{'tournament' if t_type == 'arena' else 'swiss'}/{t_id}"

        stage_results = []

        # Если турнир завершен (status 30), собираем результаты
        # Если турнир завершен (status 30), собираем результаты
        # Если турнир завершен (status 30), собираем результаты
        # Если турнир завершен (status 30), собираем результаты
        # Если турнир завершен (status 30), собираем результаты
        if t_status == 30:
            if t_type == 'arena':
                results_url = f"https://lichess.org/api/tournament/{t_id}/results"
                games_url = f"https://lichess.org/api/tournament/{t_id}/games"
            else:
                results_url = f"https://lichess.org/api/swiss/{t_id}/results"
                games_url = f"https://lichess.org/api/swiss/{t_id}/games"

            # 1. Загружаем итоговую таблицу
            results = await fetch_lichess_ndjson(results_url)

            # 2. Загружаем все партии и считаем игры для каждого игрока
            games_played_count = defaultdict(int)
            try:
                # Получаем поток партий (NDJSON)
                games = await fetch_lichess_ndjson(games_url)
                for game in games:
                    # Учитываем партию для обоих участников
                    white = game.get('players', {}).get('white', {}).get('user', {}).get('name')
                    black = game.get('players', {}).get('black', {}).get('user', {}).get('name')
                    if white: games_played_count[white] += 1
                    if black: games_played_count[black] += 1
            except Exception as e:
                print(f"!!! Ошибка загрузки партий для {t_name}: {e}", flush=True)

            print(
                f"=== [ЧЕМПИОНАТ] Турнир {t_name}: получено {len(results)} результатов, сыграно {sum(games_played_count.values())} партий ===",
                flush=True)

            # 3. Начисляем очки только тем, кто сыграл хотя бы одну партию
            for p in results:
                rank = p.get('rank', 0)
                username = p.get('username')

                # Проверяем, есть ли игрок в списке сыгравших
                if username and rank > 0 and games_played_count.get(username, 0) > 0:
                    pts = get_champ_points(rank)
                    overall_points[username] += pts
                    stage_results.append({
                        "rank": rank,
                        "username": username,
                        "points": pts
                    })
            await asyncio.sleep(1)  # Пауза, чтобы не превышать лимиты API

        stages.append({
            "id": t_id,
            "name": t_name,
            "date": t_date,
            "status": t_status,
            "url": url_link,
            "results": stage_results
        })

    sorted_overall = sorted(overall_points.items(), key=lambda x: x[1], reverse=True)

    result_data = {
        "overall": sorted_overall,
        "stages": stages
    }

    champ_cache["data"] = result_data
    champ_cache["timestamp"] = time.time()
    print("=== [ЧЕМПИОНАТ] Данные успешно загружены ===", flush=True)
    return result_data


# =====================================================================
# СБОР ДАННЫХ ДЛЯ ТАБЛИЦЫ И СТАТИСТИКИ КЛУБА
# =====================================================================
async def fetch_member_data(client, username, start_date, end_date):
    token = get_token_from_cache(username)
    headers = {"User-Agent": "VK-ChessClubBot/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def fetch_lines(url):
        for attempt in range(3):
            try:
                resp = await client.get(url, headers=headers, timeout=15.0)
                if resp.status_code == 200:
                    return resp.text.strip().split('\n')
                elif resp.status_code == 429:
                    print(f"!!! 429 ЛИМИТ для {username}, ждем 10 сек...", flush=True)
                    await asyncio.sleep(10)
                elif resp.status_code == 404:
                    return []
            except Exception:
                pass
            await asyncio.sleep(3)
        return None

    async def get_arena():
        lines = await fetch_lines(f"https://lichess.org/api/user/{username.lower()}/tournament/played")
        if lines is None: return None
        count, wins = 0, 0
        for line in lines:
            if not line.strip(): continue
            try:
                t = json.loads(line)
                starts_at = t.get('tournament', {}).get('startsAt', 0)
                t_date = datetime.fromtimestamp(starts_at / 1000).date()
                if start_date <= t_date <= end_date:
                    count += 1
                    if t.get('player', {}).get('rank') == 1: wins += 1
            except Exception:
                continue
        return count, wins

    async def get_swiss():
        lines = await fetch_lines(f"https://lichess.org/api/user/{username.lower()}/swiss/played")
        if lines is None: return None
        count, wins = 0, 0
        for line in lines:
            if not line.strip(): continue
            try:
                t = json.loads(line)
                starts_at = t.get('tournament', {}).get('startsAt', 0)
                t_date = datetime.fromtimestamp(starts_at / 1000).date()
                if start_date <= t_date <= end_date:
                    count += 1
                    if t.get('player', {}).get('rank') == 1: wins += 1
            except Exception:
                continue
        return count, wins

    async def get_puzzles():
        if not token: return 0
        lines = await fetch_lines("https://lichess.org/api/puzzle/activity")
        if lines is None: return None
        count = 0
        for line in lines:
            if not line.strip(): continue
            try:
                entry = json.loads(line)
                event_ts = entry.get("date", 0)
                event_date = datetime.fromtimestamp(event_ts / 1000).date()
                if start_date <= event_date <= end_date: count += 1
            except Exception:
                continue
        return count

    res = await asyncio.gather(get_arena(), get_swiss(), get_puzzles())

    if None in res:
        raise Exception(f"Fail {username}")

    arena_c, arena_w = res[0]
    swiss_c, swiss_w = res[1]
    puzzles_c = res[2]

    return (username, puzzles_c, arena_c + swiss_c, arena_w + swiss_w)


async def fetch_and_save_table_data(period_type):
    start_date, end_date = get_period_dates(period_type)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT lichess_username FROM users WHERE lichess_username IS NOT NULL")
        token_usernames = [row[0] for row in c.fetchall()]

        c.execute("SELECT username, surname FROM surnames")
        surname_map = {row[0].lower(): row[1] for row in c.fetchall()}

        c.execute("SELECT username FROM token_cache")
        cache_usernames = [row[0] for row in c.fetchall()]
        conn.close()

        members_list = list(set([u.lower() for u in token_usernames] + cache_usernames))
        if not members_list: return False

        old_rows_dict = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                    if period_type in cached_data:
                        for r in cached_data[period_type]["data"]:
                            old_rows_dict[r[0].lower()] = tuple(r)
            except Exception:
                pass

        members = [{"id": u} for u in members_list]
        new_results = []
        processed_count = 0

        print(f"\n=== [СБОР] НАЧАЛО. Всего участников: {len(members)} ===\n", flush=True)

        async def fetch_all():
            nonlocal processed_count
            async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
                sem = asyncio.Semaphore(3)

                async def bounded_fetch(m):
                    nonlocal processed_count
                    async with sem:
                        username = m.get('id')
                        if not username: return None
                        try:
                            res = await fetch_member_data(client, username, start_date, end_date)
                            processed_count += 1
                            print(f"=== [СБОР] {processed_count}/{len(members)}: {username} ===", flush=True)
                            return res
                        except Exception:
                            processed_count += 1
                            print(f"=== [ОШИБКА] {processed_count}/{len(members)}: {username} ===", flush=True)
                            return None

                for i in range(0, len(members), 3):
                    batch = members[i:i + 3]
                    batch_results = await asyncio.gather(*(bounded_fetch(m) for m in batch))
                    new_results.extend(batch_results)
                    await asyncio.sleep(2)

        await fetch_all()

        print("\n=== [СБОР] ЗАВЕРШЕН. ОБРАБОТКА РЕЗУЛЬТАТОВ... ===\n", flush=True)

        final_rows = []
        for m in members:
            username = m["id"]
            new_res = next((r for r in new_results if r and r[0].lower() == username.lower()), None)
            sname = (surname_map.get(username.lower()) or "").strip()

            if new_res:
                final_rows.append((new_res[0], new_res[1], new_res[2], new_res[3], sname))
            else:
                if username.lower() in old_rows_dict:
                    old_r = old_rows_dict[username.lower()]
                    old_sname = old_r[4] if len(old_r) > 4 else ""
                    final_rows.append((old_r[0], old_r[1], old_r[2], old_r[3], (old_sname or sname).strip()))
                else:
                    final_rows.append((username, 0, 0, 0, sname))

        if final_rows:
            final_rows.sort(key=lambda x: (-(x[1] + x[2]), -x[3], x[0].lower()))

            cached_data = {}
            if os.path.exists(CACHE_FILE):
                try:
                    with open(CACHE_FILE, "r", encoding="utf-8") as f:
                        cached_data = json.load(f)
                except Exception:
                    pass

            cached_data[period_type] = {"data": final_rows, "timestamp": time.time()}
            table_cache[period_type] = cached_data[period_type]

            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cached_data, f, ensure_ascii=False)
            print(f"\n=== [СБОР] ТАБЛИЦА {period_type} УСПЕШНО СОХРАНЕНА ===\n", flush=True)
            return True
    except Exception as e:
        print(f"\n!!! [СБОР] ГЛОБАЛЬНАЯ ОШИБКА: {e}\n", flush=True)
    return False


async def fetch_and_save_club_data():
    try:
        team_info = await fetch_lichess_json(f"https://lichess.org/api/team/{TEAM_ID}")
        if not team_info or 'nbMembers' not in team_info:
            return False

        tournaments = await get_all_team_tournaments()
        top_players_text = ""

        members = await fetch_lichess_ndjson(f"https://lichess.org/api/team/{TEAM_ID}/users")
        if members:
            usernames = [m.get('id') for m in members if m.get('id')]
            profiles = await fetch_users_profiles(usernames)
            modes = [('blitz', '⚡ Блиц'), ('rapid', '🐢 Рапид'), ('bullet', '🚀 Пуля')]
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

        total_t = len(tournaments)
        ongoing_t = len([t for t in tournaments if t.get('status') == 20])
        upcoming_t = len([t for t in tournaments if t.get('status') == 10])

        # Считаем завершенные по вашей формуле
        finished_t = total_t - ongoing_t - upcoming_t

        text = (
            f"🏆 Статистика клуба: {team_info.get('name', 'Наш клуб')}\n\n"
            f"👥 Участников: {team_info.get('nbMembers', 0)}\n"
            f"📊 Всего турниров: {total_t}\n"
            f"⏳ Идущих: {ongoing_t}\n"
            f"📅 Предстоящих: {upcoming_t}\n"
            f"🏁 Завершённых: {finished_t}\n"
            f"{top_players_text}\n"
        )

        club_stats_cache["text"] = text
        club_stats_cache["timestamp"] = time.time()

        with open(CLUB_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"text": text, "timestamp": time.time()}, f, ensure_ascii=False)

        print("=== СТАТИСТИКА КЛУБА ОБНОВЛЕНА ===", flush=True)
        return True
    except Exception as e:
        print(f"!!! ОШИБКА СТАТИСТИКИ КЛУБА: {e}", flush=True)
    return False


async def background_updater_loop():
    await asyncio.sleep(15)

    while True:
        msk_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(msk_tz)

        next_midnight = (now_msk + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_midnight = (next_midnight - now_msk).total_seconds()

        print(f"=== СЛЕДУЮЩЕЕ ОБНОВЛЕНИЕ ЗАПЛАНИРОВАНО НА {next_midnight.strftime('%d.%m.%Y %H:%M')} МСК ===",
              flush=True)

        await asyncio.sleep(seconds_until_midnight)

        print("=== 00:00 МСК: СБРОС КЭШЕЙ И ЗАПУСК ОБНОВЛЕНИЯ ===", flush=True)

        daily_puzzle_cache["data"] = None
        daily_puzzle_cache["timestamp"] = 0
        api_cache.clear()
        tournaments_cache["data"] = []
        champ_cache.clear()

        for file_path in [CACHE_FILE, CLUB_CACHE_FILE]:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass

        table_cache.clear()
        club_stats_cache["text"] = ""

        await fetch_and_save_table_data("autumn")
        await asyncio.sleep(5)
        await fetch_and_save_table_data("spring")
        await asyncio.sleep(5)
        await fetch_and_save_club_data()

        print("=== НОЧНОЕ ОБНОВЛЕНИЕ ЗАВЕРШЕНО ===", flush=True)


# =====================================================================
# ГЕНЕРАЦИЯ EXCEL ФАЙЛА ДЛЯ ТАБЛИЦЫ УЧАСТНИКОВ
# =====================================================================
def generate_excel_table(period_type):
    cache_entry = table_cache.get(period_type)
    if not cache_entry:
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                    if period_type in cached_data:
                        cache_entry = cached_data[period_type]
            except Exception:
                pass

    if not cache_entry: return None
    rows = cache_entry.get("data", [])
    if not rows: return None

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Таблица активности"

    headers = ["Место", "Фамилия", "Ник Lichess", "Задачи", "Турниры", "Победы", "Всего баллов"]
    ws.append(headers)

    for col in range(1, 8):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    for i, r in enumerate(rows, 1):
        username = r[0]
        puzzles = r[1]
        tourns = r[2]
        wins = r[3]
        surname = (r[4] or "").strip() if len(r) > 4 else ""
        total = puzzles + tourns
        ws.append([i, surname, username, puzzles, tourns, wins, total])

    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        ws.column_dimensions[column].width = (max_length + 2)

    filepath = data_dir / f"table_{period_type}.xlsx"
    wb.save(filepath)
    return str(filepath)


# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================
def get_main_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🏆 Чемпионат Under Pressure", payload={"cmd": "champ_under_pressure"}),
                 color=KeyboardButtonColor.NEGATIVE)
    keyboard.row()
    keyboard.add(Text("👤 Статистика игрока", payload={"cmd": "menu_player"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🧩 Статистика задач", payload={"cmd": "menu_puzzles"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📅 Статистика за периоды", payload={"cmd": "menu_periods"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🏆 Статистика клуба", payload={"cmd": "menu_club"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📊 Статистика для руководителей клуба", payload={"cmd": "leaders_menu"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📝 Добавить фамилию", payload={"cmd": "add_surname"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("🎯 Задача дня", payload={"cmd": "daily_puzzle"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔄 Сменить ник", payload={"cmd": "change_nick"}), color=KeyboardButtonColor.NEGATIVE)
    keyboard.row()
    keyboard.add(Text("🔑 Мой токен Lichess", payload={"cmd": "set_token"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_champ_main_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🏆 Турнирное положение", payload={"cmd": "champ_overall", "page": 0}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📅 Этапы", payload={"cmd": "champ_stages", "page": 0}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_champ_overall_keyboard(page, has_next):
    keyboard = Keyboard(one_time=False, inline=False)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(Text("⬅️ Назад", payload={"cmd": "champ_overall", "page": page - 1}))
    if has_next:
        nav_buttons.append(Text("Вперед ➡️", payload={"cmd": "champ_overall", "page": page + 1}))
    if nav_buttons:
        for btn in nav_buttons: keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()
    keyboard.add(Text("🔙 Назад в меню чемпионата", payload={"cmd": "champ_under_pressure"}),
                 color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_champ_stages_keyboard(stages, page):
    keyboard = Keyboard(one_time=False, inline=False)
    per_page = 5
    start_idx = page * per_page
    end_idx = start_idx + per_page

    for i, s in enumerate(stages[start_idx:end_idx]):
        global_idx = start_idx + i
        # Добавляем значок статуса
        status_icon = "🏁 " if s.get('status') == 30 else "📅 "
        short_name = s['name'][:22] + "..." if len(s['name']) > 22 else s['name']

        keyboard.add(Text(f"{status_icon}{short_name}",
                          payload={"cmd": "champ_view_stage", "idx": global_idx, "page": page, "res_page": 0}),
                     color=KeyboardButtonColor.SECONDARY)
        keyboard.row()

    nav_buttons = []
    if page > 0: nav_buttons.append(Text("⬅️ Назад", payload={"cmd": "champ_stages", "page": page - 1}))
    if end_idx < len(stages): nav_buttons.append(Text("Вперед ➡️", payload={"cmd": "champ_stages", "page": page + 1}))

    if nav_buttons:
        for btn in nav_buttons: keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 Назад в меню чемпионата", payload={"cmd": "champ_under_pressure"}),
                 color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_champ_stage_view_keyboard(idx, page, res_page, has_next):
    keyboard = Keyboard(one_time=False, inline=False)
    nav_buttons = []
    if res_page > 0:
        nav_buttons.append(
            Text("⬅️ Назад", payload={"cmd": "champ_view_stage", "idx": idx, "page": page, "res_page": res_page - 1}))
    if has_next:
        nav_buttons.append(
            Text("Вперед ➡️", payload={"cmd": "champ_view_stage", "idx": idx, "page": page, "res_page": res_page + 1}))

    if nav_buttons:
        for btn in nav_buttons: keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 К списку этапов", payload={"cmd": "champ_stages", "page": page}),
                 color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_leaders_menu_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("👥 Статистика участника", payload={"cmd": "view_member_stats"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📊 Таблица участников", payload={"cmd": "table_members"}), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("📥 Загрузить таблицу в Excel", payload={"cmd": "download_excel_menu"}),
                 color=KeyboardButtonColor.POSITIVE)
    keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_download_excel_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🍂 Осенний период", payload={"cmd": "download_excel_period", "type": "autumn"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🌸 Весенний период", payload={"cmd": "download_excel_period", "type": "spring"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_periods_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🍂 Осенний период", payload={"cmd": "period_stats", "type": "autumn"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🌸 Весенний период", payload={"cmd": "period_stats", "type": "spring"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_table_period_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🍂 Осенний период", payload={"cmd": "table_period", "type": "autumn"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text("🌸 Весенний период", payload={"cmd": "table_period", "type": "spring"}),
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
    keyboard.add(Text("🔄 Обновить статистику", payload={"cmd": "club_update"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def get_tournaments_menu_keyboard():
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("📅 Предстоящие", payload={"cmd": "tournaments_list", "type": "upcoming"}),
                 color=KeyboardButtonColor.PRIMARY)
    keyboard.add(Text("🏁 Завершённые", payload={"cmd": "tournaments_list", "type": "finished"}),
                 color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("🔄 Обновить список", payload={"cmd": "club_update"}), color=KeyboardButtonColor.SECONDARY)
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
        for btn in nav_buttons: keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
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
        for btn in nav_buttons: keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔙 К списку турниров", payload={"cmd": "tournaments_list", "type": list_type, "page": list_page}),
                 color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 К статистике клуба", payload={"cmd": "menu_club"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ТАБЛИЦЫ ---
def pluralize(n, forms):
    n = abs(n) % 100
    n1 = n % 10
    if 10 < n < 20: return forms[2]
    if 1 < n1 < 5: return forms[1]
    if n1 == 1: return forms[0]
    return forms[2]


async def send_table_page(target, period_type, page, is_edit=False):
    cache_entry = table_cache.get(period_type)
    if not cache_entry:
        cached_data = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
            except Exception:
                pass
        if period_type in cached_data:
            cache_entry = cached_data[period_type]
            table_cache[period_type] = cache_entry
        else:
            cache_entry = {"data": [], "timestamp": 0}

    rows = cache_entry.get("data", [])
    per_page = 20
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_rows = rows[start_idx:end_idx]

    is_autumn = period_type == "autumn"
    period_name = "🍂 Осенний период" if is_autumn else "🌸 Весенний период"
    start_date, end_date = get_period_dates(period_type)
    date_range = f"{start_date.strftime('%d.%m.%Y')} — {end_date.strftime('%d.%m.%Y')}"

    total_pages = max(1, (len(rows) + per_page - 1) // per_page)
    total_puzzles = sum(r[1] for r in rows)
    total_tourns = sum(r[2] for r in rows)
    total_wins = sum(r[3] for r in rows)
    active_count = sum(1 for r in rows if r[1] + r[2] > 0)

    text = f"📊Таблица активности участников\n{period_name}\n📅 {date_range}\n\nРейтинг участников · стр. {page + 1}/{total_pages}\n\n"

    if page_rows:
        for i, r in enumerate(page_rows, start_idx + 1):
            username = r[0]
            puzzles = r[1]
            tourns = r[2]
            wins = r[3]
            surname = (r[4] or "").strip() if len(r) > 4 else ""

            display_name = f"{surname} {username}".strip() if surname else username

            total = puzzles + tourns
            ball_word = pluralize(total, ('балл', 'балла', 'баллов'))
            task_word = pluralize(puzzles, ('задача', 'задачи', 'задач'))
            tourn_word = pluralize(tourns, ('турнир', 'турнира', 'турниров'))
            win_word = pluralize(wins, ('победа', 'победы', 'побед'))
            prefix = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else f"{i}. "

            text += f"{prefix} {display_name} — {total} {ball_word}\n\u00A0\u00A0\u00A0\u00A0\u00A0🧩 {puzzles} {task_word} · 🏆 {tourns} {tourn_word} · 🥇 {wins} {win_word}\n"
    else:
        text += "На этой странице нет участников.\n"

    text += "\n" + "━" * 22 + "\n\n"
    total_puzzles_word = pluralize(total_puzzles, ('задача', 'задачи', 'задач'))
    total_tourns_word = pluralize(total_tourns, ('турнир', 'турнира', 'турниров'))
    total_wins_word = pluralize(total_wins, ('победа', 'победы', 'побед'))
    text += (
        f"📈Всего: 🧩 {total_puzzles} {total_puzzles_word} · 🏆 {total_tourns} {total_tourns_word} · 🥇 {total_wins} {total_wins_word}\n"
        f"👥 Участников с токеном: {len(rows)} (активных: {active_count})")

    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text("🔄 Обновить данные", payload={"cmd": "table_update", "type": period_type}),
                 color=KeyboardButtonColor.SECONDARY)
    keyboard.row()

    nav_buttons = []
    if page > 0:
        nav_buttons.append(Text("⬅️ Назад", payload={"cmd": "table_page", "type": period_type, "page": page - 1}))
    if len(rows) > end_idx:
        nav_buttons.append(Text("Вперед ➡️", payload={"cmd": "table_page", "type": period_type, "page": page + 1}))
    if nav_buttons:
        for btn in nav_buttons: keyboard.add(btn, color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    keyboard.add(Text("🔄 Другой период", payload={"cmd": "table_members"}), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("🔙 В главное меню", payload={"cmd": "back_main"}), color=KeyboardButtonColor.SECONDARY)
    kb_json = keyboard.get_json()

    if is_edit:
        prev_id = last_table_message.get(target.peer_id)
        if prev_id is not None:
            try:
                await target.ctx_api.messages.edit(peer_id=target.peer_id, message_id=prev_id, message=text,
                                                   keyboard=kb_json, parse_mode='HTML')
                return
            except Exception:
                pass

    resp = await target.answer(text, keyboard=kb_json, parse_mode='HTML')
    mid = None
    if hasattr(resp, 'response') and resp.response:
        mid = resp.response[0].message_id
    elif isinstance(resp, list) and resp:
        mid = getattr(resp[0], 'message_id', None)
    if mid: last_table_message[target.peer_id] = mid


# =====================================================================
# РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ
# =====================================================================
async def is_club_leader(username):
    if not username:
        return False
    return username.lower() in [u.lower() for u in LEADER_USERNAMES]
def register_handlers(bot: Bot):
    @bot.on.message(text=["Начать", "начать", "Start", "start", "/Start", "/start"])
    async def start_handler(message: Message):
        waiting_for_nick.discard(message.from_id)
        waiting_for_token.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        waiting_for_table.discard(message.from_id)
        waiting_for_surname.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        user = get_user_lichess(message.from_id)
        if user:
            await message.answer(f"👋 Привет! Ты привязан как {user}.\n\nВыбери нужный раздел в меню ниже:",
                                 keyboard=get_main_keyboard())
        else:
            waiting_for_code.add(message.from_id)
            await message.answer("🔒 Это закрытый бот шахматного клуба.\n\nПожалуйста, введите секретный код доступа:")

    # --- ОБРАБОТЧИКИ ЧЕМПИОНАТА ---
    @bot.on.message(payload_contains={"cmd": "champ_under_pressure"})
    async def champ_under_pressure_handler(message: Message):
        await message.answer("🏆 Чемпионат Under Pressure\n\nВыберите нужный раздел:",
                             keyboard=get_champ_main_keyboard())

    @bot.on.message(payload_contains={"cmd": "champ_overall"})
    async def champ_overall_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        page = payload_data.get("page", 0)

        await message.answer("⏳ Загружаю турнирное положение (это может занять до минуты)...")
        data = await fetch_championship_data()
        overall = data["overall"]

        if not overall:
            await message.answer("Пока нет данных по турнирам чемпионата.", keyboard=get_champ_main_keyboard())
            return

        per_page = 20
        start_idx = page * per_page
        end_idx = start_idx + per_page
        page_items = overall[start_idx:end_idx]

        text = "🏆 Турнирное положение (Общий зачет)\n\n"
        for i, (username, pts) in enumerate(page_items, start_idx + 1):
            prefix = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else f"{i}. "
            text += f"{prefix} {username} — {pts} баллов\n"

        text += f"\nВсего участников: {len(overall)}"
        has_next = len(overall) > end_idx

        await message.answer(text, keyboard=get_champ_overall_keyboard(page, has_next))

    @bot.on.message(payload_contains={"cmd": "champ_stages"})
    async def champ_stages_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        page = payload_data.get("page", 0)

        await message.answer("⏳ Загружаю список этапов...")
        data = await fetch_championship_data()
        stages = data["stages"]

        if not stages:
            await message.answer("Турниры чемпионата еще не созданы.", keyboard=get_champ_main_keyboard())
            return

        await message.answer(f"📅 Этапы чемпионата (стр. {page + 1}):\n🏁 — завершен, 📅 — предстоит",
                             keyboard=get_champ_stages_keyboard(stages, page))

    @bot.on.message(payload_contains={"cmd": "champ_view_stage"})
    async def champ_view_stage_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        idx = payload_data.get("idx", 0)
        page = payload_data.get("page", 0)
        res_page = payload_data.get("res_page", 0)

        data = await fetch_championship_data()
        stages = data["stages"]

        if idx >= len(stages):
            await message.answer("Этап не найден.", keyboard=get_champ_stages_keyboard(stages, page))
            return

        stage = stages[idx]
        t_name = stage["name"]
        t_date = stage["date"]
        t_url = stage["url"]
        t_status = stage.get("status", 0)
        results = stage.get("results", [])

        if t_status != 30:
            # Турнир еще не завершен
            text = (f"🏆 {t_name}\n"
                    f"📅 Дата проведения: {t_date}\n"
                    f"🔗 Ссылка: {t_url}\n\n"
                    f"⚠️ Этот турнир еще не завершен. Результаты и начисленные баллы появятся после его окончания.")
            await message.answer(text, keyboard=get_champ_stage_view_keyboard(idx, page, 0, False))
            return

        # Если завершен - выводим таблицу
        per_page = 20
        start_idx = res_page * per_page
        end_idx = start_idx + per_page
        page_results = results[start_idx:end_idx]

        text = f"🏆 {t_name}\n📅 {t_date}\n🔗 {t_url}\n\n"
        text += f"Результаты (места {start_idx + 1}-{min(end_idx, len(results))}):\n"

        for r in page_results:
            text += f"{r['rank']}. {r['username']} — {r['points']} баллов\n"

        has_next = len(results) > end_idx
        await message.answer(text, keyboard=get_champ_stage_view_keyboard(idx, page, res_page, has_next))

    # --- ОСТАЛЬНЫЕ ОБРАБОТЧИКИ ---
    @bot.on.message(payload_contains={"cmd": "set_token"})
    async def set_token_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user:
            await message.answer("Сначала привяжите ник (напишите 'Начать').")
            return
        waiting_for_nick.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        waiting_for_table.discard(message.from_id)
        waiting_for_surname.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        waiting_for_token.add(message.from_id)
        await message.answer(
            f"🔑 Отправьте ваш персональный токен Lichess.\n\nОн должен принадлежать вашему аккаунту: {user}.\nЕго можно создать здесь: https://lichess.org/account/oauth/token/create\n",
            keyboard=Keyboard(one_time=True).get_json())

    @bot.on.message(payload_contains={"cmd": "add_surname"})
    async def add_surname_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user:
            await message.answer("Сначала привяжите аккаунт (напишите 'Начать').")
            return
        waiting_for_nick.discard(message.from_id)
        waiting_for_token.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        waiting_for_table.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        waiting_for_surname.add(message.from_id)

        await message.answer(
            f"📝 Введите вашу фамилию для ника {user}.\nОна будет отображаться в таблице участников перед ником.",
            keyboard=Keyboard(one_time=True).get_json())

    @bot.on.message(text="📊 Статистика для руководителей клуба")
    async def leaders_menu_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user:
            await message.answer("Сначала привяжите аккаунт (напишите 'Начать').")
            return

        if not await is_club_leader(user):
            await message.answer(
                f"❌ Эта функция доступна только руководителям.")
            return

        await message.answer("📊 Статистика для руководителей клуба\nВыберите раздел:",
                             keyboard=get_leaders_menu_keyboard())

    @bot.on.message(payload_contains={"cmd": "view_member_stats"})
    async def view_member_stats_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user or not await is_club_leader(user):
            await message.answer("❌ Эта функция доступна только руководителям.")
            return
        waiting_for_nick.discard(message.from_id)
        waiting_for_token.discard(message.from_id)
        waiting_for_table.discard(message.from_id)
        waiting_for_surname.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        waiting_for_member_nick.add(message.from_id)
        await message.answer("Введите ник участника клуба на Lichess или его фамилию:",
                             keyboard=Keyboard(one_time=True).get_json())

    @bot.on.message(payload_contains={"cmd": "table_members"})
    async def table_members_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user or not await is_club_leader(user):
            await message.answer("❌ Эта функция доступна только руководителям.")
            return
        waiting_for_nick.discard(message.from_id)
        waiting_for_token.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        waiting_for_surname.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        waiting_for_table.add(message.from_id)
        await message.answer("Выберите период для таблицы участников:", keyboard=get_table_period_keyboard())

    @bot.on.message(payload_contains={"cmd": "download_excel_menu"})
    async def download_excel_menu_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user or not await is_club_leader(user):
            await message.answer("❌ Эта функция доступна только руководителям.")
            return
        await message.answer("Выберите период, за который нужно скачать таблицу в Excel:",
                             keyboard=get_download_excel_keyboard())

    @bot.on.message(payload_contains={"cmd": "download_excel_period"})
    async def download_excel_period_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        period_type = payload_data.get("type", "autumn")

        if period_type not in table_cache and not os.path.exists(CACHE_FILE):
            await message.answer("⏳ Данных пока нет. Сначала собираю таблицу (это займет время)...")
            await fetch_and_save_table_data(period_type)

        await message.answer("⏳ Генерирую Excel файл...")

        filepath = generate_excel_table(period_type)
        if not filepath:
            await message.answer(
                "⚠️ Нет данных для создания файла. Сначала откройте таблицу в боте, чтобы она собралась.")
            return

        try:
            attachment = await upload_doc_to_vk(message.ctx_api, message.peer_id, filepath,
                                                f"Таблица_{period_type}.xlsx")
            await message.answer(f"📊 Таблица за {period_type} в формате Excel:", attachment=attachment)
        except Exception as e:
            await message.answer(f"⚠️ Ошибка при загрузке файла: {e}")
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)

    @bot.on.message(payload_contains={"cmd": "table_period"})
    async def table_period_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        period_type = payload_data.get("type", "autumn")
        period_name = "Осенний период" if period_type == "autumn" else "Весенний период"
        if period_type in table_cache:
            await send_table_page(message, period_type, 0)
            return
        cached_data = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
            except Exception:
                pass
        if period_type in cached_data:
            table_cache[period_type] = cached_data[period_type]
            await send_table_page(message, period_type, 0)
            return
        await message.answer(
            f"⏳ База только создается. Собираю таблицу за {period_name} в первый раз. Это займет около 2-3 минут, подождите...")
        success = await fetch_and_save_table_data(period_type)
        if success:
            await send_table_page(message, period_type, 0)
        else:
            await message.answer("⚠️ Не удалось собрать данные.", keyboard=get_table_period_keyboard())

    @bot.on.message(payload_contains={"cmd": "table_update"})
    async def table_update_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        period_type = payload_data.get("type", "autumn")
        await message.answer(f"⏳ Принудительно обновляю таблицу. Это может занять 2-3 минуты, пожалуйста, подождите...")
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                if period_type in cached_data: del cached_data[period_type]
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cached_data, f, ensure_ascii=False)
            except Exception:
                pass
        if period_type in table_cache: del table_cache[period_type]
        success = await fetch_and_save_table_data(period_type)
        if success:
            await send_table_page(message, period_type, 0)
        else:
            await message.answer("⚠️ Не удалось обновить данные.", keyboard=get_table_period_keyboard())

    @bot.on.message(payload_contains={"cmd": "table_page"})
    async def table_page_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        period_type = payload_data.get("type", "autumn")
        page = payload_data.get("page", 0)
        await send_table_page(message, period_type, page, is_edit=True)

    @bot.on.message(payload_contains={"cmd": "member_period"})
    async def member_period_handler(message: Message):
        payload_data = json.loads(message.payload) if isinstance(message.payload, str) else message.payload
        period_type = payload_data.get("type", "autumn")
        target_username = pending_member_target.get(message.from_id)
        if not target_username:
            await message.answer("Сначала выберите участника.")
            return

        cache_key = f"member_period_{target_username.lower()}_{period_type}"
        if cache_key in api_cache and time.time() - api_cache[cache_key]["timestamp"] < 86400:
            await message.answer(api_cache[cache_key]["text"], keyboard=get_member_period_keyboard())
            return

        start_date, end_date = get_period_dates(period_type)
        period_name = "Осенний период" if period_type == "autumn" else "Весенний период"
        await message.answer(f"⏳ Собираю статистику для {target_username} за {period_name}...")

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
                    if t.get('player', {}).get('rank') == 1: period_tourn_wins += 1

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
                                if is_win: puzzles_won += 1
                                puzzles_solved += 1
                                rating = entry.get("puzzle", {}).get("rating")
                                if rating:
                                    if start_rating is None: start_rating = rating
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

            text = f"👤 Статистика игрока {target_username}\n📅 Период: {period_name}\n\n🏆 Турниры за период:\n• Участий: {period_tourn_count}\n• Побед (1 место): {period_tourn_wins}\n\n"
            if puzzles_solved > 0:
                text += f"🧩 Задачи за период:\n• Правильно: {puzzles_won}\n• Неправильно: {puzzles_solved - puzzles_won}\n"
                if start_rating and end_rating: text += f"• Рейтинг: {start_rating} ➡️ {end_rating}\n"
                text += "\n"
            else:
                text += f"🧩 Задачи за период: нет данных (токен не привязан)\n\n" if not user_token else f"🧩 Задачи за период: нет решенных задач\n\n"
            if rating_lines: text += f"📈 Текущие рейтинги:\n" + "\n".join(rating_lines) + "\n"
            api_cache[cache_key] = {"text": text, "timestamp": time.time()}
            await message.answer(text, keyboard=get_member_period_keyboard())
        except Exception as e:
            await message.answer(f"⚠️ Ошибка: {e}", keyboard=get_member_period_keyboard())

    @bot.on.message(payload_contains={"cmd": "menu_periods"})
    async def menu_periods_handler(message: Message):
        user = get_user_lichess(message.from_id)
        if not user:
            await message.answer("Сначала привяжите аккаунт.")
            return
        await message.answer("📅 Выберите период:", keyboard=get_periods_keyboard())

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
            await message.answer(f"Нужен токен для {user}. Нажмите «🔑 Мой токен Lichess».",
                                 keyboard=get_periods_keyboard())
            return

        cache_key = f"period_{user.lower()}_{period_type}"
        if cache_key in api_cache and time.time() - api_cache[cache_key]["timestamp"] < 86400:
            await message.answer(api_cache[cache_key]["text"], keyboard=get_periods_keyboard())
            return

        start_date, end_date = get_period_dates(period_type)
        period_name = "Осенний период" if period_type == "autumn" else "Весенний период"
        await message.answer(f"⏳ Собираю статистику за {period_name}...")

        try:
            puzzles_data = await fetch_lichess_ndjson("https://lichess.org/api/puzzle/activity", token=user_token)
            puzzles_solved, puzzles_won, start_rating, end_rating = 0, 0, None, None
            for entry in puzzles_data:
                try:
                    event_ts = entry.get("date", 0)
                    event_date = datetime.fromtimestamp(event_ts / 1000).date()
                    if start_date <= event_date <= end_date:
                        is_win = entry.get("win", False)
                        if is_win: puzzles_won += 1
                        puzzles_solved += 1
                        rating = entry.get("puzzle", {}).get("rating")
                        if rating:
                            if start_rating is None: start_rating = rating
                            end_rating = rating
                except Exception:
                    continue

            played_tournaments = await fetch_lichess_ndjson(
                f"https://lichess.org/api/user/{user.lower()}/tournament/played")
            tourn_count, tourn_wins = 0, 0
            for t in played_tournaments:
                starts_at = t.get('tournament', {}).get('startsAt', 0)
                t_date = datetime.fromtimestamp(starts_at / 1000).date()
                if start_date <= t_date <= end_date:
                    tourn_count += 1
                    if t.get('player', {}).get('rank') == 1: tourn_wins += 1

            if puzzles_solved == 0 and tourn_count == 0:
                await message.answer(f"За {period_name.lower()} у вас нет активности.", keyboard=get_periods_keyboard())
                return

            text = f"📊 Статистика за {period_name}\n\n"
            if puzzles_solved > 0:
                text += f"🧩 Задачи:\n• Правильно решено: {puzzles_won}\n• Неправильно решено: {puzzles_solved - puzzles_won}\n"
                if start_rating and end_rating: text += f"• Рейтинг: {start_rating} ➡️ {end_rating}\n"
                text += "\n"
            text += f"🏆 Турниры:\n• Сыграно турниров: {tourn_count}\n• Побед (1 место): {tourn_wins}\n"
            api_cache[cache_key] = {"text": text, "timestamp": time.time()}
            await message.answer(text, keyboard=get_periods_keyboard())
        except Exception as e:
            await message.answer(f"⚠️ Ошибка: {e}", keyboard=get_periods_keyboard())

    @bot.on.message(payload_contains={"cmd": "back_main"})
    async def back_main_handler(message: Message):
        pending_member_target.pop(message.from_id, None)
        waiting_for_member_nick.discard(message.from_id)
        waiting_for_table.discard(message.from_id)
        waiting_for_surname.discard(message.from_id)
        user = get_user_lichess(message.from_id)
        await message.answer(f"👋 Главное меню\n\nВы привязаны как: {user}\nВыберите, что хотите посмотреть:",
                             keyboard=get_main_keyboard())

    @bot.on.message(payload_contains={"cmd": "change_nick"})
    async def change_nick_handler(message: Message):
        waiting_for_nick.add(message.from_id)
        waiting_for_token.discard(message.from_id)
        waiting_for_member_nick.discard(message.from_id)
        waiting_for_table.discard(message.from_id)
        waiting_for_surname.discard(message.from_id)
        pending_member_target.pop(message.from_id, None)
        await message.answer("🔗 Регистрация\n\nОтправьте ваш новый ник на Lichess:",
                             keyboard=Keyboard(one_time=True).get_json())

    @bot.on.message(payload_contains={"cmd": "menu_player"})
    async def menu_player_handler(message: Message):
        lichess_user = get_user_lichess(message.from_id)
        if not lichess_user:
            await message.answer("Сначала привяжите аккаунт.")
            return

        cache_key = f"player_{lichess_user.lower()}"
        if cache_key in api_cache and time.time() - api_cache[cache_key]["timestamp"] < 3600:
            await message.answer(api_cache[cache_key]["text"], keyboard=get_back_to_main_keyboard())
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
        lines = [f"👤 Статистика игрока {lichess_user}\n", f"🏆 Всего сыграно турниров: {total_tournaments}",
                 f"🥇 Побед (1 место): {wins}\n", f"📈 Текущие рейтинги Lichess:"]
        perfs = user_data.get('perfs', {})
        for mode in ['bullet', 'blitz', 'rapid', 'classical']:
            if mode in perfs and 'rating' in perfs[mode]:
                prov = " (provisional)" if perfs[mode].get('prov') else ""
                lines.append(f"• {mode.capitalize()}: {perfs[mode]['rating']}{prov}")
        final_text = "\n".join(lines)
        api_cache[cache_key] = {"text": final_text, "timestamp": time.time()}
        await message.answer(final_text, keyboard=get_back_to_main_keyboard())

    @bot.on.message(payload_contains={"cmd": "menu_puzzles"})
    async def menu_puzzles_handler(message: Message):
        lichess_user = get_user_lichess(message.from_id)
        if not lichess_user:
            await message.answer("Сначала привяжите аккаунт.")
            return

        cache_key = f"puzzles_{lichess_user.lower()}"
        if cache_key in api_cache and time.time() - api_cache[cache_key]["timestamp"] < 3600:
            await message.answer(api_cache[cache_key]["text"], keyboard=get_back_to_main_keyboard())
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
        final_text = "\n".join(lines)
        api_cache[cache_key] = {"text": final_text, "timestamp": time.time()}
        await message.answer(final_text, keyboard=get_back_to_main_keyboard())

    @bot.on.message(payload_contains={"cmd": "daily_puzzle"})
    async def daily_puzzle_handler(message: Message):
        if daily_puzzle_cache["data"] and (time.time() - daily_puzzle_cache["timestamp"] < 86400):
            data = daily_puzzle_cache["data"]
        else:
            await message.answer("⏳ Загружаю задачу дня...")
            try:
                data = await fetch_lichess_json("https://lichess.org/api/puzzle/daily")
                if not data:
                    await message.answer("⚠️ Ограничение запросов.", keyboard=get_back_to_main_keyboard())
                    return
                daily_puzzle_cache["data"] = data
                daily_puzzle_cache["timestamp"] = time.time()
            except Exception as e:
                await message.answer(f"⚠️ Ошибка: {e}", keyboard=get_back_to_main_keyboard())
                return

        try:
            rating = data.get('puzzle', {}).get('rating', '?')
            themes = ", ".join(data.get('puzzle', {}).get('themes', []))
            text = f"🎯 Ежедневная задача Lichess\n\n📈 Рейтинг задачи: {rating}\n🏷️ Темы: {themes}\n\nРешить: https://lichess.org/training/daily"
            await message.answer(text, keyboard=get_back_to_main_keyboard())
        except Exception as e:
            await message.answer(f"⚠️ Ошибка: {e}", keyboard=get_back_to_main_keyboard())

    @bot.on.message(payload_contains={"cmd": "menu_club"})
    async def menu_club_handler(message: Message):
        if not club_stats_cache["text"] and os.path.exists(CLUB_CACHE_FILE):
            try:
                with open(CLUB_CACHE_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    club_stats_cache["text"] = saved.get("text", "")
                    club_stats_cache["timestamp"] = saved.get("timestamp", 0)
            except Exception:
                pass

        if club_stats_cache["text"] and (time.time() - club_stats_cache["timestamp"] < 86400):
            await message.answer(club_stats_cache["text"], keyboard=get_club_keyboard(True))
            return

        await message.answer("⏳ Собираю статистику клуба (это может занять время)...")
        success = await fetch_and_save_club_data()
        if success:
            await message.answer(club_stats_cache["text"], keyboard=get_club_keyboard(True))
        else:
            await message.answer("⚠️ Не удалось получить данные клуба. Попробуйте позже.",
                                 keyboard=get_back_to_main_keyboard())

    @bot.on.message(payload_contains={"cmd": "club_update"})
    async def club_update_handler(message: Message):
        club_stats_cache["text"] = ""
        tournaments_cache["data"] = []
        await message.answer("🔄 Кэш сброшен. Загружаю заново...")
        await menu_club_handler(message)

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

        cache_key = f"{t_format}_{tid}"
        if cache_key in tournament_results_cache:
            results = tournament_results_cache[cache_key]
        else:
            await message.answer("⏳ Загружаю результаты турнира...")
            url = f"https://lichess.org/api/{t_format}/{tid}/results"
            try:
                results = await fetch_lichess_ndjson(url)
                results.sort(key=lambda x: x.get('rank', 9999))
            except Exception as e:
                await message.answer(f"⚠️ Ошибка загрузки: {e}", keyboard=back_kb)
                return
            if status == 30:
                tournament_results_cache[cache_key] = results

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
        lines = [f"🏆 Результаты турнира", f"📅 Дата: {date_str}", f"👥 Участников: {players}", f"🔗 Ссылка: {link}\n",
                 f"Таблица (места {start_idx + 1}-{min(end_idx, len(results))}):"]
        for r in page_results:
            lines.append(f"{r.get('rank', '?')}. {r.get('username', 'Unknown')} — {r.get('score', '?')} очк.")

        has_next = len(results) > end_idx
        final_kb = get_tournament_results_keyboard(t_format, tid, list_type, list_page, res_page, has_next, status,
                                                   date_str, players)
        await message.answer("\n".join(lines), keyboard=final_kb)

    # --- КОМАНДЫ ДИАГНОСТИКИ ---
    @bot.on.message(text="!dbcheck")
    async def dbcheck_handler(message: Message):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT username FROM token_cache")
            tokens = c.fetchall()
            conn.close()
            await message.answer(f"🔍 БАЗА: {DB_PATH}\nТОКЕНЫ: {tokens}")
        except Exception as e:
            await message.answer(f"❌ Ошибка базы: {e}")

    @bot.on.message(text="!clear_cache")
    async def clear_cache_handler(message: Message):
        api_cache.clear()
        club_stats_cache["text"] = ""
        tournaments_cache["data"] = []
        table_cache.clear()
        champ_cache.clear()

        deleted_files = []
        for file_path in [CACHE_FILE, CLUB_CACHE_FILE]:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    deleted_files.append(os.path.basename(file_path))
                except Exception:
                    pass

        await message.answer(
            f"🗑 Кэш успешно очищен!\n"
            f"Удалены файлы: {', '.join(deleted_files) if deleted_files else 'файлов не было'}\n"
            f"Бот загрузит свежие данные при следующем запросе."
        )

    @bot.on.message(text="!test_api")
    async def test_api_handler(message: Message):
        await message.answer("⏳ Проверяю связь с Lichess...")
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                resp = await client.get(f"https://lichess.org/api/team/{TEAM_ID}",
                                        headers={"User-Agent": "VK-ChessClubBot/1.0"})

                if resp.status_code == 200:
                    await message.answer("✅ Lichess отвечает нормально (200 OK).\nЗапросы проходят!")
                elif resp.status_code == 429:
                    await message.answer(
                        "🚫 Lichess БЛОКИРУЕТ запросы (429 Too Many Requests).\nВам нужно подождать 15-30 минут, чтобы IP разблокировали.")
                else:
                    await message.answer(f"⚠️ Lichess вернул статус: {resp.status_code}")
        except Exception as e:
            await message.answer(f"❌ Ошибка соединения: {e}")

    @bot.on.message(text="<text>")
    async def any_text_handler(message: Message, text: str):
        if message.payload: return

        if message.from_id in waiting_for_code:
            if text.strip().lower() == SECRET_CODE.lower():
                waiting_for_code.discard(message.from_id)
                waiting_for_nick.add(message.from_id)
                await message.answer("✅ Код верный! Отлично.\n\nТеперь отправьте ваш ник на Lichess для привязки:")
            else:
                await message.answer("❌ Неверный код доступа. Попробуйте еще раз или напишите /start для отмены.")
            return

        if message.from_id in waiting_for_token:
            token = text.strip()
            if not token.startswith("lip_"):
                await message.answer("❌ Токен должен начинаться с `lip_`.")
                return
            user = get_user_lichess(message.from_id)
            await message.answer("⏳ Проверяю токен...")
            is_valid = await check_token_valid(token, user)
            if not is_valid:
                await message.answer(f"❌ Токен недействителен или не принадлежит аккаунту {user}.")
                return
            save_user_token(message.from_id, token)
            waiting_for_token.discard(message.from_id)
            await message.answer(f"✅ Токен успешно привязан! Теперь статистика задач будет доступна в полном объеме.",
                                 keyboard=get_main_keyboard())
            return

        if message.from_id in waiting_for_surname:
            surname = text.strip()
            if not surname or len(surname) > 30:
                await message.answer("❌ Некорректная фамилия. Попробуйте еще раз:")
                return

            save_user_surname(message.from_id, surname)
            waiting_for_surname.discard(message.from_id)

            user = get_user_lichess(message.from_id)
            await message.answer(
                f"✅ Фамилия '{surname}' успешно сохранена для ника {user} и будет отображаться в таблице!",
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

            save_user(message.from_id, username)
            waiting_for_nick.discard(message.from_id)
            cached_token = get_token_from_cache(username)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            if cached_token:
                c.execute("UPDATE users SET lichess_token=? WHERE vk_id=?", (cached_token, message.from_id))
                conn.commit()
                conn.close()
                table_cache.clear()
                api_cache.clear()
                await message.answer(
                    f"✅ Успешно! Вы привязаны как {username}.\n🔑 В базе найден сохраненный токен для этого ника.",
                    keyboard=get_main_keyboard())
            else:
                c.execute("UPDATE users SET lichess_token=NULL WHERE vk_id=?", (message.from_id,))
                conn.commit()
                conn.close()
                table_cache.clear()
                api_cache.clear()
                await message.answer(
                    f"✅ Успешно! Вы привязаны как {username}.\n⚠️ Токен для этого ника не найден. Пожалуйста, привяжите его через «🔑 Мой токен Lichess».",
                    keyboard=get_main_keyboard())
            return

        if message.from_id in waiting_for_member_nick:
            query = text.strip()

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute(
                "SELECT username, surname FROM surnames WHERE LOWER(username)=LOWER(?) OR LOWER(surname)=LOWER(?)",
                (query, query))
            row = c.fetchone()
            conn.close()

            target_username = None
            if row:
                target_username = row[0]
                display_surname = row[1]
                await message.answer(
                    f"✅ Найден участник: {display_surname} ({target_username})" if display_surname else f"✅ Найден участник: {target_username}")
            else:
                await message.answer("⏳ Проверяю ник на Lichess...")
                if await check_user_exists(query):
                    target_username = query
                else:
                    await message.answer(f"❌ Игрок или фамилия '{query}' не найдены.")
                    return

            pending_member_target[message.from_id] = target_username
            waiting_for_member_nick.discard(message.from_id)

            await message.answer(f"Выберите период для просмотра статистики:", keyboard=get_member_period_keyboard())
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

    asyncio.create_task(background_updater_loop())

    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())