from ns_config import *
import sys
import os
import random
import re
from pathlib import Path

import markdown

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QPushButton,
    QTextBrowser,
    QSizePolicy
)
from PyQt6.QtCore import Qt


os.environ["QT_QPA_PLATFORM"] = "xcb"


# ============================================================
# CONFIG
# ============================================================


WIDGET_WIDTH = 300
WIDGET_HEIGHT = 450

HORIZONTAL_PADDING = 10
TEXT_WIDTH = WIDGET_WIDTH - HORIZONTAL_PADDING * 2

EXCLUDED_FILES = {
    "diary.md",
}

DATE_IN_FILENAME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}"
)

GENERATED_MARKER = "neuro:generated_topic"


# ============================================================
# WIDGET
# ============================================================

class NeuroObsidianWidget(QWidget):

    def __init__(self):
        super().__init__()

        self.old_pos = None

        self.initial_x = 100
        self.initial_y = 220

        self.init_ui()

        # Размер и положение ВСЕГДА фиксированы.
        self.setFixedSize(
            WIDGET_WIDTH,
            WIDGET_HEIGHT
        )

        self.move(
            self.initial_x,
            self.initial_y
        )

        self.refresh_random_note()

    # ========================================================
    # OBSIDIAN
    # ========================================================

    def is_valid_file(self, path: Path):
        name = path.name.lower()

        # Diary.md
        if name in EXCLUDED_FILES:
            return False

        # Любой файл, в имени которого есть дата.
        if DATE_IN_FILENAME_PATTERN.search(path.name):
            return False

        try:
            with open(
                path,
                "r",
                encoding="utf-8"
            ) as f:
                content = f.read()

            # Берет только файлы с пометкой автогенерации
            if GENERATED_MARKER not in content:
                return False

            # Пропускает учебные заметки с #STUDY
            if "#STUDY" in content:
                return False

        except (OSError, UnicodeDecodeError):
            return False

        return True

    def read_file(self, path: Path):
        try:
            with open(
                path,
                "r",
                encoding="utf-8"
            ) as f:
                return f.read()

        except (OSError, UnicodeDecodeError):
            return ""

    # ========================================================
    # FIND ALL MARKDOWN FILES
    # ========================================================

    def get_all_markdown_files(self):
        """
        Полностью обходит всё дерево Obsidian.
        Собираются только файлы с пометкой автогенерации.
        """

        if not OBSIDIAN_VAULT.exists():
            return []

        result = []

        for path in OBSIDIAN_VAULT.rglob("*.md"):

            if not path.is_file():
                continue

            if path.name.startswith("."):
                continue

            if not self.is_valid_file(path):
                continue

            result.append(path)

        return result

    # ========================================================
    # CONTENT EXTRACTION
    # ========================================================

    def extract_note_content(self, text):
        """
        Забирает текст после всех верхних комментариев и хештегов
        и до самого низа файла.
        """

        if not text.strip():
            return ""

        lines = text.splitlines()
        last_meta_idx = -1

        # Поиск верхней границы метаданных и хештегов
        for i, line in enumerate(lines[:25]):
            stripped = line.strip()

            if not stripped:
                continue

            is_comment = stripped.startswith("<!--") or stripped.endswith("-->")
            words = stripped.split()
            is_hashtag_line = bool(words) and all(
                w.startswith("#") and not w.startswith("##") and len(w) > 1
                for w in words
            )
            has_hashtag = (
                bool(re.search(r"(?<!\w)#[a-zA-Zа-яА-Я0-9_-]+", stripped))
                and not stripped.startswith("#")
            )

            if is_comment or is_hashtag_line or has_hashtag:
                last_meta_idx = i

        if last_meta_idx != -1:
            content_lines = lines[last_meta_idx + 1:]
        else:
            content_lines = lines

        # Очистка пустых строк в начале текста
        while content_lines and not content_lines[0].strip():
            content_lines.pop(0)

        return "\n".join(content_lines).strip()

    # ========================================================
    # MARKDOWN
    # ========================================================

    def prepare_markdown(self, text):

        # Obsidian [[WikiLink]]
        text = re.sub(
            r"\[\[([^|\]]+)(?:\|([^\]]+))?\]\]",
            lambda m: m.group(2) or m.group(1),
            text
        )

        # Obsidian ==highlight==
        text = re.sub(
            r"==(.+?)==",
            r"<mark>\1</mark>",
            text
        )

        return text

    def markdown_to_html(self, text):

        text = self.prepare_markdown(text)

        rendered = markdown.markdown(
            text,
            extensions=[
                "tables",
                "fenced_code",
                "sane_lists",
            ]
        )

        # Гарантированный обход бага QTextBrowser:
        # заменяем семантические теги h1-h6 на div с классами,
        # чтобы сбросить жестко вшитые в Qt множители масштаба.
        for i in range(1, 7):
            rendered = re.sub(
                rf"<h{i}[^>]*>(.*?)</h{i}>",
                rf'<div class="h{i}">\1</div>',
                rendered,
                flags=re.DOTALL | re.IGNORECASE
            )

        return f"""
        <html>
        <head>
        <style>

            body {{
                background: transparent;
                color: {COLORS['text']};
                font-family: '{FONT_FAMILY}', 'Consolas', sans-serif;
                font-size: 13px;  /* Основной текст оставлен 13px */
                margin: 0;
                padding: 0;
            }}

            p {{
                margin-top: 0;
                margin-bottom: 9px;
            }}

            /* Заголовки теперь рендерятся через классы и строго слушаются font-size */
            .h1 {{
                color: {COLORS['pink_bright']};
                font-size: 14px; 
                font-weight: bold;
                margin-top: 2px;
                margin-bottom: 2px;
            }}

            .h2 {{
                color: {COLORS['pink_bright']};
                font-size: 13px;
                font-weight: bold;
                margin-top: 2px;
                margin-bottom: 2px;
            }}

            .h3, .h4, .h5, .h6 {{
                color: {COLORS['pink_bright']};
                font-size: 12px;
                font-weight: bold;
                margin-top: 2px;
                margin-bottom: 2px;
            }}

            strong {{
                color: {COLORS['white']};
                font-weight: bold;
            }}

            em {{
                color: {COLORS['pink_soft']};
            }}

            a {{
                color: {COLORS['pink_bright']};
            }}

            blockquote {{
                margin: 5px 0;
                padding-left: 8px;
                border-left: 2px solid {COLORS['pink']};
                color: {COLORS['text_muted']};
            }}

            ul, ol {{
                margin-top: 2px;
                margin-bottom: 6px;
                padding-left: 18px;
            }}

            li {{
                margin-bottom: 2px;
            }}

            code {{
                color: #ff8ac5;
                background: #180611;
                padding: 1px 3px;
            }}

            pre {{
                background: #12040b;
                border: 1px solid #5b163c;
                padding: 7px;
                white-space: pre-wrap;
            }}

            pre code {{
                background: transparent;
                padding: 0;
            }}

            table {{
                border-collapse: collapse;
                margin-top: 5px;
                margin-bottom: 6px;
            }}

            th {{
                color: {COLORS['pink_bright']};
                font-weight: bold;
            }}

            th, td {{
                border: 1px solid #6e1743;
                padding: 4px 6px;
            }}

            tr {{
                background: #100309;
            }}

            mark {{
                background: #5b1640;
                color: {COLORS['white']};
            }}

            hr {{
                border: none;
                border-top: 1px solid {COLORS['pink']};
            }}

        </style>
        </head>

        <body>
            {rendered}
        </body>
        </html>
        """

    # ========================================================
    # RANDOM SEARCH
    # ========================================================

    def get_random_note_and_content(self):
        """
        Находит автосгенерированную заметку и забирает текст от верхних хештегов до конца.
        """

        while True:

            files = self.get_all_markdown_files()

            if not files:
                continue

            random.shuffle(files)

            for file_path in files:

                text = self.read_file(
                    file_path
                )

                if not text:
                    continue

                content = self.extract_note_content(
                    text
                )

                if not content:
                    continue

                return (
                    file_path,
                    content
                )

    # ========================================================
    # REFRESH
    # ========================================================

    def refresh_random_note(self):

        file_path, content = (
            self.get_random_note_and_content()
        )

        self.source_label.setText(
            f"// {file_path.stem}"
        )

        html_text = self.markdown_to_html(
            content
        )

        self.text_browser.setHtml(
            html_text
        )

        self.refresh_button.setEnabled(
            True
        )

        self.resize_widget()

    # ========================================================
    # FIXED SIZE
    # ========================================================

    def resize_widget(self):

        self.setFixedSize(
            WIDGET_WIDTH,
            WIDGET_HEIGHT
        )

    # ========================================================
    # UI
    # ========================================================

    def init_ui(self):

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnBottomHint |
            Qt.WindowType.Tool
        )

        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )

        self.setFixedSize(
            WIDGET_WIDTH,
            WIDGET_HEIGHT
        )

        self.main_frame = QFrame()

        self.main_frame.setObjectName(
            "WidgetFrame"
        )

        self.main_frame.setStyleSheet(frame_style() + f"""
            QPushButton {{
                background-color: rgba(20, 4, 12, 230);
                color: {COLORS['pink_bright']};
                border: 1px solid {COLORS['pink']};
                border-radius: 2px;
                padding: 4px 10px;
                font-family: '{FONT_FAMILY}';
                font-size: 9px;
                font-weight: bold;
            }}

            QPushButton:hover {{
                background-color: rgba(48, 11, 32, 240);
                color: {COLORS['pink_hover']};
            }}

            QPushButton:pressed {{
                background-color: rgba(80, 12, 50, 240);
            }}

            QPushButton:disabled {{
                color: {COLORS['disabled']};
                border-color: {COLORS['disabled_border']};
            }}
        """)

        outer = QVBoxLayout(self)

        outer.setContentsMargins(
            0,
            0,
            0,
            0
        )

        outer.addWidget(
            self.main_frame
        )

        self.main_layout = QVBoxLayout(
            self.main_frame
        )

        self.main_layout.setContentsMargins(
            10,
            8,
            10,
            8
        )

        self.main_layout.setSpacing(
            6
        )

        # ----------------------------------------------------
        # HEADER
        # ----------------------------------------------------

        top = QHBoxLayout()

        self.title_label = QLabel(
            "OBSIDIAN"
        )

        self.title_label.setStyleSheet(
            f"color: {COLORS['pink']}; "
            "font-size: 11px; "
            "font-weight: bold;"
        )

        self.source_label = QLabel(
            "// RANDOM NOTE"
        )

        self.source_label.setAlignment(
            Qt.AlignmentFlag.AlignRight
        )

        self.source_label.setStyleSheet(
            f"color: {COLORS['pink_dim']}; "
            "font-size: 8px;"
        )

        top.addWidget(
            self.title_label
        )

        top.addStretch()

        top.addWidget(
            self.source_label
        )

        self.main_layout.addLayout(
            top
        )

        # ----------------------------------------------------
        # MARKDOWN VIEW
        # ----------------------------------------------------

        self.text_browser = QTextBrowser()

        self.text_browser.setFixedWidth(
            TEXT_WIDTH
        )

        self.text_browser.setReadOnly(
            True
        )

        self.text_browser.setOpenExternalLinks(
            False
        )

        self.text_browser.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.text_browser.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.text_browser.setFrameShape(
            QFrame.Shape.NoFrame
        )

        self.text_browser.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding
        )

        self.text_browser.setStyleSheet(f"""
            QTextBrowser {{
                background: transparent;
                border: none;
                color: {COLORS['text']};
                padding: 0;
                margin: 0;
            }}
        """)

        self.main_layout.addWidget(
            self.text_browser
        )

        # ----------------------------------------------------
        # BUTTON
        # ----------------------------------------------------

        button_row = QHBoxLayout()

        button_row.addStretch()

        self.refresh_button = QPushButton(
            "↻  REFRESH"
        )

        self.refresh_button.clicked.connect(
            self.refresh_random_note
        )

        button_row.addWidget(
            self.refresh_button
        )

        self.main_layout.addLayout(
            button_row
        )

    # ========================================================
    # DRAG
    # ========================================================

    def mousePressEvent(self, event):

        if event.button() == Qt.MouseButton.LeftButton:
            self.old_pos = (
                event.globalPosition().toPoint()
            )

    def mouseMoveEvent(self, event):

        if self.old_pos is None:
            return

        delta = (
            event.globalPosition().toPoint()
            - self.old_pos
        )

        self.move(
            self.x() + delta.x(),
            self.y() + delta.y()
        )

        self.old_pos = (
            event.globalPosition().toPoint()
        )

    def mouseReleaseEvent(self, event):
        self.old_pos = None


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    app = QApplication(sys.argv)

    widget = NeuroObsidianWidget()

    widget.show()

    sys.exit(app.exec())
