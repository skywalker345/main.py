#!/usr/bin/env python3
"""
AlphaTrackerBot — управление дропами и командной работой
"""

import sqlite3
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple
import re

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

TZ = ZoneInfo("Europe/Kyiv")
WORK_START = 11
WORK_END = 23
DAILY_FEE_USD = 2.5

COMMISSIONED_MEMBERS = {
    "Назар": 0.2,
    "releZz": 0.2,
    "Ангелина 19": 0.2,
    "Ваня": 0.0,
}

KNOWN_PARTICIPANTS = [
    "Назар", "releZz", "Ангелина 19", "Ваня", 
    "Андрей", "Ярик", "Серёга"
]

# ============================================================================
# БАЗА ДАННЫХ
# ============================================================================

class Database:
    def __init__(self, db_path="drops.db"):
        self.db_path = db_path
        self.init_db()

    def get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self.get_conn()
        c = conn.cursor()

        # Члены команды
        c.execute("""CREATE TABLE IF NOT EXISTS members (
            name TEXT PRIMARY KEY,
            ap INTEGER DEFAULT 0,
            refund_days TEXT DEFAULT '[]',
            last_gap_days TEXT DEFAULT '[0,0]',
            updated_at TEXT
        )""")

        # События (дропы)
        c.execute("""CREATE TABLE IF NOT EXISTS drop_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            when_ts INTEGER NOT NULL,
            ap_threshold INTEGER,
            symbol TEXT,
            status TEXT DEFAULT 'planned',
            created_at TEXT
        )""")

        # Рекомендации
        c.execute("""CREATE TABLE IF NOT EXISTS recommendations (
            event_id INTEGER,
            member_name TEXT,
            score INTEGER,
            signals TEXT,
            rank INTEGER,
            PRIMARY KEY (event_id, member_name)
        )""")

        # Бронирования
        c.execute("""CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER,
            member_name TEXT,
            status TEXT DEFAULT 'requested',
            timestamp TEXT
        )""")

        # Отчёты о продажах
        c.execute("""CREATE TABLE IF NOT EXISTS trade_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER,
            member_name TEXT,
            asset TEXT,
            qty REAL,
            sell_price REAL,
            gross_usd REAL,
            fees_usd REAL,
            net_usd REAL,
            share_model REAL,
            created_at TEXT
        )""")

        conn.commit()
        conn.close()

        # Инициализация участников
        self.init_members()

    def init_members(self):
        conn = self.get_conn()
        c = conn.cursor()
        for name in KNOWN_PARTICIPANTS:
            c.execute("INSERT OR IGNORE INTO members (name, updated_at) VALUES (?, ?)",
                     (name, self._now()))
        conn.commit()
        conn.close()

    @staticmethod
    def _now():
        return datetime.now(TZ).isoformat()

    def update_member(self, name: str, ap: int, refund_days: List[int], last_gap: Tuple[int, int]):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""UPDATE members 
                    SET ap=?, refund_days=?, last_gap_days=?, updated_at=? 
                    WHERE name=?""",
                 (ap, json.dumps(refund_days), json.dumps(list(last_gap)), self._now(), name))
        conn.commit()
        conn.close()

    def get_member(self, name: str) -> Optional[Dict]:
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM members WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "name": row["name"],
                "ap": row["ap"],
                "refund_days": json.loads(row["refund_days"]),
                "last_gap_days": json.loads(row["last_gap_days"]),
                "updated_at": row["updated_at"]
            }
        return None

    def get_all_members(self) -> List[Dict]:
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM members")
        rows = c.fetchall()
        conn.close()
        result = []
        for row in rows:
            result.append({
                "name": row["name"],
                "ap": row["ap"],
                "refund_days": json.loads(row["refund_days"]),
                "last_gap_days": json.loads(row["last_gap_days"]),
                "updated_at": row["updated_at"]
            })
        return result

    def create_drop_event(self, when_ts: int, ap_threshold: int, symbol: Optional[str] = None) -> int:
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""INSERT INTO drop_events (when_ts, ap_threshold, symbol, created_at)
                    VALUES (?, ?, ?, ?)""",
                 (when_ts, ap_threshold, symbol or "", self._now()))
        conn.commit()
        event_id = c.lastrowid
        conn.close()
        return event_id

    def get_drop_event(self, event_id: int) -> Optional[Dict]:
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM drop_events WHERE id=?", (event_id,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "id": row["id"],
                "when_ts": row["when_ts"],
                "ap_threshold": row["ap_threshold"],
                "symbol": row["symbol"],
                "status": row["status"]
            }
        return None

    def save_recommendations(self, event_id: int, recs: List[Tuple[str, int, str, int]]):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM recommendations WHERE event_id=?", (event_id,))
        for member_name, score, signals, rank in recs:
            c.execute("""INSERT INTO recommendations 
                        (event_id, member_name, score, signals, rank)
                        VALUES (?, ?, ?, ?, ?)""",
                     (event_id, member_name, score, signals, rank))
        conn.commit()
        conn.close()

    def create_trade_report(self, event_id: int, member_name: str, asset: str, qty: float,
                           sell_price: float, fees_usd: float):
        gross = qty * sell_price
        net = gross - fees_usd
        share_model = COMMISSIONED_MEMBERS.get(member_name, 0.0)

        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""INSERT INTO trade_reports 
                    (event_id, member_name, asset, qty, sell_price, gross_usd, fees_usd, net_usd, share_model, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (event_id, member_name, asset, qty, sell_price, gross, fees_usd, net, share_model, self._now()))
        conn.commit()
        conn.close()

    def get_stats(self) -> str:
        members = self.get_all_members()
        if not members:
            return "❌ Нет участников."

        lines = ["📊 **СТАТИСТИКА**\n"]
        for m in members:
            refund = m["refund_days"]
            gap = m["last_gap_days"]
            refund_str = ", ".join(map(str, refund)) if refund else "—"
            gap_str = f"{gap[0]}-{gap[1]}" if gap else "—"
            lines.append(f"• **{m['name']}**: {m['ap']}AP | вернут [{refund_str}] | последний [{gap_str}]")
        return "\n".join(lines)


# ============================================================================
# ПРИОРИТИЗАЦИЯ (SCORE)
# ============================================================================

def calculate_score(member: Dict, ap_threshold: int) -> Tuple[int, str]:
    """
    Возвращает (score, signals_text)
    
    +2 — если хотя бы одна дата возврата AP ≤ 3 дней
    +1 — если AP ≥ (порог + 20)
    +1 — если последний дроп ≥ 7 дней назад
    """
    score = 0
    signals = []

    # Проверка refund_days
    refund_days = member.get("refund_days", [])
    if refund_days and min(refund_days) <= 3:
        score += 2
        signals.append(f"refund≤3")

    # Проверка AP
    ap = member.get("ap", 0)
    if ap >= ap_threshold + 20:
        score += 1
        signals.append(f"surplus≥20")

    # Проверка last_gap
    gap = member.get("last_gap_days", [0, 0])
    if gap and gap[1] >= 7:
        score += 1
        signals.append(f"rest≥7")

    signals_text = ", ".join(signals) if signals else "—"
    return min(score, 4), signals_text


def get_top_candidates(db: Database, ap_threshold: int, limit: int = 3) -> List[Dict]:
    """Возвращает топ-N кандидатов по score"""
    members = db.get_all_members()
    
    scored = []
    for m in members:
        score, signals = calculate_score(m, ap_threshold)
        scored.append({
            "name": m["name"],
            "ap": m["ap"],
            "refund_days": m["refund_days"],
            "last_gap_days": m["last_gap_days"],
            "score": score,
            "signals": signals
        })

    # Сортировка: score (DESC) → AP (DESC) → min(refund_days) → max(last_gap)
    scored.sort(key=lambda x: (
        -x["score"],
        -x["ap"],
        min(x["refund_days"]) if x["refund_days"] else 999,
        -max(x["last_gap_days"]) if x["last_gap_days"] else 0
    ))

    # Сохраняем в БД
    recs = [(m["name"], m["score"], m["signals"], i + 1) for i, m in enumerate(scored[:limit])]
    # db.save_recommendations(event_id, recs)  # если нужно

    return scored[:limit]


# ============================================================================
# ПАРСЕР КОМАНД
# ============================================================================

def parse_drop_command(text: str) -> Optional[Dict]:
    """
    Парсит /drop Серёга 240AP вернут 3,5 последний 5-7
    """
    # Примерный паттерн: /drop <Имя> <AP> вернут <список> последний <диапазон>
    pattern = r"/drop\s+(.+?)\s+(\d+)\s*AP\s+вернут\s+([\d,]+)\s+последний\s+(\d+)-(\d+)"
    match = re.search(pattern, text)
    
    if not match:
        return None

    name = match.group(1).strip()
    ap = int(match.group(2))
    refund = [int(x.strip()) for x in match.group(3).split(",")]
    gap_min = int(match.group(4))
    gap_max = int(match.group(5))

    return {
        "name": name,
        "ap": ap,
        "refund_days": refund,
        "last_gap": (gap_min, gap_max)
    }


def parse_newdrop_command(text: str) -> Optional[Dict]:
    """
    Парсит /newdrop завтра 14:00 порог 200 [CORL]
    или /newdrop 16:30 порог 210 CORL
    """
    pattern = r"/newdrop\s+(?:(завтра|сегодня)\s+)?(\d{1,2}):(\d{2})\s+порог\s+(\d+)(?:\s+(.+))?$"
    match = re.search(pattern, text)
    
    if not match:
        return None

    day_str = match.group(1) or "сегодня"
    hour = int(match.group(2))
    minute = int(match.group(3))
    threshold = int(match.group(4))
    symbol = match.group(5) or ""

    now = datetime.now(TZ)
    if day_str == "завтра":
        target_date = now + timedelta(days=1)
    else:
        target_date = now

    target_dt = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    when_ts = int(target_dt.timestamp())

    return {
        "when_ts": when_ts,
        "ap_threshold": threshold,
        "symbol": symbol.strip(),
        "datetime": target_dt
    }


def parse_sold_command(text: str) -> Optional[Dict]:
    """
    Парсит /sold Серёга CORL 125шт по 0.80$ комса 2.5$
    """
    pattern = r"/sold\s+(.+?)\s+(\w+)\s+(\d+)\s*шт\s+по\s+([\d.]+)\s*\$?\s+комса\s+([\d.]+)\s*\$?"
    match = re.search(pattern, text)
    
    if not match:
        return None

    name = match.group(1).strip()
    asset = match.group(2).strip()
    qty = int(match.group(3))
    sell_price = float(match.group(4))
    fees = float(match.group(5))

    return {
        "member_name": name,
        "asset": asset,
        "qty": qty,
        "sell_price": sell_price,
        "fees_usd": fees
    }


# ============================================================================
# ИНТЕРФЕЙС БОТа (CLI)
# ============================================================================

class AlphaTrackerBot:
    def __init__(self):
        self.db = Database()
        self.last_drop_event = None

    def is_working_hours(self) -> bool:
        now = datetime.now(TZ)
        return WORK_START <= now.hour < WORK_END

    def handle_command(self, text: str) -> str:
        """Основной обработчик команд"""
        
        if not self.is_working_hours() and not text.startswith("/start") and not text.startswith("/stats"):
            return f"😴 Бот отдыхает до {WORK_START}:00 ⏰"

        text = text.strip()

        if text == "/start":
            return self.cmd_start()
        elif text == "/stats":
            return self.db.get_stats()
        elif text.startswith("/drop"):
            return self.cmd_drop(text)
        elif text.startswith("/newdrop"):
            return self.cmd_newdrop(text)
        elif text.startswith("/sold"):
            return self.cmd_sold(text)
        elif text.startswith("/who"):
            return self.cmd_who(text)
        else:
            return "❓ Неизвестная команда. Используй /start для справки."

    def cmd_start(self) -> str:
        return """🤖 **AlphaTrackerBot** — управление дропами

**Основные команды:**
• `/drop <Имя> <AP> вернут X,Y последний A-B` — обновить данные участника
• `/newdrop [завтра|сегодня] HH:MM порог N [тикер]` — создать дроп
• `/sold <Имя> <ASSET> QTYшт по PRICE$ комса FEE$` — отчёт о продаже
• `/stats` — статистика по участникам
• `/who порог N` — топ-3 кандидатов на порог N

**Примеры:**
/drop Серёга 240AP вернут 3,5 последний 5-7
/newdrop завтра 14:00 порог 200 CORL
/sold Серёга CORL 125шт по 0.80$ комса 2.5$
/who порог 210

🔗 Рабочие часы: 11:00–23:00 (Kyiv)
"""

    def cmd_drop(self, text: str) -> str:
        parsed = parse_drop_command(text)
        if not parsed:
            return "❌ Неверный формат. Используй:\n/drop <Имя> <AP> вернут X,Y последний A-B"

        name = parsed["name"]
        ap = parsed["ap"]
        refund = parsed["refund_days"]
        gap = parsed["last_gap"]

        self.db.update_member(name, ap, refund, gap)
        
        refund_str = ", ".join(map(str, refund))
        gap_str = f"{gap[0]}-{gap[1]}"
        return f"✅ {name}: {ap}AP | вернут [{refund_str}] | последний [{gap_str}]"

    def cmd_newdrop(self, text: str) -> str:
        parsed = parse_newdrop_command(text)
        if not parsed:
            return "❌ Неверный формат. Используй:\n/newdrop завтра 14:00 порог 200 [CORL]"

        event_id = self.db.create_drop_event(
            parsed["when_ts"],
            parsed["ap_threshold"],
            parsed["symbol"]
        )
        self.last_drop_event = event_id

        # Получаем топ-3
        top3 = get_top_candidates(self.db, parsed["ap_threshold"], limit=3)

        dt_str = parsed["datetime"].strftime("%d.%m %H:%M")
        result = f"💧 **Дроп {dt_str}** | Порог {parsed['ap_threshold']}AP"
        if parsed["symbol"]:
            result += f" | {parsed['symbol']}"
        result += "\n"

        result += f"✅ Лучше подходят:\n"
        for i, cand in enumerate(top3, 1):
            refund = cand["refund_days"]
            gap = cand["last_gap_days"]
            refund_str = ", ".join(map(str, refund)) if refund else "—"
            gap_str = f"{gap[0]}-{gap[1]}" if gap else "—"
            result += f"  {i}. **{cand['name']}** (score {cand['score']}) | {cand['ap']}AP | вернут [{refund_str}] | последний [{gap_str}]\n"

        result += f"\n⏰ Напоминания: -4ч, -3ч, -2ч, -1ч\n"
        result += f"(Дроп ID: {event_id})"
        return result

    def cmd_sold(self, text: str) -> str:
        parsed = parse_sold_command(text)
        if not parsed:
            return "❌ Неверный формат. Используй:\n/sold <Имя> <ASSET> QTYшт по PRICE$ комса FEE$"

        member_name = parsed["member_name"]
        asset = parsed["asset"]
        qty = parsed["qty"]
        sell_price = parsed["sell_price"]
        fees = parsed["fees_usd"]

        gross = qty * sell_price
        net = gross - fees

        share_model = COMMISSIONED_MEMBERS.get(member_name, 0.0)
        if share_model > 0:
            member_payout = net * (1 - share_model)
            team_commission = net * share_model
            result = f"✅ **{member_name}** продал {qty} {asset} по ${sell_price}\n"
            result += f"Gross: ${gross:.2f} | Net: ${net:.2f}\n"
            result += f"🧑 Участнику: ${member_payout:.2f}\n"
            result += f"👥 Команде (20%): ${team_commission:.2f}"
        else:
            result = f"✅ **{member_name}** продал {qty} {asset} по ${sell_price}\n"
            result += f"Gross: ${gross:.2f} | Net: ${net:.2f}\n"
            result += f"💰 Участнику: ${net:.2f} (без комиссии)"

        # Сохраняем в БД
        event_id = self.last_drop_event or 0
        self.db.create_trade_report(event_id, member_name, asset, qty, sell_price, fees)

        return result

    def cmd_who(self, text: str) -> str:
        """Быстрый топ-3 без создания дропа"""
        pattern = r"/who\s+порог\s+(\d+)"
        match = re.search(pattern, text)
        if not match:
            return "❌ Используй: /who порог N"

        threshold = int(match.group(1))
        top3 = get_top_candidates(self.db, threshold, limit=3)

        result = f"🎯 **Топ-3 на порог {threshold}AP**\n"
        for i, cand in enumerate(top3, 1):
            refund_str = ", ".join(map(str, cand["refund_days"])) if cand["refund_days"] else "—"
            gap_str = f"{cand['last_gap_days'][0]}-{cand['last_gap_days'][1]}" if cand["last_gap_days"] else "—"
            result += f"{i}. **{cand['name']}** ({cand['score']}) | {cand['ap']}AP | [{refund_str}] | [{gap_str}]\n"

        return result


# ============================================================================
# TELEGRAM BOT
# ============================================================================

import os
from typing import Optional

# 👇 СЮДА ВСТАВЬ ТОКЕН! Или задай переменную окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Если хочешь использовать Telegram, раскомментируй:
# pip install pyTelegramBotAPI
# import telebot
# bot_tg = telebot.TeleBot(BOT_TOKEN)

class TelegramBotAdapter:
    """Адаптер для pyTelegramBotAPI"""
    
    def __init__(self, token: str):
        try:
            import telebot
            self.bot = telebot.TeleBot(token)
            self.tracker = AlphaTrackerBot()
            self.is_connected = True
            print(f"✅ Telegram бот подключен!")
        except ImportError:
            print("❌ Библиотека pyTelegramBotAPI не установлена.")
            print("   Установи: pip install pyTelegramBotAPI")
            self.is_connected = False
        except Exception as e:
            print(f"❌ Ошибка подключения: {e}")
            self.is_connected = False

    def setup_handlers(self):
        """Регистрирует обработчики сообщений"""
        if not self.is_connected:
            return

        @self.bot.message_handler(func=lambda message: True)
        def handle_message(message):
            text = message.text.strip()
            response = self.tracker.handle_command(text)
            self.bot.reply_to(message, response, parse_mode="Markdown")

    def start_polling(self):
        """Запускает polling"""
        if not self.is_connected:
            print("❌ Telegram адаптер не инициализирован.")
            return

        print("🚀 Telegram бот начал слушать сообщения...")
        try:
            self.bot.infinity_polling()
        except KeyboardInterrupt:
            print("\n👋 Бот остановлен")


# ============================================================================
# ГЛАВНОЕ МЕНЮ (CLI + Telegram)
# ============================================================================

def main():
    print("🤖 AlphaTrackerBot v1.0\n")
    
    # Проверяем токен
    if BOT_TOKEN == "7813840039:AAFquVUm1z_IXM60VJwWqftocUCFYGhHRYI":
        print("⚠️  Токен Telegram не установлен!")
        print("   Выбери режим:\n")
        print("   1) CLI режим (консоль)")
        print("   2) Telegram режим (нужен токен)\n")
        
        choice = input("Выбор (1 или 2): ").strip()
        
        if choice == "2":
            print("\n📌 Для Telegram режима:\n")
            print("   a) Получи токен у @BotFather в Telegram")
            print("   b) Задай переменную окружения:")
            print("      export BOT_TOKEN='твой_токен_здесь'")
            print("   c) Или отредактируй строку 362 в коде:")
            print("      BOT_TOKEN = 'твой_токен_здесь'\n")
            return
        
        choice = "1"
    else:
        choice = input("CLI (1) или Telegram (2)? [1]: ").strip() or "1"

    if choice == "2":
        # Telegram режим
        tg_bot = TelegramBotAdapter(BOT_TOKEN)
        if tg_bot.is_connected:
            tg_bot.setup_handlers()
            tg_bot.start_polling()
    else:
        # CLI режим
        bot = AlphaTrackerBot()
        print("🤖 AlphaTrackerBot запущен (CLI режим)")
        print("Введи команду или /start для справки. /exit для выхода.\n")

        while True:
            try:
                user_input = input(">>> ").strip()
                if user_input.lower() == "/exit":
                    print("👋 До свидания!")
                    break
                if not user_input:
                    continue

                response = bot.handle_command(user_input)
                print(f"\n{response}\n")
            except KeyboardInterrupt:
                print("\n👋 Выход...")
                break
            except Exception as e:
                print(f"❌ Ошибка: {e}\n")


if __name__ == "__main__":
    main()
