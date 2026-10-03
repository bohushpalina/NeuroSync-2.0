# ns_config.py
# Вынесли стиль, чтобы использовать одни цвета и в шторке, и в виджете
import os
import subprocess
from pathlib import Path

# ============================================================
# ЛИЧНЫЕ НАСТРОЙКИ (токены, ключи, пути)
# Берутся из переменных окружения или из local_config.py
# (он в .gitignore). Шаблон — local_config.example.py
# ============================================================
try:
    import local_config as _local
except ImportError:
    _local = None


def _get(name, default=None):
    value = os.environ.get(name)
    if value:
        return value
    return getattr(_local, name, default)


# ============================================================
# THEME
# Единая тема NeuroSync — "Violet Terminal".
# Подогнана под фиолетово-чёрные обои и маджента-акцент терминала.
# Меняешь цвет/радиус здесь — он меняется сразу во всех 5 виджетах.
# ============================================================

FONT_FAMILY = "MonoCraft"

COLORS = {
    "pink":        "#c13cff",  # основной акцент (заголовки, рамки, главная кнопка) — фиолетовая маджента, как в терминале
    "pink_bright": "#df8bff",  # акцент светлее (заголовки в диалогах, ховер-текст)
    "pink_hover":  "#efc2ff",  # текст кнопки при наведении
    "pink_soft":   "#d6b3ff",  # приглушённый акцентный текст
    "pink_dim":    "#9c7ab8",  # второстепенный текст (подписи источников и т.п.)
    "pink_pressed": "#8a1fd9", # состояние "нажато" / STOP-hover
    "cyan":        "#00e5ff",  # вторичный акцент (RESET и подобные действия)
    "purple":      "#6a2fe0",  # третий акцент, глубокий индиго (QUOTE и подобные действия)
    "text":        "#e7dcff",  # основной текст — светлый лавандовый
    "text_muted":  "#c1a8dd",  # приглушённый текст (blockquote и т.п.)
    "white":       "#ffffff",
    "black":       "#000000",
    "border_dim":  "#3a1a52",  # рамки полей ввода / списков
    "input_bg":    "#150a24",  # фон полей ввода и кнопок по умолчанию
    "disabled":    "#5c4570",
    "disabled_border": "#3f2a55",
}

# Базовый фон карточек-виджетов: (R, G, B); альфа задаётся отдельно
# Глубокий фиолетово-чёрный, как обои рабочего стола, а не бордовый.
BG_RGB = (13, 6, 24)
BG_ALPHA = 225          # стандартная плотность фона виджета
BG_ALPHA_DENSE = 242    # более плотный фон (развёрнутые панели/шторки)

# RGB основного акцента — нужен для точечных rgba() с прозрачностью
ACCENT_RGB = (193, 60, 255)

RADIUS_SM = 2   # маленькие плавающие карточки (панель статуса, таймер, обсидиан)
RADIUS_MD = 4   # кнопки, инпуты
RADIUS_LG = 10  # крупные панели
RADIUS_PILL = 20  # скруглённые "шторки" (выезжающая панель снизу)


def rgba(rgb=BG_RGB, alpha=BG_ALPHA):
    """Собрать rgba(...) строку для Qt stylesheet из темы."""
    r, g, b = rgb
    return f"rgba({r}, {g}, {b}, {alpha})"


def frame_style(radius=RADIUS_SM, alpha=BG_ALPHA, border_color=None,
                 object_name="WidgetFrame", font_size=12, border_width=1):
    """
    Стиль тёмной карточки-виджета: фон + рамка + текст лейблов внутри.
    Используется как setStyleSheet() для главного QFrame каждого виджета.
    """
    border_color = border_color or COLORS["pink"]
    return f"""
        #{object_name} {{
            background-color: {rgba(alpha=alpha)};
            border: {border_width}px solid {border_color};
            border-radius: {radius}px;
        }}
        QLabel {{
            color: {COLORS['text']};
            font-family: '{FONT_FAMILY}';
            font-size: {font_size}px;
        }}
    """


def button_style(color=None, radius=RADIUS_MD, font_size=11, bold=True,
                  padding="4px", filled=False):
    """
    Обычная кнопка NeuroSync: тёмный фон + цветная рамка + инверсия на hover.
    filled=True — уже "нажатая"/активная кнопка (залитый фон, как STOP).
    """
    color = color or COLORS["pink"]
    weight = "font-weight: bold;" if bold else ""
    hover_text = COLORS["black"] if color == COLORS["cyan"] else COLORS["white"]

    if filled:
        return f"""
            QPushButton {{
                background-color: {color};
                color: {COLORS['white']};
                border: 1px solid {color};
                border-radius: {radius}px;
                font-family: '{FONT_FAMILY}';
                font-size: {font_size}px;
                {weight}
                padding: {padding};
            }}
            QPushButton:hover {{
                background-color: {COLORS['pink_pressed']};
            }}
        """

    return f"""
        QPushButton {{
            background-color: {COLORS['input_bg']};
            color: {color};
            border: 1px solid {color};
            border-radius: {radius}px;
            font-family: '{FONT_FAMILY}';
            font-size: {font_size}px;
            {weight}
            padding: {padding};
        }}
        QPushButton:hover {{
            background-color: {color};
            color: {hover_text};
        }}
    """


def dialog_style():
    """Стиль всплывающих QDialog (лог сессии, ачивки)."""
    return f"""
        QDialog {{
            background-color: {rgba(alpha=255)};
            color: {COLORS['white']};
        }}
        QLabel {{
            color: {COLORS['pink']};
            font-family: '{FONT_FAMILY}';
            font-weight: bold;
            font-size: 11px;
        }}
        QTextEdit {{
            background-color: {COLORS['input_bg']};
            color: {COLORS['cyan']};
            border: 1px solid {COLORS['pink']};
            border-radius: 3px;
            padding: 6px;
            font-family: '{FONT_FAMILY}';
            font-size: 11px;
        }}
        QTextEdit:focus {{
            border: 1px solid {COLORS['pink']};
        }}
        QPushButton {{
            background-color: {COLORS['input_bg']};
            color: {COLORS['pink']};
            border: 1px solid {COLORS['pink']};
            border-radius: 3px;
            font-family: '{FONT_FAMILY}';
            font-weight: bold;
            padding: 6px 14px;
            min-width: 80px;
        }}
        QPushButton:hover {{
            background-color: {COLORS['pink']};
            color: {COLORS['white']};
        }}
    """


STYLE = f"""
QWidget {{
    background: transparent;
    color: {COLORS['text']};
    font-family: '{FONT_FAMILY}', 'Consolas';
    font-size: 11px;
}}
#WidgetFrame {{
    background-color: {rgba(alpha=BG_ALPHA)};
    border: 2px solid {COLORS['pink']};
    border-radius: {RADIUS_LG}px;
}}

#MainFrame {{
    background-color: {rgba(rgb=(18, 8, 30), alpha=BG_ALPHA_DENSE)};
    border: 2px solid {COLORS['pink']};
    border-top: none;
    border-bottom-left-radius: {RADIUS_PILL}px;
    border-bottom-right-radius: {RADIUS_PILL}px;
}}

QLabel#SectionHeader {{
    font-weight: bold;
    color: {COLORS['pink']};
    text-transform: uppercase;
}}

QLabel#FocusTarget {{
    color: {COLORS['cyan']};
    font-family: '{FONT_FAMILY}';
    font-size: 10px;
    font-weight: bold;
    padding: 2px 0;
}}

QListWidget {{
    background: rgba(0, 0, 0, 160);
    border: 1px solid {COLORS['border_dim']};
    color: {COLORS['text']};
    border-radius: 6px;
}}

QListWidget::item:selected {{
    background-color: {COLORS['pink']};
    color: {COLORS['black']};
}}

QLineEdit,
QTextEdit {{
    background: rgba(0, 0, 0, 180);
    border: 1px solid {COLORS['border_dim']};
    color: {COLORS['white']};
    border-radius: 4px;
    padding: 4px;
}}

QPushButton {{
    background: rgba({ACCENT_RGB[0]}, {ACCENT_RGB[1]}, {ACCENT_RGB[2]}, 30);
    border: 1px solid {COLORS['pink']};
    color: {COLORS['pink']};
    font-weight: bold;
    border-radius: 4px;
    padding: 5px;
}}

QPushButton:hover {{
    background: {COLORS['pink']};
    color: {COLORS['black']};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: #241033;
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {COLORS['pink']};
    width: 12px;
    height: 12px;
    margin: -4px 0;
    border-radius: 6px;
}}

QSplitter::handle {{
    background: rgba({ACCENT_RGB[0]}, {ACCENT_RGB[1]}, {ACCENT_RGB[2]}, 20);
    height: 1px;
}}
"""

DATA_FILE = Path.home() / ".neurosync_game.json"
LIVE_STATE_FILE = Path.home() / ".neurosync_live.json"
MAX_LEVEL = 50
MAX_HP = 100.0

XP_PER_FOCUS_MINUTE = 1.0
HP_LOSS_PER_FOCUS_MINUTE = 0.5

HP_RECOVERY_HOURS = 3
HP_RECOVERY_PER_SECOND = MAX_HP / (HP_RECOVERY_HOURS * 60 * 60)

COMEBACK_MIN_BREAK = 5 * 60
COMEBACK_REWARD = [
    (15 * 60, 2),
    (30 * 60, 3),
    (60 * 60, 5),
    (3 * 60 * 60, 7),
    (float("inf"), 10),
]

SLEEP_REWARD = {
    4: (20, 0),
    5: (35, 0),
    6: (50, 0),
    7: (65, 10),
    8: (80, 15),
    9: (90, 20),
    10: (90, 20),
}
SLEEP_CHECK_HOUR = 7

# Momentum
MAX_MOMENTUM = 100.0
MOMENTUM_GAIN_PER_SECOND = MAX_MOMENTUM / (60 * 60)
MOMENTUM_DECAY_FAST = 4.0 / 60.0
MOMENTUM_DECAY_MEDIUM = 2.0 / 60.0
MOMENTUM_DECAY_SLOW = 0.5 / 60.0
MOMENTUM_FAST_BREAK = 5 * 60
MOMENTUM_MEDIUM_BREAK = 30 * 60

# Achievements
ACHIEVEMENTS = [
    {"key": "steady_start", "name": "STEADY START", "desc": "Провести минимум 3 учебные сессии."},
    {"key": "first_depth", "name": "FIRST DEPTH", "desc": "Набрать 90 минут суммарного фокуса."},
    {"key": "returner", "name": "RETURNER", "desc": "Сделать 3 COMEBACK."},
    {"key": "two_hours", "name": "TWO HOURS", "desc": "Набрать 2 часа суммарного фокуса."},
    {"key": "ten_sessions", "name": "TEN", "desc": "Провести 10 учебных сессий."},
    {"key": "late_bloom", "name": "LATE BLOOM", "desc": "Сделать COMEBACK после перерыва более 1 часа."},
    {"key": "three_hours", "name": "THREE HOURS", "desc": "Набрать 3 часа суммарного фокуса."},
    {"key": "deep_week", "name": "DEEP WEEK", "desc": "Набрать 5 часов суммарного фокуса."},
    {"key": "fifteen_sessions", "name": "FIFTEEN", "desc": "Провести 15 учебных сессий."},
    {"key": "ten_comebacks", "name": "BOOMERANG", "desc": "Сделать 10 COMEBACK."},
    {"key": "level_5", "name": "LVL 5", "desc": "Достичь 5 уровня."},
    {"key": "ten_hours", "name": "TEN HOURS", "desc": "Набрать 10 часов суммарного фокуса."},
    {"key": "twenty_sessions", "name": "TWENTY", "desc": "Провести 20 учебных сессий."},
    {"key": "sleep_cycle", "name": "SLEEP CYCLE", "desc": "Зарегистрировать сон 7 разных дней."},
    {"key": "level_10", "name": "LVL 10", "desc": "Достичь 10 уровня."},
    {"key": "twenty_hours", "name": "TWENTY HOURS", "desc": "Набрать 20 часов суммарного фокуса."},
    {"key": "fifty_sessions", "name": "FIFTY", "desc": "Провести 50 учебных сессий."},
    {"key": "twenty_comebacks", "name": "REBOUND", "desc": "Сделать 20 COMEBACK."},
    {"key": "sleep_month", "name": "SLEEP TRACKED", "desc": "Зарегистрировать сон 30 разных дней."},
    {"key": "level_15", "name": "LVL 15", "desc": "Достичь 15 уровня."},
    {"key": "fifty_hours", "name": "FIFTY HOURS", "desc": "Набрать 50 часов суммарного фокуса."},
    {"key": "hundred_sessions", "name": "CENTURION", "desc": "Провести 100 учебных сессий."},
    {"key": "fifty_comebacks", "name": "PHOENIX", "desc": "Сделать 50 COMEBACK."},
    {"key": "level_20", "name": "LVL 20", "desc": "Достичь 20 уровня."},
    {"key": "hundred_hours", "name": "ONE HUNDRED", "desc": "Набрать 100 часов суммарного фокуса."},
    {"key": "two_hundred_sessions", "name": "UNSTOPPABLE", "desc": "Провести 200 учебных сессий."},
    {"key": "hundred_comebacks", "name": "COMEBACK MACHINE", "desc": "Сделать 100 COMEBACK."},
    {"key": "level_30", "name": "LVL 30", "desc": "Достичь 30 уровня."},
    {"key": "two_hundred_hours", "name": "OBSIDIAN MIND", "desc": "Набрать 200 часов суммарного фокуса."},
    {"key": "level_50", "name": "NEUROSYNC", "desc": "Достичь максимального 50 уровня."},
]


OBSIDIAN_VAULT = Path(_get("OBSIDIAN_VAULT", str(Path.home() / "Obsidian")))

# SoundCloud
CLIENT_ID = _get("SOUNDCLOUD_CLIENT_ID", "")
OAUTH_TOKEN = _get("SOUNDCLOUD_OAUTH_TOKEN", "")

TEMP_TRACK = Path("/tmp/neuro_current.mp3")
NOTES_DIR = os.path.expanduser(_get("NOTES_DIR", "~/NeuroNotes"))
PET_DIR = Path(__file__).parent / "pet"

# ============================================================
# GEMINI / AI
# Единая точка конфигурации ИИ-модуля — используется и в
# llm.py (чат-шторка), и в ai_sorter.py (сортировщик файлов).
# ============================================================

GEMINI_API_KEY = _get("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3.1-flash-lite"
API_VERSION = "v1beta"

GEMINI_DOMAIN = "generativelanguage.googleapis.com"


def gemini_url(model: str = MODEL_NAME) -> str:
    """Собрать URL для generateContent с текущим ключом/версией API."""
    return f"https://{GEMINI_DOMAIN}/{API_VERSION}/models/{model}:generateContent?key={GEMINI_API_KEY}"


# ============================================================
# TELEGRAM-БОТ (bot.py)
# ============================================================
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", "")
# Telegram user id владельца (узнать: написать боту @userinfobot).
# Можно несколько через запятую: "123456,789012"
TELEGRAM_ALLOWED_USER_IDS = {
    int(x) for x in str(_get("TELEGRAM_ALLOWED_USER_IDS", "")).replace(" ", "").split(",") if x.isdigit()
}
METRO_CARD_NUMBER = _get("METRO_CARD_NUMBER", "")


# --- AI SORTER (ai_sorter.py) ---
DEFAULT_IGNORE = ('neuro.txt', '.ds_store', 'desktop.ini', 'thumbs.db')
SORT_CHUNK_SIZE = 15          # файлов на одну пачку в запросе к ИИ
NEURO_LOG_NAME = "neuro.txt"  # имя лог-файла сессии сортировки внутри целевой папки

# ============================================================
# NETWORK / VPN
# openvpn-client@myvpn — системный сервис самого VPN (кнопка WARP
# в шторке включает/выключает и опрашивает именно его). Раньше здесь
# ещё был WARP_PROXIES (SOCKS5 на 127.0.0.1:40000) — но requests.post
# в llm.py его нигде не подключает: трафик и так идёт через VPN
# благодаря полной подмене маршрута (0.0.0.0/1 + 128.0.0.0/1), которую
# openvpn-client@myvpn прописывает на уровне ОС. Прокси был мёртвым
# кодом, убрал во избежание путаницы.
# ============================================================

VPN_SERVICE_NAME = _get("VPN_SERVICE_NAME", "openvpn-client@myvpn")

# Раньше команды гонялись через алиасы vpn-on/vpn-off и `bash -ic`, чтобы
# эти алиасы вообще раскрылись. Но `bash -i` в фоновом процессе без
# настоящего tty ломает job control (это и есть те самые "[1]+ Остановлен"
# в выводе systemctl) и вообще хрупко. Поэтому теперь дергаем systemctl
# напрямую, без алиасов и без интерактивного шелла.
VPN_ON_CMD = ["sudo", "-n", "systemctl", "start", VPN_SERVICE_NAME]
VPN_OFF_CMD = ["sudo", "-n", "systemctl", "stop", VPN_SERVICE_NAME]


def run_vpn_command(cmd: list) -> subprocess.CompletedProcess:
    """
    Выполняет команду включения/выключения VPN и ЖДЁТ результата (в отличие
    от Popen-и-забыть) — чтобы можно было сразу узнать, реально ли сработало,
    и откатить чекбокс, если нет.

    `sudo -n` (non-interactive): если для
    'systemctl start/stop openvpn-client@myvpn' не настроен NOPASSWD в
    sudoers, sudo сразу вернёт ошибку вместо зависания в ожидании пароля,
    которого неоткуда взять в фоновом GUI-процессе.
    """
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def get_vpn_status() -> bool:
    """
    True, если VPN сейчас активен. Проверяется напрямую через
    `systemctl is-active` — это НЕ требует sudo (в отличие от start/stop),
    так что статус можно спокойно опрашивать в фоне хоть каждую секунду.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", VPN_SERVICE_NAME],
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False
