# -*- coding: utf-8 -*-
# Alpha Drop Bot v2.5 — Railway
# Новое в v2.5:
# (4) Автоподтверждение за 1 час до старта, если нет броней → авто-назначаем топ-1 (с кнопкой отмены)
# (6) Самообучение: динамический trust_score (+/-) на основе фактических исходов и прогнозов
# (7) Кастомные напоминания: /newdrop ... remind=6,3,1 (часы до старта). По умолчанию: 10:00, -4/-3/-2/-1, старт
#
# Основа (из v2.4): модель Alpha (скользящее окно 15 дней), прогноз к дате, мультибронь (топ-3),
# «забрал/не получилось», пост-резюме+архив, статистика, бэкап, меню, /forecast, /mystatus и т.д.

import os, re, json, sqlite3, time, io
from datetime import datetime, timedelta, date
from dateutil import parser as duparser
import pytz

import telebot
from telebot.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, InputFile
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

# ---------- ENV ----------
DB_PATH = os.getenv("DB_PATH", "alpha_bot.db")
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is required")

DEFAULT_TZ = os.getenv("TZ_KYIV", "Europe/Kyiv")
KYIV_TZ = pytz.timezone(DEFAULT_TZ)

GLOBAL_CHAT_ID = os.getenv("CHAT_ID")
GLOBAL_THREAD_ID = os.getenv("THREAD_ID")
if GLOBAL_THREAD_ID:
    try: GLOBAL_THREAD_ID = int(GLOBAL_THREAD_ID)
    except: GLOBAL_THREAD_ID = None

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
scheduler = BackgroundScheduler(timezone=KYIV_TZ); scheduler.start()

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS config(
        key TEXT PRIMARY KEY, value TEXT
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE,
        username TEXT, first_name TEXT, last_name TEXT,
        rate_active INTEGER DEFAULT 17,        -- 17/18 ap/day
        daily_window TEXT,                     -- JSON: ≤15 вкладов по дням
        points INTEGER DEFAULT 0,              -- кэш суммы окна
        last_window_date TEXT,                 -- дата последнего слота
        last_pickup TEXT,                      -- последний «забрал»
        last_update TEXT,                      -- последняя ручная активность
        trust_score INTEGER DEFAULT 80,        -- 0..100
        taken_count INTEGER DEFAULT 0,
        fail_count INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS drops(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_kyiv TEXT NOT NULL,
        points_required INTEGER NOT NULL,
        chat_id TEXT, thread_id INTEGER, created_by INTEGER,
        status TEXT DEFAULT 'scheduled',       -- scheduled/finished/cancelled
        note TEXT,
        reserved_by TEXT,                      -- JSON [tg_id,...]
        picked_by TEXT,                        -- JSON [tg_id,...]
        failed_by TEXT,                        -- JSON [tg_id,...]
        max_reserves INTEGER DEFAULT 3,
        summary_posted INTEGER DEFAULT 0,
        remind_plan TEXT,                      -- JSON [6,3,1] (часы до старта), None → дефолт
        predicted_at_create TEXT,              -- JSON {tg_id:pred} снимок прогнозов на момент создания
        predicted_at_minus1h TEXT              -- JSON {tg_id:pred} снимок прогнозов за 1 час (для самообучения)
    );""")
    conn.commit(); conn.close()
init_db()

# ---------- HELPERS ----------
def ns(s): return re.sub(r"\s+", " ", s or "").strip()
def jloads(s, default):
    try: return json.loads(s) if s else default
    except: return default
def jdump(obj): return json.dumps(obj, ensure_ascii=False)
def now_kyiv(): return datetime.now(KYIV_TZ)
def human_dt(dt: datetime): return dt.strftime("%d.%m.%Y %H:%M")
def human_date(d: date): return d.strftime("%d.%m.%Y")

def set_config(key, value):
    conn = db()
    conn.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
    conn.commit(); conn.close()

def get_config(key):
    conn=db(); row=conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone(); conn.close()
    return row["value"] if row else None

def get_config_int(key):
    v = get_config(key)
    try: return int(v) if v is not None else None
    except: return None

def ensure_chat_thread(m: Message):
    chat_id = GLOBAL_CHAT_ID or str(m.chat.id)
    thread_id = GLOBAL_THREAD_ID
    if getattr(m, "message_thread_id", None): thread_id = m.message_thread_id
    c = get_config("CHAT_ID"); t = get_config("THREAD_ID")
    if c: chat_id = c
    if t:
        try: thread_id = int(t)
        except: thread_id = None
    return chat_id, thread_id

def send(chat_id, text, thread_id=None, reply_markup=None):
    try:
        return bot.send_message(chat_id, text,
            message_thread_id=thread_id if thread_id else None,
            disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as e:
        print("Send error:", e); return None

def parse_points(txt: str):
    m = re.search(r"(\d{1,5})\s*(ap|ап|points?)?\b", txt, re.IGNORECASE)
    return int(m.group(1)) if m else None

def parse_rate(txt: str):
    m = re.search(r"(rate|рейт|скорость)\s*(\d{1,3})\b", txt, re.IGNORECASE)
    if m:
        v = int(m.group(2)); return max(0, min(50, v))
    return None

def parse_date_str(txt: str):
    for pat in [r"\b(\d{4}-\d{2}-\d{2})\b", r"\b(\d{2}\.\d{2}\.\d{4})\b", r"\b(\d{2}/\d{2}/\d{4})\b"]:
        m = re.search(pat, txt)
        if m:
            raw = m.group(1)
            try:
                if "-" in raw: return datetime.strptime(raw, "%Y-%m-%d").date()
                if "." in raw: return datetime.strptime(raw, "%d.%m.%Y").date()
                return datetime.strptime(raw, "%d/%m/%Y").date()
            except: pass
    if re.search(r"сегодня", txt, re.IGNORECASE): return now_kyiv().date()
    if re.search(r"вчера", txt, re.IGNORECASE): return (now_kyiv() - timedelta(days=1)).date()
    m = re.search(r"(\d{1,2})\s*(дн|дня|дней)\s*назад", txt, re.IGNORECASE)
    if m: return (now_kyiv() - timedelta(days=int(m.group(1)))).date()
    return None

def parse_drop_datetime(text: str):
    t = ns(text)
    pts = None
    # кастомные remind=6,3,1 вырежем заранее, сохраним строкой
    remind_plan = None
    r = re.search(r"remind=([0-9,\s]+)", t, re.IGNORECASE)
    if r:
        remind_plan = [int(x) for x in re.findall(r"\d+", r.group(1))]
        t = (t[:r.start()] + t[r.end():]).strip()

    pts_m = re.search(r"(\d{2,5})\s*(ap|ап|балл|баллов|points?)?\s*$", t, re.IGNORECASE)
    if pts_m:
        try: pts = int(pts_m.group(1)); t = t[:pts_m.start()]
        except: pass
    base = now_kyiv()
    try:
        dt = duparser.parse(t, dayfirst=True, default=base.replace(hour=0, minute=0, second=0, microsecond=0))
        dt = dt if dt.tzinfo else KYIV_TZ.localize(dt)
        if dt < base and not re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}", t):
            dt = dt + timedelta(days=1)
    except: return None, None, None
    return dt, pts, remind_plan

# ---------- WINDOW / POINTS ----------
MAX_WIN = 15

def _sum_window(win): 
    return sum([int(x) for x in (win or [])])

def _load_user(tg_id):
    conn=db()
    u=conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    conn.close()
    return u

def _save_user_window_and_points(tg_id, win, today=None):
    win = (win or [])[-MAX_WIN:]
    pts = _sum_window(win)
    conn=db()
    conn.execute("UPDATE users SET daily_window=?, points=?, last_window_date=?, updated_at=CURRENT_TIMESTAMP WHERE tg_id=?",
                 (jdump(win), pts, (today or now_kyiv().date().isoformat()), tg_id))
    conn.commit(); conn.close()
    return pts

def _ensure_today_slot(urow):
    today = now_kyiv().date()
    last_date = urow["last_window_date"]
    win = jloads(urow["daily_window"], [])
    rate = urow["rate_active"] or 17

    if not win and (urow["points"] or 0) > 0:
        approx = [rate]*MAX_WIN
        need = urow["points"] - _sum_window(approx)
        approx[-1] = max(0, approx[-1] + need)
        win = approx[-MAX_WIN:]

    if not last_date:
        if not win: win=[0]
        _save_user_window_and_points(urow["tg_id"], win, today.isoformat())
        urow = _load_user(urow["tg_id"])
        win = jloads(urow["daily_window"], [])
        last_date = urow["last_window_date"]

    if not last_date: last_date = today.isoformat()
    last = datetime.strptime(last_date, "%Y-%m-%d").date()
    if last == today: return win

    delta_days = (today - last).days
    if delta_days <= 0: return win

    for _ in range(delta_days):
        if len(win) >= MAX_WIN: win.pop(0)
        win.append(rate)  # считаем, что фармили rate в пропущенные дни
    _save_user_window_and_points(urow["tg_id"], win, today.isoformat())
    return win

def _apply_today_take(win):
    if not win: return [0]
    win = win[:]
    win[-1] = 0
    return win

def _model_future_points(win, rate, days_ahead):
    sim = list((win or [])[-MAX_WIN:])
    for _ in range(days_ahead):
        if len(sim) >= MAX_WIN: sim.pop(0)
        sim.append(rate)
    return _sum_window(sim)

# ---------- USERS ----------
def upsert_user_profile(user, text: str):
    points = parse_points(text)
    rate = parse_rate(text)
    last_pick = parse_date_str(text)

    conn = db()
    row = conn.execute("SELECT * FROM users WHERE tg_id=?", (user.id,)).fetchone()
    username = user.username or ""; fn = user.first_name or ""; ln = user.last_name or ""
    today = now_kyiv().date().isoformat()

    if row:
        new_rate = rate if rate is not None else (row["rate_active"] or 17)
        new_last_pick = (last_pick.isoformat() if last_pick else row["last_pickup"])
        win = jloads(row["daily_window"], [])
        if points is not None:
            approx = [new_rate]*MAX_WIN
            need = points - _sum_window(approx)
            approx[-1] = max(0, approx[-1] + need)
            win = approx[-MAX_WIN:]
            pts = _sum_window(win)
            conn.execute("""UPDATE users SET username=?, first_name=?, last_name=?,
                            rate_active=?, daily_window=?, points=?, last_window_date=?,
                            last_pickup=?, last_update=?, updated_at=CURRENT_TIMESTAMP
                            WHERE tg_id=?""",
                         (username, fn, ln, new_rate, jdump(win), pts, today,
                          new_last_pick, today, user.id))
        else:
            conn.execute("""UPDATE users SET username=?, first_name=?, last_name=?,
                            rate_active=?, last_pickup=?, last_update=?, updated_at=CURRENT_TIMESTAMP
                            WHERE tg_id=?""",
                         (username, fn, ln, new_rate, new_last_pick, today, user.id))
    else:
        init_rate = rate if rate is not None else 17
        win=[]; pts=0
        if points is not None:
            approx=[init_rate]*MAX_WIN
            need = points - _sum_window(approx)
            approx[-1] = max(0, approx[-1] + need)
            win = approx[-MAX_WIN:]; pts = _sum_window(win)
        conn.execute("""INSERT INTO users(tg_id, username, first_name, last_name,
                        rate_active, daily_window, points, last_window_date, last_pickup, last_update)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     (user.id, username, fn, ln, init_rate, jdump(win), pts, today,
                      last_pick.isoformat() if last_pick else None, today))
    conn.commit(); conn.close()

def format_user_tag(urow):
    if not urow: return "—"
    return f"@{urow['username']}" if urow["username"] else f"id:{urow['tg_id']}"

# ---------- Прогноз / скоринг / выбор ----------
def predicted_points_to_date(urow, drop_dt: datetime):
    rate = urow["rate_active"] or 17
    win = _ensure_today_slot(urow)
    days = max(0, (drop_dt.date() - now_kyiv().date()).days)
    return _model_future_points(win, rate, days)

def choose_best(points_required: int, drop_dt: datetime, top_k=4):
    conn=db()
    users=conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    scored=[]
    for u in users:
        pred = predicted_points_to_date(u, drop_dt)
        eligible = pred >= points_required
        score = (1000 if eligible else 0) + max(0, pred - points_required)
        if u["last_pickup"]:
            try: days_since=(now_kyiv().date()-datetime.strptime(u["last_pickup"],"%Y-%m-%d").date()).days
            except: days_since=0
            score += days_since*5
        else:
            score += 30
        score += int((u["trust_score"] or 80)/5)
        scored.append((u, eligible, pred, score))
    scored.sort(key=lambda x: (x[1], x[3]), reverse=True)
    return scored[:top_k]

# ---------- Самообучение ----------
# Простая система поправок trust_score:
# +3 за реальный «забрал»; +2 если прогноз <= порога, но всё равно забрал (супергерой)
# -4 за «не получилось»; -2 если был в броне, но не забрал; -1 если прогнозировал «легко проходит», но не забрал
def adjust_trust(tg_id: int, delta: int, reason: str):
    conn=db()
    u=conn.execute("SELECT trust_score FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if not u: conn.close(); return
    new = max(0, min(100, int((u["trust_score"] or 80) + delta)))
    conn.execute("UPDATE users SET trust_score=?, updated_at=CURRENT_TIMESTAMP WHERE tg_id=?", (new, tg_id))
    conn.commit(); conn.close()
    # лог можно печатать в консоль
    print(f"[TRUST] {tg_id} {('+' if delta>=0 else '')}{delta} — {reason}")

# ---------- Напоминания / авто-подтверждение ----------
def schedule_reminders(drop_id: int, when_dt: datetime, chat_id: str, thread_id: int, pts_required: int, remind_plan=None):
    def add_job(dt, label, fn= None, args=None, job_id=None):
        scheduler.add_job(
            fn if fn else notify_drop,
            trigger=DateTrigger(run_date=dt),
            args=args if args is not None else [drop_id, chat_id, thread_id, pts_required, label],
            id=job_id or f"drop_{drop_id}_{label}_{int(dt.timestamp())}",
            replace_existing=True
        )
    now = now_kyiv()

    # кастомные «за X часов» (числа часов)
    used_hours = set()
    if remind_plan:
        for h in sorted(set(int(x) for x in remind_plan if int(x)>=1), reverse=True):
            t = when_dt - timedelta(hours=h)
            if t > now:
                add_job(t, f"minus{h}h")
                used_hours.add(h)

    # дефолтные, если не перекрыты кастомом
    default_hours = [4,3,2,1]
    for h in default_hours:
        if h in used_hours: continue
        t = when_dt - timedelta(hours=h)
        if t > now:
            add_job(t, f"minus{h}h")

    # 10:00 в день дропа
    ten = when_dt.replace(hour=10, minute=0, second=0, microsecond=0)
    if ten > now and ten.date()==when_dt.date():
        add_job(ten, "ten_am")

    # авто-подтверждение за 1 час (если нет броней) — отдельный job
    minus1 = when_dt - timedelta(hours=1)
    if minus1 > now:
        add_job(minus1, "autoconfirm", fn=auto_confirm_if_empty, 
                args=[drop_id], job_id=f"drop_{drop_id}_autoconfirm")

    # старт
    if when_dt > now:
        add_job(when_dt, "start")

    # пост-резюме через 4 часа
    post_sum = when_dt + timedelta(hours=4)
    if post_sum > now:
        add_job(post_sum, "summary", fn=post_drop_summary, args=[drop_id, chat_id, thread_id], job_id=f"drop_{drop_id}_summary")

def build_reserve_markup(drop_id: int):
    mk=InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("Забираю ✅", callback_data=f"reserve:{drop_id}"))
    mk.add(InlineKeyboardButton("Отменить ❌", callback_data=f"cancel:{drop_id}"))
    return mk

def snapshot_predictions(drop_id: int, lbl_field: str):
    """Сохраняем срез прогнозов {tg_id: pred} для самообучения (на создании дропа и за 1 час)."""
    conn=db()
    d=conn.execute("SELECT * FROM drops WHERE id=?", (drop_id,)).fetchone()
    if not d: conn.close(); return
    when_dt = datetime.fromisoformat(d["ts_kyiv"]); when_dt = when_dt if when_dt.tzinfo else KYIV_TZ.localize(when_dt)
    pts_req = d["points_required"]
    # по всем юзерам снимем прогноз
    users = conn.execute("SELECT * FROM users").fetchall()
    pred_map = {}
    for u in users:
        # аккуратно — используем ту же логику, что в choose_best
        rate = u["rate_active"] or 17
        # быстренько смоделируем дни
        days = max(0, (when_dt.date() - now_kyiv().date()).days)
        win = jloads(u["daily_window"], [])
        if not win and (u["points"] or 0) > 0:
            approx=[rate]*MAX_WIN
            need = u["points"] - _sum_window(approx)
            approx[-1] = max(0, approx[-1] + need)
            win=approx[-MAX_WIN:]
        pred = _model_future_points(win, rate, days)
        pred_map[str(u["tg_id"])] = pred
    conn.execute(f"UPDATE drops SET {lbl_field}=? WHERE id=?", (jdump(pred_map), drop_id))
    conn.commit(); conn.close()

def notify_drop(drop_id: int, chat_id: str, thread_id: int, pts_required: int, label: str):
    conn=db()
    d=conn.execute("SELECT * FROM drops WHERE id=?", (drop_id,)).fetchone()
    conn.close()
    if not d or d["status"]!="scheduled": return
    when_dt = datetime.fromisoformat(d["ts_kyiv"]); when_dt = when_dt if when_dt.tzinfo else KYIV_TZ.localize(when_dt)

    # снимок прогнозов на -1 час — пригодится самообучению (для неточностей)
    if label == "minus1h":
        snapshot_predictions(drop_id, "predicted_at_minus1h")

    picks = choose_best(pts_required, when_dt, top_k=4)
    lines=[]
    header = "🔔 Напоминание по дропу" if label!="start" else "🚀 ДРОП СЕЙЧАС!"
    lines.append(f"<b>{header}</b>\n🕒 {human_dt(when_dt)} (Киев)")
    if label!="start":
        diff = when_dt - now_kyiv()
        h = int(diff.total_seconds()//3600); m = int((diff.total_seconds()%3600)//60)
        left = "сейчас" if diff.total_seconds()<=0 else (f"через {h} ч {m} мин" if h>=1 else f"через {m} мин")
        lines.append(f"До старта: <i>{left}</i>")

    recommended=[]
    for i,(u, elig, pred, sc) in enumerate(picks, start=1):
        nm = format_user_tag(u)
        recommended.append((nm, pred, elig))
        if i==3: break
    if recommended:
        lines.append("\n💡 <b>Рекомендуемые (прогноз к дате):</b>")
        for i,(nm, pred, elig) in enumerate(recommended, start=1):
            mark = "✅" if elig else "⚠️"
            lines.append(f"{i}️⃣ {nm} — ~{pred} ap {mark}")

    rlist = jloads(d["reserved_by"], [])
    if rlist:
        show=[]
        for uid in rlist:
            u=_load_user(uid); show.append(format_user_tag(u))
        lines.append(f"\n✅ Уже бронировали: {', '.join(show)}")

    lines.append(f"\n📏 Требуется: <b>{pts_required} ap</b>")
    send(chat_id, "\n".join(lines), thread_id=thread_id, reply_markup=build_reserve_markup(drop_id))

def auto_confirm_if_empty(drop_id: int):
    """(4) За 1 час до старта: если нет брони — назначаем топ-1 автоматически."""
    conn=db()
    d=conn.execute("SELECT * FROM drops WHERE id=?", (drop_id,)).fetchone()
    if not d or d["status"]!="scheduled":
        conn.close(); return
    when_dt = datetime.fromisoformat(d["ts_kyiv"]); when_dt = when_dt if when_dt.tzinfo else KYIV_TZ.localize(when_dt)
    pts_req = d["points_required"]
    reserved = jloads(d["reserved_by"], [])

    # сохраним снимок прогнозов на -1ч (если вдруг не было)
    if not d["predicted_at_minus1h"]:
        snapshot_predictions(drop_id, "predicted_at_minus1h")

    if reserved:
        conn.close(); return  # уже есть бронь

    picks = choose_best(pts_req, when_dt, top_k=1)
    if not picks:
        conn.close(); return
    top_user = picks[0][0]
    reserved = [top_user["tg_id"]]
    conn.execute("UPDATE drops SET reserved_by=? WHERE id=?", (jdump(reserved), drop_id))
    conn.commit(); conn.close()

    # оповещение
    chat_id, thread_id = d["chat_id"], d["thread_id"]
    send(chat_id,
         f"⚡ Автоподтверждение: {format_user_tag(top_user)} назначен на дроп ID {drop_id} (за час до старта не было броней).\n"
         f"Если не согласен — нажми «Отменить ❌».",
         thread_id)

# ---------- DAILY CRON 00:05 ----------
def daily_tick():
    try:
        today = now_kyiv().date().isoformat()
        conn=db()
        users=conn.execute("SELECT * FROM users").fetchall()
        for u in users:
            rate = u["rate_active"] or 17
            win = jloads(u["daily_window"], [])
            if u["last_window_date"] == today:
                continue
            if len(win) >= MAX_WIN: win.pop(0)
            win.append(rate)
            pts = _sum_window(win)
            conn.execute("""UPDATE users SET daily_window=?, points=?, last_window_date=?, 
                            last_update=?, updated_at=CURRENT_TIMESTAMP WHERE tg_id=?""",
                         (jdump(win), pts, today, today, u["tg_id"]))
        conn.commit()

        # Ежедневная короткая сводка (можно отключить — закомментируй send)
        chat_id = get_config("CHAT_ID") or GLOBAL_CHAT_ID
        thread_id = get_config_int("THREAD_ID") or GLOBAL_THREAD_ID
        if chat_id:
            rows = conn.execute("SELECT * FROM users ORDER BY points DESC").fetchall()
            lines = ["📊 Сумма по окну 15дн (ежедневный апдейт):"]
            for u in rows:
                lines.append(f"{format_user_tag(u)} — {u['points']} ap (rate {u['rate_active']}/д)")
            send(chat_id, "\n".join(lines), thread_id)

        # Пинг неактивных (3+ дней не обновляли профиль вручную)
        stale = conn.execute("""
            SELECT * FROM users 
            WHERE last_update IS NULL OR julianday(date('now')) - julianday(last_update) >= 3
        """).fetchall()
        if stale and chat_id:
            names = ", ".join([format_user_tag(u) for u in stale[:10]])
            send(chat_id, f"🔔 Напоминание: {names}\nОбновите профиль: /drop 250ap rate 17", thread_id)

        conn.close()
    except Exception as e:
        print("daily_tick error:", e)

scheduler.add_job(daily_tick, trigger=CronTrigger(hour=0, minute=5))

# ---------- Итоги/архив + самообучение ----------
def post_drop_summary(drop_id:int, chat_id:str, thread_id:int):
    conn=db()
    d=conn.execute("SELECT * FROM drops WHERE id=?", (drop_id,)).fetchone()
    if not d or d["summary_posted"]:
        conn.close(); return

    if d["status"]=="scheduled":
        conn.execute("UPDATE drops SET status='finished' WHERE id=?", (drop_id,))
        conn.commit()

    picked = jloads(d["picked_by"], [])
    failed = jloads(d["failed_by"], [])
    reserved = jloads(d["reserved_by"], [])
    pts_req = d["points_required"]

    # Самообучение: используем снимки прогнозов
    pred_create = jloads(d["predicted_at_create"], {})
    pred_minus1 = jloads(d["predicted_at_minus1h"], {})

    # Реакции на исходы
    for uid in picked:
        adjust_trust(uid, +3, "picked_success")
        conn.execute("""UPDATE users SET taken_count=taken_count+1, last_pickup=?, 
                        updated_at=CURRENT_TIMESTAMP WHERE tg_id=?""",
                     (now_kyiv().date().isoformat(), uid))
        # В день «забрал» вклад сегодняшнего дня = 0
        u = _load_user(uid)
        win = _ensure_today_slot(u)
        win = _apply_today_take(win)
        _save_user_window_and_points(uid, win)

    for uid in failed:
        adjust_trust(uid, -4, "failed_drop")
        conn.execute("UPDATE users SET fail_count=fail_count+1, updated_at=CURRENT_TIMESTAMP WHERE tg_id=?", (uid,))

    # Если был в броне, но не забрал → лёгкий минус
    for uid in reserved:
        if uid not in picked:
            adjust_trust(uid, -2, "reserved_but_not_picked")

    # Наказание/бонус за неточный прогноз (-1ч срез)
    # если -1ч прогноз говорил «≥порог», но в picked юзера нет → -1
    # если -1ч прогноз говорил «<порог», но он оказался в picked → +2
    for k, pred in pred_minus1.items():
        try: uid = int(k)
        except: continue
        if pred >= pts_req and uid not in picked:
            adjust_trust(uid, -1, "predicted_ok_but_not_picked")
        if pred < pts_req and uid in picked:
            adjust_trust(uid, +2, "predicted_fail_but_picked")

    conn.commit()

    def tags(lst):
        return "—" if not lst else ", ".join([format_user_tag(_load_user(uid)) for uid in lst])

    when_dt = datetime.fromisoformat(d["ts_kyiv"]); when_dt = when_dt if when_dt.tzinfo else KYIV_TZ.localize(when_dt)
    send(chat_id,
         f"📦 Дроп {human_dt(when_dt)} завершён.\n"
         f"Забрали: {tags(picked)}\n"
         f"Не смогли: {tags(failed)}\n"
         f"Архив готов ✅",
         thread_id)
    conn.execute("UPDATE drops SET summary_posted=1 WHERE id=?", (drop_id,))
    conn.commit(); conn.close()

# ---------- Команды / Меню ----------
def set_main_menu():
    try:
        bot.set_my_commands([
            BotCommand("newdrop", "Создать дроп"),
            BotCommand("who", "Анализ кандидатов"),
            BotCommand("listdrops", "Список дропов"),
            BotCommand("drop", "Обновить профиль"),
            BotCommand("rate", "Скорость (ap/д)"),
            BotCommand("forecast", "Прогноз к дате"),
            BotCommand("mystatus", "Мой статус"),
            BotCommand("stats", "Статистика"),
            BotCommand("backup", "Бэкап JSON"),
            BotCommand("setslots", "Слоты брони"),
            BotCommand("setchat", "Запомнить чат/ветку"),
            BotCommand("menu", "Кнопки")
        ])
    except Exception as e:
        print("set_my_commands error:", e)
set_main_menu()

def reply_menu():
    kb=ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("/newdrop 14:00 20.10.2025 200 remind=6,3,1"), KeyboardButton("/who"))
    kb.row(KeyboardButton("/listdrops"), KeyboardButton("/mystatus"))
    kb.row(KeyboardButton("/forecast 25.10.2025"), KeyboardButton("/stats"))
    return kb

@bot.message_handler(commands=['menu'])
def cmd_menu(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    send(chat_id, "Меню включено 👇", thread_id, reply_markup=reply_menu())

@bot.message_handler(commands=['start','help'])
def cmd_start(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    text = (
        "👋 Alpha Drop Bot v2.5\n\n"
        "<b>Инициализация:</b>\n"
        "• /setchat — запомнить этот чат/ветку\n"
        "• /drop 255ap rate 17 — задать профиль (баллы + скорость)\n\n"
        "<b>Дропы:</b>\n"
        "• /newdrop <время дата баллы> [remind=6,3,1]\n"
        "• /who — анализ (прогноз к дате)\n"
        "• /listdrops — список дропов\n\n"
        "<b>Личное:</b>\n"
        "• /rate 18 — сменить скорость\n"
        "• /mystatus — окно 15дн + текущая сумма\n"
        "• /forecast 25.10.2025 — прогноз баллов\n\n"
        "🧠 Самообучение: trust_score корректируется по итогам, броням и точности прогнозов.\n"
        "⚡ Автоподтверждение: за 1 час без броней бот назначит топ-1.\n"
    )
    send(chat_id, text, thread_id, reply_markup=reply_menu())

@bot.message_handler(commands=['setchat'])
def cmd_setchat(m: Message):
    chat_id=str(m.chat.id); thread_id=getattr(m, "message_thread_id", None)
    set_config("CHAT_ID", chat_id)
    if thread_id is not None: set_config("THREAD_ID", str(thread_id))
    send(chat_id, f"✅ Чат сохранён: <code>{chat_id}</code>\nВетка: <code>{thread_id or '-'}</code>", thread_id)

@bot.message_handler(commands=['drop'])
def cmd_drop(m: Message):
    payload = m.text[len("/drop"):].strip()
    if not payload:
        send(m.chat.id, "Пример: <code>/drop 255ap rate 17</code>", getattr(m, "message_thread_id", None)); return
    upsert_user_profile(m.from_user, payload)
    send(m.chat.id, "📝 Профиль обновлён.", getattr(m, "message_thread_id", None))

@bot.message_handler(commands=['rate'])
def cmd_rate(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    r = parse_rate(m.text)
    if r is None:
        send(chat_id, "Использование: <code>/rate 17</code>", thread_id); return
    conn=db()
    conn.execute("UPDATE users SET rate_active=?, updated_at=CURRENT_TIMESTAMP WHERE tg_id=?", (r, m.from_user.id))
    conn.commit(); conn.close()
    send(chat_id, f"✅ Скорость установлена: {r} ap/день.", thread_id)

@bot.message_handler(commands=['forecast'])
def cmd_forecast(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    payload = m.text[len("/forecast"):].strip()
    if not payload:
        send(chat_id, "Использование: <code>/forecast 25.10.2025</code>", thread_id); return
    try:
        dt = duparser.parse(payload, dayfirst=True)
        dt = dt if dt.tzinfo else KYIV_TZ.localize(dt)
    except:
        send(chat_id, "Не понял дату. Формат: 25.10.2025", thread_id); return
    conn=db(); u=conn.execute("SELECT * FROM users WHERE tg_id=?", (m.from_user.id,)).fetchone(); conn.close()
    if not u:
        send(chat_id, "Сначала задай профиль: /drop 255ap rate 17", thread_id); return
    pred = predicted_points_to_date(u, dt)
    send(chat_id, f"🔮 Прогноз к {human_dt(dt)}: ~<b>{pred}</b> ap (rate {u['rate_active']}/д)", thread_id)

@bot.message_handler(commands=['mystatus'])
def cmd_mystatus(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    u=_load_user(m.from_user.id)
    if not u:
        send(chat_id, "Профиль не найден. Отправь: /drop 255ap rate 17", thread_id); return
    win = _ensure_today_slot(u)
    text = (
        f"👤 {format_user_tag(u)}\n"
        f"Скорость: {u['rate_active']} ap/д\n"
        f"Окно (15дн): {win}\n"
        f"Текущие баллы: <b>{_sum_window(win)}</b> ap\n"
        f"Последний забор: {u['last_pickup'] or '—'} | Доверие: {u['trust_score']}%\n"
    )
    send(chat_id, text, thread_id)

@bot.message_handler(commands=['newdrop'])
def cmd_newdrop(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    payload = m.text[len("/newdrop"):].strip()
    if not payload:
        send(chat_id, "Пример: <code>/newdrop 14:00 20.10.2025 200 remind=6,3,1</code>", thread_id); return
    when_dt, pts, rplan = parse_drop_datetime(payload)
    if not when_dt or not pts:
        send(chat_id, "Не понял дату/время или баллы. Пример: <code>/newdrop 14:00 20.10.2025 200 remind=6,3,1</code>", thread_id); return
    max_slots = int(get_config("MAX_SLOTS") or 3)
    conn=db()
    conn.execute("""INSERT INTO drops(ts_kyiv, points_required, chat_id, thread_id, created_by,
                    status, reserved_by, picked_by, failed_by, max_reserves, remind_plan)
                    VALUES(?,?,?,?,?, 'scheduled', ?, ?, ?, ?, ?)""",
                 (when_dt.isoformat(), pts, chat_id, thread_id, m.from_user.id,
                  jdump([]), jdump([]), jdump([]), max_slots, jdump(rplan) if rplan else None))
    drop_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit(); conn.close()

    # снимок прогнозов при создании — для самообучения
    snapshot_predictions(drop_id, "predicted_at_create")

    schedule_reminders(drop_id, when_dt, chat_id, thread_id, pts, remind_plan=rplan)
    send(chat_id, f"✅ Дроп создан: <b>{human_dt(when_dt)}</b> (Киев), требуется <b>{pts} ap</b>\n"
                  f"ID: <code>{drop_id}</code>\n"
                  f"Напоминания: {'кастомные '+str(rplan) if rplan else 'дефолт'}\n"
                  f"Слотов брони: {max_slots}",
         thread_id)

@bot.message_handler(commands=['listdrops'])
def cmd_listdrops(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    now_iso = now_kyiv().isoformat()
    conn=db()
    rows=conn.execute("SELECT * FROM drops WHERE status='scheduled' AND ts_kyiv >= ? ORDER BY ts_kyiv ASC",(now_iso,)).fetchall()
    conn.close()
    if not rows:
        send(chat_id, "Нет запланированных дропов.", thread_id); return
    lines=["📅 Ближайшие дропы:"]
    for r in rows:
        dt = datetime.fromisoformat(r["ts_kyiv"]); dt = dt if dt.tzinfo else KYIV_TZ.localize(dt)
        rp = jloads(r["remind_plan"], None)
        rp_disp = f"remind={','.join(map(str,rp))}" if rp else "def"
        lines.append(f"• ID {r['id']}: {human_dt(dt)} — нужно: {r['points_required']} ap (слотов: {r['max_reserves']}, {rp_disp})")
    send(chat_id, "\n".join(lines), thread_id)

@bot.message_handler(commands=['who'])
def cmd_who(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    now_iso = now_kyiv().isoformat()
    conn=db()
    d=conn.execute("SELECT * FROM drops WHERE status='scheduled' AND ts_kyiv >= ? ORDER BY ts_kyiv ASC LIMIT 1",(now_iso,)).fetchone()
    conn.close()
    if not d:
        send(chat_id, "Нет ближайшего дропа. Создай через /newdrop", thread_id); return
    when_dt = datetime.fromisoformat(d["ts_kyiv"]); when_dt = when_dt if when_dt.tzinfo else KYIV_TZ.localize(when_dt)
    pts_req = d["points_required"]
    picks = choose_best(pts_req, when_dt)
    lines=[f"🔎 Анализ на дроп {human_dt(when_dt)} (Киев)\n📏 Требуется: {pts_req} ap\n"]
    if not picks:
        lines.append("Нет данных по участникам. Пусть отправят /drop …")
        send(chat_id, "\n".join(lines), thread_id); return
    for i,(u, elig, pred, sc) in enumerate(picks, start=1):
        nm = format_user_tag(u)
        lp = u["last_pickup"] or "—"
        mark = "✅" if elig else "⚠️"
        lines.append(f"{mark} <b>#{i}</b> {nm} — прогноз: ~{pred} ap к дате, забирал: {lp} (trust {u['trust_score']}%)")
    lines.append("\n💡 Рекомендация: #1 — основной; #2–#4 — запасные.")
    send(chat_id, "\n".join(lines), thread_id)

@bot.message_handler(commands=['stats'])
def cmd_stats(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    conn=db()
    total_drops = conn.execute("SELECT COUNT(*) c FROM drops").fetchone()["c"]
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    top_taker = conn.execute("SELECT * FROM users ORDER BY taken_count DESC, points DESC LIMIT 1").fetchone()
    avg_rate = conn.execute("SELECT AVG(rate_active) a FROM users").fetchone()["a"]
    conn.close()
    lines = ["📊 Общая статистика:"]
    lines.append(f"• Дропов всего: {total_drops}")
    lines.append(f"• Участников: {total_users}")
    if top_taker: lines.append(f"• Самый активный: {format_user_tag(top_taker)} ({top_taker['taken_count']} дропов)")
    if avg_rate: lines.append(f"• Средняя скорость: {round(avg_rate,1)} ap/д")
    send(chat_id, "\n".join(lines), thread_id)

@bot.message_handler(commands=['backup'])
def cmd_backup(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    conn=db()
    data={"users":[dict(r) for r in conn.execute("SELECT * FROM users")],
          "drops":[dict(r) for r in conn.execute("SELECT * FROM drops")]}
    conn.close()
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.name = f"alpha_backup_{now_kyiv().strftime('%Y%m%d_%H%M')}.json"
    try:
        bot.send_document(chat_id, InputFile(buf), visible_file_name=buf.name,
                          caption="📦 Бэкап данных", message_thread_id=thread_id if thread_id else None)
    except Exception as e:
        print("backup send_document error:", e)
        send(chat_id, "Не удалось отправить файл бэкапа.", thread_id)

@bot.message_handler(commands=['setslots'])
def cmd_setslots(m: Message):
    chat_id, thread_id = ensure_chat_thread(m)
    parts=m.text.strip().split()
    if len(parts)<2 or not parts[1].isdigit():
        send(chat_id, "Использование: <code>/setslots 3</code>", thread_id); return
    n=max(1,min(5,int(parts[1])))
    set_config("MAX_SLOTS", str(n))
    send(chat_id, f"✅ Кол-во слотов брони: {n}", thread_id)

# ---------- CALLBACKS: reserve/cancel ----------
def _allow_top3(drop_row, user_id:int):
    dt = datetime.fromisoformat(drop_row["ts_kyiv"]); dt = dt if dt.tzinfo else KYIV_TZ.localize(dt)
    picks = choose_best(drop_row["points_required"], dt, top_k=3)
    allow_ids = {p[0]["tg_id"] for p in picks}
    return user_id in allow_ids

@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith("reserve:") or c.data.startswith("cancel:")))
def cb_reserve_cancel(call: CallbackQuery):
    try:
        action, drop_id_s = call.data.split(":"); drop_id=int(drop_id_s)
    except: bot.answer_callback_query(call.id, "Ошибка данных."); return
    uid = call.from_user.id
    conn=db(); d=conn.execute("SELECT * FROM drops WHERE id=?", (drop_id,)).fetchone()
    if not d or d["status"]!="scheduled":
        conn.close(); bot.answer_callback_query(call.id, "Дроп недоступен."); return
    reserved=jloads(d["reserved_by"], []); max_res=d["max_reserves"] or 3
    chat_id, thread_id = d["chat_id"], d["thread_id"]

    if action=="reserve":
        if not _allow_top3(d, uid):
            conn.close(); bot.answer_callback_query(call.id, "Сейчас ты не в топ-3."); return
        if uid in reserved:
            conn.close(); bot.answer_callback_query(call.id, "У тебя уже есть бронь."); return
        if len(reserved)>=max_res:
            conn.close(); bot.answer_callback_query(call.id, "Свободных мест нет."); return
        reserved.append(uid)
        conn.execute("UPDATE drops SET reserved_by=? WHERE id=?", (jdump(reserved), drop_id)); conn.commit()
        u=_load_user(uid); conn.close()
        bot.answer_callback_query(call.id, "Бронь принята ✅")
        send(chat_id, f"✅ {format_user_tag(u)} подтвердил участие в дропе ID {drop_id}. Осталось мест: {max_res-len(reserved)}.", thread_id)
    else:
        if uid not in reserved:
            conn.close(); bot.answer_callback_query(call.id, "Брони нет."); return
        reserved=[x for x in reserved if x!=uid]
        conn.execute("UPDATE drops SET reserved_by=? WHERE id=?", (jdump(reserved), drop_id)); conn.commit()
        u=_load_user(uid); conn.close()
        bot.answer_callback_query(call.id, "Бронь отменена ❌")
        send(chat_id, f"❌ {format_user_tag(u)} отменил бронь дропа ID {drop_id}.", thread_id)

# ---------- FREE TEXT: «забрал» / «не получилось» ----------
@bot.message_handler(func=lambda m: bool(m.text) and re.search(r"\b(забрал|забрала|i took|took)\b", m.text.lower()))
def handle_picked(m: Message):
    uid=m.from_user.id; today=now_kyiv().date().isoformat()
    u=_load_user(uid)
    if not u:
        bot.reply_to(m, "Сначала задай профиль: /drop 255ap rate 17"); return
    win = _ensure_today_slot(u)
    win = _apply_today_take(win)         # сегодняшнее значение = 0
    _save_user_window_and_points(uid, win, today)
    conn=db()
    conn.execute("UPDATE users SET last_pickup=?, last_update=?, updated_at=CURRENT_TIMESTAMP, taken_count=taken_count+1 WHERE tg_id=?",
                 (today, today, uid))
    # Привяжем к последнему начавшемуся дропу (≤6ч назад)
    now_iso = now_kyiv().isoformat()
    d=conn.execute("SELECT * FROM drops WHERE status='scheduled' AND ts_kyiv <= ? ORDER BY ts_kyiv DESC LIMIT 1",(now_iso,)).fetchone()
    if d:
        picked=jloads(d["picked_by"], [])
        if uid not in picked:
            picked.append(uid)
            conn.execute("UPDATE drops SET picked_by=? WHERE id=?", (jdump(picked), d["id"]))
    conn.commit(); conn.close()
    # самообучение: зафиксированный успех
    adjust_trust(uid, +3, "picked_success_live")
    bot.reply_to(m, "✅ Принято! Вклад сегодняшнего дня = 0. Окно 15дн обновлено.")

@bot.message_handler(func=lambda m: bool(m.text) and re.search(r"\b(не получилось|не успел|fail|missed)\b", m.text.lower()))
def handle_fail(m: Message):
    uid=m.from_user.id; today=now_kyiv().date().isoformat()
    conn=db()
    conn.execute("UPDATE users SET fail_count=fail_count+1, last_update=?, updated_at=CURRENT_TIMESTAMP WHERE tg_id=?",
                 (today, uid))
    now_iso = now_kyiv().isoformat()
    d=conn.execute("SELECT * FROM drops WHERE status='scheduled' AND ts_kyiv <= ? ORDER BY ts_kyiv DESC LIMIT 1",(now_iso,)).fetchone()
    if d:
        failed=jloads(d["failed_by"], [])
        if uid not in failed:
            failed.append(uid)
            conn.execute("UPDATE drops SET failed_by=? WHERE id=?", (jdump(failed), d["id"]))
    conn.commit(); conn.close()
    adjust_trust(uid, -4, "failed_live")
    bot.reply_to(m, "⚠️ Понял. Отметил «не получилось» для последнего дропа.")

# ---------- BOT LOOP ----------
def main():
    print("Bot started (polling) v2.5 …")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print("Polling error:", e); time.sleep(3)

if __name__ == "__main__":
    main()
