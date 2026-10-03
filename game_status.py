"""
game_status.py — read-only статус NeuroGame для бота (без Qt).

Ничего не пишет и не трогает файлы виджета: только читает DATA_FILE,
LIVE_STATE_FILE и заметки Obsidian, а отображаемое состояние считает так же,
как виджет (get_display_state / update_momentum).
"""
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from ns_config import (
    DATA_FILE, LIVE_STATE_FILE, OBSIDIAN_VAULT, ACHIEVEMENTS,
    MAX_HP, MAX_LEVEL, MAX_MOMENTUM,
    XP_PER_FOCUS_MINUTE, HP_LOSS_PER_FOCUS_MINUTE, HP_RECOVERY_PER_SECOND,
    MOMENTUM_GAIN_PER_SECOND, MOMENTUM_FAST_BREAK, MOMENTUM_MEDIUM_BREAK,
    MOMENTUM_DECAY_FAST, MOMENTUM_DECAY_MEDIUM, MOMENTUM_DECAY_SLOW,
    SLEEP_REWARD, COMEBACK_MIN_BREAK, COMEBACK_REWARD,
)

GAME_NOTE_SUFFIX = "_game"
LEVEL_XP = {lvl: 100 + (lvl - 1) * 50 for lvl in range(1, MAX_LEVEL)}

# Если файл игры не обновлялся дольше этого времени, виджет считаем закрытым
WIDGET_FRESH_SECONDS = 20.0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _is_studying(live, resample):
    """Учёба идёт прямо сейчас (логика как в виджете, но без истории):
    сначала явные флаги, потом — растёт ли elapsed_seconds."""
    if not live or not live.get("active"):
        return False
    for key in ("paused", "is_paused", "pause", "stopped", "is_stopped", "stop"):
        if live.get(key):
            return False
    for key in ("running", "is_running", "timer_running", "studying", "is_studying"):
        if key in live:
            return bool(live[key])
    status = str(live.get("status", "")).strip().lower()
    if status in {"paused", "pause", "stopped", "stop", "idle", "rest"}:
        return False
    if status in {"running", "active", "studying", "focus"}:
        return True
    if "elapsed_seconds" not in live:
        return True
    try:
        first = float(live["elapsed_seconds"])
    except (TypeError, ValueError):
        return True
    time.sleep(2.0)
    second_state = resample()
    try:
        second = float((second_state or {}).get("elapsed_seconds", first))
    except (TypeError, ValueError):
        return True
    return second > first + 0.25


def _level_from(level, xp):
    level = max(1, min(MAX_LEVEL, int(level)))
    xp = max(0.0, float(xp))
    while level < MAX_LEVEL and xp >= LEVEL_XP[level]:
        xp -= LEVEL_XP[level]
        level += 1
    return level, xp


def _momentum_value(value, elapsed, active, break_seconds):
    value, elapsed, brk = float(value), max(0.0, elapsed), max(0.0, float(break_seconds))
    if active:
        return min(MAX_MOMENTUM, value + elapsed * MOMENTUM_GAIN_PER_SECOND)
    left = elapsed
    fast = min(left, max(0.0, MOMENTUM_FAST_BREAK - brk))
    value -= fast * MOMENTUM_DECAY_FAST
    left -= fast
    brk += fast
    medium = min(left, max(0.0, MOMENTUM_MEDIUM_BREAK - brk))
    value -= medium * MOMENTUM_DECAY_MEDIUM
    left -= medium
    if left > 0:
        value -= left * MOMENTUM_DECAY_SLOW
    return max(0.0, min(MAX_MOMENTUM, value))


def momentum_label(value):
    if value < 20:
        return "COLD"
    if value < 50:
        return "WARMING"
    if value < 75:
        return "FOCUSED"
    if value < 90:
        return "FLOW"
    return "OVERDRIVE"


def _today_from_obsidian(day):
    """(секунд фокуса, число сессий, часы сна или None) за день из заметок."""
    vault = Path(OBSIDIAN_VAULT)
    seconds = count = 0
    try:
        text = (vault / f"{day}.md").read_text(encoding="utf-8")
        for m in re.finditer(r"^###\s*Session:\s*\d{2}:\d{2}\s+\((\d+):(\d{2}):(\d{2})\)", text, re.M):
            h, mi, s = map(int, m.groups())
            seconds += h * 3600 + mi * 60 + s
            count += 1
    except OSError:
        pass

    sleep = None
    try:
        text = (vault / f"{day}{GAME_NOTE_SUFFIX}.md").read_text(encoding="utf-8")
        m = re.search(r"^### Sleep:\s*([0-9]+(?:\.[0-9]+)?)h\s*$", text, re.M)
        if m:
            sleep = float(m.group(1))
    except OSError:
        pass
    return seconds, count, sleep


def get_game_status():
    """Снимок игры в том виде, в каком его показывает виджет. Блокирующая
    функция (до ~2 c, если идёт сессия) — вызывать через asyncio.to_thread."""
    now = time.time()
    data = _read_json(DATA_FILE)
    if data is None:
        return None

    def num(key, default):
        try:
            return float(data.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    live = _read_json(LIVE_STATE_FILE)
    present = bool(live and live.get("active"))
    studying = _is_studying(live, lambda: _read_json(LIVE_STATE_FILE)) if present else False

    xp, hp = num("xp", 0.0), num("hp", MAX_HP)
    level = int(num("level", 1))

    # Восстановление HP с момента последнего сохранения (кроме времени учёбы)
    if not studying:
        hp = min(MAX_HP, max(0.0, hp) + max(0.0, now - num("last_hp_update", now)) * HP_RECOVERY_PER_SECOND)

    live_seconds = 0.0
    if present:
        try:
            live_seconds = max(0.0, float(live.get("elapsed_seconds", 0.0)))
        except (TypeError, ValueError):
            live_seconds = 0.0
        minutes = live_seconds / 60.0
        xp += minutes * XP_PER_FOCUS_MINUTE
        hp = max(0.0, hp - minutes * HP_LOSS_PER_FOCUS_MINUTE)
        level, xp = _level_from(level, xp)

    # Моментум: если виджет закрыт, он только затухает (как после его перезапуска)
    last_upd = num("momentum_last_update", now)
    widget_alive = (now - last_upd) <= WIDGET_FRESH_SECONDS
    momentum = _momentum_value(
        num("momentum", 0.0), now - last_upd,
        studying and widget_alive, num("momentum_break_seconds", 0.0),
    )

    t_seconds, t_count, sleep = _today_from_obsidian(date.today())
    if present:
        t_seconds += live_seconds
        t_count += 1

    unlocked = set(data.get("unlocked_achievements") or [])
    return {
        "level": level,
        "xp": xp,
        "xp_need": LEVEL_XP.get(level),  # None на максимальном уровне
        "hp": hp,
        "momentum": momentum,
        "momentum_state": momentum_label(momentum),
        "today_minutes": t_seconds / 60.0,
        "today_sessions": t_count,
        "sleep_hours": sleep,
        "live": "studying" if studying else ("paused" if present else None),
        "live_seconds": live_seconds,
        "achievements": (len([a for a in ACHIEVEMENTS if a["key"] in unlocked]), len(ACHIEVEMENTS)),
    }


def get_achievements():
    """[(name, desc, unlocked)] в порядке, заданном в ACHIEVEMENTS."""
    data = _read_json(DATA_FILE) or {}
    unlocked = set(data.get("unlocked_achievements") or [])
    return [(a["name"], a["desc"], a["key"] in unlocked) for a in ACHIEVEMENTS]


# ============================================================
# НЕДЕЛЬНАЯ СТАТИСТИКА
# ============================================================

SESSION_RE = re.compile(
    r"^###\s*Session:\s*(\d{2}):(\d{2})\s+\((\d+):(\d{2}):(\d{2})\)", re.M
)
SLEEP_RE = re.compile(r"^### Sleep:\s*([0-9]+(?:\.[0-9]+)?)h\s*$", re.M)


def get_week_report(days=7):
    """По дню за последние `days` дней (включая сегодня), от старого к новому:
    {day, minutes, sessions, sleep_hours|None}. Идущая сессия добавляется к сегодня."""
    today = date.today()
    live = _read_json(LIVE_STATE_FILE)
    live_seconds = 0.0
    if live and live.get("active"):
        try:
            live_seconds = max(0.0, float(live.get("elapsed_seconds", 0.0)))
        except (TypeError, ValueError):
            live_seconds = 0.0
        live_present = True
    else:
        live_present = False

    report = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        seconds, count, sleep = _today_from_obsidian(day)
        if day == today and live_present:
            seconds += live_seconds
            count += 1
        report.append({
            "day": day,
            "minutes": seconds / 60.0,
            "sessions": count,
            "sleep_hours": sleep,
        })
    return report


def _sleep_reward(hours):
    if hours < 4:
        return 10, 0
    for limit, key in ((5, 4), (6, 5), (7, 6), (8, 7), (9, 8), (10, 9)):
        if hours < limit:
            return SLEEP_REWARD[key]
    return 90, 20


def _comeback_reward(away):
    for limit, xp in COMEBACK_REWARD:
        if away <= limit:
            return xp
    return 10


def _achievement_condition(key, v):
    sessions, focus = v["sessions"], v["focus_minutes"]
    comebacks, sleep_days = v["comebacks"], v["sleep_days"]
    level, longest = v["level"], v["longest_comeback_seconds"]
    return bool({
        "steady_start": sessions >= 3,
        "first_depth": focus >= 90,
        "returner": comebacks >= 3,
        "two_hours": focus >= 120,
        "ten_sessions": sessions >= 10,
        "late_bloom": longest > 60 * 60,
        "three_hours": focus >= 180,
        "deep_week": focus >= 300,
        "fifteen_sessions": sessions >= 15,
        "ten_comebacks": comebacks >= 10,
        "level_5": level >= 5,
        "ten_hours": focus >= 600,
        "twenty_sessions": sessions >= 20,
        "sleep_cycle": sleep_days >= 7,
        "level_10": level >= 10,
        "twenty_hours": focus >= 1200,
        "fifty_sessions": sessions >= 50,
        "twenty_comebacks": comebacks >= 20,
        "sleep_month": sleep_days >= 30,
        "level_15": level >= 15,
        "fifty_hours": focus >= 3000,
        "hundred_sessions": sessions >= 100,
        "fifty_comebacks": comebacks >= 50,
        "level_20": level >= 20,
        "hundred_hours": focus >= 6000,
        "two_hundred_sessions": sessions >= 200,
        "hundred_comebacks": comebacks >= 100,
        "level_30": level >= 30,
        "two_hundred_hours": focus >= 12000,
        "level_50": level >= 50,
    }.get(key, False))


def get_week_achievements(days=7):
    """Достижения, полученные за последние `days` дней: [(day, name, desc)].

    Виджет не хранит дату получения, поэтому история пересчитывается по заметкам
    Obsidian день за днём: берётся первый день, когда условие достижения
    стало истинным. Дата получается с точностью до дня."""
    vault = Path(OBSIDIAN_VAULT)
    sessions, sleeps = {}, {}
    try:
        for path in vault.glob("????-??-??.md"):
            try:
                day = date.fromisoformat(path.stem)
                text = path.read_text(encoding="utf-8")
            except (ValueError, OSError):
                continue
            items = []
            for m in SESSION_RE.finditer(text):
                hh, mm, h, mi, s = map(int, m.groups())
                items.append((hh * 3600 + mm * 60, h * 3600 + mi * 60 + s))
            if items:
                sessions[day] = sorted(items)
        for path in vault.glob(f"????-??-??{GAME_NOTE_SUFFIX}.md"):
            try:
                day = date.fromisoformat(path.stem.replace(GAME_NOTE_SUFFIX, ""))
                m = SLEEP_RE.search(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if m:
                sleeps[day] = float(m.group(1))
    except OSError:
        return []

    total_sessions = comebacks = sleep_days = 0
    focus_min = comeback_xp = sleep_xp = longest = 0.0
    first = {}

    for day in sorted(set(sessions) | set(sleeps)):
        items = sessions.get(day, [])
        for i, (start, secs) in enumerate(items):
            total_sessions += 1
            focus_min += secs / 60.0
            if i > 0:
                prev_start, prev_secs = items[i - 1]
                away = start - (prev_start + prev_secs)
                longest = max(longest, max(0.0, away))
                if away >= COMEBACK_MIN_BREAK:
                    comebacks += 1
                    comeback_xp += _comeback_reward(away)
        if day in sleeps:
            sleep_days += 1
            sleep_xp += _sleep_reward(sleeps[day])[1]

        level, _ = _level_from(1, focus_min * XP_PER_FOCUS_MINUTE + comeback_xp + sleep_xp)
        values = {
            "sessions": total_sessions, "focus_minutes": focus_min,
            "comebacks": comebacks, "sleep_days": sleep_days,
            "level": level, "longest_comeback_seconds": longest,
        }
        for a in ACHIEVEMENTS:
            if a["key"] not in first and _achievement_condition(a["key"], values):
                first[a["key"]] = day

    data = _read_json(DATA_FILE)
    unlocked = set((data or {}).get("unlocked_achievements") or [])
    since = date.today() - timedelta(days=days - 1)
    result = []
    for a in ACHIEVEMENTS:
        day = first.get(a["key"])
        if day is None or day < since:
            continue
        if data is not None and a["key"] not in unlocked:
            continue
        result.append((day, a["name"], a["desc"]))
    result.sort(key=lambda r: r[0])
    return result
