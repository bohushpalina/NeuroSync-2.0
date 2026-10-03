import sys
import os
import urllib.request
import json
from pathlib import Path
from datetime import datetime
import time

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QDialog,
    QTextEdit,
    QDialogButtonBox
)
from PyQt6.QtCore import Qt, QTimer, QPoint

from ns_config import COLORS, FONT_FAMILY, frame_style, button_style, dialog_style, OBSIDIAN_VAULT

# Форсируем X11 для Linux
os.environ["QT_QPA_PLATFORM"] = "xcb"


# =========================
# НАСТРОЙКИ OBSIDIAN
# =========================

MAIN_JOURNAL_NOTE = "Diary.md"
LIVE_STATE_FILE = Path.home() / ".neurosync_live.json"

# =========================
# ПЕРЕТАСКИВАЕМОЕ СЕРДЕЧКО
# =========================

class HeartButton(QPushButton):
    def __init__(self, parent_widget):
        super().__init__("♥", parent_widget)

        self.parent_widget = parent_widget
        self.drag_start_pos = None
        self.was_dragged = False

        self.setFixedSize(46, 46)

        self.setCursor(
            Qt.CursorShape.PointingHandCursor
        )

        self.setToolTip(
            "Нажми — развернуть • потяни — переместить"
        )

        self.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(10, 2, 5, 245);
                color: {COLORS['pink']};
                border: 2px solid {COLORS['pink']};
                border-radius: 23px;
                font-family: '{FONT_FAMILY}';
                font-size: 22px;
                font-weight: bold;
                padding: 0;
            }}

            QPushButton:hover {{
                background-color: {COLORS['pink']};
                color: {COLORS['white']};
            }}
        """)

        self.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_start_pos = (
                event.globalPosition().toPoint()
            )
            self.was_dragged = False
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self.drag_start_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            current_pos = (
                event.globalPosition().toPoint()
            )

            delta = (
                current_pos
                - self.drag_start_pos
            )

            if delta.manhattanLength() > 5:
                self.was_dragged = True

            if self.was_dragged:
                self.parent_widget.move(
                    self.parent_widget.pos() + delta
                )

                self.drag_start_pos = current_pos

            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragged = self.was_dragged

            self.drag_start_pos = None
            self.was_dragged = False

            if not was_dragged:
                self.parent_widget.expand_widget()

            event.accept()
            return

        super().mouseReleaseEvent(event)


class StudyTimerWidget(QWidget):
    def __init__(self):
        super().__init__()

        self.old_pos = None
        self.seconds_elapsed = 0
        self.is_running = False
        self.is_minimized = False

        self.widget_width = 310
        self.widget_height = 135

        self.init_ui()
        self.init_logic()

        self.setGeometry(
            100,
            100,
            self.widget_width,
            self.widget_height
        )

        self.fetch_quote()

    # =========================
    # UI
    # =========================

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )

        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(
            0,
            0,
            0,
            0
        )

        # =========================
        # ОСНОВНОЙ FRAME
        # =========================

        self.main_frame = QFrame()
        self.main_frame.setObjectName(
            "WidgetFrame"
        )

        self.main_frame.setStyleSheet(frame_style(radius=4, font_size=11))

        self.layout.addWidget(
            self.main_frame
        )

        self.content_layout = QVBoxLayout(
            self.main_frame
        )

        self.content_layout.setContentsMargins(
            8,
            6,
            8,
            6
        )

        self.content_layout.setSpacing(4)

        # =========================
        # ВЕРХНЯЯ СТРОКА
        # =========================

        header_layout = QHBoxLayout()

        header_layout.setContentsMargins(
            0,
            0,
            0,
            0
        )

        header_layout.setSpacing(4)

        self.quote_label = QLabel(
            "⚡ [ ! ] // FOCUS_MODE"
        )

        self.quote_label.setWordWrap(True)

        self.quote_label.setStyleSheet(f"""
            color: {COLORS['white']};
            font-family: '{FONT_FAMILY}';
            font-size: 11px;
            font-weight: bold;
        """)

        self.quote_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        header_layout.addWidget(
            self.quote_label
        )

        # =========================
        # КНОПКА СЕРДЦА
        # =========================

        self.mini_btn = QPushButton(
            "♥"
        )

        self.mini_btn.setFixedSize(
            28,
            24
        )

        self.mini_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )

        self.mini_btn.setToolTip(
            "Свернуть в сердечко"
        )

        self.mini_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {COLORS['pink']};
                border: none;
                font-family: '{FONT_FAMILY}';
                font-size: 17px;
                font-weight: bold;
                padding: 0;
            }}

            QPushButton:hover {{
                color: {COLORS['white']};
            }}
        """)

        self.mini_btn.clicked.connect(
            self.minimize_widget
        )

        header_layout.addWidget(
            self.mini_btn
        )

        self.content_layout.addLayout(
            header_layout
        )

        # =========================
        # ТАЙМЕР
        # =========================

        self.time_value = QLabel(
            "00:00:00"
        )

        self.time_value.setStyleSheet(f"""
            font-size: 16px;
            color: {COLORS['pink']};
            font-weight: bold;
            font-family: '{FONT_FAMILY}';
        """)

        self.time_value.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        self.content_layout.addWidget(
            self.time_value
        )

        # =========================
        # КНОПКИ
        # =========================

        self.buttons_layout = QHBoxLayout()

        self.buttons_layout.setSpacing(4)

        self.buttons_layout.setContentsMargins(
            0,
            0,
            0,
            0
        )

        # =========================
        # СТАРТ / СТОП
        # =========================

        self.toggle_btn = QPushButton(
            "СТАРТ"
        )

        self.toggle_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )

        self.apply_start_btn_style()

        self.toggle_btn.clicked.connect(
            self.toggle_timer
        )

        # =========================
        # СБРОС
        # =========================

        self.reset_btn = QPushButton(
            "СБРОС"
        )

        self.reset_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )

        self.reset_btn.setStyleSheet(button_style(color=COLORS["cyan"], radius=3))

        self.reset_btn.clicked.connect(
            self.reset_timer
        )

        

        # =========================
        # ЦИТАТА
        # =========================

        self.quote_btn = QPushButton(
            "ЦИТАТА"
        )

        self.quote_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )

        self.quote_btn.setStyleSheet(button_style(color=COLORS["purple"], radius=3, bold=False))

        self.quote_btn.clicked.connect(
            self.fetch_quote
        )

        self.buttons_layout.addWidget(
            self.toggle_btn
        )

        self.buttons_layout.addWidget(
            self.reset_btn
        )

        self.buttons_layout.addWidget(
            self.quote_btn
        )

        self.content_layout.addLayout(
            self.buttons_layout
        )

        # =========================
        # МАЛЕНЬКОЕ СЕРДЦЕ
        # =========================

        self.mini_heart_btn = HeartButton(
            self
        )

    # =========================
    # ЛОГИКА ТАЙМЕРА
    # =========================

    def init_logic(self):
        self.timer = QTimer(self)

        self.timer.timeout.connect(
            self.update_time
        )

        self.timer.start(
            1000
        )

    # =========================
    # МИНИ-РЕЖИМ
    # =========================

    def minimize_widget(self):
        if self.is_minimized:
            return

        # Запоминаем текущую левую верхнюю точку
        # перед сворачиванием.
        self.saved_x = self.x()
        self.saved_y = self.y()

        self.is_minimized = True

        # Прячем большую панель.
        self.main_frame.hide()

        # Размер окна становится размером сердца.
        self.resize(
            46,
            46
        )

        # Сердце занимает всё окно.
        self.mini_heart_btn.show()
        self.mini_heart_btn.setGeometry(
            0,
            0,
            46,
            46
        )

        self.mini_heart_btn.raise_()

    def expand_widget(self):
        if not self.is_minimized:
            return

        # Сердце находится в текущей позиции окна.
        # Поэтому обычная панель появится именно здесь.
        x = self.x()
        y = self.y()

        self.is_minimized = False

        self.mini_heart_btn.hide()

        self.resize(
            self.widget_width,
            self.widget_height
        )

        self.main_frame.setGeometry(
            0,
            0,
            self.widget_width,
            self.widget_height
        )

        self.main_frame.show()

        # Не возвращаем старую позицию —
        # используем текущую позицию сердечка.
        self.move(
            x,
            y
        )

        self.main_frame.raise_()
    def write_live_state(self, active, elapsed):
        try:
            data = {
                "active": active,
                "elapsed_seconds": elapsed,
                "started_at": time.time() - elapsed if active else None,
                "updated_at": time.time()
            }

            with open(
                LIVE_STATE_FILE,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(data, f)

        except OSError:
            pass

    def toggle_minimized(self):
        if self.is_minimized:
            self.expand_widget()
        else:
            self.minimize_widget()

    # =========================
    # СТИЛЬ СТАРТ
    # =========================

    def apply_start_btn_style(self):
        self.toggle_btn.setText(
            "СТАРТ"
        )

        self.toggle_btn.setStyleSheet(button_style(color=COLORS["pink"], radius=3))

    # =========================
    # СТИЛЬ СТОП
    # =========================

    def apply_stop_btn_style(self):
        self.toggle_btn.setText(
            "СТОП"
        )

        self.toggle_btn.setStyleSheet(button_style(color=COLORS["pink"], radius=3, filled=True))

    # =========================
    # ПОЛУЧЕНИЕ ЦИТАТЫ
    # =========================

    def fetch_quote(self):
        try:
            url = (
                "https://zenquotes.io/api/random"
            )

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )

            with urllib.request.urlopen(
                req,
                timeout=3
            ) as response:
                data = json.loads(
                    response.read().decode()
                )

                q_text = data[0]["q"]

                self.quote_label.setText(
                    f"# {q_text}"
                )

        except Exception:
            self.quote_label.setText(
                ">> CODE // COMPILE // REPEAT <<"
            )

    # =========================
    # СТАРТ / СТОП
    # =========================

    def toggle_timer(self):
        if self.is_running:
            self.is_running = False
            self.write_live_state(
                True,
                self.seconds_elapsed
            )
            self.apply_start_btn_style()

        else:
            self.is_running = True
            self.write_live_state(
                True,
                self.seconds_elapsed
            )
            self.apply_stop_btn_style()

    # =========================
    # СБРОС
    # =========================

    def reset_timer(self):
        # Открываем окно записи.
        self.prompt_and_save_log()
        self.write_live_state(False,0)

        # После этого сбрасываем таймер.
        self.is_running = False
        self.seconds_elapsed = 0

        self.time_value.setText(
            "00:00:00"
        )

        self.apply_start_btn_style()

    # =========================
    # ОБНОВЛЕНИЕ ВРЕМЕНИ
    # =========================

    def update_time(self):
        if self.is_running:
            self.seconds_elapsed += 1

            hours = self.seconds_elapsed // 3600
            minutes = (self.seconds_elapsed % 3600) // 60
            seconds = self.seconds_elapsed % 60

            self.time_value.setText(
                f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            )

            self.write_live_state(
                True,
                self.seconds_elapsed
            )

    # =========================
    # ОКНО: ЧТО СДЕЛАНО
    # =========================

    def prompt_and_save_log(self):
        dialog = QDialog(
            self
        )

        dialog.setWindowTitle(
            "OBSIDIAN_LOG"
        )

        dialog.resize(
            500,
            320
        )

        dialog.setStyleSheet(dialog_style())

        layout = QVBoxLayout(
            dialog
        )

        layout.setContentsMargins(
            14,
            14,
            14,
            14
        )

        layout.setSpacing(
            10
        )

        label = QLabel(
            "Что сделано за сессию?"
        )

        layout.addWidget(
            label
        )

        text_edit = QTextEdit()

        text_edit.setPlaceholderText(
            "Например:\n"
            "• Разобрала тему по Java\n"
            "• Сделала лабораторную №3\n"
            "• Исправила ошибки в проекте"
        )

        text_edit.setMinimumHeight(
            180
        )

        layout.addWidget(
            text_edit
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )

        buttons.accepted.connect(
            dialog.accept
        )

        buttons.rejected.connect(
            dialog.reject
        )

        layout.addWidget(
            buttons
        )

        if (
            dialog.exec()
            == QDialog.DialogCode.Accepted
        ):
            comment = (
                text_edit
                .toPlainText()
                .strip()
            )

            if comment:
                self.write_to_obsidian(
                    comment
                )

    # =========================
    # ЗАПИСЬ В OBSIDIAN
    # =========================

    def write_to_obsidian(
        self,
        comment
    ):
        try:
            OBSIDIAN_VAULT.mkdir(
                parents=True,
                exist_ok=True
            )

            now = datetime.now()

            date_str = now.strftime(
                "%Y-%m-%d"
            )

            time_str = now.strftime(
                "%H:%M"
            )

            duration_str = (
                self.time_value.text()
            )

            daily_note_path = (
                OBSIDIAN_VAULT
                / f"{date_str}.md"
            )

            main_note_path = (
                OBSIDIAN_VAULT
                / MAIN_JOURNAL_NOTE
            )

            # ЗАПИСЬ СЕССИИ
            file_is_new = (
                not daily_note_path.exists()
            )

            with open(
                daily_note_path,
                "a",
                encoding="utf-8"
            ) as f:

                if file_is_new:
                    f.write(
                        f"# Note date: "
                        f"{date_str}\n\n"
                    )

                f.write(
                    f"### Session: "
                    f"{time_str} "
                    f"({duration_str})\n"
                )

                f.write(
                    f"- {comment}\n\n"
                )

            # ДОБАВЛЕНИЕ ССЫЛКИ
            link_tag = (
                f"- [[{date_str}]]"
            )

            main_content = ""

            if main_note_path.exists():
                with open(
                    main_note_path,
                    "r",
                    encoding="utf-8"
                ) as f:
                    main_content = f.read()

            if link_tag not in main_content:
                with open(
                    main_note_path,
                    "a",
                    encoding="utf-8"
                ) as f:

                    if (
                        main_content
                        and not main_content.endswith(
                            "\n"
                        )
                    ):
                        f.write("\n")

                    f.write(
                        f"{link_tag}\n"
                    )

        except Exception as e:
            print(
                f"Ошибка записи в Obsidian: {e}"
            )

    # =========================
    # ПЕРЕТАСКИВАНИЕ ОБЫЧНОГО
    # ВИДЖЕТА
    # =========================

    def mousePressEvent(
        self,
        event
    ):
        if (
            event.button()
            == Qt.MouseButton.LeftButton
        ):
            self.old_pos = (
                event.globalPosition()
                .toPoint()
            )

    def mouseMoveEvent(
        self,
        event
    ):
        if self.old_pos is not None:
            delta = (
                event.globalPosition()
                .toPoint()
                - self.old_pos
            )

            self.move(
                self.x() + delta.x(),
                self.y() + delta.y()
            )

            self.old_pos = (
                event.globalPosition()
                .toPoint()
            )

    def mouseReleaseEvent(
        self,
        event
    ):
        self.old_pos = None


# =========================
# ЗАПУСК ПРИЛОЖЕНИЯ
# =========================

if __name__ == "__main__":
    app = QApplication(
        sys.argv
    )

    widget = StudyTimerWidget()

    widget.show()

    sys.exit(
        app.exec()
    )
