import sys
import os
import json
import time
import hashlib
import re
from pathlib import Path
from datetime import date, datetime, timedelta
from ns_config import *

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QInputDialog,
    QDialog,
    QPushButton,
)
from PyQt6.QtCore import Qt, QTimer, QFileSystemWatcher

os.environ["QT_QPA_PLATFORM"] = "xcb"


# ============================================================
# CONFIG
# ============================================================

LEVEL_XP = {
    level: 100 + (level - 1) * 50
    for level in range(1, MAX_LEVEL)
}

GAME_NOTE_SUFFIX = "_game"


class NeuroGameWidget(QWidget):
    """
    Source of truth:
      * completed study sessions -> simple Obsidian daily files
      * sleep -> game Obsidian files
      * JSON -> only runtime state: HP recovery clock, momentum, unlocked achievements
        and a snapshot of the last Obsidian state.

    IMPORTANT:
      * Live session never changes persistent XP/HP.
      * A live session is only a UI preview.
      * Persistent focus XP/HP appears only after Study Timer has written
        the completed session to the simple Obsidian file.
      * The game never rewrites the simple daily note; it only adds the GAME link.
    """

    def __init__(self):
        super().__init__()

        self.old_pos = None
        self.pending_achievements = []
        self.achievement_dialog_open = False
        self._dialog_refs = []

        self.data = self.load_data()
        self.completed_sessions = []
        self.sleep_history = {}
        self.persistent_stats = self.empty_stats()
        self.live_state = None
        self.live_active = False          # учеба реально идет
        self.live_studying = False        # STOP/PAUSE => False
        self.live_session_present = False # текущая сессия существует, даже на STOP

        # Study Widget может оставлять active=True при STOP и просто
        # замораживать elapsed_seconds. Поэтому состояние учебы определяем
        # по изменению elapsed_seconds, а не только по active.
        self._last_live_elapsed = None
        self._last_live_elapsed_mono = None
        self._live_elapsed_epsilon = 0.25
        self._live_elapsed_last_changed_mono = None
        self._live_elapsed_local = 0.0
        self._live_clock_last_mono = time.monotonic()
        self._live_stop_detection_seconds = 2.5

        # Последнее корректно прочитанное live-состояние.
        # Нужна защита от краткого момента, когда Study Timer
        # перезаписывает live-файл и его содержимое ещё нельзя прочитать.
        self._last_valid_live_state = None
        self._last_valid_live_read = 0.0
        self._live_read_grace_seconds = 3.0

        # Runtime HP recovery must survive program restarts.
        # The authoritative HP baseline is stored in DATA_FILE, while
        # last_hp_update is the exact wall-clock point from which idle
        # recovery continues.
        self.last_obsidian_signature = None
        self.simple_files_mtime_signature = None
        self.last_runtime_save = 0.0
        self.last_display_state = None
        self._last_ui_texts = None
        self._runtime_day = date.today()
        self._own_note_write_until = 0.0

        self.init_ui()
        self.update_ui()

        # Только UI/runtime. Никакого постоянного опроса Obsidian.
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.update_runtime_state)
        self.ui_timer.start(1000)

        # Obsidian сообщает игре сам, когда обычный дневник изменился.
        self.note_watcher = QFileSystemWatcher(self)
        self.note_watcher.fileChanged.connect(self._on_daily_note_changed)

        self.sync_debounce = QTimer(self)
        self.sync_debounce.setSingleShot(True)
        self.sync_debounce.timeout.connect(self.sync_game)

        self.setGeometry(400, 100, 340, 165)

        QTimer.singleShot(300, self.initial_sync)

    # ========================================================
    # JSON
    # ========================================================

    def default_data(self):
        now = time.time()
        return {
            "xp": 0.0,
            "hp": MAX_HP,
            "level": 1,
            "last_hp_update": now,
            "last_date": str(date.today()),
            "sleep_history": {},
            "unlocked_achievements": [],
            "longest_comeback_seconds": 0.0,
            "momentum": 0.0,
            "momentum_last_update": now,
            "momentum_active_last": False,
            "momentum_break_seconds": 0.0,
            "obsidian_snapshot": {},
            # Compatibility with older versions; never used as source of truth.
            "processed_sessions": [],
            "total_focus_minutes": 0.0,
            "total_sessions": 0,
            "comebacks": 0,
        }

    def load_data(self):
        data = self.default_data()
        if not DATA_FILE.exists():
            return data

        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, json.JSONDecodeError, TypeError):
            return data

        for key in ("xp", "hp", "last_hp_update", "momentum", "momentum_last_update", "momentum_break_seconds"):
            try:
                data[key] = float(data.get(key, 0.0))
            except (TypeError, ValueError):
                data[key] = 0.0

        data["hp"] = max(0.0, min(MAX_HP, data["hp"]))
        data["momentum"] = max(0.0, min(MAX_MOMENTUM, data["momentum"]))

        try:
            data["level"] = int(data.get("level", 1))
        except (TypeError, ValueError):
            data["level"] = 1
        data["level"] = max(1, min(MAX_LEVEL, data["level"]))

        if not isinstance(data.get("sleep_history"), dict):
            data["sleep_history"] = {}
        if not isinstance(data.get("unlocked_achievements"), list):
            data["unlocked_achievements"] = []
        if not isinstance(data.get("obsidian_snapshot"), dict):
            data["obsidian_snapshot"] = {}
        if not isinstance(data.get("momentum_active_last"), bool):
            data["momentum_active_last"] = False

        # Mark whether the persisted HP value is a real runtime baseline.
        # Existing files always have "hp"; keep it unchanged.
        try:
            data["hp"] = float(data.get("hp", MAX_HP))
        except (TypeError, ValueError):
            data["hp"] = MAX_HP
        data["hp"] = max(0.0, min(MAX_HP, data["hp"]))

        return data

    def save_data(self):
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = DATA_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            tmp.replace(DATA_FILE)
        except OSError as e:
            print(f"NeuroGame JSON write error: {e}")

    # ========================================================
    # PATHS / FILE IO
    # ========================================================

    def daily_note_path(self, day=None):
        day = day or date.today()
        return OBSIDIAN_VAULT / f"{day}.md"

    def game_note_path(self, day=None):
        day = day or date.today()
        return OBSIDIAN_VAULT / f"{day}{GAME_NOTE_SUFFIX}.md"

    def read_text(self, path):
        try:
            if not path.exists():
                return ""
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_text_atomic(self, path, text):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            return True
        except OSError as e:
            print(f"NeuroGame file write error: {e}")
            return False

    def read_daily_note(self, day=None):
        return self.read_text(self.daily_note_path(day))

    def read_game_note(self, day=None):
        return self.read_text(self.game_note_path(day))

    def read_live_state(self):
        """
        Read the live session without allowing a transient file-write glitch
        to make the UI think that the session has ended.
        """
        now = time.monotonic()

        try:
            if LIVE_STATE_FILE.exists():
                with open(LIVE_STATE_FILE, "r", encoding="utf-8") as f:
                    value = json.load(f)

                if isinstance(value, dict):
                    self._last_valid_live_state = value
                    self._last_valid_live_read = now
                    return value
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

        cached = self._last_valid_live_state
        if (
            cached
            and cached.get("active")
            and now - self._last_valid_live_read <= self._live_read_grace_seconds
        ):
            return cached

        return None

    # ========================================================
    # OBSIDIAN SESSIONS
    # ========================================================

    def list_simple_days(self):
        if not OBSIDIAN_VAULT.exists():
            return []

        days = []
        for path in OBSIDIAN_VAULT.glob("????-??-??.md"):
            if path.name.endswith(f"{GAME_NOTE_SUFFIX}.md"):
                continue
            try:
                days.append(date.fromisoformat(path.stem))
            except ValueError:
                pass
        return sorted(set(days))

    def parse_sessions(self, text, day):
        """Parse only study sessions. GAME lines are ignored."""
        sessions = []
        lines = text.splitlines()
        occurrence_by_time = {}

        for line_index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line.startswith("### Session:"):
                continue

            marker = line.split("### Session:", 1)[1].strip()
            parts = marker.split()
            if len(parts) < 2:
                continue

            session_time = parts[0]
            duration = parts[1].strip("()")
            if not re.fullmatch(r"\d{2}:\d{2}", session_time):
                continue
            if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", duration):
                continue

            try:
                h, m, s = map(int, duration.split(":"))
                total_seconds = h * 3600 + m * 60 + s
            except ValueError:
                continue

            occurrence = occurrence_by_time.get(session_time, 0)
            occurrence_by_time[session_time] = occurrence + 1

            comment = ""
            if line_index + 1 < len(lines):
                next_line = lines[line_index + 1].strip()
                if next_line.startswith("-"):
                    next_line = next_line[1:].strip()
                    if not next_line.startswith("NEURO GAME:"):
                        comment = next_line

            stable_raw = f"{day}|{session_time}|{occurrence}"
            session_id = hashlib.sha1(stable_raw.encode("utf-8")).hexdigest()

            sessions.append({
                "id": session_id,
                "day": day,
                "time": session_time,
                "duration": duration,
                "seconds": total_seconds,
                "comment": comment,
            })

        sessions.sort(key=lambda s: (s["time"], s["id"]))
        return sessions

    def get_all_saved_sessions(self):
        sessions = []
        for day in self.list_simple_days():
            text = self.read_daily_note(day)
            for session in self.parse_sessions(text, day):
                sessions.append(session)
        sessions.sort(key=lambda s: self.session_start_timestamp(s))
        return sessions

    def session_start_timestamp(self, session):
        try:
            day = session["day"]
            return datetime.strptime(
                f"{day} {session['time']}",
                "%Y-%m-%d %H:%M",
            ).timestamp()
        except (ValueError, TypeError, KeyError):
            return None

    def session_end_timestamp(self, session):
        start = self.session_start_timestamp(session)
        if start is None:
            return None
        try:
            return start + float(session["seconds"])
        except (TypeError, ValueError, KeyError):
            return None

    # ========================================================
    # SLEEP: GAME FILE IS SOURCE OF TRUTH
    # ========================================================

    def get_sleep_reward(self, hours):
        if hours < 4:
            return 10, 0
        if hours < 5:
            return SLEEP_REWARD[4]
        if hours < 6:
            return SLEEP_REWARD[5]
        if hours < 7:
            return SLEEP_REWARD[6]
        if hours < 8:
            return SLEEP_REWARD[7]
        if hours < 9:
            return SLEEP_REWARD[8]
        if hours < 10:
            return SLEEP_REWARD[9]
        return 90, 20

    def parse_sleep_from_game_note(self, day):
        text = self.read_game_note(day)
        match = re.search(r"^### Sleep:\s*([0-9]+(?:\.[0-9]+)?)h\s*$", text, re.MULTILINE)
        if not match:
            return None

        try:
            hours = float(match.group(1))
        except ValueError:
            return None

        hp_gain, xp_gain = self.get_sleep_reward(hours)
        return {"hours": hours, "hp": hp_gain, "xp": xp_gain}

    def collect_sleep_history(self):
        history = {}
        if not OBSIDIAN_VAULT.exists():
            return history

        for path in OBSIDIAN_VAULT.glob(f"????-??-??{GAME_NOTE_SUFFIX}.md"):
            try:
                day = date.fromisoformat(path.stem.replace(GAME_NOTE_SUFFIX, ""))
            except ValueError:
                continue
            stats = self.parse_sleep_from_game_note(day)
            if stats:
                history[str(day)] = stats

        return history

    # ========================================================
    # GAME LINK IN SIMPLE NOTE
    # ========================================================

    def ensure_game_link(self, day):
        path = self.daily_note_path(day)
        if not path.exists():
            return False

        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            return False

        link = f"[[{day}{GAME_NOTE_SUFFIX}|GAME]]"
        if link in original:
            return False

        if original:
            updated = link + "\n" + original
        else:
            updated = link + "\n"

        self._own_note_write_until = time.monotonic() + 1.0
        return self.write_text_atomic(path, updated)

    # ========================================================
    # GAME NOTE RENDERING
    # ========================================================

    def render_current_game_note(self, day):
        sessions = [s for s in self.completed_sessions if s["day"] == day]
        sleep = self.sleep_history.get(str(day))

        lines = [
            f"# Игровая статистика за {day}",
            "",
        ]

        if sleep:
            lines += [
                f"### Sleep: {float(sleep['hours']):g}h",
                f"- HP +{sleep['hp']} | XP +{sleep['xp']}",
                "",
            ]

        lines += ["## Sessions", ""]

        for session in sessions:
            minutes = session["seconds"] / 60.0
            hp_loss = minutes * HP_LOSS_PER_FOCUS_MINUTE
            xp_gain = minutes * XP_PER_FOCUS_MINUTE
            comeback_xp = self.get_comeback_for_session(session)

            lines.append(f"### Session: {session['time']} ({session['duration']})")
            if comeback_xp:
                lines.append(
                    f"- HP -{hp_loss:.1f} | XP +{xp_gain:.1f} | COMEBACK +{comeback_xp} XP"
                )
            else:
                lines.append(f"- HP -{hp_loss:.1f} | XP +{xp_gain:.1f}")
            lines.append("")

        text = "\n".join(lines).rstrip() + "\n"
        return self.write_text_atomic(self.game_note_path(day), text)

    def finalize_game_note(self, day):
        path = self.game_note_path(day)
        existing = self.read_game_note(day)

        if re.search(r"^## Statistics\s*$", existing, re.MULTILINE):
            return False

        if not existing:
            self.render_current_game_note(day)
            existing = self.read_game_note(day)

        sessions = [s for s in self.completed_sessions if s["day"] == day]
        sleep = self.sleep_history.get(str(day))

        total_focus_minutes = sum(s["seconds"] for s in sessions) / 60.0
        total_focus_xp = total_focus_minutes * XP_PER_FOCUS_MINUTE
        total_hp_loss = total_focus_minutes * HP_LOSS_PER_FOCUS_MINUTE

        comeback_count = 0
        comeback_xp = 0.0
        for session in sessions:
            reward = self.get_comeback_for_session(session)
            if reward:
                comeback_count += 1
                comeback_xp += reward

        sleep_xp = sleep["xp"] if sleep else 0
        sleep_hp = sleep["hp"] if sleep else 0
        daily_xp = total_focus_xp + comeback_xp + sleep_xp
        daily_hp = -total_hp_loss + sleep_hp
        sign = "+" if daily_hp >= 0 else ""

        block = [
            "",
            "## Statistics",
            "",
            f"- Sessions: {len(sessions)}",
            f"- Focus: {int(total_focus_minutes // 60)}h {int(total_focus_minutes % 60):02d}m",
            f"- Comebacks: {comeback_count}",
            f"- XP from focus: +{total_focus_xp:.1f}",
            f"- HP from focus: -{total_hp_loss:.1f}",
            f"- XP from comebacks: +{comeback_xp:.1f}",
        ]

        if sleep:
            block += [
                f"- Sleep: {float(sleep['hours']):g}h",
                f"- HP from sleep: +{sleep_hp}",
                f"- XP from sleep: +{sleep_xp}",
            ]

        block += [
            "",
            "## Day total",
            "",
            f"- XP: +{daily_xp:.1f}",
            f"- HP: {sign}{daily_hp:.1f}",
            "",
        ]

        text = existing
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join(block)
        return self.write_text_atomic(path, text)

    # ========================================================
    # COMEBACK
    # ========================================================

    def get_comeback_reward(self, away_seconds):
        for limit, xp in COMEBACK_REWARD:
            if away_seconds <= limit:
                return xp
        return 10

    def get_comeback_for_session(self, session):
        same_day = [s for s in self.completed_sessions if s["day"] == session["day"]]
        same_day.sort(key=lambda s: (s["time"], s["id"]))

        try:
            index = next(i for i, s in enumerate(same_day) if s["id"] == session["id"])
        except StopIteration:
            return 0

        if index <= 0:
            return 0

        previous = same_day[index - 1]
        previous_end = self.session_end_timestamp(previous)
        current_start = self.session_start_timestamp(session)
        if previous_end is None or current_start is None:
            return 0

        away = current_start - previous_end
        if away < COMEBACK_MIN_BREAK:
            return 0
        return self.get_comeback_reward(away)

    def count_comebacks(self, sessions):
        old = self.completed_sessions
        self.completed_sessions = sessions
        count = sum(1 for s in sessions if self.get_comeback_for_session(s))
        self.completed_sessions = old
        return count

    def longest_comeback(self, sessions):
        old = self.completed_sessions
        self.completed_sessions = sessions
        longest = 0.0
        for session in sessions:
            same_day = [s for s in sessions if s["day"] == session["day"]]
            same_day.sort(key=lambda s: (s["time"], s["id"]))
            try:
                index = next(i for i, s in enumerate(same_day) if s["id"] == session["id"])
            except StopIteration:
                continue
            if index <= 0:
                continue
            prev_end = self.session_end_timestamp(same_day[index - 1])
            cur_start = self.session_start_timestamp(session)
            if prev_end is not None and cur_start is not None:
                longest = max(longest, max(0.0, cur_start - prev_end))
        self.completed_sessions = old
        return longest

    # ========================================================
    # STATE REBUILD FROM OBSIDIAN
    # ========================================================

    def empty_stats(self):
        return {
            "xp": 0.0,
            "hp": MAX_HP,
            "level": 1,
            "total_focus_minutes": 0.0,
            "total_sessions": 0,
            "comebacks": 0,
            "longest_comeback_seconds": 0.0,
            "sleep_days": 0,
        }

    def build_obsidian_signature(self, sessions, sleep_history):
        payload = {
            "sessions": [
                {
                    "id": s["id"],
                    "day": str(s["day"]),
                    "time": s["time"],
                    "seconds": s["seconds"],
                }
                for s in sessions
            ],
            "sleep": sleep_history,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def level_from_xp(self, xp):
        level = 1
        remaining = float(xp)
        while level < MAX_LEVEL:
            need = LEVEL_XP[level]
            if remaining < need:
                break
            remaining -= need
            level += 1
        return level, remaining

    def simulate_hp(self, sessions, sleep_history, stop_at=None):
        """Rebuild HP chronologically from saved events only."""
        events = []

        for session in sessions:
            start = self.session_start_timestamp(session)
            end = self.session_end_timestamp(session)
            if start is None or end is None:
                continue
            events.append((start, "session_start", session))
            events.append((end, "session_end", session))

        for day_str, sleep in sleep_history.items():
            try:
                day = date.fromisoformat(day_str)
                ts = datetime(day.year, day.month, day.day, SLEEP_CHECK_HOUR, 0).timestamp()
            except ValueError:
                continue
            events.append((ts, "sleep", sleep))

        events.sort(key=lambda e: (e[0], 0 if e[1] == "sleep" else 1))

        hp = MAX_HP
        last_ts = events[0][0] if events else time.time()
        studying = False

        for ts, kind, payload in events:
            if stop_at is not None and ts > stop_at:
                break

            delta = max(0.0, ts - last_ts)
            if not studying:
                hp = min(MAX_HP, hp + delta * HP_RECOVERY_PER_SECOND)

            if kind == "sleep":
                hp = min(MAX_HP, hp + float(payload.get("hp", 0)))
            elif kind == "session_start":
                studying = True
            elif kind == "session_end":
                minutes = payload["seconds"] / 60.0
                hp = max(0.0, hp - minutes * HP_LOSS_PER_FOCUS_MINUTE)
                studying = False

            last_ts = ts

        if stop_at is not None:
            if not studying:
                delta = max(0.0, stop_at - last_ts)
                hp = min(MAX_HP, hp + delta * HP_RECOVERY_PER_SECOND)
        else:
            now = time.time()
            if not studying:
                hp = min(MAX_HP, hp + max(0.0, now - last_ts) * HP_RECOVERY_PER_SECOND)

        return max(0.0, min(MAX_HP, hp))

    def rebuild_persistent_stats(self, sessions, sleep_history):
        """Every saved-session change rebuilds XP/HP/stats from Obsidian."""
        total_focus_minutes = sum(s["seconds"] for s in sessions) / 60.0
        comeback_count = self.count_comebacks(sessions)
        longest_break = self.longest_comeback(sessions)
        comeback_xp = 0.0

        old = self.completed_sessions
        self.completed_sessions = sessions
        for s in sessions:
            comeback_xp += self.get_comeback_for_session(s)
        self.completed_sessions = old

        focus_xp = total_focus_minutes * XP_PER_FOCUS_MINUTE
        sleep_xp = sum(float(v.get("xp", 0)) for v in sleep_history.values())
        total_xp_earned = focus_xp + comeback_xp + sleep_xp
        level, remaining_xp = self.level_from_xp(total_xp_earned)

        stats = self.empty_stats()
        stats.update({
            "xp": remaining_xp,
            "hp": self.simulate_hp(sessions, sleep_history),
            "level": level,
            "total_focus_minutes": total_focus_minutes,
            "total_sessions": len(sessions),
            "comebacks": comeback_count,
            "longest_comeback_seconds": longest_break,
            "sleep_days": len(sleep_history),
        })
        return stats

    def commit_obsidian_state(self, force=False):
        sessions = self.get_all_saved_sessions()
        sleep_history = self.collect_sleep_history()
        signature = self.build_obsidian_signature(sessions, sleep_history)

        changed = force or signature != self.last_obsidian_signature
        if not changed:
            return False

        self.completed_sessions = sessions
        self.sleep_history = sleep_history
        self.persistent_stats = self.rebuild_persistent_stats(sessions, sleep_history)

        self.last_obsidian_signature = signature
        self.data["obsidian_snapshot"] = {
            "signature": signature,
            "session_count": len(sessions),
            "sleep_days": len(sleep_history),
            "saved_at": time.time(),
        }

        self.data["xp"] = self.persistent_stats["xp"]
        self.data["level"] = self.persistent_stats["level"]
        self.data["total_focus_minutes"] = self.persistent_stats["total_focus_minutes"]
        self.data["total_sessions"] = self.persistent_stats["total_sessions"]
        self.data["comebacks"] = self.persistent_stats["comebacks"]
        self.data["longest_comeback_seconds"] = self.persistent_stats["longest_comeback_seconds"]
        self.data["sleep_history"] = sleep_history

        # На STOP/PAUSE текущая live-сессия все еще существует, поэтому
        # не затираем runtime HP, которое в этот момент может восстанавливаться.
        if not self.live_session_present:
            self.data["hp"] = self.persistent_stats["hp"]

        self.data["hp"] = max(0.0, min(MAX_HP, float(self.data.get("hp", MAX_HP))))
        self.data["last_hp_update"] = time.time()
        self.save_data()
        return True

    # ========================================================
    # RUNTIME HP RECOVERY
    # ========================================================

    def apply_runtime_hp_recovery(self, now=None):
        now = float(now if now is not None else time.time())

        if self.live_active:
            self.data["last_hp_update"] = now
            return

        try:
            last = float(self.data.get("last_hp_update", now))
        except (TypeError, ValueError):
            last = now

        if now <= last:
            return

        elapsed = now - last
        current_hp = float(self.data.get("hp", MAX_HP))
        self.data["hp"] = min(
            MAX_HP,
            max(0.0, current_hp) + elapsed * HP_RECOVERY_PER_SECOND,
        )
        self.data["last_hp_update"] = now

    # ========================================================
    # LIVE SESSION
    # ========================================================

    def is_live_studying(self, live=None):
        """True только когда учеба реально идет прямо сейчас.

        Study Widget может оставлять active=True на STOP/PAUSE и просто
        переставать увеличивать elapsed_seconds. Поэтому сначала учитываем
        явные paused/stopped-флаги, а затем определяем остановку по тому,
        продолжает ли расти elapsed_seconds.
        """
        live = live if live is not None else self.live_state
        if not isinstance(live, dict):
            return False

        # Явная пауза/STOP всегда главнее active=True.
        for key in ("paused", "is_paused", "pause", "stopped", "is_stopped", "stop"):
            if key in live and bool(live.get(key)):
                return False

        # Если Study Widget явно сообщает running-состояние — используем его.
        for key in ("running", "is_running", "timer_running", "studying", "is_studying"):
            if key in live:
                return bool(live.get(key))

        status = str(live.get("status", "")).strip().lower()
        if status in {"paused", "pause", "stopped", "stop", "idle", "rest"}:
            return False
        if status in {"running", "active", "studying", "focus"}:
            return True

        if not bool(live.get("active")):
            return False

        # Нет elapsed_seconds — active=True считаем рабочим состоянием.
        if "elapsed_seconds" not in live:
            return True

        try:
            elapsed = max(0.0, float(live.get("elapsed_seconds", 0.0)))
        except (TypeError, ValueError):
            return True

        now_mono = time.monotonic()

        if self._last_live_elapsed is None:
            self._last_live_elapsed = elapsed
            self._last_live_elapsed_mono = now_mono
            self._live_elapsed_last_changed_mono = now_mono
            self._live_elapsed_local = elapsed
            self._live_clock_last_mono = now_mono
            return True

        previous = float(self._last_live_elapsed)

        # Новый запуск мог сбросить elapsed_seconds.
        if elapsed + self._live_elapsed_epsilon < previous:
            self._last_live_elapsed = elapsed
            self._last_live_elapsed_mono = now_mono
            self._live_elapsed_last_changed_mono = now_mono
            self._live_elapsed_local = elapsed
            self._live_clock_last_mono = now_mono
            return True

        # JSON-таймер вырос — точно работаем.
        if elapsed > previous + self._live_elapsed_epsilon:
            self._last_live_elapsed = elapsed
            self._last_live_elapsed_mono = now_mono
            self._live_elapsed_last_changed_mono = now_mono
            self._live_elapsed_local = max(self._live_elapsed_local, elapsed)
            return True

        # После короткого debounce без изменений считаем STOP/PAUSE.
        last_changed = self._live_elapsed_last_changed_mono
        if last_changed is None:
            self._live_elapsed_last_changed_mono = now_mono
            return True

        return now_mono - last_changed < self._live_stop_detection_seconds

    def has_live_session(self, live=None):
        """True только если текущая незавершенная сессия реально существует."""
        live = live if live is not None else self.live_state
        if not isinstance(live, dict):
            return False

        # Study Widget оставляет active=True во время самой сессии,
        # включая STOP/PAUSE. После завершения сессии active становится False.
        return bool(live.get("active"))

    def update_live_flags(self):
        previous_present = self.live_session_present
        self.live_studying = self.is_live_studying(self.live_state)
        self.live_session_present = self.has_live_session(self.live_state)

        if not self.live_session_present:
            self._last_live_elapsed = None
            self._last_live_elapsed_mono = None
            self._live_elapsed_last_changed_mono = None
            self._live_elapsed_local = 0.0
            self._live_clock_last_mono = time.monotonic()
        elif not previous_present and self.live_state is not None:
            try:
                base = max(0.0, float(self.live_state.get("elapsed_seconds", 0.0)))
            except (TypeError, ValueError):
                base = 0.0
            now_mono = time.monotonic()
            self._last_live_elapsed = base
            self._last_live_elapsed_mono = now_mono
            self._live_elapsed_last_changed_mono = now_mono
            self._live_elapsed_local = base
            self._live_clock_last_mono = now_mono

        # Остальная игровая логика использует live_active как "учеба идет".
        self.live_active = self.live_studying

    def live_elapsed(self, live=None):
        """Показываем суммарное время текущей сессии без скачков на STOP/RESUME."""
        live = live if live is not None else self.live_state
        if not isinstance(live, dict):
            return 0.0

        try:
            remote_elapsed = max(0.0, float(live.get("elapsed_seconds", 0.0)))
        except (TypeError, ValueError):
            remote_elapsed = 0.0

        now_mono = time.monotonic()

        # Синхронизируемся с фактическим elapsed из Study Widget.
        if self._last_live_elapsed is None:
            self._last_live_elapsed = remote_elapsed
            self._last_live_elapsed_mono = now_mono
            self._live_elapsed_last_changed_mono = now_mono
            self._live_elapsed_local = remote_elapsed
            self._live_clock_last_mono = now_mono
            return remote_elapsed

        if remote_elapsed > self._live_elapsed_local + self._live_elapsed_epsilon:
            self._live_elapsed_local = remote_elapsed
            self._last_live_elapsed = remote_elapsed
            self._live_elapsed_last_changed_mono = now_mono

        # Локально добавляем секунды только пока реально идет учеба.
        if self.live_studying:
            dt = max(0.0, now_mono - self._live_clock_last_mono)
            self._live_elapsed_local += dt

        self._live_clock_last_mono = now_mono
        return max(self._live_elapsed_local, remote_elapsed)

    def live_start_timestamp(self, live=None):
        live = live if live is not None else self.live_state
        elapsed = self.live_elapsed(live)

        if isinstance(live, dict):
            for key in ("start_timestamp", "started_timestamp", "start_time", "started_at"):
                value = live.get(key)
                if value is None:
                    continue
                try:
                    if isinstance(value, (int, float)):
                        return float(value)
                    return datetime.fromisoformat(
                        str(value).replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, TypeError, OverflowError):
                    pass

        return time.time() - elapsed

    def get_display_state(self):
        # На STOP/PAUSE recovery работает, потому что live_studying=False.
        self.apply_runtime_hp_recovery()

        xp = float(self.data.get("xp", 0.0))
        hp = float(self.data.get("hp", MAX_HP))
        level = int(self.data.get("level", 1))

        # Live-preview сохраняется на STOP/PAUSE тоже:
        # пока текущая сессия существует, показываем её накопленный
        # XP/HP-эффект. На STOP XP остаётся на месте, а HP отдельно
        # восстанавливается через apply_runtime_hp_recovery().
        if self.live_session_present:
            elapsed_minutes = self.live_elapsed() / 60.0
            xp += elapsed_minutes * XP_PER_FOCUS_MINUTE
            hp = max(0.0, hp - elapsed_minutes * HP_LOSS_PER_FOCUS_MINUTE)
            level, xp = self.level_from_xp_from_level(level, xp)

        return level, xp, hp

    def level_from_xp_from_level(self, level, xp):
        level = max(1, min(MAX_LEVEL, int(level)))
        xp = max(0.0, float(xp))
        while level < MAX_LEVEL and xp >= LEVEL_XP[level]:
            xp -= LEVEL_XP[level]
            level += 1
        return level, xp

    # ========================================================
    # MOMENTUM
    # ========================================================

    def get_momentum_state(self, value=None):
        value = float(self.data.get("momentum", 0.0) if value is None else value)
        if value < 20:
            return "COLD"
        if value < 50:
            return "WARMING"
        if value < 75:
            return "FOCUSED"
        if value < 90:
            return "FLOW"
        return "OVERDRIVE"

    def calculate_momentum_value(self, value, elapsed, active, break_seconds):
        value = float(value)
        elapsed = max(0.0, float(elapsed))
        break_seconds = max(0.0, float(break_seconds))

        if active:
            value += elapsed * MOMENTUM_GAIN_PER_SECOND
            break_seconds = 0.0
        else:
            remaining = elapsed
            current_break = break_seconds

            fast_left = max(0.0, MOMENTUM_FAST_BREAK - current_break)
            fast_part = min(remaining, fast_left)
            value -= fast_part * MOMENTUM_DECAY_FAST
            remaining -= fast_part
            current_break += fast_part

            medium_left = max(0.0, MOMENTUM_MEDIUM_BREAK - current_break)
            medium_part = min(remaining, medium_left)
            value -= medium_part * MOMENTUM_DECAY_MEDIUM
            remaining -= medium_part
            current_break += medium_part

            if remaining > 0:
                value -= remaining * MOMENTUM_DECAY_SLOW
                current_break += remaining

            break_seconds = current_break

        return max(0.0, min(MAX_MOMENTUM, value)), break_seconds

    def update_momentum(self, now=None, force_save=False):
        now = float(now if now is not None else time.time())

        try:
            last_update = float(self.data.get("momentum_last_update", now))
        except (TypeError, ValueError):
            last_update = now

        elapsed = max(0.0, now - last_update)
        value, break_seconds = self.calculate_momentum_value(
            self.data.get("momentum", 0.0),
            elapsed,
            self.live_active,
            self.data.get("momentum_break_seconds", 0.0),
        )

        self.data["momentum"] = value
        self.data["momentum_last_update"] = now
        self.data["momentum_active_last"] = self.live_active
        self.data["momentum_break_seconds"] = break_seconds

        if force_save:
            self.save_data()

    # ========================================================
    # OBSIDIAN WATCHER
    # ========================================================

    def arm_daily_note_watcher(self, day=None):
        day = day or date.today()
        path = str(self.daily_note_path(day))

        watched = self.note_watcher.files()
        if watched:
            self.note_watcher.removePaths(watched)

        if Path(path).exists():
            self.note_watcher.addPath(path)

    def _on_daily_note_changed(self, path):
        if time.monotonic() < self._own_note_write_until:
            QTimer.singleShot(150, lambda: self.arm_daily_note_watcher(date.today()))
            return

        self.arm_daily_note_watcher(date.today())
        self.sync_debounce.start(250)

    def handle_new_day(self):
        today = date.today()
        if today == self._runtime_day:
            return

        self._runtime_day = today
        self.sync_game()

    # ========================================================
    # MORNING FLOW
    # ========================================================

    def initial_sync(self):
        self.live_state = self.read_live_state()
        self.update_live_flags()

        self.commit_obsidian_state(force=True)
        self.ensure_game_link(date.today())

        today = date.today()
        yesterday = today - timedelta(days=1)

        if datetime.now().hour >= SLEEP_CHECK_HOUR:
            self.check_sleep_on_start()

        self.commit_obsidian_state(force=True)

        if yesterday in self.list_simple_days() or self.game_note_path(yesterday).exists():
            self.finalize_game_note(yesterday)

        self.render_current_game_note(today)
        self._runtime_day = today
        self.arm_daily_note_watcher(today)
        self.update_momentum(force_save=True)
        self.check_achievements()
        self.save_data()
        self.update_ui()

    def check_sleep_on_start(self):
        today = date.today()
        today_str = str(today)

        if today_str in self.sleep_history:
            return

        existing = self.parse_sleep_from_game_note(today)
        if existing:
            self.sleep_history[today_str] = existing
            self.data["sleep_history"] = self.sleep_history
            self.save_data()
            return

        hours, ok = QInputDialog.getDouble(
            self,
            "SLEEP",
            "Сколько часов ты спала?",
            8.0,
            1.0,
            16.0,
            1,
        )
        if not ok:
            return

        hp_gain, xp_gain = self.get_sleep_reward(hours)
        stats = {
            "hours": float(hours),
            "hp": int(hp_gain),
            "xp": int(xp_gain),
        }

        self.sleep_history[today_str] = stats
        self.data["sleep_history"] = self.sleep_history

        self.render_current_game_note(today)
        self.save_data()

    # ========================================================
    # SYNC
    # ========================================================

    def get_simple_files_mtime_signature(self):
        if not OBSIDIAN_VAULT.exists():
            return ()
        path = self.daily_note_path(date.today())
        try:
            stat = path.stat()
            return ((path.name, stat.st_mtime_ns, stat.st_size),)
        except OSError:
            return ()

    def sync_game(self):
        current_day = date.today()
        self._runtime_day = current_day

        self.live_state = self.read_live_state()
        self.update_live_flags()

        yesterday = current_day - timedelta(days=1)

        self.commit_obsidian_state(force=True)
        self.ensure_game_link(current_day)

        if datetime.now().hour >= SLEEP_CHECK_HOUR:
            before = len(self.sleep_history)
            self.check_sleep_on_start()
            if len(self.sleep_history) != before:
                self.commit_obsidian_state(force=True)

        if (
            self.data.get("last_date") != str(current_day)
            and (yesterday in self.list_simple_days()
                 or self.game_note_path(yesterday).exists())
        ):
            self.finalize_game_note(yesterday)

        self.render_current_game_note(current_day)
        self.data["last_date"] = str(current_day)
        self.arm_daily_note_watcher(current_day)
        self.update_momentum(force_save=True)
        self.check_achievements()
        self.save_data()
        self.update_ui()

    # ========================================================
    # ACHIEVEMENTS
    # ========================================================

    def get_achievement_values(self):
        return {
            "sessions": int(self.persistent_stats.get("total_sessions", 0)),
            "focus_minutes": float(self.persistent_stats.get("total_focus_minutes", 0.0)),
            "comebacks": int(self.persistent_stats.get("comebacks", 0)),
            "sleep_days": int(self.persistent_stats.get("sleep_days", 0)),
            "level": int(self.persistent_stats.get("level", 1)),
            "longest_comeback_seconds": float(self.persistent_stats.get("longest_comeback_seconds", 0.0)),
        }

    def achievement_condition(self, key, values):
        sessions = values["sessions"]
        focus = values["focus_minutes"]
        comebacks = values["comebacks"]
        sleep_days = values["sleep_days"]
        level = values["level"]
        longest_break = values["longest_comeback_seconds"]

        conditions = {
            "steady_start": sessions >= 3,
            "first_depth": focus >= 90,
            "returner": comebacks >= 3,
            "two_hours": focus >= 120,
            "ten_sessions": sessions >= 10,
            "late_bloom": longest_break > 60 * 60,
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
        }
        return bool(conditions.get(key, False))

    def check_achievements(self):
        values = self.get_achievement_values()
        unlocked = set(self.data.get("unlocked_achievements", []))
        newly_unlocked = []

        for achievement in ACHIEVEMENTS:
            key = achievement["key"]
            if key in unlocked:
                continue
            if self.achievement_condition(key, values):
                unlocked.add(key)
                newly_unlocked.append(achievement)

        if not newly_unlocked:
            return

        self.data["unlocked_achievements"] = sorted(unlocked)
        self.pending_achievements.extend(newly_unlocked)
        self.save_data()
        self.show_next_achievement()

    def show_next_achievement(self):
        if self.achievement_dialog_open or not self.pending_achievements:
            return

        achievement = self.pending_achievements.pop(0)
        self.achievement_dialog_open = True

        dialog = QDialog(self)
        self._dialog_refs.append(dialog)
        dialog.setWindowTitle("ACHIEVEMENT UNLOCKED")
        dialog.setFixedSize(390, 205)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        frame = QFrame(dialog)
        frame.setGeometry(0, 0, 390, 205)
        frame.setStyleSheet(
            f"""
            QFrame {{
                background-color: {rgba(alpha=248)};
                border: 1px solid {COLORS['pink']};
                border-radius: 4px;
            }}
            QLabel {{
                color: {COLORS['text']};
                font-family: {FONT_FAMILY};
            }}
            QPushButton {{
                background: #1a0712;
                color: {COLORS['pink_bright']};
                border: 1px solid {COLORS['pink']};
                padding: 6px 18px;
                font-family: {FONT_FAMILY};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: #300b20;
            }}
            """
        )

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(8)

        title = QLabel("✦  ACHIEVEMENT UNLOCKED  ✦")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {COLORS['pink']}; font-size: 13px; font-weight: bold;")

        name = QLabel(achievement["name"])
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setStyleSheet(f"color: {COLORS['white']}; font-size: 19px; font-weight: bold;")

        desc = QLabel(achievement["desc"])
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {COLORS['pink_soft']}; font-size: 10px;")

        button = QPushButton("OK")
        button.setFixedWidth(90)
        button.clicked.connect(dialog.close)

        layout.addWidget(title)
        layout.addWidget(name)
        layout.addWidget(desc)
        layout.addStretch()

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        dialog.finished.connect(self._achievement_dialog_finished)
        dialog.show()

    def _achievement_dialog_finished(self, *_):
        self.achievement_dialog_open = False
        QTimer.singleShot(80, self.show_next_achievement)

    # ========================================================
    # UI UPDATE
    # ========================================================

    def make_bar(self, value, maximum=100, length=16):
        value = max(0.0, min(float(value), float(maximum)))
        filled = int((value / maximum) * length) if maximum else 0
        return "█" * filled + "░" * (length - filled)

    def get_today_display_stats(self):
        today = date.today()
        sessions = [s for s in self.completed_sessions if s["day"] == today]
        focus_seconds = sum(s["seconds"] for s in sessions)
        session_count = len(sessions)

        if self.live_session_present:
            focus_seconds += self.live_elapsed()
            session_count += 1

        return focus_seconds / 60.0, session_count

    def update_runtime_state(self):
        if date.today() != self._runtime_day:
            self.handle_new_day()
            return

        previous_present = self.live_session_present

        self.live_state = self.read_live_state()
        self.update_live_flags()

        # Когда текущая сессия только что завершилась, Study Widget уже
        # убрал live-сессию. Не полагаемся только на QFileSystemWatcher:
        # после atomic replace наблюдение за обычным daily note может быть
        # потеряно. Поэтому один раз явно синхронизируем Obsidian после
        # перехода active-сессия -> завершена. Небольшая задержка даёт
        # Study Widget время дописать завершённую Session в обычный note.
        if previous_present and not self.live_session_present:
            QTimer.singleShot(500, self.sync_game)

        self.apply_runtime_hp_recovery()
        self.update_momentum()
        self.update_ui()

        now = time.time()
        if now - self.last_runtime_save >= 10.0:
            self.last_runtime_save = now
            self.save_data()

    def update_ui(self):
        level, xp, hp = self.get_display_state()
        focus_minutes, session_count = self.get_today_display_stats()

        level_text = f"LVL {level}/{MAX_LEVEL}"

        if level < MAX_LEVEL:
            next_xp = LEVEL_XP[level]
            display_xp = min(xp, next_xp)
            xp_text = (
                f"XP   [{self.make_bar(display_xp, next_xp)}] "
                f"{display_xp:.0f}/{next_xp}"
            )
        else:
            xp_text = "XP   [████████████████] MAX"

        hp_text = f"{hp:.1f}".rstrip("0").rstrip(".")
        hp_label_text = f"HP   [{self.make_bar(hp)}] {hp_text}/100"

        momentum = float(self.data.get("momentum", 0.0))
        momentum_text = (
            f"MOMENTUM [{self.make_bar(momentum, MAX_MOMENTUM, 16)}] "
            f"{momentum:.0f}% {self.get_momentum_state(momentum)}"
        )

        hours = int(focus_minutes) // 60
        minutes = int(focus_minutes) % 60
        word = "SESSION" if session_count == 1 else "SESSIONS"
        stats_text = f"FOCUS {hours}h {minutes:02d}m    {session_count} {word}"

        ui_texts = (level_text, xp_text, hp_label_text, momentum_text, stats_text)
        if ui_texts == self._last_ui_texts:
            return

        self._last_ui_texts = ui_texts
        self.level_label.setText(level_text)
        self.xp_label.setText(xp_text)
        self.hp_label.setText(hp_label_text)
        self.momentum_label.setText(momentum_text)
        self.stats_label.setText(stats_text)

    # ========================================================
    # UI
    # ========================================================

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnBottomHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.main_frame = QFrame()
        self.main_frame.setObjectName("WidgetFrame")
        self.main_frame.setStyleSheet(frame_style())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.main_frame)

        layout = QVBoxLayout(self.main_frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        top = QHBoxLayout()
        self.title_label = QLabel("NEUROSYNC")
        self.title_label.setStyleSheet(
            f"font-size: 11px; color: {COLORS['pink']}; font-weight: bold;"
        )
        self.level_label = QLabel("LVL 1/50")
        self.level_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        top.addWidget(self.title_label)
        top.addStretch()
        top.addWidget(self.level_label)
        layout.addLayout(top)

        self.xp_label = QLabel()
        self.hp_label = QLabel()
        self.momentum_label = QLabel()
        self.stats_label = QLabel()

        layout.addWidget(self.xp_label)
        layout.addWidget(self.hp_label)
        layout.addWidget(self.momentum_label)
        layout.addWidget(self.stats_label)

    # ========================================================
    # DRAG
    # ========================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.old_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.old_pos is None:
            return
        delta = event.globalPosition().toPoint() - self.old_pos
        self.move(self.x() + delta.x(), self.y() + delta.y())
        self.old_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.old_pos = None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    widget = NeuroGameWidget()
    widget.show()
    widget.lower()
    sys.exit(app.exec())
