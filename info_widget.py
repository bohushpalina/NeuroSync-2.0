import sys
import os
import time
import socket
import platform
import subprocess
from pathlib import Path

import psutil

from ns_config import COLORS, FONT_FAMILY, frame_style

# Форсируем X11 для Linux
os.environ["QT_QPA_PLATFORM"] = "xcb"

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame,
)
from PyQt6.QtCore import Qt, QTimer


class NeuroSystemWidget(QWidget):

    def __init__(self):
        super().__init__()

        self.old_pos = None

        self.last_net = psutil.net_io_counters()
        self.last_net_time = time.time()

        self.widget_width = 330
        self.widget_height = 200

        self.setGeometry(
            400,
            265,
            self.widget_width,
            self.widget_height
        )

        self.init_ui()
        self.init_logic()

    # =========================================================
    # UI
    # =========================================================

    def init_ui(self):

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnBottomHint |
            Qt.WindowType.Tool
        )

        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )

        # -----------------------------------------------------
        # Главный layout
        # -----------------------------------------------------

        main_layout = QVBoxLayout(self)

        main_layout.setContentsMargins(
            0,
            0,
            0,
            0
        )

        # -----------------------------------------------------
        # Главная рамка
        # -----------------------------------------------------

        self.main_frame = QFrame()

        self.main_frame.setObjectName(
            "WidgetFrame"
        )

        self.main_frame.setStyleSheet(frame_style(radius=3))

        main_layout.addWidget(
            self.main_frame
        )

        # -----------------------------------------------------
        # Контент
        # -----------------------------------------------------

        self.content_layout = QVBoxLayout(
            self.main_frame
        )

        self.content_layout.setContentsMargins(
            10,
            9,
            10,
            9
        )

        self.content_layout.setSpacing(5)

        # =====================================================
        # HEADER
        # =====================================================

        self.title = QLabel(
            "SYSTEM"
        )

        self.title.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        self.title.setStyleSheet(f"""
            color: {COLORS['pink']};
            font-family: '{FONT_FAMILY}';
            font-size: 12px;
            font-weight: bold;
        """)

        self.content_layout.addWidget(
            self.title
        )

        # -----------------------------------------------------
        # Линия
        # -----------------------------------------------------

        separator = QFrame()

        separator.setFixedHeight(1)

        separator.setStyleSheet(
            f"background-color: {COLORS['pink']};"
        )

        self.content_layout.addWidget(
            separator
        )

        # =====================================================
        # SYSTEM
        # =====================================================

        self.system_section = self.create_section(
            "SYSTEM"
        )

        self.system_section.setText(
            ""
        )

        self.content_layout.addWidget(
            self.system_section
        )

        # =====================================================
        # CPU
        # =====================================================

        self.cpu_section = self.create_section(
            "CPU"
        )

        self.content_layout.addWidget(
            self.cpu_section
        )

        # =====================================================
        # MEMORY
        # =====================================================

        self.memory_section = self.create_section(
            "MEMORY"
        )

        self.content_layout.addWidget(
            self.memory_section
        )

        # =====================================================
        # STORAGE
        # =====================================================

        self.storage_section = self.create_section(
            "STORAGE"
        )

        self.content_layout.addWidget(
            self.storage_section
        )

        # =====================================================
        # NETWORK
        # =====================================================

        self.network_section = self.create_section(
            "NETWORK"
        )

        self.content_layout.addWidget(
            self.network_section
        )

        # =====================================================
        # BATTERY
        # =====================================================

        self.battery_section = self.create_section(
            "BATTERY"
        )

        self.content_layout.addWidget(
            self.battery_section
        )

        # =====================================================
        # FOOTER
        # =====================================================

        self.content_layout.addStretch()

        self.footer = QLabel(
            "● SYSTEM ONLINE"
        )

        self.footer.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        self.footer.setStyleSheet(f"""
            color: {COLORS['cyan']};
            font-family: '{FONT_FAMILY}';
            font-size: 9px;
            font-weight: bold;
        """)

        self.content_layout.addWidget(
            self.footer
        )

    # =========================================================
    # SECTION CREATOR
    # =========================================================

    def create_section(self, title):

        label = QLabel()

        label.setStyleSheet(f"""
            color: {COLORS['text']};
            font-family: '{FONT_FAMILY}';
            font-size: 10px;
        """)

        # Начальный текст
        label.setText(
            f'<span style="color:{COLORS['pink']}; '
            f'font-weight:bold;">{title}</span>'
        )

        label.setWordWrap(True)

        return label

    # =========================================================
    # LOGIC
    # =========================================================

    def init_logic(self):

        self.timer = QTimer(self)

        self.timer.timeout.connect(
            self.refresh
        )

        self.timer.start(
            500
        )

        self.refresh()

    # =========================================================
    # HELPERS
    # =========================================================

    @staticmethod
    def format_bytes(value):

        units = [
            "B",
            "KB",
            "MB",
            "GB",
            "TB"
        ]

        value = float(value)

        for unit in units:

            if value < 1024:

                return (
                    f"{value:.1f} {unit}"
                )

            value /= 1024

        return (
            f"{value:.1f} PB"
        )

    @staticmethod
    def format_uptime(seconds):

        seconds = int(seconds)

        days = seconds // 86400

        seconds %= 86400

        hours = seconds // 3600

        seconds %= 3600

        minutes = seconds // 60

        seconds %= 60

        if days > 0:

            return (
                f"{days}d "
                f"{hours:02d}h "
                f"{minutes:02d}m"
            )

        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    @staticmethod
    def get_cpu_temperature():

        try:

            temperatures = (
                psutil.sensors_temperatures()
            )

            if not temperatures:
                return None

            preferred = [
                "coretemp",
                "k10temp",
                "cpu_thermal",
                "zenpower",
                "acpitz",
            ]

            for name in preferred:

                if name in temperatures:

                    for entry in temperatures[name]:

                        if entry.current is not None:

                            return entry.current

            for entries in temperatures.values():

                for entry in entries:

                    if entry.current is not None:

                        return entry.current

        except Exception:
            pass

        return None

    @staticmethod
    def get_gpu_info():

        try:

            result = subprocess.run(
                [
                    "nvidia-smi",

                    "--query-gpu="
                    "name,"
                    "utilization.gpu,"
                    "temperature.gpu,"
                    "memory.used,"
                    "memory.total",

                    "--format=csv,"
                    "noheader,"
                    "nounits"
                ],

                capture_output=True,

                text=True,

                timeout=1
            )

            if result.returncode != 0:
                return None

            lines = (
                result.stdout
                .strip()
                .splitlines()
            )

            if not lines:
                return None

            parts = [
                item.strip()
                for item in lines[0].split(",")
            ]

            if len(parts) < 5:
                return None

            return {
                "name": parts[0],
                "usage": parts[1],
                "temp": parts[2],
                "mem_used": parts[3],
                "mem_total": parts[4],
            }

        except Exception:
            return None

    @staticmethod
    def get_os_name():

        try:

            path = Path(
                "/etc/os-release"
            )

            if path.exists():

                data = {}

                for line in path.read_text().splitlines():

                    if "=" not in line:
                        continue

                    key, value = line.split(
                        "=",
                        1
                    )

                    data[key] = (
                        value.strip('"')
                    )

                return data.get(
                    "PRETTY_NAME",
                    platform.system()
                )

        except Exception:
            pass

        return platform.system()

    # =========================================================
    # REFRESH
    # =========================================================

    def refresh(self):

        try:

            # =================================================
            # CPU
            # =================================================

            cpu_usage = psutil.cpu_percent(
                interval=None
            )

            cpu_freq = psutil.cpu_freq()

            if cpu_freq:

                cpu_frequency = (
                    f"{cpu_freq.current / 1000:.2f} GHz"
                )

            else:

                cpu_frequency = "N/A"

            cpu_temp = (
                self.get_cpu_temperature()
            )

            if cpu_temp is not None:

                cpu_temperature = (
                    f"{cpu_temp:.0f}°C"
                )

            else:

                cpu_temperature = "N/A"

            # =================================================
            # RAM
            # =================================================

            memory = psutil.virtual_memory()

            ram_used = self.format_bytes(
                memory.used
            )

            ram_total = self.format_bytes(
                memory.total
            )

            ram_percent = memory.percent

            # =================================================
            # SWAP
            # =================================================

            swap = psutil.swap_memory()

            swap_used = self.format_bytes(
                swap.used
            )

            swap_total = self.format_bytes(
                swap.total
            )

            # =================================================
            # DISK
            # =================================================

            disk = psutil.disk_usage("/")

            disk_used = self.format_bytes(
                disk.used
            )

            disk_total = self.format_bytes(
                disk.total
            )

            disk_percent = disk.percent

            # =================================================
            # BATTERY
            # =================================================

            battery = (
                psutil.sensors_battery()
            )

            if battery:

                battery_percent = (
                    f"{battery.percent:.0f}%"
                )

                if battery.power_plugged:

                    battery_status = (
                        "CHARGING"
                    )

                else:

                    battery_status = (
                        "BATTERY"
                    )

            else:

                battery_percent = "N/A"

                battery_status = (
                    "NO SENSOR"
                )

            # =================================================
            # NETWORK
            # =================================================

            net = (
                psutil.net_io_counters()
            )

            now = time.time()

            elapsed = (
                now -
                self.last_net_time
            )

            if elapsed <= 0:
                elapsed = 1

            download_speed = (
                net.bytes_recv -
                self.last_net.bytes_recv
            ) / elapsed

            upload_speed = (
                net.bytes_sent -
                self.last_net.bytes_sent
            ) / elapsed

            self.last_net = net

            self.last_net_time = now

            download = self.format_bytes(
                download_speed
            )

            upload = self.format_bytes(
                upload_speed
            )

            # =================================================
            # UPTIME
            # =================================================

            uptime = self.format_uptime(
                time.time() -
                psutil.boot_time()
            )

            # =================================================
            # SYSTEM
            # =================================================

            hostname = socket.gethostname()

            os_name = self.get_os_name()

            kernel = platform.release()

            architecture = platform.machine()

            # =================================================
            # GPU
            # =================================================

            gpu = self.get_gpu_info()

            # =================================================
            # SYSTEM SECTION
            # =================================================

            self.system_section.setText(
                f"""
<span style="color:{COLORS['pink']};font-weight:bold;">SYSTEM</span>

HOST       {hostname}
OS         {os_name}
KERNEL     {kernel}
ARCH       {architecture}
UPTIME     {uptime}
"""
            )

            # =================================================
            # CPU SECTION
            # =================================================

            self.cpu_section.setText(
                f"""
<span style="color:{COLORS['pink']};font-weight:bold;">CPU</span>

USAGE      {cpu_usage:5.1f}%
FREQ       {cpu_frequency}
TEMP       {cpu_temperature}
"""
            )

            # =================================================
            # GPU
            # =================================================

            if gpu:

                self.cpu_section.setText(
                    f"""
<span style="color:{COLORS['pink']};font-weight:bold;">CPU</span>

USAGE      {cpu_usage:5.1f}%
FREQ       {cpu_frequency}
TEMP       {cpu_temperature}

<span style="color:{COLORS['pink']};font-weight:bold;">GPU</span>

{gpu["name"]}
USAGE      {gpu["usage"]}%
TEMP       {gpu["temp"]}°C
VRAM       {gpu["mem_used"]} / {gpu["mem_total"]} MB
"""
                )

            # =================================================
            # MEMORY SECTION
            # =================================================

            self.memory_section.setText(
                f"""
<span style="color:{COLORS['pink']};font-weight:bold;">MEMORY</span>

RAM        {ram_used} / {ram_total}
RAM LOAD   {ram_percent:5.1f}%
SWAP       {swap_used} / {swap_total}
"""
            )

            # =================================================
            # STORAGE
            # =================================================

            self.storage_section.setText(
                f"""
<span style="color:{COLORS['pink']};font-weight:bold;">STORAGE</span>

DISK       {disk_used} / {disk_total}
DISK LOAD  {disk_percent:5.1f}%
"""
            )

            # =================================================
            # NETWORK
            # =================================================

            self.network_section.setText(
                f"""
<span style="color:{COLORS['pink']};font-weight:bold;">NETWORK</span>

↓          {download}/s
↑          {upload}/s
"""
            )

            # =================================================
            # BATTERY
            # =================================================

            self.battery_section.setText(
                f"""
<span style="color:{COLORS['pink']};font-weight:bold;">BATTERY</span>

LEVEL      {battery_percent}
STATUS     {battery_status}
"""
            )

            # =================================================
            # FOOTER
            # =================================================

            if cpu_usage >= 90:

                self.footer.setText(
                    "● SYSTEM LOAD // HIGH"
                )

                self.footer.setStyleSheet(f"""
                    color: {COLORS['pink']};
                    font-family: '{FONT_FAMILY}';
                    font-size: 9px;
                    font-weight: bold;
                """)

            elif ram_percent >= 90:

                self.footer.setText(
                    "● MEMORY LOAD // HIGH"
                )

                self.footer.setStyleSheet(f"""
                    color: {COLORS['pink']};
                    font-family: '{FONT_FAMILY}';
                    font-size: 9px;
                    font-weight: bold;
                """)

            else:

                self.footer.setText(
                    "● SYSTEM ONLINE"
                )

                self.footer.setStyleSheet(f"""
                    color: {COLORS['cyan']};
                    font-family: '{FONT_FAMILY}';
                    font-size: 9px;
                    font-weight: bold;
                """)

        except Exception as error:

            self.footer.setText(
                f"● ERROR // {str(error)[:30]}"
            )

    # =========================================================
    # DRAG
    # =========================================================

    def mousePressEvent(self, event):

        if event.button() == Qt.MouseButton.LeftButton:

            self.old_pos = (
                event.globalPosition().toPoint()
            )

    def mouseMoveEvent(self, event):

        if self.old_pos is not None:

            current_pos = (
                event.globalPosition().toPoint()
            )

            delta = (
                current_pos -
                self.old_pos
            )

            self.move(
                self.x() + delta.x(),
                self.y() + delta.y()
            )

            self.old_pos = current_pos

    def mouseReleaseEvent(self, event):

        self.old_pos = None


# =============================================================
# MAIN
# =============================================================

if __name__ == "__main__":

    app = QApplication(sys.argv)

    widget = NeuroSystemWidget()

    widget.show()

    sys.exit(
        app.exec()
    )
