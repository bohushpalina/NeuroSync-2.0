import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QPushButton, QSizePolicy,
)
from PyQt6.QtCore import Qt

from ns_config import COLORS, frame_style

# Форсируем X11 — так же, как в остальных виджетах панели.
import os
os.environ["QT_QPA_PLATFORM"] = "xcb"

# Те же два флага, которые читают llm.py (левая шторка-чат) и
# neuro_panel.py (верхняя шторка-панель) в своих hover_check().
#
# ВАЖНО: этот виджет НИКОГДА не трогает сами процессы шторок — не убивает,
# не перезапускает, ничего не шлёт им напрямую. Он только кладёт/убирает
# пустой файл-флаг на диске. Обе шторки продолжают жить и работать как
# обычно (музыка в верхней шторке не остановится), меняется только одно:
# реагирует ли шторка на наведение курсора на свой край, чтобы открыться.
# Уже открытую шторку выключение не закрывает — она закроется сама, когда
# курсор уйдёт, как обычно.
LLM_DISABLED_FLAG = Path("/tmp/neuro_shutter_llm_disabled")
PANEL_DISABLED_FLAG = Path("/tmp/neuro_shutter_panel_disabled")


def _is_enabled(flag_path: Path) -> bool:
    # Файла нет -> шторка включена (по умолчанию, как до появления этого
    # виджета — если он вообще не запущен, ничего не меняется).
    return not flag_path.exists()


def _set_enabled(flag_path: Path, enabled: bool) -> None:
    try:
        if enabled:
            flag_path.unlink(missing_ok=True)
        else:
            flag_path.touch(exist_ok=True)
    except OSError:
        # Диск/права — не критично, просто не сохранится между запусками.
        pass


class ShutterToggleRow(QWidget):
    """Одна строка переключателя: название шторки + кнопка ВКЛ/ВЫКЛ."""

    def __init__(self, title: str, flag_path: Path, parent=None):
        super().__init__(parent)
        self.flag_path = flag_path

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            f"color: {COLORS['pink']}; font-size: 13px; font-weight: bold;"
        )
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        self.toggle_btn = QPushButton()
        self.toggle_btn.setFixedSize(64, 26)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(self.toggle)

        layout.addWidget(self.title_label)
        layout.addWidget(self.toggle_btn)

        self.refresh_from_disk()

    def refresh_from_disk(self):
        """Подтягивает состояние с диска — на случай, если флаг поменяли
        снаружи (например, вручную из терминала)."""
        self._render(_is_enabled(self.flag_path))

    def toggle(self):
        now_enabled = _is_enabled(self.flag_path)
        _set_enabled(self.flag_path, not now_enabled)
        self._render(not now_enabled)

    def _render(self, enabled: bool):
        if enabled:
            self.toggle_btn.setText("ВКЛ")
            self.toggle_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {COLORS['cyan']};
                    color: #10121a;
                    border: none;
                    border-radius: 13px;
                    font-weight: bold;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: {COLORS['purple']};
                }}
                """
            )
        else:
            self.toggle_btn.setText("ВЫКЛ")
            self.toggle_btn.setStyleSheet(
                """
                QPushButton {
                    background-color: #3a3a44;
                    color: #9a9aa5;
                    border: none;
                    border-radius: 13px;
                    font-weight: bold;
                    font-size: 12px;
                }
                QPushButton:hover {
                    background-color: #4a4a56;
                }
                """
            )


class NeuroShutterSwitch(QWidget):
    """
    Маленький виджет на рабочий стол (по аналогии с NeuroPanelWidget) с двумя
    кнопками-переключателями: разрешать ли левой шторке (llm.py) и верхней
    шторке (neuro_panel.py) открываться по наведению курсора на край экрана.

    Это НЕ замена команде ns и НЕ управление процессами — оба процесса
    по-прежнему запускаются как обычно. Переключатели здесь влияют только на
    открытие/закрытие: выключенная шторка просто не разворачивается по
    hover, всё остальное в ней (плеер, таймеры, фоновая работа) продолжает
    выполняться без изменений.
    """

    def __init__(self):
        super().__init__()
        self.old_pos = None

        self.init_ui()

        self.setFixedSize(220, 100)
        self.move(400, 460)

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnBottomHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.main_frame = QFrame()
        self.main_frame.setObjectName("WidgetFrame")
        self.main_frame.setStyleSheet(frame_style())
        outer.addWidget(self.main_frame)

        content = QVBoxLayout(self.main_frame)
        content.setContentsMargins(8, 8, 8, 8)
        content.setSpacing(4)

        header = QLabel("ШТОРКИ")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet(
            f"color: {COLORS['cyan']}; font-size: 12px; font-weight: bold; "
            f"letter-spacing: 2px;"
        )
        content.addWidget(header)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet(f"background-color: {COLORS['purple']};")
        separator.setFixedHeight(1)
        content.addWidget(separator)

        self.llm_row = ShutterToggleRow("Чат (лево)", LLM_DISABLED_FLAG)
        self.panel_row = ShutterToggleRow("Панель (верх)", PANEL_DISABLED_FLAG)
        content.addWidget(self.llm_row)
        content.addWidget(self.panel_row)

    # =========================================================
    # DRAG
    # =========================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.old_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.old_pos is not None:
            delta = event.globalPosition().toPoint() - self.old_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.old_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.old_pos = None


if __name__ == "__main__":
    app = QApplication(sys.argv)

    widget = NeuroShutterSwitch()
    widget.show()

    sys.exit(app.exec())
