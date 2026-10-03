import sys
import os
import time
import threading
import requests
import signal
import webbrowser
import re
import random
from ns_config import *

from pathlib import Path
from datetime import datetime
from indexer import add_tag
from neuro_pet import (
    load_last_fed,
    save_last_fed,
    get_hunger_percentage,
    is_sleep_time,
)

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QHBoxLayout,
    QFrame,
    QSlider,
    QLineEdit,
    QTextEdit,
    QSplitter,
    QMessageBox,
    QProgressBar,
)

from PyQt6.QtCore import (
    Qt,
    QTimer,
    QRect,
    QUrl,
    pyqtSignal,
    QFileSystemWatcher,
)

from PyQt6.QtGui import (
    QCursor,
    QShortcut,
    QKeySequence,
)

from PyQt6.QtMultimedia import (
    QMediaPlayer,
    QAudioOutput,
    QMediaDevices,
)


# Форсируем X11 для корректной работы слоёв в GNOME / Xorg
os.environ["QT_QPA_PLATFORM"] = "xcb"

# Флаг отключения шторки от внешнего переключателя (neuro_shutter_switch.py).
# Файла нет -> шторка открывается по наведению как раньше (совместимость,
# если переключатель вообще не запущен). Файл есть -> наведение на верхний
# край больше НЕ открывает шторку. На плеер/фоновую работу панели это не
# влияет — только на вызов open_panel() внутри hover_check().
SHUTTER_DISABLED_FLAG = Path("/tmp/neuro_shutter_panel_disabled")

if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


# =========================================================
# CONFIG
# =========================================================

# Суточная цель фокуса
FOCUS_TARGET_HOURS = 3.0
FOCUS_BAR_LENGTH = 10

NOW_PLAYING_TITLE = Path("/tmp/neuro_now_playing_title")

PANEL_STYLE = f"""
QWidget {{
    background: transparent;
    color: #e7dcff;
    font-family: 'MonoCraft', 'DejaVu Sans Mono', 'Consolas';
    font-size: 10px;
}}

#MainFrame {{
    background-color: rgba(30, 30, 34, 195);
    color: #e7dcff;

    border: 1px solid #5a5a62;
    border-top: none;

    border-bottom-left-radius: 2px;
    border-bottom-right-radius: 2px;
}}

QLabel {{
    color: #d2d2d8;
    background: transparent;
}}

QLabel#SectionHeader {{
    color: #b77ad9;
    font-weight: bold;
    font-size: 10px;
}}

QLabel#FocusTarget {{
    color: #00e5ff;
    font-family: 'MonoCraft', 'DejaVu Sans Mono', 'Consolas';
    font-size: 10px;
    font-weight: bold;
    padding: 2px 0;
}}

QListWidget {{
    background-color: rgb(20, 20, 23);
    color: #e7dcff;

    border: 1px solid #47474f;
    border-radius: 1px;

    padding: 1px;
    outline: none;
}}

QListWidget::item {{
    background: transparent;
    padding: 3px 4px;
    border: none;
}}

QListWidget::item:hover {{
    background-color: rgb(48, 42, 52);
    color: #f2f2f5;
}}

QListWidget::item:selected {{
    background-color: #9f5fb8;
    color: #f0eef2;
}}

QLineEdit,
QTextEdit {{
    background-color: rgb(6, 5, 10);
    color: #f2f2f5;

    border: 1px solid #47474f;
    border-radius: 1px;

    padding: 4px;
    selection-background-color: #9f5fb8;
    selection-color: #f0eef2;
}}

QLineEdit:focus,
QTextEdit:focus {{
    border: 1px solid #b83cff;
}}

QPushButton {{
    background-color: rgb(17, 8, 25);
    color: #cf62ff;

    border: 1px solid #9b30d1;
    border-radius: 1px;

    font-family: 'MonoCraft', 'DejaVu Sans Mono', 'Consolas';
    font-size: 9px;
    font-weight: bold;

    padding: 4px 6px;
}}

QPushButton:hover {{
    background-color: #9f5fb8;
    color: #f0eef2;
    border-color: #e8a6ff;
}}

QPushButton:pressed {{
    background-color: #7e1caf;
    color: #f2f2f5;
    border-color: #b83cff;
}}

QPushButton:checked {{
    background-color: #7e1caf;
    color: #f2f2f5;
    border-color: #e8a6ff;
}}

QPushButton:disabled {{
    background-color: rgb(10, 8, 14);
    color: #604a70;
    border-color: #321b3d;
}}

QSlider::groove:horizontal {{
    height: 3px;
    background: #24132e;
    border: 1px solid #34153f;
    border-radius: 0px;
}}

QSlider::sub-page:horizontal {{
    background: #8f2bc2;
    border: 1px solid #b83cff;
    border-radius: 0px;
}}

QSlider::add-page:horizontal {{
    background: #17101e;
    border: 1px solid #29132f;
    border-radius: 0px;
}}

QSlider::handle:horizontal {{
    background: #d96cff;
    border: 1px solid #f0bcff;

    width: 9px;
    height: 9px;
    margin: -4px 0;

    border-radius: 1px;
}}

QSplitter::handle:vertical {{
    height: 1px;
    background: #4b1b61;
}}

QScrollBar:vertical {{
    background: #08050d;
    width: 8px;
    margin: 0px;
    border: none;
}}

QScrollBar::handle:vertical {{
    background: #4a215e;
    min-height: 20px;
    border: 1px solid #6f2a8d;
    border-radius: 0px;
}}

QScrollBar::handle:vertical:hover {{
    background: #9b30d1;
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: #08050d;
}}

QScrollBar:horizontal {{
    background: #08050d;
    height: 8px;
    margin: 0px;
    border: none;
}}

QScrollBar::handle:horizontal {{
    background: #4a215e;
    min-width: 20px;
    border: 1px solid #6f2a8d;
    border-radius: 0px;
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: #08050d;
}}
"""


class NeuroSystemPanel(QWidget):
    # Рабочий поток НЕ может запускать QTimer.singleShot с задержкой
    # (в нём нет event loop) — результат загрузки отдаём в главный поток
    # через сигналы.
    download_succeeded = pyqtSignal()
    download_failed = pyqtSignal(object)   # все попытки загрузки исчерпаны
    download_status = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        self.expanded_h = 770
        self.panel_width = 380
        self.is_open = False

        # =========================
        # AUDIO ENGINE
        # =========================

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)

        self.player.setAudioOutput(
            self.audio_output
        )

        self.audio_output.setVolume(0.8)
        self.player.setPlaybackRate(1.0)

        # Отслеживаем аудиоустройства
        self.media_devices = QMediaDevices(self)

        self.media_devices.audioOutputsChanged.connect(
            self.handle_audio_devices_changed
        )

        QTimer.singleShot(
            100,
            self.update_audio_output
        )

        # =========================
        # STATE
        # =========================

        self.tracks = []
        self.current_index = -1
        self.current_track = None
        self.is_dragging_slider = False

        self.liked_track_ids = set()
        self.liked_tracks_cache = {}
        self.showing_likes = True
        self.retry_count = 0

        # Сколько попыток загрузки даём треку, прежде чем пропустить его
        self.max_attempts = 5
        # Пауза между попытками = retry_delay * номер_попытки (сек):
        # 0.5, 1.0, 1.5, 2.0 -> ~5 сек на весь трек вместо ~12
        self.retry_delay = 0.5
        # Файл скачался, но не декодируется — тут повторять почти нет смысла
        self.max_decode_attempts = 2
        # Одна сессия на все загрузки: не переоткрываем TLS-соединение каждый раз
        self.http = requests.Session()
        # Сколько треков подряд пропущено из-за ошибок (защита от бесконечного цикла)
        self.skip_streak = 0
        # Идёт ли сейчас загрузка/ретрай трека
        self.is_loading = False

        # Режим "вперемешку" (работает только в лайках)
        self.shuffle_enabled = False
        self.shuffle_queue = []
        self.shuffle_history = []

        # =========================
        # NOTES
        # =========================

        self.notes_path = Path(NOTES_DIR)

        self.notes_path.mkdir(
            parents=True,
            exist_ok=True
        )

        self.current_note_file = None

        # =========================
        # UI
        # =========================

        self.init_ui()

        # =========================
        # GEOMETRY
        # =========================

        self.hidden_rect = QRect(
            0,
            0,
            self.panel_width,
            1
        )

        self.shown_rect = QRect(
            0,
            0,
            self.panel_width,
            self.expanded_h
        )

        self.main_frame.setFixedSize(
            self.panel_width,
            self.expanded_h
        )

        self.setGeometry(
            self.hidden_rect
        )

        self.show()

        # =========================
        # HOVER TIMER
        # =========================

        self.hover_timer = QTimer(self)

        self.hover_timer.timeout.connect(
            self.hover_check
        )

        self.hover_timer.start(100)

        # Флаг читаем не на каждый тик hover_timer (100 мс), а раз в 500 мс —
        # это лишь разрешение на открытие по наведению, лишний I/O тут не нужен.
        self.shutter_enabled = not SHUTTER_DISABLED_FLAG.exists()
        self.shutter_flag_timer = QTimer(self)
        self.shutter_flag_timer.timeout.connect(self._refresh_shutter_enabled)
        self.shutter_flag_timer.start(500)

        # =========================
        # PLAYER SIGNALS
        # =========================

        self.player.positionChanged.connect(
            self.update_slider_pos
        )

        self.player.durationChanged.connect(
            self.update_slider_range
        )

        self.player.mediaStatusChanged.connect(
            self.handle_media_status
        )

        self.player.errorOccurred.connect(
            self.handle_player_error
        )

        self.download_succeeded.connect(
            self.start_playback
        )

        self.download_failed.connect(
            self._skip_failed_track
        )

        self.download_status.connect(
            self.music_status.setText
        )

        # =========================
        # INITIAL LOAD
        # =========================

        QTimer.singleShot(
            500,
            self.load_tracks
        )

        # =========================
        # NOTES + INITIAL FOCUS
        # =========================

        self.refresh_notes()
        self.update_focus_target()
        self.watcher = QFileSystemWatcher(self)
        self.watcher.addPath(str(self.notes_path))
        self.watcher.directoryChanged.connect(self.refresh_notes)

        self.update_focus_target()


    # =========================================================
    # AUDIO OUTPUT / BLUETOOTH
    # =========================================================

    def update_audio_output(self):
        outputs = QMediaDevices.audioOutputs()

        if not outputs:
            print("AUDIO OUTPUTS: none")
            return

        bluetooth_keywords = (
            "bluetooth",
            "bluez",
            "headset",
            "headphone",
            "earbud",
            "earbuds",
            "airpods",
            "buds",
            "wireless",
        )

        bluetooth_device = None

        for device in outputs:
            description = (
                device.description()
                or ""
            ).lower()

            device_id = (
                bytes(device.id())
                .decode(
                    "utf-8",
                    errors="ignore"
                )
                .lower()
            )

            device_text = (
                f"{description} {device_id}"
            )

            if any(
                keyword in device_text
                for keyword in bluetooth_keywords
            ):
                bluetooth_device = device
                break

        if bluetooth_device:
            self.audio_output.setDevice(
                bluetooth_device
            )

            print(
                "AUDIO OUTPUT:",
                bluetooth_device.description()
            )

            if hasattr(self, "music_status"):
                self.music_status.setText(
                    f"[BT AUDIO] "
                    f"{bluetooth_device.description()[:28]}"
                )

        else:
            default_device = (
                QMediaDevices.defaultAudioOutput()
            )

            self.audio_output.setDevice(
                default_device
            )

            print(
                "AUDIO OUTPUT:",
                default_device.description()
            )

    def handle_audio_devices_changed(self):
        print(
            "AUDIO DEVICES CHANGED"
        )

        QTimer.singleShot(
            300,
            self.update_audio_output
        )


    # =========================================================
    # UI
    # =========================================================

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )

        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )

        self.main_frame = QFrame(self)

        self.main_frame.setObjectName(
            "MainFrame"
        )

        self.main_frame.setGeometry(
            0,
            0,
            self.panel_width,
            self.expanded_h
        )

        content_layout = QVBoxLayout(
            self.main_frame
        )

        header = QLabel(
            "NEURO_PANEL"
        )

        header.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        content_layout.addWidget(
            header
        )

        self.splitter = QSplitter(
            Qt.Orientation.Vertical
        )

        # =====================================================
        # AUDIO
        # =====================================================

        audio_win = QWidget()

        audio_vbox = QVBoxLayout(
            audio_win
        )

        audio_vbox.setContentsMargins(
            0,
            0,
            0,
            0
        )

        self.music_status = QLabel(
            "AUDIO: STANDBY"
        )

        self.music_status.setObjectName(
            "SectionHeader"
        )

        # -------------------------
        # SEARCH
        # -------------------------

        search_layout = QHBoxLayout()

        self.search_input = QLineEdit()

        self.search_input.setPlaceholderText(
            "track name..."
        )

        self.search_input.returnPressed.connect(
            self.search_soundcloud
        )

        btn_search = QPushButton(
            "🔍"
        )

        btn_search.setFixedWidth(35)

        btn_search.clicked.connect(
            self.search_soundcloud
        )

        self.btn_like = QPushButton(
            "♡"
        )

        self.btn_like.setFixedWidth(35)

        self.btn_like.clicked.connect(
            self.like_selected
        )

        self.btn_shuffle = QPushButton(
            "🔀"
        )

        self.btn_shuffle.setFixedWidth(35)

        self.btn_shuffle.setCheckable(True)

        self.btn_shuffle.setToolTip(
            "Вперемешку (только в лайках)"
        )

        self.btn_shuffle.toggled.connect(
            self.toggle_shuffle
        )

        self.btn_likes = QPushButton(
            "who"
        )

        self.btn_likes.clicked.connect(
            self.load_tracks
        )

        search_layout.addWidget(
            self.search_input
        )

        search_layout.addWidget(
            btn_search
        )

        search_layout.addWidget(
            self.btn_like
        )

        search_layout.addWidget(
            self.btn_shuffle
        )

        search_layout.addWidget(
            self.btn_likes
        )

        # -------------------------
        # TRACK LIST
        # -------------------------

        self.track_list = QListWidget()

        self.track_list.itemDoubleClicked.connect(
            self.play_selected
        )

        self.track_list.itemSelectionChanged.connect(
            self._update_like_button
        )

        # -------------------------
        # SLIDER
        # -------------------------

        self.slider = QSlider(
            Qt.Orientation.Horizontal
        )

        self.slider.mousePressEvent = (
            self.slider_mouse_press
        )

        self.slider.mouseReleaseEvent = (
            self.slider_mouse_release
        )

        # -------------------------
        # CONTROLS
        # -------------------------

        ctrls = QHBoxLayout()

        btn_prev = QPushButton("⏮")
        self.btn_play = QPushButton("▶")
        btn_next = QPushButton("⏭")

        btn_prev.clicked.connect(
            self._on_prev_clicked
        )

        self.btn_play.clicked.connect(
            self.toggle_play
        )

        btn_next.clicked.connect(
            self._on_next_clicked
        )

        for button in [
            btn_prev,
            self.btn_play,
            btn_next
        ]:
            ctrls.addWidget(button)

        audio_vbox.addWidget(
            self.music_status
        )

        audio_vbox.addLayout(
            search_layout
        )

        audio_vbox.addWidget(
            self.track_list
        )

        audio_vbox.addWidget(
            self.slider
        )

        audio_vbox.addLayout(
            ctrls
        )

        # =====================================================
        # NOTES
        # =====================================================

        notes_win = QWidget()

        notes_vbox = QVBoxLayout(
            notes_win
        )

        notes_vbox.setContentsMargins(
            0,
            10,
            0,
            0
        )

        notes_vbox.addWidget(
            QLabel("── NOTES ──")
        )

        self.notes_list = QListWidget()

        self.notes_list.itemClicked.connect(
            self.load_note
        )

        self.input_title = QLineEdit()

        self.input_title.setPlaceholderText(
            "ENTRY_NAME..."
        )

        self.input_content = QTextEdit()

        notes_ctrl = QHBoxLayout()

        btn_new = QPushButton(
            "NEW"
        )

        btn_sync = QPushButton(
            "SYNC"
        )

        btn_delete = QPushButton(
            "DELETE"
        )

        btn_new.clicked.connect(
            self.new_note
        )

        btn_sync.clicked.connect(
            self.sync_focus
        )

        btn_delete.clicked.connect(
            self.delete_note
        )

        notes_ctrl.addWidget(
            btn_new
        )

        notes_ctrl.addWidget(
            btn_sync
        )

        notes_ctrl.addWidget(
            btn_delete
        )

        notes_vbox.addWidget(
            self.notes_list
        )

        notes_vbox.addWidget(
            self.input_title
        )

        notes_vbox.addWidget(
            self.input_content
        )

        notes_vbox.addLayout(
            notes_ctrl
        )

        # =====================================================
        # ПИТОМЕЦ
        # =====================================================

        pet_row = QWidget()

        pet_layout = QVBoxLayout(pet_row)
        pet_layout.setContentsMargins(0, 10, 0, 0)
        pet_layout.setSpacing(4)

        self.pet_hunger_bar = QProgressBar()
        self.pet_hunger_bar.setRange(0, 100)
        self.pet_hunger_bar.setTextVisible(False)
        self.pet_hunger_bar.setFixedHeight(8)
        self.pet_hunger_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #47474f;
                border-radius: 1px;
                background-color: rgb(6, 5, 10);
            }
            QProgressBar::chunk {
                background-color: #9f5fb8;
                border-radius: 1px;
            }
        """)

        self.pet_feed_button = QPushButton("FEED")
        self.pet_feed_button.clicked.connect(
            self._on_feed_pet_clicked
        )

        pet_layout.addWidget(self.pet_hunger_bar)
        pet_layout.addWidget(self.pet_feed_button)

        notes_vbox.addWidget(pet_row)

        self.pet_timer = QTimer(self)
        self.pet_timer.timeout.connect(self._refresh_pet_ui)
        self.pet_timer.start(1000)
        self._refresh_pet_ui()

        # =====================================================
        # SILENT FOCUS TARGET
        # =====================================================

        self.focus_target_label = QLabel(
            "[░░░░░░░░░░] 0.0 / 3 ч"
        )

        self.focus_target_label.setObjectName(
            "FocusTarget"
        )

        self.focus_target_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        self.focus_target_label.setToolTip(
            "Суммарное чистое время фокуса за сегодня"
        )

        notes_vbox.addWidget(
            self.focus_target_label
        )

        # =====================================================
        # SPLITTER
        # =====================================================

        self.splitter.addWidget(
            audio_win
        )

        self.splitter.addWidget(
            notes_win
        )

        content_layout.addWidget(
            self.splitter
        )

        self.setStyleSheet(
            PANEL_STYLE
        )

        # ESC
        self.esc_shortcut = QShortcut(
            QKeySequence("Esc"),
            self
        )

        self.esc_shortcut.activated.connect(
            self.handle_escape
        )


    # =========================================================
    # FOCUS / OBSIDIAN
    # =========================================================

    def get_today_focus_seconds(self):
        today = datetime.now().strftime(
            "%Y-%m-%d"
        )

        daily_note = (
            OBSIDIAN_VAULT
            / f"{today}.md"
        )

        print(
            "--------------------------------"
        )

        print(
            "FOCUS SYNC FILE:",
            daily_note
        )

        if not daily_note.exists():
            print(
                "FOCUS FILE DOES NOT EXIST"
            )

            print(
                "--------------------------------"
            )

            return 0

        try:
            content = daily_note.read_text(
                encoding="utf-8"
            )

            print(
                "FOCUS FILE SIZE:",
                len(content),
                "characters"
            )

            pattern = re.compile(
                r"^\s*###\s+Session:\s+"
                r"\d{1,2}:\d{2}\s+"
                r"\((\d+):(\d{2}):(\d{2})\)"
                r"\s*$",
                re.MULTILINE
            )

            total_seconds = 0
            session_count = 0

            for match in pattern.finditer(
                content
            ):
                hours = int(
                    match.group(1)
                )

                minutes = int(
                    match.group(2)
                )

                seconds = int(
                    match.group(3)
                )

                session_seconds = (
                    hours * 3600
                    + minutes * 60
                    + seconds
                )

                total_seconds += (
                    session_seconds
                )

                session_count += 1

                print(
                    "SESSION:",
                    f"{hours:02}:{minutes:02}:{seconds:02}"
                )

            print(
                "FOCUS SESSIONS FOUND:",
                session_count
            )

            print(
                "FOCUS TOTAL SECONDS:",
                total_seconds
            )

            print(
                "FOCUS TOTAL HOURS:",
                round(
                    total_seconds / 3600,
                    2
                )
            )

            print(
                "--------------------------------"
            )

            return total_seconds

        except Exception as e:
            print(
                "FOCUS TARGET READ ERROR:",
                repr(e)
            )

            print(
                "--------------------------------"
            )

            return 0


    def update_focus_target(self):
        total_seconds = (
            self.get_today_focus_seconds()
        )

        total_hours = (
            total_seconds / 3600
        )

        target_seconds = (
            FOCUS_TARGET_HOURS * 3600
        )

        progress = (
            total_seconds / target_seconds
            if target_seconds > 0
            else 0
        )

        progress = max(
            0.0,
            min(
                1.0,
                progress
            )
        )

        filled_count = int(
            round(
                progress
                * FOCUS_BAR_LENGTH
            )
        )

        empty_count = (
            FOCUS_BAR_LENGTH
            - filled_count
        )

        bar = (
            "█" * filled_count
            + "░" * empty_count
        )

        self.focus_target_label.setText(
            f"[{bar}] "
            f"{total_hours:.1f} / "
            f"{FOCUS_TARGET_HOURS:g} ч"
        )

        print(
            "FOCUS DISPLAY:",
            f"{total_hours:.1f} / "
            f"{FOCUS_TARGET_HOURS:g} h"
        )


    def sync_focus(self):
        title = self.input_title.text().strip()
        content = self.input_content.toPlainText()

        if (self.current_note_file is not None or title or content):
            self.save_note()
            
        self.update_focus_target()


    # =========================================================
    # SLIDER / SEEK
    # =========================================================

    def slider_mouse_press(
        self,
        event
    ):
        if (
            event.button()
            != Qt.MouseButton.LeftButton
        ):
            event.accept()
            return

        duration = self.player.duration()

        if duration <= 0:
            event.accept()
            return

        self.is_dragging_slider = True

        ratio = (
            event.position().x()
            / self.slider.width()
        )

        ratio = max(
            0.0,
            min(
                1.0,
                ratio
            )
        )

        position = int(
            duration * ratio
        )

        self.player.setPosition(
            position
        )

        self.slider.setValue(
            position
        )

        event.accept()


    def slider_mouse_release(
        self,
        event
    ):
        if (
            event.button()
            == Qt.MouseButton.LeftButton
        ):
            self.is_dragging_slider = False
            event.accept()
            return

        event.accept()


    def update_slider_pos(
        self,
        pos
    ):
        duration = self.player.duration()

        if duration <= 0:
            return

        if not self.is_dragging_slider:
            self.slider.setRange(
                0,
                duration
            )

            self.slider.setValue(
                pos
            )

        cur_sec = pos // 1000

        cur_min, cur_sec = divmod(
            cur_sec,
            60
        )

        dur_sec = duration // 1000

        dur_min, dur_sec = divmod(
            dur_sec,
            60
        )

        title = (
            self.current_track["title"][:18]
            if self.current_track
            else "STANDBY"
        )

        self.music_status.setText(
            f"[{cur_min:02}:{cur_sec:02}/"
            f"{dur_min:02}:{dur_sec:02}] | "
            f"{title}..."
        )


    def update_slider_range(
        self,
        duration
    ):
        print(
            "REAL PLAYER DURATION:",
            duration,
            "ms"
        )

        if duration > 0:
            self.slider.setRange(
                0,
                duration
            )

            self.slider.setEnabled(
                True
            )

        else:
            self.slider.setRange(
                0,
                0
            )

            self.slider.setEnabled(
                False
            )


    # =========================================================
    # PANEL VISIBILITY
    # =========================================================

    def _refresh_shutter_enabled(self):
        self.shutter_enabled = not SHUTTER_DISABLED_FLAG.exists()

    def hover_check(self):
        pos = QCursor.pos()

        in_x = (
            0 <= pos.x()
            <= self.panel_width
        )

        # Наведение на верхний край открывает шторку — но только если она не
        # отключена переключателем. Уже открытую панель это не трогает:
        # закрывается она всегда штатно, по уходу курсора, независимо от
        # состояния переключателя (плеер и всё остальное продолжают работать).
        if not self.is_open:
            if self.shutter_enabled and pos.y() <= 3 and in_x:
                self.open_panel()

        else:
            if (
                pos.y()
                > self.expanded_h + 60
                or pos.x()
                > self.panel_width + 60
                or pos.x()
                < -60
            ):
                self.close_panel()


    def open_panel(self):
        if not self.is_open:
            self.is_open = True

            self.setGeometry(
                self.shown_rect
            )

            self.activateWindow()
            self.raise_()
            
            self.refresh_notes()
            self.update_focus_target()


    def close_panel(self):
        if self.is_open:
            self.is_open = False

            self.setGeometry(
                self.hidden_rect
            )


    def handle_escape(self):
        if self.is_open:
            self.close_panel()
        else:
            QApplication.quit()


    # =========================================================
    # SOUNDCLOUD / LIKES
    # =========================================================

    def load_tracks(self):
        self.showing_likes = True

        threading.Thread(
            target=self.fetch_all_likes,
            daemon=True
        ).start()


    def refresh_likes(self):
        threading.Thread(
            target=self.fetch_all_likes,
            daemon=True
        ).start()


    @staticmethod
    def _progressive_url(track):
        if not isinstance(
            track,
            dict
        ):
            return None

        media = track.get("media")

        if not isinstance(
            media,
            dict
        ):
            return None

        transcodings = (
            media.get("transcodings")
            or []
        )

        if not isinstance(
            transcodings,
            list
        ):
            return None

        for tc in transcodings:
            if not isinstance(
                tc,
                dict
            ):
                continue

            fmt = tc.get("format") or {}

            if (
                isinstance(
                    fmt,
                    dict
                )
                and fmt.get(
                    "protocol"
                ) == "progressive"
                and tc.get("url")
            ):
                return tc["url"]

        return None


    @staticmethod
    def _unwrap_like_item(item):
        if not isinstance(
            item,
            dict
        ):
            return None

        track = item.get("track")

        if isinstance(
            track,
            dict
        ):
            return track

        if (
            item.get("id")
            and item.get("title")
        ):
            return item

        return None


    def fetch_all_likes(self):
        if not OAUTH_TOKEN:
            print(
                "ERROR: SOUNDCLOUD_OAUTH_TOKEN "
                "is not set"
            )
            return

        headers = {
            "Authorization": (
                f"Bearer {OAUTH_TOKEN}"
            ),
            "User-Agent": "Mozilla/5.0",
        }

        try:
            me_res = requests.get(
                (
                    "https://api-v2.soundcloud.com/"
                    f"me?client_id={CLIENT_ID}"
                ),
                headers=headers,
                timeout=10,
            )

            if me_res.status_code != 200:
                raise Exception(
                    f"ME Error "
                    f"{me_res.status_code}: "
                    f"{me_res.text}"
                )

            me = me_res.json()

            user_id = (
                me.get("id")
                if isinstance(
                    me,
                    dict
                )
                else None
            )

            if not user_id:
                raise Exception(
                    "SoundCloud user id not found"
                )

            self.soundcloud_user_id = user_id

            likes_res = requests.get(
                (
                    "https://api-v2.soundcloud.com/"
                    f"users/{user_id}/track_likes"
                ),
                headers=headers,
                params={
                    "client_id": CLIENT_ID,
                    "oauth_token": OAUTH_TOKEN,
                    "limit": 200,
                    "offset": 0,
                    "linked_partitioning": 1,
                },
                timeout=10,
            )

            if likes_res.status_code != 200:
                raise Exception(
                    f"Likes Error "
                    f"{likes_res.status_code}: "
                    f"{likes_res.text}"
                )

            likes = likes_res.json()

            collection = (
                likes.get(
                    "collection",
                    []
                )
                if isinstance(
                    likes,
                    dict
                )
                else []
            )

            if not isinstance(
                collection,
                list
            ):
                collection = []

            new_tracks = []

            for item in collection:
                track = self._unwrap_like_item(
                    item
                )

                if not track:
                    continue

                api_url = self._progressive_url(
                    track
                )

                if not api_url:
                    continue

                track_id = track.get("id")

                parsed = {
                    "id": track_id,
                    "title": track.get(
                        "title",
                        "UNKNOWN TRACK"
                    ),
                    "api_url": api_url,
                    "permalink_url": track.get(
                        "permalink_url",
                        ""
                    ),
                    "liked": True,
                }

                new_tracks.append(
                    parsed
                )

            self.liked_track_ids = {
                t["id"]
                for t in new_tracks
                if t.get("id")
            }

            self.liked_tracks_cache = {
                t["id"]: t.copy()
                for t in new_tracks
                if t.get("id")
            }

            self.tracks = new_tracks
            self.current_index = -1

            QTimer.singleShot(
                0,
                self.update_music_list
            )

            print(
                f"Loaded likes: "
                f"{len(new_tracks)}"
            )

        except Exception as e:
            print(
                f"Error fetching likes: {e}"
            )


    # =========================================================
    # SEARCH
    # =========================================================

    def search_soundcloud(self):
        query = (
            self.search_input
            .text()
            .strip()
        )

        if not query:
            return

        threading.Thread(
            target=self.perform_search,
            args=(query,),
            daemon=True,
        ).start()


    def perform_search(
        self,
        query
    ):
        if not OAUTH_TOKEN:
            print(
                "ERROR: SOUNDCLOUD_OAUTH_TOKEN "
                "is not set"
            )
            return

        headers = {
            "Authorization": (
                f"Bearer {OAUTH_TOKEN}"
            ),
            "User-Agent": "Mozilla/5.0",
        }

        try:
            url = (
                "https://api-v2.soundcloud.com/"
                "search/tracks"
                f"?q={requests.utils.quote(query)}"
                f"&client_id={CLIENT_ID}"
                "&limit=100"
            )

            res_request = requests.get(
                url,
                headers=headers,
                timeout=10,
            )

            if res_request.status_code != 200:
                raise Exception(
                    f"Search Error "
                    f"{res_request.status_code}: "
                    f"{res_request.text}"
                )

            res = res_request.json()

            collection = (
                res.get(
                    "collection",
                    []
                )
                if isinstance(
                    res,
                    dict
                )
                else []
            )

            if not isinstance(
                collection,
                list
            ):
                collection = []

            new_tracks = []

            for t in collection:
                if not isinstance(
                    t,
                    dict
                ):
                    continue

                track_id = t.get("id")
                title = t.get("title")

                if not track_id or not title:
                    continue

                api_url = self._progressive_url(t)

                if not api_url:
                    continue

                new_tracks.append({
                    "id": track_id,
                    "title": title,
                    "api_url": api_url,
                    "permalink_url": t.get(
                        "permalink_url",
                        ""
                    ),
                    "liked": (
                        track_id
                        in self.liked_track_ids
                    ),
                })

            self.tracks = new_tracks
            self.showing_likes = False
            self.current_index = -1

            QTimer.singleShot(
                0,
                self.update_music_list
            )

            print(
                f"Search results: "
                f"{len(new_tracks)}"
            )

        except Exception as e:
            print(
                f"Search error: {e}"
            )

            QTimer.singleShot(
                0,
                lambda:
                    self.music_status.setText(
                        "[SEARCH FAILED]"
                    )
            )


    def update_music_list(self):
        # Список изменился — индексы в очереди/истории больше не валидны
        self.shuffle_queue = []
        self.shuffle_history = []

        self.track_list.clear()

        for track in self.tracks:
            prefix = (
                "♥ "
                if track.get("liked")
                else "♡ "
            )

            self.track_list.addItem(
                prefix + track.get(
                    "title",
                    "UNKNOWN TRACK"
                )
            )

        self._update_like_button()


    def _update_like_button(self):
        index = self.track_list.currentRow()

        if not (
            0 <= index < len(self.tracks)
        ):
            self.btn_like.setText("♡")
            return

        self.btn_like.setText(
            "♥"
            if self.tracks[index].get("liked")
            else "♡"
        )


    # =========================================================
    # BROWSER / LIKE BUTTON
    # =========================================================

    def _open_track_in_browser(
        self,
        url
    ):
        if not url:
            self._show_status_if_idle(
                "[NO TRACK URL]"
            )
            return

        try:
            threading.Thread(
                target=webbrowser.open,
                args=(url,),
                daemon=True
            ).start()

            print(
                "Opened SoundCloud track "
                "in browser:",
                url
            )

            self._show_status_if_idle(
                "[OPENED IN BROWSER]"
            )

        except Exception as e:
            print(
                "Could not open SoundCloud "
                f"in browser: {e}"
            )

            self._show_status_if_idle(
                "[BROWSER ERROR]"
            )


    def _show_status_if_idle(
        self,
        text
    ):
        self.music_status.setText(text)

        QTimer.singleShot(
            3000,
            self._restore_music_status
        )


    def _restore_music_status(self):
        if (
            self.player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        ):
            return

        if self.current_track:
            title = self.current_track[
                "title"
            ][:18]

            self.music_status.setText(
                f"[READY] | {title}..."
            )

        else:
            self.music_status.setText(
                "AUDIO: STANDBY"
            )


    def like_selected(self):
        track = self.current_track

        if not track:
            self._show_status_if_idle(
                "[NO PLAYING TRACK]"
            )
            return

        permalink = track.get(
            "permalink_url"
        )

        if permalink:
            self._open_track_in_browser(
                permalink
            )
            return

        track_id = track.get("id")

        if not track_id:
            self._show_status_if_idle(
                "[NO TRACK URL]"
            )
            return

        threading.Thread(
            target=self._load_permalink_and_open,
            args=(track_id,),
            daemon=True
        ).start()


    def _load_permalink_and_open(
        self,
        track_id
    ):
        if not OAUTH_TOKEN:
            return

        try:
            response = requests.get(
                (
                    "https://api-v2.soundcloud.com/"
                    f"tracks/{track_id}"
                ),
                params={
                    "client_id": CLIENT_ID
                },
                headers={
                    "Authorization": (
                        f"Bearer {OAUTH_TOKEN}"
                    ),
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=10,
            )

            if response.status_code != 200:
                raise Exception(
                    f"Track Error "
                    f"{response.status_code}: "
                    f"{response.text[:200]}"
                )

            data = response.json()

            permalink = data.get(
                "permalink_url"
            )

            if not permalink:
                raise Exception(
                    "SoundCloud did not return "
                    "permalink_url"
                )

            QTimer.singleShot(
                0,
                lambda url=permalink:
                    self._open_track_in_browser(
                        url
                    )
            )

        except Exception as e:
            print(
                "Could not get SoundCloud "
                f"track URL: {e}"
            )

            QTimer.singleShot(
                0,
                lambda:
                    self._show_status_if_idle(
                        "[BROWSER ERROR]"
                    )
            )


    def _apply_like_state(
        self,
        track_id,
        liked,
        source_track
    ):
        if liked:
            self.liked_track_ids.add(
                track_id
            )

            cached = source_track.copy()
            cached["liked"] = True

            self.liked_tracks_cache[
                track_id
            ] = cached

        else:
            self.liked_track_ids.discard(
                track_id
            )

            self.liked_tracks_cache.pop(
                track_id,
                None
            )

        for track in self.tracks:
            if track.get("id") == track_id:
                track["liked"] = liked

        if self.showing_likes:
            if (
                liked
                and not any(
                    t.get("id") == track_id
                    for t in self.tracks
                )
            ):
                self.tracks.insert(
                    0,
                    source_track.copy()
                )

                self.tracks[0][
                    "liked"
                ] = True

            elif not liked:
                self.tracks = [
                    t
                    for t in self.tracks
                    if t.get("id") != track_id
                ]

                if (
                    self.current_index
                    >= len(self.tracks)
                ):
                    self.current_index = (
                        len(self.tracks) - 1
                    )

        self.update_music_list()
        self._update_like_button()


    # =========================================================
    # PLAYBACK
    # =========================================================

    def play_selected(
        self,
        item
    ):
        index = self.track_list.row(item)

        if not (
            0 <= index < len(self.tracks)
        ):
            return

        self.skip_streak = 0

        if self._shuffle_active():
            if self.current_index >= 0:
                self._push_shuffle_history(
                    self.current_index
                )

            self.shuffle_queue = [
                i
                for i in self.shuffle_queue
                if i != index
            ]

        self.current_index = index

        self.current_track = (
            self.tracks[index].copy()
        )

        self._update_now_playing_title()

        self.retry_count = 0

        print(
            "PLAY SELECTED:",
            self.current_index,
            self.current_track.get("title")
        )

        self._start_async_download()


    def _start_async_download(self):
        if not self.current_track:
            if not (
                0 <= self.current_index
                < len(self.tracks)
            ):
                return

            self.current_track = (
                self.tracks[
                    self.current_index
                ].copy()
            )

            self._update_now_playing_title()

        title = self.current_track[
            "title"
        ][:18]

        self.music_status.setText(
            f"[LOADING] | {title}..."
        )

        self.player.stop()
        self.player.setSource(QUrl())

        # Пока трек грузится — звука нет, и кнопка/флаг кавалайзера
        # не должны показывать "играет"
        self.is_loading = True
        self.btn_play.setText("▶")

        try:
            Path("/tmp/neuro_playing").unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

        threading.Thread(
            target=self.download_and_play,
            args=(self.current_track.copy(),),
            daemon=True,
        ).start()


    def _is_current_track(
        self,
        track
    ):
        current = self.current_track

        return bool(
            current
            and current.get("id")
            == track.get("id")
        )


    def _download_track_file(
        self,
        track
    ):
        headers = {
            "Authorization": (
                f"Bearer {OAUTH_TOKEN}"
            )
        }

        api_url = track.get(
            "api_url"
        )

        if not api_url:
            raise Exception(
                "Progressive stream URL not found"
            )

        api_res = self.http.get(
            f"{api_url}?client_id={CLIENT_ID}",
            headers=headers,
            timeout=(4, 8),
        )

        if api_res.status_code != 200:
            raise Exception(
                f"Auth Error "
                f"{api_res.status_code}: "
                f"{api_res.text[:200]}"
            )

        api_data = api_res.json()

        stream_url = (
            api_data.get("url")
            if isinstance(
                api_data,
                dict
            )
            else None
        )

        if not stream_url:
            raise Exception(
                "SoundCloud returned no stream URL"
            )

        audio_data = self.http.get(
            stream_url,
            timeout=(4, 30),
        )

        if audio_data.status_code != 200:
            raise Exception(
                f"Net Error "
                f"{audio_data.status_code}"
            )

        if TEMP_TRACK.exists():
            try:
                TEMP_TRACK.unlink()
            except PermissionError:
                pass

        with open(
            TEMP_TRACK,
            "wb"
        ) as file:
            file.write(
                audio_data.content
            )

        file_size = (
            TEMP_TRACK.stat().st_size
        )

        if file_size < 50000:
            raise Exception(
                f"File too small: "
                f"{file_size} bytes"
            )


    def download_and_play(
        self,
        track
    ):
        # Все ретраи крутятся прямо в рабочем потоке — без перезапуска
        # всей цепочки и без ожидания таймеров главного потока.
        # Результат уходит в главный поток только сигналами.
        if not OAUTH_TOKEN:
            self.download_failed.emit(track)
            return

        for attempt in range(
            1,
            self.max_attempts + 1
        ):
            # Пользователь уже переключил трек — эту загрузку бросаем
            if not self._is_current_track(track):
                return

            started = time.time()

            try:
                self._download_track_file(track)

            except Exception as e:
                print(
                    f"Playback attempt "
                    f"{attempt}/{self.max_attempts} "
                    f"failed in "
                    f"{time.time() - started:.1f}s: "
                    f"{e}"
                )

                if attempt < self.max_attempts:
                    self.download_status.emit(
                        f"[RETRY {attempt}/"
                        f"{self.max_attempts}]..."
                    )

                    time.sleep(
                        self.retry_delay * attempt
                    )

                continue

            if self._is_current_track(track):
                self.download_succeeded.emit()

            return

        if self._is_current_track(track):
            self.download_failed.emit(track)


    def _handle_track_failure(
        self,
        track
    ):
        # Вызывается из mediaStatus: файл скачан, но плеер не смог его
        # декодировать (InvalidMedia). Повторов тут немного.

        # Пока шёл запрос, пользователь мог переключить трек —
        # тогда ошибка старого трека нас не касается
        if (
            not self.current_track
            or self.current_track.get("id")
            != track.get("id")
        ):
            return

        self.retry_count += 1

        attempt = self.retry_count
        track_id = track.get("id")

        if attempt < self.max_decode_attempts:
            self.music_status.setText(
                f"[RETRY {attempt}/"
                f"{self.max_decode_attempts}]..."
            )

            QTimer.singleShot(
                500,
                lambda tid=track_id:
                    self._retry_if_current(tid)
            )

        else:
            self._skip_failed_track(track)


    def _retry_if_current(
        self,
        track_id
    ):
        if (
            self.current_track
            and self.current_track.get("id")
            == track_id
        ):
            self._start_async_download()


    def _skip_failed_track(
        self,
        track
    ):
        track_id = track.get("id")

        if (
            not self.current_track
            or self.current_track.get("id")
            != track_id
        ):
            return

        self.skip_streak += 1

        title = track.get(
            "title",
            "TRACK"
        )[:14]

        print(
            f"SKIP TRACK after "
            f"{self.max_attempts} attempts:",
            track.get("title"),
            "| STREAK:",
            self.skip_streak
        )

        # Если подряд не загрузились все треки списка — останавливаемся,
        # иначе будем крутиться по кругу вечно
        if self.skip_streak >= len(self.tracks):
            self.is_loading = False

            self.music_status.setText(
                "[ALL TRACKS FAILED]"
            )

            self.btn_play.setText("▶")

            self._update_playback_flag(False)

            return

        self.music_status.setText(
            f"[SKIP] {title}"
        )

        QTimer.singleShot(
            200,
            lambda tid=track_id:
                self._next_if_current(tid)
        )


    def _next_if_current(
        self,
        track_id
    ):
        if (
            self.current_track
            and self.current_track.get("id")
            == track_id
        ):
            self.next_track()


    def start_playback(self):
        if not TEMP_TRACK.exists():
            print(
                "TEMP TRACK DOES NOT EXIST"
            )
            return

        self.skip_streak = 0
        self.is_loading = False

        self.update_audio_output()

        self.player.stop()

        source = QUrl.fromLocalFile(
            str(
                TEMP_TRACK.resolve()
            )
        )

        self.player.setSource(source)
        self.player.setPlaybackRate(1.0)

        print(
            "SOURCE:",
            TEMP_TRACK.resolve()
        )

        print(
            "PLAYBACK RATE BEFORE PLAY:",
            self.player.playbackRate()
        )

        self.player.play()

        QTimer.singleShot(
            100,
            lambda:
                self.player.setPlaybackRate(1.0)
        )

        QTimer.singleShot(
            150,
            lambda:
                print(
                    "PLAYBACK RATE AFTER START:",
                    self.player.playbackRate(),
                    "| DURATION:",
                    self.player.duration()
                )
        )

        self.btn_play.setText("⏸")

        self._update_now_playing_title()
        self._update_playback_flag(True)


    def toggle_play(self):
        if (
            self.player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        ):
            self.player.pause()

            self.btn_play.setText("▶")

            self._update_playback_flag(False)

            if self.current_track:
                title = self.current_track[
                    "title"
                ][:18]

                self.music_status.setText(
                    f"[PAUSED] | {title}..."
                )

        else:
            has_media = (
                self.player.source().isValid()
                and self.player.mediaStatus()
                not in (
                    QMediaPlayer.MediaStatus.NoMedia,
                    QMediaPlayer.MediaStatus.InvalidMedia,
                )
            )

            if (
                has_media
                and self.current_track
            ):
                self.update_audio_output()

                self.player.setPlaybackRate(1.0)

                print(
                    "RESUME RATE:",
                    self.player.playbackRate(),
                    "| POS:",
                    self.player.position(),
                    "| DUR:",
                    self.player.duration()
                )

                self.player.play()

                self.btn_play.setText("⏸")

                self._update_playback_flag(True)

            elif self.current_track:
                # Трек ещё грузится / ретраится — второй поток не запускаем
                if self.is_loading:
                    return

                self.retry_count = 0
                self.skip_streak = 0
                self._start_async_download()


    def _on_next_clicked(self):
        self.skip_streak = 0
        self.next_track()


    def _on_prev_clicked(self):
        self.skip_streak = 0
        self.prev_track()


    def toggle_shuffle(
        self,
        checked
    ):
        self.shuffle_enabled = bool(checked)

        self.shuffle_queue = []
        self.shuffle_history = []

        self._show_status_if_idle(
            "[SHUFFLE ON]"
            if self.shuffle_enabled
            else "[SHUFFLE OFF]"
        )


    def _shuffle_active(self):
        return (
            self.shuffle_enabled
            and self.showing_likes
            and len(self.tracks) > 1
        )


    def _push_shuffle_history(
        self,
        index
    ):
        self.shuffle_history.append(index)

        if len(self.shuffle_history) > 200:
            del self.shuffle_history[:-200]


    def _pick_shuffle_index(self):
        count = len(self.tracks)

        self.shuffle_queue = [
            i
            for i in self.shuffle_queue
            if 0 <= i < count
            and i != self.current_index
        ]

        # Очередь кончилась — тасуем заново, все треки кроме текущего,
        # чтобы один и тот же трек не шёл два раза подряд
        if not self.shuffle_queue:
            self.shuffle_queue = [
                i
                for i in range(count)
                if i != self.current_index
            ]

            random.shuffle(
                self.shuffle_queue
            )

        return self.shuffle_queue.pop()


    def next_track(self):
        if not self.tracks:
            return

        if self._shuffle_active():
            if self.current_index >= 0:
                self._push_shuffle_history(
                    self.current_index
                )

            next_index = (
                self._pick_shuffle_index()
            )

        elif self.current_index < 0:
            next_index = 0

        else:
            next_index = (
                self.current_index + 1
            ) % len(self.tracks)

        self.current_index = next_index

        self.current_track = (
            self.tracks[
                self.current_index
            ].copy()
        )

        self._update_now_playing_title()

        print(
            "NEXT TRACK:",
            self.current_index,
            "| MODE:",
            (
                "LIKES"
                if self.showing_likes
                else "SEARCH"
            ),
            "| TITLE:",
            self.current_track.get(
                "title"
            )
        )

        self.track_list.setCurrentRow(
            self.current_index
        )

        self._update_like_button()
        self.retry_count = 0

        self._start_async_download()


    def prev_track(self):
        if not self.tracks:
            return

        prev_index = None

        # В shuffle "назад" = вернуться к реально игравшему предыдущему треку
        if self._shuffle_active():
            while self.shuffle_history:
                candidate = (
                    self.shuffle_history.pop()
                )

                if (
                    0 <= candidate
                    < len(self.tracks)
                ):
                    prev_index = candidate
                    break

        if prev_index is not None:
            pass

        elif self.current_index < 0:
            prev_index = (
                len(self.tracks) - 1
            )

        else:
            prev_index = (
                self.current_index - 1
            ) % len(self.tracks)

        self.current_index = prev_index

        self.current_track = (
            self.tracks[
                self.current_index
            ].copy()
        )

        self._update_now_playing_title()

        print(
            "PREVIOUS TRACK:",
            self.current_index,
            "| MODE:",
            (
                "LIKES"
                if self.showing_likes
                else "SEARCH"
            ),
            "| TITLE:",
            self.current_track.get(
                "title"
            )
        )

        self.track_list.setCurrentRow(
            self.current_index
        )

        self._update_like_button()
        self.retry_count = 0

        self._start_async_download()


    # =========================================================
    # MEDIA STATUS / ERROR
    # =========================================================

    def handle_media_status(
        self,
        status
    ):
        print(
            "MEDIA STATUS:",
            status,
            "| CURRENT INDEX:",
            self.current_index,
            "| MODE:",
            (
                "LIKES"
                if self.showing_likes
                else "SEARCH"
            ),
            "| POSITION:",
            self.player.position(),
            "| DURATION:",
            self.player.duration(),
            "| RATE:",
            self.player.playbackRate()
        )

        if (
            status
            == QMediaPlayer.MediaStatus.EndOfMedia
        ):
            self._update_playback_flag(False)

            self.player.stop()

            QTimer.singleShot(
                200,
                self.next_track
            )

        if (
            status
            == QMediaPlayer.MediaStatus.InvalidMedia
            and self.current_track
        ):
            self._update_playback_flag(False)

            self.btn_play.setText("▶")

            self._handle_track_failure(
                self.current_track.copy()
            )


    def handle_player_error(
        self,
        error,
        error_string
    ):
        if (
            error
            != QMediaPlayer.Error.NoError
        ):
            print(
                f"QMediaPlayer error: "
                f"{error} | "
                f"{error_string}"
            )


    # =========================================================
    # ПИТОМЕЦ
    # =========================================================

    def _refresh_pet_ui(self):
        last_fed = load_last_fed()
        hunger_val = int(get_hunger_percentage(last_fed))
        sleeping = is_sleep_time()

        self.pet_hunger_bar.setValue(hunger_val)

        chunk_color = "#9f5fb8" if hunger_val > 0 else "#ff3333"
        self.pet_hunger_bar.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid #47474f;
                border-radius: 1px;
                background-color: rgb(6, 5, 10);
            }}
            QProgressBar::chunk {{
                background-color: {chunk_color};
                border-radius: 1px;
            }}
        """)

        if sleeping:
            self.pet_feed_button.setText("Zzz...")
            self.pet_feed_button.setEnabled(False)
        else:
            self.pet_feed_button.setText("FEED")
            self.pet_feed_button.setEnabled(True)

    def _on_feed_pet_clicked(self):
        if is_sleep_time():
            return

        save_last_fed(time.time())
        self._refresh_pet_ui()

    # =========================================================
    # NOTES
    # =========================================================

    def refresh_notes(self):
        current_item = self.notes_list.currentItem()
        selected_name = current_item.text() if current_item else None
        self.notes_list.clear()

        files = sorted(
            self.notes_path.glob("*.md"),
            key=os.path.getmtime,
            reverse=True
        )

        for file in files:
            self.notes_list.addItem(
                file.name
            )
        if selected_name:
            items = self.notes_list.findItems(selected_name, Qt.MatchFlag.MatchExactly)
            if items:
                self.notes_list.setCurrentItem(items[0])


    def load_note(
        self,
        item
    ):
        self.current_note_file = (
            self.notes_path
            / item.text()
        )

        if not self.current_note_file.exists():
            self.current_note_file = None

            self.refresh_notes()

            return

        self.input_title.setText(
            self.current_note_file.stem
        )

        try:
            self.input_content.setText(
                self.current_note_file.read_text(
                    encoding="utf-8"
                )
            )

        except Exception as e:
            print(
                "Ошибка чтения заметки:",
                e
            )


    def new_note(self):
        self.current_note_file = None

        self.input_title.clear()
        self.input_content.clear()


    def save_note(self):
        title = (
            self.input_title
            .text()
            .strip()
            or "unnamed"
        )

        new_path = (
            self.notes_path
            / f"{title}.md"
        )

        try:
            if (
                self.current_note_file
                and self.current_note_file.exists()
                and self.current_note_file != new_path
            ):
                self.current_note_file.unlink()

            new_path.write_text(
                self.input_content.toPlainText(),
                encoding="utf-8"
            )

            self.current_note_file = new_path

            try:
                abs_path = str(
                    new_path.resolve()
                )

                add_tag(
                    abs_path,
                    "note"
                )

            except Exception as e:
                print(
                    "Ошибка при сохранении тега:",
                    e
                )

            self.refresh_notes()

            items = self.notes_list.findItems(
                new_path.name,
                Qt.MatchFlag.MatchExactly
            )

            if items:
                self.notes_list.setCurrentItem(
                    items[0]
                )

            return True

        except Exception as e:
            print(
                "Ошибка сохранения заметки:",
                e
            )

            return False


    def delete_note(self):
        note_file = self.current_note_file

        if note_file is None:
            item = self.notes_list.currentItem()

            if item:
                note_file = (
                    self.notes_path
                    / item.text()
                )

        if not note_file:
            QMessageBox.information(
                self,
                "DELETE NOTE",
                "Сначала выбери заметку."
            )

            return

        if not note_file.exists():
            self.current_note_file = None

            self.refresh_notes()

            self.input_title.clear()
            self.input_content.clear()

            return

        reply = QMessageBox.question(
            self,
            "DELETE NOTE",
            f"Удалить заметку\n"
            f"«{note_file.stem}»?",
            (
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
            ),
            QMessageBox.StandardButton.No
        )

        if (
            reply
            != QMessageBox.StandardButton.Yes
        ):
            return

        try:
            note_file.unlink()

            self.current_note_file = None

            self.input_title.clear()
            self.input_content.clear()

            self.refresh_notes()

            print(
                "NOTE DELETED:",
                note_file
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "DELETE ERROR",
                f"Не удалось удалить заметку:\n{e}"
            )


    # =========================================================
    # PLAYING FLAG
    # =========================================================

    def _update_now_playing_title(self):
        title = ""

        if self.current_track:
            title = str(
                self.current_track.get("title", "")
            ).strip()

        try:
            if title:
                NOW_PLAYING_TITLE.write_text(
                    title,
                    encoding="utf-8"
                )
            elif NOW_PLAYING_TITLE.exists():
                NOW_PLAYING_TITLE.unlink()
        except Exception as e:
            print(
                "NOW PLAYING TITLE ERROR:",
                repr(e)
            )


    def _update_playback_flag(
        self,
        playing: bool
    ):
        flag = Path(
            "/tmp/neuro_playing"
        )

        if playing:
            flag.touch()

        elif flag.exists():
            flag.unlink()

            if NOW_PLAYING_TITLE.exists():
                try:
                    NOW_PLAYING_TITLE.unlink()
                except OSError:
                    pass


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    signal.signal(
        signal.SIGINT,
        signal.SIG_DFL
    )

    app = QApplication(sys.argv)

    panel = NeuroSystemPanel()

    sys.exit(
        app.exec()
    )
