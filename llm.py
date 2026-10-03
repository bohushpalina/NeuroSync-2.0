import sys
import os
import re
import threading
import requests
import logging
import subprocess
import signal
from pathlib import Path
import markdown  # Для парсинга ответов
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel,
    QPushButton, QHBoxLayout, QFrame,
    QTextEdit, QCheckBox
)
from PyQt6.QtCore import Qt, QTimer, QRect, pyqtSignal, QObject
from PyQt6.QtGui import QCursor, QShortcut, QKeySequence

# Импортируем функцию сортировки из вашего обновленного файла ai_sorter.py
from ai_sorter import ai_sort_folder
from obs_manager import obsidian_split, obsidian_find, obsidian_overview, obsidian_branch_overview, obsidian_define, obsidian_repair_duplicates
from indexer import get_recent_files, get_by_name
import socket
import requests.packages.urllib3.util.connection as urllib3_cn
from urllib3.connection import HTTPConnection

import ns_config

# Флаг отключения шторки от внешнего переключателя (neuro_shutter_switch.py).
# Файла нет -> шторка открывается по наведению как раньше (совместимость,
# если переключатель вообще не запущен). Файл есть -> наведение на край
# больше НЕ открывает шторку. На сам процесс/фоновую работу это не влияет —
# только на вызов open_panel() внутри hover_check().
SHUTTER_DISABLED_FLAG = Path("/tmp/neuro_shutter_llm_disabled")

# Принудительно заставляем requests использовать только IPv4
def allowed_gai_family():
    return socket.AF_INET

urllib3_cn.allowed_gai_family = allowed_gai_family

# === VPN ТОЛЬКО ДЛЯ ЭТОГО ВИДЖЕТА ===
# OpenVPN поднимается без системных маршрутов (route-nopull), поэтому весь
# остальной трафик системы идёт мимо него. Все HTTP-соединения ЭТОГО процесса
# (requests/urllib3 — включая ai_sorter, obs_manager, indexer) привязываются
# к интерфейсу VPN через SO_BINDTODEVICE. Список default_socket_options
# общий для всех соединений urllib3, поэтому дополняем его "на месте".
# Имя интерфейса должно совпадать с `dev` в конфиге OpenVPN.
VPN_IFACE = getattr(ns_config, "VPN_IFACE", "vpn0")
_bind_opt = (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, VPN_IFACE.encode())
if _bind_opt not in HTTPConnection.default_socket_options:
    HTTPConnection.default_socket_options.append(_bind_opt)

# === КОНФИГУРАЦИЯ ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
os.environ["QT_QPA_PLATFORM"] = "xcb"

if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

class WorkerSignals(QObject):
    response_received = pyqtSignal(str)


def _normalize_intent_text(text: str) -> str:
    """
    Модель иногда копирует markdown-оформление из примеров в системной
    инструкции (обратные кавычки вокруг `INTENT_...`, тройной код-блок,
    вводную фразу перед командой) буквально в свой ответ. Из-за этого
    строгая проверка ai_text.startswith("INTENT_...") не срабатывает,
    служебная команда проваливается в обычный разговорный сценарий и
    просто показывается пользователю как есть — а нужное действие (сортировка,
    obsidian_split и т.п.) вообще не выполняется. Здесь мы снимаем типичные
    обёртки, прежде чем сверять текст с ожидаемыми префиксами.
    """
    stripped = text.strip()

    # Тройной код-блок ```...``` (с возможной меткой языка на первой строке).
    stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
    stripped = re.sub(r"\n?```$", "", stripped).strip()

    # Одиночные обратные кавычки вокруг всего ответа: `INTENT_...: ...`
    if stripped.startswith("`") and stripped.endswith("`") and len(stripped) > 1:
        stripped = stripped[1:-1].strip()

    # Если INTENT_ встречается не в самом начале, но всё, что стоит перед
    # ним — это только "мусор" форматирования (пробелы, звёздочки, тире,
    # двоеточия, обратные кавычки), а не содержательный текст, — подрезаем
    # ответ до самого INTENT_. Если перед INTENT_ есть настоящие слова
    # (например, живой разговорный ответ, где модель просто упомянула
    # префикс), НЕ трогаем текст — это не ложное совпадение.
    match = re.search(r"INTENT_[A-Z_]+:?", stripped)
    if match and match.start() > 0:
        prefix_junk = stripped[: match.start()]
        if not prefix_junk.strip(" \n\t*-:>`"):
            stripped = stripped[match.start():]

    return stripped.strip()

class GrowingInput(QTextEdit):
    returnPressed = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Input")
        self.setPlaceholderText("ENTER SOMETHING...")
        self.setFixedHeight(35)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.textChanged.connect(self.adjust_height)

    def adjust_height(self):
        doc = self.document()
        h = doc.documentLayout().documentSize().height() + 10
        new_h = max(35, min(h, 120))
        self.setFixedHeight(int(new_h))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if h > 120 else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.returnPressed.emit()
        else:
            super().keyPressEvent(event)

STYLE = """
QWidget {
    background: transparent;
    color: #e7dcff;
    font-family: 'MonoCraft', 'DejaVu Sans Mono', 'Consolas';
    font-size: 10px;
}

#MainFrame {
    background-color: rgba(30, 30, 34, 195);
    color: #e7dcff;
    border: 1px solid #5a5a62;
    border-right: none;
    border-top-left-radius: 2px;
    border-bottom-left-radius: 2px;
}

QTextEdit#History, #Input {
    background-color: rgb(6, 5, 10);
    color: #f2f2f5;
    border: 1px solid #47474f;
    border-radius: 1px;
    padding: 4px;
    selection-background-color: #9f5fb8;
    selection-color: #f0eef2;
}

QTextEdit#History:focus, #Input:focus {
    border: 1px solid #b83cff;
}

code { color: #00e5ff; background-color: rgba(0, 229, 255, 20); border-radius: 1px; }
pre { background-color: rgb(6, 5, 10); padding: 5px; border: 1px solid #47474f; border-radius: 1px; }

QPushButton {
    background-color: rgb(17, 8, 25);
    color: #cf62ff;
    border: 1px solid #9b30d1;
    border-radius: 1px;
    font-family: 'MonoCraft', 'DejaVu Sans Mono', 'Consolas';
    font-size: 9px;
    font-weight: bold;
    padding: 4px 6px;
}

QPushButton:hover {
    background-color: #9f5fb8;
    color: #f0eef2;
    border-color: #e8a6ff;
}

QPushButton:pressed {
    background-color: #7e1caf;
    color: #f2f2f5;
    border-color: #b83cff;
}

QPushButton:disabled {
    background-color: rgb(10, 8, 14);
    color: #604a70;
    border-color: #321b3d;
}

QCheckBox { 
    color: #cf62ff; 
    spacing: 6px; 
    font-family: 'MonoCraft', 'DejaVu Sans Mono', 'Consolas';
    font-size: 9px;
    font-weight: bold;
}
QCheckBox::indicator {
    width: 12px;
    height: 12px;
    border-radius: 1px;
    background: rgb(6, 5, 10);
    border: 1px solid #47474f;
}
QCheckBox::indicator:checked { 
    background: #9f5fb8;
    border-color: #b83cff;
}

QScrollBar:vertical {
    background: #08050d;
    width: 8px;
    margin: 0px;
    border: none;
}
QScrollBar::handle:vertical {
    background: #4a215e;
    min-height: 20px;
    border: 1px solid #6f2a8d;
    border-radius: 0px;
}
QScrollBar::handle:vertical:hover {
    background: #9b30d1;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #08050d; }
"""

class NeuroAiSidePanel(QWidget):
    def __init__(self):
        super().__init__()
        self.panel_width = 400
        self.panel_height = 650
        
        self.conversation_history = []

        # NORMAL = обычная Gemini, MANAGER = файлы, OBSIDIAN = заметки Obsidian
        self.ai_mode = "normal"

        screen_geo = QApplication.primaryScreen().availableGeometry()
        self.screen_x = screen_geo.x()
        self.screen_y = screen_geo.y()
        self.screen_w = screen_geo.width()
        self.screen_h = screen_geo.height()

        self.is_open = False
        self.pin_panel = False
        self.y_pos = self.screen_y + self.screen_h - self.panel_height - 50

        # Шторка прикреплена к ЛЕВОМУ краю.
        # Не пытаемся уводить top-level окно в отрицательные координаты:
        # X11/оконный менеджер может автоматически вернуть его в экран.
        # Вместо этого в закрытом состоянии само окно имеет ширину 10 px,
        # а в открытом — полную ширину панели. Поэтому "ручка" реально
        # остаётся видимой слева и панель не зависает полностью открытой.
        self.left_edge = self.screen_x
        self.edge_handle = -10

        self.hidden_rect = QRect(
            self.left_edge,
            self.y_pos,
            self.edge_handle,
            self.panel_height,
        )
        self.shown_rect = QRect(
            self.left_edge,
            self.y_pos,
            self.panel_width,
            self.panel_height,
        )


        self.signals = WorkerSignals()
        self.signals.response_received.connect(self.display_response)
        
        self.init_ui()
        self.set_ai_mode("normal", announce=False)
        self.setGeometry(self.hidden_rect)
        self.show()

        self.hover_timer = QTimer(self)
        self.hover_timer.timeout.connect(self.hover_check) 
        self.hover_timer.start(50)

        # Флаг читаем не на каждый тик hover_timer (50 мс), а раз в 500 мс —
        # это лишь разрешение на открытие по наведению, лишний I/O тут не нужен.
        self.shutter_enabled = not SHUTTER_DISABLED_FLAG.exists()
        self.shutter_flag_timer = QTimer(self)
        self.shutter_flag_timer.timeout.connect(self._refresh_shutter_enabled)
        self.shutter_flag_timer.start(500)

        # Подтягиваем реальное состояние VPN сразу при старте и дальше
        # раз в 5 секунд — если VPN включили/выключили из терминала (или
        # через vpn-on/vpn-off), чекбокс WARP это тоже увидит.
        self._sync_vpn_checkbox()
        self.vpn_status_timer = QTimer(self)
        self.vpn_status_timer.timeout.connect(self._sync_vpn_checkbox)
        self.vpn_status_timer.start(5000)

    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("MainFrame")
        self.main_frame.setFixedSize(self.panel_width, self.panel_height)
        
        layout = QVBoxLayout(self.main_frame)
        layout.setContentsMargins(15, 20, 15, 20)

        header = QLabel("── NEURAL_GPT ──")
        top_bar = QHBoxLayout()

        self.clear_btn = QPushButton("CLEAR")
        self.clear_btn.setFixedSize(60, 22) 
        self.clear_btn.clicked.connect(self.clear_history)
        top_bar.addWidget(self.clear_btn)

        top_bar.addStretch()

        # Переключение режимов — ТОЛЬКО кнопками, никакого разбора текста.
        self.mode_buttons = {}
        mode_button_specs = (
            ("normal", "AI"),
            ("manager", "FILES"),
            ("obsidian", "OBSIDIAN"),
        )
        for mode_key, mode_text in mode_button_specs:
            btn = QPushButton(mode_text)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.clicked.connect(lambda _checked, m=mode_key: self.set_ai_mode(m))
            top_bar.addWidget(btn)
            self.mode_buttons[mode_key] = btn

        top_bar.addStretch()
        self.warp_switch = QCheckBox("WARP")
        self.warp_switch.toggled.connect(self.toggle_warp)
        top_bar.addWidget(self.warp_switch)
        layout.addLayout(top_bar)
        
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet("color: #b77ad9; font-weight: bold; font-size: 10px;")
        layout.addWidget(header)

        self.chat_history = QTextEdit()
        self.chat_history.setObjectName("History")
        self.chat_history.setReadOnly(True)
        layout.addWidget(self.chat_history)

        input_container = QHBoxLayout()
        self.user_input = GrowingInput()
        self.user_input.returnPressed.connect(self.send_query)
        
        self.send_btn = QPushButton("SEND")
        self.send_btn.setFixedSize(60, 35)
        self.send_btn.clicked.connect(self.send_query)
        
        input_container.addWidget(self.user_input)
        input_container.addWidget(self.send_btn)
        layout.addLayout(input_container)

        self.setStyleSheet(STYLE)
        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close_panel)

    def _refresh_shutter_enabled(self):
        self.shutter_enabled = not SHUTTER_DISABLED_FLAG.exists()

    def hover_check(self):
        pos = QCursor.pos()
        panel_right = self.left_edge + self.panel_width
        in_y = self.y_pos <= pos.y() <= self.y_pos + self.panel_height

        # В закрытом состоянии top-level окно имеет ширину 10 px у левого края.
        # Наведение на этот край открывает шторку — но только если шторка не
        # отключена переключателем (self.shutter_enabled). Уже открытую
        # панель это не трогает: закрывается она всегда штатно, по уходу
        # курсора, независимо от состояния переключателя.
        if not self.is_open:
            if self.shutter_enabled and pos.x() <= self.left_edge + self.edge_handle and in_y:
                self.open_panel()
        else:
            # Пока курсор внутри панели — держим её открытой.
            # После ухода курсора вправо за пределы панели с запасом
            # шторка снова прячется.
            margin = 150 if self.user_input.hasFocus() else 80
            if pos.x() > panel_right + margin or not in_y:
                self.close_panel()

    def open_panel(self):
        if not self.is_open:
            self.is_open = True
            # Раскрываем top-level окно с 10 px до полной ширины.
            self.setGeometry(self.shown_rect)
            self.activateWindow()
            self.user_input.setFocus()

    def close_panel(self):
        if self.is_open:
            self.is_open = False
            self.user_input.clearFocus()
            # Сжимаем top-level окно до 10 px у ЛЕВОЙ границы.
            # Это надёжнее, чем отрицательная X-координата: оконный менеджер
            # не сможет принудительно вернуть панель обратно в экран.
            self.setGeometry(self.hidden_rect)
    
    def clear_history(self):
        self.conversation_history.clear()
        self.chat_history.clear()
        self.chat_history.append("<span style='color:#7e1caf;'>[SYSTEM]: MEMORY_PURGED...</span>")

    def toggle_warp(self, checked):
        try:
            cmd = ns_config.VPN_ON_CMD if checked else ns_config.VPN_OFF_CMD
            result = ns_config.run_vpn_command(cmd)

            if result.returncode != 0:
                error_text = (result.stderr or result.stdout or "").strip()
                if "password" in error_text.lower() or "a password is required" in error_text.lower():
                    error_text = (
                        "sudo просит пароль — для systemctl start/stop "
                        f"{ns_config.VPN_SERVICE_NAME} нужен NOPASSWD в sudoers."
                    )
                raise RuntimeError(error_text or f"systemctl вернул код {result.returncode}")

            if checked:
                self.chat_history.append("<span style='color:#00e5ff;'>[SYSTEM]: WARP CONNECTED</span>")
            else:
                self.chat_history.append("<span style='color:#b77ad9;'>[SYSTEM]: WARP DISCONNECTED</span>")
        except Exception as e:
            self.chat_history.append(f"<span style='color:red;'>[ERROR]: {str(e)}</span>")
            # Команда не сработала — откатываем чекбокс к РЕАЛЬНОМУ состоянию VPN,
            # а не оставляем его во "вранье".
            self._sync_vpn_checkbox()

    def _sync_vpn_checkbox(self):
        """
        Подтягивает текущее состояние VPN (systemctl is-active) и молча
        (без повторного вызова toggle_warp) выставляет чекбокс. Используется
        и при старте, и по таймеру, и после неудачной попытки переключения.
        """
        is_on = ns_config.get_vpn_status()

        if self.warp_switch.isChecked() == is_on:
            return  # состояние не поменялось — лога не пишем, чтобы не спамить

        self.warp_switch.blockSignals(True)
        self.warp_switch.setChecked(is_on)
        self.warp_switch.blockSignals(False)

    def set_ai_mode(self, mode: str, announce: bool = True) -> None:
        mode = mode.strip().lower()
        if mode not in {"normal", "manager", "obsidian"}:
            return

        self.ai_mode = mode
        labels = {
            "normal": "MODE: AI",
            "manager": "MODE: FILES",
            "obsidian": "MODE: OBSIDIAN",
        }
        colors = {
            "normal": "#b77ad9",
            "manager": "#00e5ff",
            "obsidian": "#ff8cc6",
        }

        if hasattr(self, "mode_buttons"):
            for key, btn in self.mode_buttons.items():
                is_active = key == mode
                btn.blockSignals(True)
                btn.setChecked(is_active)
                btn.setStyleSheet(
                    ns_config.button_style(color=colors[key], filled=is_active, font_size=9, padding="2px 8px")
                )
                btn.blockSignals(False)

        if announce:
            self.chat_history.append(
                f"<span style='color:{colors[mode]};'>[SYSTEM]: {labels[mode]}</span>"
            )

    def send_query(self):
        text = self.user_input.toPlainText().strip()
        if not text: return
        
        self.chat_history.append(f"<b style='color:#cf62ff;'>[YOU]:</b> {text}")
        self.user_input.clear()
        self.send_btn.setEnabled(False)
        
        # Передаем управление интеллектуальному роутеру в фоновом потоке
        threading.Thread(target=self.intelligent_router, args=(text,), daemon=True).start()

    def intelligent_router(self, user_prompt):
        url = ns_config.gemini_url()
        headers = {'Content-Type': 'application/json'}
        
        mode_intro = {
            "normal": (
                "Ты — Neural GPT, живой дружелюбный ИИ-собеседник с характером внутри NeuroOS. "
                "Сейчас включён режим AI: приоритет — обычное человеческое общение. Отвечай тепло, "
                "неформально, с личностью и, где уместно, с лёгким юмором. НИКОГДА не представляйся "
                "'файловым менеджером' или 'системным модулем' и не начинай ответ с сухого описания "
                "своих функций, если тебя не спросили об этом напрямую."
            ),
            "manager": (
                "Ты — Neural GPT в режиме FILES: помогаешь искать, сортировать и наводить порядок "
                "в файловой системе NeuroOS. Дружелюбно, но по делу."
            ),
            "obsidian": (
                "Ты — Neural GPT в режиме OBSIDIAN: помогаешь с заметками и структурой Obsidian-"
                "хранилища. Дружелюбно, но по делу."
            ),
        }

        # ЖЁСТКОЕ РАЗГРАНИЧЕНИЕ РЕЖИМОВ.
        # Раньше здесь было "режим — это просто приоритет, а не запрет", из-за
        # чего в режиме OBSIDIAN команда вроде "разгреби ветку X" (где "разгрести"
        # используется и как триггер INTENT_SORT в сценарии 1) улетала в
        # файловый менеджер и пыталась искать несуществующую папку ~/X, вместо
        # того чтобы раздробить Obsidian-ветку X. Теперь набор доступных
        # интентов физически ограничивается активным режимом на уровне промпта
        # (ниже) и дополнительно перепроверяется в коде после ответа Gemini
        # (см. MODE_ALLOWED_INTENT_PREFIXES).
        mode_separation = {
            "normal": (
                "Сейчас режим AI: тебе доступны И файловые сценарии (сортировка/поиск), И "
                "Obsidian-сценарии одновременно — выбирай нужный по смыслу запроса."
            ),
            "manager": (
                "ЖЁСТКОЕ ПРАВИЛО РЕЖИМА: сейчас активен ТОЛЬКО режим FILES. Тебе ЗАПРЕЩЕНО "
                "отвечать любым `INTENT_OBSIDIAN_*`, даже если в запросе есть слова 'заметка', "
                "'обсидиан', 'ветка', 'тема', '.md' — в этом режиме 'ветка'/'разгрести'/'почистить' "
                "означают папку и файлы на диске, а не Obsidian-заметки. Если запрос однозначно "
                "про заметки/Obsidian, а не про файлы на диске, НЕ выполняй его как файловый "
                "сценарий — ответь как в пункте БЕСЕДА и мягко подскажи переключиться на кнопку "
                "OBSIDIAN."
            ),
            "obsidian": (
                "ЖЁСТКОЕ ПРАВИЛО РЕЖИМА: сейчас активен ТОЛЬКО режим OBSIDIAN. Тебе ЗАПРЕЩЕНО "
                "отвечать `INTENT_SORT` или `INTENT_FIND` (файловый менеджер), даже если в запросе "
                "есть слова 'разгреби', 'убери', 'наведи порядок', 'почисти', 'обработай' — в этом "
                "режиме такие слова относятся к заметкам/веткам/файлам Obsidian, а НЕ к папкам на "
                "диске, даже если название темы похоже на путь (например 'Computer network' здесь "
                "почти всегда значит .md-заметку или логическую ветку ссылок, а не директорию "
                "~/Computer network). Если пользователь просит 'разгрести/раздробить/обработать "
                "всю ветку <X>' — сопоставь <X> с корневым Obsidian-файлом этой ветки и ответь "
                "`INTENT_OBSIDIAN_SPLIT: <корневой файл ветки>` (сценарий 3): дробилка сама "
                "рекурсивно пройдёт по ВСЕМ файлам, связанным ссылками из этого корня, отдельно "
                "перечислять каждый файл ветки не нужно. Если запрос однозначно про настоящую "
                "папку на диске, а не про заметки, НЕ выполняй его как Obsidian-сценарий — ответь "
                "как в пункте БЕСЕДА и подскажи переключиться на кнопку FILES."
            ),
        }

        sort_find_block = (
            "1. СЦЕНАРИЙ СORTИРОВКИ:\n"
            "Если пользователь просит отсортировать, разгрести, убрать файлы или навести порядок в какой-либо папке "
            "(например: 'разгреби загрузки', 'уберись на рабочем столе', 'отсортируй папку Право'), "
            "ты ОБЯЗАН ответить СТРОГО в формате: INTENT_SORT: <путь_к_папке>. "
            "КРИТИЧЕСКИ ВАЖНО: НЕ ПЕРЕВОДИ русские названия папок на английский язык! Формируй путь точно из слов пользователя "
            "с добавлением домашней директории (например: 'загрузки' -> '~/Загрузки', 'рабочий стол' -> '~/Рабочий стол'). "
            "БЕЗУСЛОВНОЕ ПРАВИЛО ИСКЛЮЧЕНИЙ: Если пользователь просит проигнорировать, не трогать или оставить определённые файлы "
            "внутри этой папки, ты ВСЁ РАВНО возвращаешь только базовую команду вида `INTENT_SORT: <путь_к_папке>`. "
            "Ни в коем случае не подмешивай файлы-исключения, условия или списки файлов в строку формата INTENT_SORT.\n\n"
            "2. СЦЕНАРИЙ ПОИСКА (ОБЫЧНЫЙ):\n"
            "Если пользователь спрашивает, где находится файл, куда улетел файл или просит найти файл по описанию/имени в конкретной директории "
            "(например: 'где в папке право лабораторная по уголовке', 'куда делся отчет в ~/Downloads', 'найди файл по физике в загрузках'), "
            "ты должен ответить СТРОГО в формате: `INTENT_FIND: <путь_к_папке> | <поисковый_запрос_или_описание>`.\n"
            "Пример: Пользователь: 'где в Право лежит иск к банку?'. Ответ: `INTENT_FIND: ~/Право | иск к банку`.\n"
            "Опять же, пути к стандартным папкам пиши как '~/Загрузки', '~/Рабочий стол', '~/Документы'."
        )

        obsidian_block = (
            "3. СЦЕНАРИЙ OBSIDIAN — РАЗДРОБИТЬ:\n"
            "Используй, когда пользователь явно просит раздробить/разбить/декомпозировать/"
            "переструктурировать файл или ЦЕЛУЮ ВЕТКУ на отдельные атомарные темы (например: "
            "'раздроби лекцию на темы', 'разбей файл по атомарным', 'декомпозируй конспект', "
            "'разгреби всю ветку Компьютерные сети', 'обработай ветку X и все ссылки из неё'). "
            "НЕ используй этот сценарий, если пользователь просто просит ОБЪЯСНИТЬ, ПЕРЕСКАЗАТЬ, "
            "ДАТЬ СПИСОК определений или РАССКАЗАТЬ содержимое файла — для этого есть сценарий 4 (НАЙТИ).\n"
            "ВАЖНО (КРИТИЧЕСКИ): в ответе может быть ТОЛЬКО ОДИН интент одного типа. НИКОГДА не "
            "смешивай два разных INTENT_ в одном ответе и не подставляй текст одного INTENT_ внутрь "
            "другого как будто это имя файла.\n"
            "Ответь СТРОГО: `INTENT_OBSIDIAN_SPLIT: <файл>`.\n"
            "В <файл> передай название или путь к нужному Markdown-файлу. Не переводить название файла.\n"
            "Пример: 'раздроби в Obsidian лекцию по сетям и все связанные с ней файлы' -> "
            "`INTENT_OBSIDIAN_SPLIT: Лекция по сетям.md`.\n"
            "ЕСЛИ пользователь просит 'разгрести/раздробить/обработать ВСЮ ВЕТКУ <X>' целиком (а не "
            "перечисляет конкретные отдельные файлы) — укажи ОДИН корневой файл этой ветки, "
            "`INTENT_OBSIDIAN_SPLIT: <корневой файл ветки X>`. Не нужно перечислять все файлы ветки "
            "вручную через ' || ' — дробилка сама рекурсивно обойдёт все связанные ссылками файлы "
            "от указанного корня.\n"
            "ЕСЛИ пользователь просит обработать НЕСКОЛЬКО РАЗНЫХ, независимых файлов сразу (например "
            "'лекцию 2 и лекцию 3', 'обработай эти два файла'), перечисли ВСЕ файлы в ОДНОМ ответе через "
            "разделитель ' || ' (пробел, две вертикальные черты, пробел), не задавай уточняющих вопросов "
            "и не выбирай только один файл: "
            "`INTENT_OBSIDIAN_SPLIT: <файл1> || <файл2> || <файл3>`. "
            "Каждый файл будет обработан отдельно и по очереди, один за другим. "
            "В список через ' || ' попадают ТОЛЬКО реальные имена файлов — никогда не подставляй туда "
            "другую команду INTENT_ или текст запроса.\n"
            "Пример: 'переделай лекцию 2 и лекцию 3 субд' -> "
            "`INTENT_OBSIDIAN_SPLIT: Лекция 2 СУБД.md || Лекция 3 СУБД.md`.\n\n"
            "4. СЦЕНАРИЙ OBSIDIAN — НАЙТИ / ОБЪЯСНИТЬ СОДЕРЖИМОЕ:\n"
            "Используй этот сценарий, когда пользователь просит найти в документе ссылки на тему, "
            "ОБЪЯСНИТЬ/ПЕРЕСКАЗАТЬ содержимое, ДАТЬ СПИСОК определений/понятий из файла и т.п. "
            "(например: 'поясни за лекцию 2 все основные определения', 'что там про наследование', "
            "'расскажи что в этом файле по теме нормализации').\n"
            "Ответь СТРОГО: `INTENT_OBSIDIAN_FIND: <файл> | <что найти>`.\n"
            "Пример: 'в лекции по ООП найди ссылку на наследование в Obsidian' -> "
            "`INTENT_OBSIDIAN_FIND: Лекция по ООП.md | наследование`.\n"
            "Пример: 'поясни мне за лекцию 2 тад все основные определения там какие' -> "
            "`INTENT_OBSIDIAN_FIND: Лекция 2 ТАД.md | основные определения`.\n"
            "Если путь файла дан пользователем, сохрани его максимально точно.\n\n"
            "5. СЦЕНАРИЙ OBSIDIAN — ОБЗОР ХРАНИЛИЩА И ВЕТОК:\n"
            "Если пользователь упоминает Obsidian, либо предыдущий диалог уже был про Obsidian, "
            "и просит рассказать, что лежит в хранилище, показать структуру, ветки, разделы, "
            "сколько всего лежит в ветке, какие материалы входят в ветку, что изучается в ветке "
            "или сделать краткий обзор содержимого (БЕЗ дробления файлов), НЕ используй INTENT_FIND.\n"
            "Для общего обзора ответь СТРОГО: `INTENT_OBSIDIAN_OVERVIEW`.\n"
            "Для обзора конкретной ветки ответь СТРОГО: `INTENT_OBSIDIAN_BRANCH: <запрос_ветки>`.\n"
            "К запросам ветки относятся фразы вроде: 'в ветке по СУБД сколько всего лежит', "
            "'покажи ветку СУБД', 'что есть в разделе модели данных', 'сколько материалов в этой ветке'.\n"
            "Критически важно: ветки в этом Obsidian могут быть ЛОГИЧЕСКИМИ, образованными Markdown-ссылками, "
            "даже если все файлы физически находятся в корне. Не требуй наличия отдельных папок.\n"
            "Если текущая реплика является продолжением предыдущего Obsidian-обзора и содержит "
            "'эта ветка', 'ветка', 'раздел', 'там', 'в ней' и вопрос о количестве/содержимом, "
            "обрабатывай её как `INTENT_OBSIDIAN_BRANCH`.\n"
            "Для этого интента никакой файл у пользователя спрашивать не нужно.\n\n"
            "7. СЦЕНАРИЙ OBSIDIAN — ДАТЬ ОПРЕДЕЛЕНИЕ ПОНЯТИЯ:\n"
            "Если пользователь просит дать/найти/напомнить определение ОДНОГО конкретного термина или понятия "
            "именно из Obsidian/конспектов (например: 'дай определение СУБД из обсидиана', "
            "'что такое инкапсуляция по конспекту', 'напомни определение целостности данных', "
            "'какое там было определение у нормальной формы'), ответь СТРОГО: `INTENT_OBSIDIAN_DEFINE: <термин>`.\n"
            "В <термин> передай ТОЛЬКО само понятие в исходной форме, без слов "
            "'дай определение', 'из обсидиана', 'что такое', 'по конспекту' и т.п.\n"
            "Пример: 'дай определение СУБД из обсидиана' -> `INTENT_OBSIDIAN_DEFINE: СУБД`.\n"
            "Если пользователь просит СПИСОК/ВСЕ определений из целого файла (а не одно конкретное понятие) — "
            "это сценарий 4 (НАЙТИ), а не этот.\n\n"
            "9. СЦЕНАРИЙ OBSIDIAN — ПОЧИНИТЬ ДУБЛИ АТОМАРНЫХ ТЕМ:\n"
            "Если пользователь жалуется на задвоенные/повторяющиеся темы, дубликаты атомарных файлов, "
            "или просит почистить/починить Obsidian после сбоя дробления, ответь СТРОГО: "
            "`INTENT_OBSIDIAN_REPAIR_DUPES`. Аргументы не нужны."
        )

        conversation_block = (
            "8. БЕСЕДА:\n"
            "Если это обычный вопрос, реплика, шутка, приветствие, или запрос из чужого домена, "
            "недоступного в текущем режиме — отвечай как живой дружелюбный собеседник с характером: "
            "тепло, неформально, по существу вопроса. НЕ представляйся файловым менеджером/модулем "
            "NeuroOS и не перечисляй свои функции, если не спросили."
        )

        scenario_blocks = []
        if self.ai_mode in ("manager", "normal"):
            scenario_blocks.append(sort_find_block)
        if self.ai_mode in ("obsidian", "normal"):
            scenario_blocks.append(obsidian_block)
        scenario_blocks.append(conversation_block)

        system_instruction = (
            mode_intro.get(self.ai_mode, mode_intro["normal"]) + "\n\n"
            + mode_separation.get(self.ai_mode, mode_separation["normal"]) + "\n\n"
            "Переключение самого режима происходит только кнопками в интерфейсе, а не текстом — "
            "если пользователь просит сменить режим словами, просто объясни, что для этого есть "
            "кнопки AI / FILES / OBSIDIAN.\n\n"
            + "\n\n".join(scenario_blocks)
        )
        
        temp_history = list(self.conversation_history)
        temp_history.append({"role": "user", "parts": [{"text": user_prompt}]})
        
        payload = {
            "contents": temp_history,
            "systemInstruction": {"parts": [{"text": system_instruction}]}
        }
        
        # Раньше при упавшем VPN запрос всё равно улетал и падал с непонятным
        # UPLINK_CRASH [400]: User location is not supported. На деле это VPN
        # (openvpn-client@myvpn) сам отвалился по AUTH_FAILED за несколько
        # секунд до этого — видно даже в обычном терминале, без приложения.
        # Проверяем реальное состояние VPN ДО запроса, чтобы сразу сказать
        # честно, а не тратить 30 секунд на гарантированно проваленный запрос.
        if not ns_config.get_vpn_status():
            self.signals.response_received.emit(
                "<span style='color:red;'>[SYSTEM]: VPN сейчас не поднят "
                f"({ns_config.VPN_SERVICE_NAME} неактивен) — запрос не отправляю, "
                "иначе Gemini вернёт 400 (регион не поддерживается). "
                "Если ты только что включал VPN и он тут же погас — это похоже на "
                "AUTH_FAILED от самого VPN-сервера (проверь `journalctl -u "
                f"{ns_config.VPN_SERVICE_NAME}`), а не баг шторки. Переподключи VPN и повтори.</span>"
            )
            return

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            data = response.json()
            
            if response.status_code == 200 and 'candidates' in data and data['candidates']:
                ai_text = data['candidates'][0]['content']['parts'][0]['text'].strip()
                ai_text = _normalize_intent_text(ai_text)

                # Второй, независимый от промпта уровень защиты (defense in
                # depth): даже если Gemini всё-таки ошибётся с классификацией
                # (как в баге, где INTENT_SORT сработал в режиме OBSIDIAN),
                # чужой для текущего режима интент здесь физически не
                # исполняется — код молча не даёт файловому менеджеру полезть
                # в Obsidian-режим и наоборот.
                MODE_ALLOWED_INTENT_PREFIXES = {
                    "manager": ("INTENT_SORT:", "INTENT_FIND:"),
                    "obsidian": (
                        "INTENT_OBSIDIAN_SPLIT:",
                        "INTENT_OBSIDIAN_FIND:",
                        "INTENT_OBSIDIAN_OVERVIEW",
                        "INTENT_OBSIDIAN_BRANCH:",
                        "INTENT_OBSIDIAN_DEFINE:",
                        "INTENT_OBSIDIAN_REPAIR_DUPES",
                    ),
                }

                allowed_prefixes = MODE_ALLOWED_INTENT_PREFIXES.get(self.ai_mode)
                if (
                    allowed_prefixes is not None
                    and ai_text.startswith("INTENT_")
                    and not ai_text.startswith(allowed_prefixes)
                ):
                    wrong_mode_label = "OBSIDIAN" if self.ai_mode == "manager" else "FILES"
                    current_mode_label = "FILES" if self.ai_mode == "manager" else "OBSIDIAN"
                    logging.warning(
                        "Заблокирован чужой для режима %s интент: %r",
                        self.ai_mode, ai_text,
                    )
                    ai_text = (
                        f"Похоже, этот запрос — команда для режима {wrong_mode_label}, а сейчас "
                        f"активен режим {current_mode_label}, так что я её не выполняю. "
                        "Переключи режим кнопкой сверху или переформулируй запрос в терминах "
                        f"текущего режима {current_mode_label}."
                    )

                if ai_text.startswith("INTENT_SORT:"):
                    target_folder = ai_text.replace("INTENT_SORT:", "").strip()
                    self.signals.response_received.emit(f"<i style='color:#b77ad9;'>[SYSTEM]: Инициализация разбора директории `{target_folder}`...</i>")
                    
                    # Вызов вашей ИИ-сортировки из ai_sorter.py — с живыми логами по пачкам
                    raw_sort_report = ai_sort_folder(target_folder, progress_callback=self._emit_progress)
                    
                    # Вторая итерация: Просим ЛЛМ сжать огромный технический лог в краткий красивый отчет
                    summary_prompt = (
                        f"Пользователь дал команду на сортировку папки '{target_folder}'. Операция успешно завершена.\n"
                        f"Вот технический лог работы утилиты:\n{raw_sort_report}\n\n"
                        f"Сгенерируй для пользователя очень красивый, краткий, емкий и понятный отчет о проделанной работе. "
                        f"Укажи общие результаты (сколько файлов перемещено, в какие категории) и оформи ключевые данные лаконичным списком."
                    )
                    
                    summary_payload = {
                        "contents": [{"role": "user", "parts": [{"text": summary_prompt}]}],
                        "systemInstruction": {"parts": [{"text": "Ты лаконичный и стильный системный ассистент NeuroOS."}]}
                    }
                    
                    res_sum = requests.post(url, json=summary_payload, headers=headers, timeout=30)
                    data_sum = res_sum.json()
                    
                    if res_sum.status_code == 200 and 'candidates' in data_sum and data_sum['candidates']:
                        final_report = data_sum['candidates'][0]['content']['parts'][0]['text']
                    else:
                        final_report = f"Сортировка выполнена успешно!\n\nСырой лог системы:\n{raw_sort_report}"
                    
                    # Фиксируем результат в истории общения
                    self.conversation_history.append({"role": "user", "parts": [{"text": user_prompt}]})
                    self.conversation_history.append({"role": "model", "parts": [{"text": final_report}]})
                    
                    self.signals.response_received.emit(final_report)
                
                # ================= СЦЕНАРИЙ Б: УМНЫЙ ПОИСК ФАЙЛА (ДОБАВЛЕНО) =================
                elif ai_text.startswith("INTENT_FIND:"):
                    self.set_ai_mode("manager", announce=False)
                    content = ai_text.replace("INTENT_FIND:", "").strip()
                    parts = content.split("|")
                    
                    target_folder_str = parts[0].strip()
                    search_query = parts[1].strip() if len(parts) > 1 else ""
                    
                    self.signals.response_received.emit(f"<i style='color:#b77ad9;'>[SYSTEM]: Сканирование индексов и neuro.txt в `{target_folder_str}` по запросу '{search_query}'...</i>")
                    
                    target_dir = Path(target_folder_str).expanduser().resolve()
                    neuro_log_content = ""
                    
                    neuro_txt_path = target_dir / "neuro.txt"
                    if neuro_txt_path.exists():
                        try:
                            with open(neuro_txt_path, "r", encoding="utf-8") as f:
                                neuro_log_content = f.read()
                        except Exception as e:
                            print(f"[ERROR] Не удалось прочесть neuro.txt: {e}")
                    
                    # МОДЕРНИЗИРОВАННЫЙ АССОЦИАТИВНЫЙ АНАЛИЗ ЛОГОВ ИИ
                    analysis_prompt = f"""
                    Пользователь ищет конкретные файлы в директории '{target_folder_str}'.
                    Его поисковый запрос/описание: "{search_query}"
                    
                    ВНИМАНИЕ: Названия искомых файлов могут НЕ содержать напрямую слова из запроса (например, файлы выкроек могут называться 'pattern_dress.pdf', 'skirt_lecalo.zip', иметь в названии бренды вроде 'vikisews', 'burda', или просто содержать теги типа 'sewing', 'craft', 'clothes', 'выкройка-платье').
                    
                    Твоя задача — использовать ассоциативное мышление и сопоставить контекст запроса с именами файлов, названиями папок назначения и сгенерированными тегами.
                    
                    Ниже представлен лог прошлых сессий сортировки NeuroOS:
                    --- НАЧАЛО ЛОГА NEURO.TXT ---
                    {neuro_log_content if neuro_log_content else "Лог пуст или отсутствует."}
                    --- КОНЕЦ ЛОГА NEURO.TXT ---
                    
                    Выдели файлы, которые с высокой долей вероятности относятся к запросу пользователя. 
                    Выдай структурированный, понятный ответ: в какую именно папку перемещен каждый подходящий файл и какие теги ему были присвоены.
                    Если ничего даже отдаленно похожего не найдено, аккуратно сообщи об этом.
                    """
                    
                    analysis_payload = {
                        "contents": [{"role": "user", "parts": [{"text": analysis_prompt}]}],
                        "systemInstruction": {"parts": [{"text": "Ты — умный ИИ-навигатор файловой системы NeuroOS. Находишь скрытые логические связи между описанием пользователя, именами файлов и тегами. Отвечаешь стильно, технически точно."}]}
                    }
                    
                    res_analysis = requests.post(url, json=analysis_payload, headers=headers, timeout=30)
                    
                    if res_analysis.status_code == 200 and 'candidates' in res_analysis.json() and res_analysis.json()['candidates']:
                        search_report = res_analysis.json()['candidates'][0]['content']['parts'][0]['text']
                    else:
                        search_report = "Ошибка при анализе структуры файлов нейросетью."
                        
                    self.conversation_history.append({"role": "user", "parts": [{"text": user_prompt}]})
                    self.conversation_history.append({"role": "model", "parts": [{"text": search_report}]})
                    self.signals.response_received.emit(search_report)
                
                # ================= СЦЕНАРИЙ В: OBSIDIAN — РАЗДРОБИТЬ (1 или несколько файлов, ПО ОЧЕРЕДИ) =================
                elif ai_text.startswith("INTENT_OBSIDIAN_SPLIT:"):
                    self.set_ai_mode("obsidian", announce=False)
                    raw_refs = ai_text.replace("INTENT_OBSIDIAN_SPLIT:", "").strip()
                    note_refs = [ref.strip() for ref in raw_refs.split("||") if ref.strip()]
                    if not note_refs:
                        note_refs = [raw_refs]

                    combined_reports = []

                    for idx, note_ref in enumerate(note_refs, start=1):
                        progress_prefix = f"({idx}/{len(note_refs)}) " if len(note_refs) > 1 else ""
                        self.signals.response_received.emit(
                            f"<i style='color:#b77ad9;'>[SYSTEM]: {progress_prefix}Подготовка Obsidian-графа для `{note_ref}`...</i>"
                        )

                        def _split_progress(message, _prefix=progress_prefix):
                            self._emit_progress(f"{_prefix}{message}")

                        try:
                            single_report = obsidian_split(note_ref, progress_callback=_split_progress)
                        except Exception as exc:
                            single_report = f"Ошибка Obsidian split для '{note_ref}': {exc}"

                        if len(note_refs) > 1:
                            combined_reports.append(f"### {note_ref}\n{single_report}")
                        else:
                            combined_reports.append(single_report)

                    report = "\n\n".join(combined_reports)

                    self.conversation_history.append(
                        {"role": "user", "parts": [{"text": user_prompt}]}
                    )
                    self.conversation_history.append(
                        {"role": "model", "parts": [{"text": report}]}
                    )
                    self.signals.response_received.emit(report)

                # ================= СЦЕНАРИЙ Г: OBSIDIAN — НАЙТИ ССЫЛКИ =================
                elif ai_text.startswith("INTENT_OBSIDIAN_FIND:"):
                    self.set_ai_mode("obsidian", announce=False)
                    content = ai_text.replace("INTENT_OBSIDIAN_FIND:", "").strip()
                    parts = content.split("|", 1)

                    note_ref = parts[0].strip()
                    search_query = parts[1].strip() if len(parts) > 1 else ""

                    self.signals.response_received.emit(
                        f"<i style='color:#b77ad9;'>[SYSTEM]: Поиск связанных Obsidian-ссылок в `{note_ref}`...</i>"
                    )

                    try:
                        report = obsidian_find(note_ref, search_query)
                    except Exception as exc:
                        report = f"Ошибка Obsidian find: {exc}"

                    self.conversation_history.append(
                        {"role": "user", "parts": [{"text": user_prompt}]}
                    )
                    self.conversation_history.append(
                        {"role": "model", "parts": [{"text": report}]}
                    )
                    self.signals.response_received.emit(report)

                # ================= СЦЕНАРИЙ Д: OBSIDIAN — ОБЗОР ХРАНИЛИЩА =================
                elif ai_text.startswith("INTENT_OBSIDIAN_OVERVIEW"):
                    self.set_ai_mode("obsidian", announce=False)
                    self.signals.response_received.emit(
                        "<i style='color:#b77ad9;'>[SYSTEM]: Сканирование структуры текущего Obsidian vault...</i>"
                    )

                    try:
                        report = obsidian_overview()
                    except Exception as exc:
                        report = f"Ошибка Obsidian overview: {exc}"

                    self.conversation_history.append(
                        {"role": "user", "parts": [{"text": user_prompt}]}
                    )
                    self.conversation_history.append(
                        {"role": "model", "parts": [{"text": report}]}
                    )
                    self.signals.response_received.emit(report)

                # ================= СЦЕНАРИЙ Е: OBSIDIAN — КОНКРЕТНАЯ ВЕТКА =================
                elif ai_text.startswith("INTENT_OBSIDIAN_BRANCH:"):
                    self.set_ai_mode("obsidian", announce=False)
                    branch_query = ai_text.replace("INTENT_OBSIDIAN_BRANCH:", "", 1).strip()

                    self.signals.response_received.emit(
                        f"<i style='color:#b77ad9;'>[SYSTEM]: Поиск логической ветки Obsidian по запросу `{branch_query}`...</i>"
                    )

                    try:
                        report = obsidian_branch_overview(branch_query)
                    except Exception as exc:
                        report = f"Ошибка Obsidian branch: {exc}"

                    self.conversation_history.append(
                        {"role": "user", "parts": [{"text": user_prompt}]}
                    )
                    self.conversation_history.append(
                        {"role": "model", "parts": [{"text": report}]}
                    )
                    self.signals.response_received.emit(report)

                # ================= СЦЕНАРИЙ Ж: OBSIDIAN — ДАТЬ ОПРЕДЕЛЕНИЕ =================
                elif ai_text.startswith("INTENT_OBSIDIAN_DEFINE:"):
                    self.set_ai_mode("obsidian", announce=False)
                    term = ai_text.replace("INTENT_OBSIDIAN_DEFINE:", "").strip()

                    self.signals.response_received.emit(
                        f"<i style='color:#b77ad9;'>[SYSTEM]: Поиск определения «{term}» в Obsidian...</i>"
                    )

                    try:
                        report = obsidian_define(term)
                    except Exception as exc:
                        report = f"Ошибка поиска определения: {exc}"

                    self.conversation_history.append(
                        {"role": "user", "parts": [{"text": user_prompt}]}
                    )
                    self.conversation_history.append(
                        {"role": "model", "parts": [{"text": report}]}
                    )
                    self.signals.response_received.emit(report)

                # ================= СЦЕНАРИЙ И: OBSIDIAN — ПОЧИНИТЬ ДУБЛИ =================
                elif ai_text.startswith("INTENT_OBSIDIAN_REPAIR_DUPES"):
                    self.signals.response_received.emit(
                        "<i style='color:#b77ad9;'>[SYSTEM]: Ищу уже раздробленные файлы без защиты "
                        "от повторной обработки и чиню...</i>"
                    )

                    try:
                        report = obsidian_repair_duplicates(progress_callback=self._emit_progress)
                    except Exception as exc:
                        report = f"Ошибка починки дублей: {exc}"

                    self.conversation_history.append(
                        {"role": "user", "parts": [{"text": user_prompt}]}
                    )
                    self.conversation_history.append(
                        {"role": "model", "parts": [{"text": report}]}
                    )
                    self.signals.response_received.emit(report)

                # Сценарий З: Обычный поисковый запрос или беседа
                else:
                    self.conversation_history.append({"role": "user", "parts": [{"text": user_prompt}]})
                    self.conversation_history.append({"role": "model", "parts": [{"text": ai_text}]})
                    self.signals.response_received.emit(ai_text)
            else:
                error_msg = data.get('error', {}).get('message', 'Unknown API Error')
                self.signals.response_received.emit(f"UPLINK_CRASH [{response.status_code}]: {error_msg}")
        except Exception as e:
            self.signals.response_received.emit(f"CONNECTION_LOST: {str(e)}")

    def _emit_progress(self, message: str) -> None:
        """
        Живой лог этапа долгой операции (сортировка/Obsidian-дробление) —
        прокидывается как progress_callback в ai_sort_folder/obsidian_split,
        поэтому вызывается уже из фонового потока intelligent_router. Это
        тот же паттерн emit, что и везде в этом файле — PyQt сам маршалит
        сигнал в основной поток, доп. синхронизация не нужна.
        """
        self.signals.response_received.emit(
            f"<i style='color:#b77ad9;'>[SYSTEM]: {message}</i>"
        )

    def display_response(self, text):
        self.send_btn.setEnabled(True)
        html_text = markdown.markdown(text, extensions=['fenced_code', 'tables'])
        self.chat_history.append(f"<b style='color:#00e5ff;'>[AI]:</b> {html_text}")
        self.chat_history.ensureCursorVisible()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    panel = NeuroAiSidePanel()
    sys.exit(app.exec())
