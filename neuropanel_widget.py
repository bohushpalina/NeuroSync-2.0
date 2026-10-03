import sys
import os
import math
import time
import threading
import subprocess
from pathlib import Path
import psutil
import datetime

import numpy as np

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QFrame, QHBoxLayout,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFontMetrics

from ns_config import COLORS, frame_style

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None

# Форсируем X11 для Linux
os.environ["QT_QPA_PLATFORM"] = "xcb"

PLAYING_FLAG = Path("/tmp/neuro_playing")
TEMP_TRACK = Path("/tmp/neuro_current.mp3")
NOW_PLAYING_TITLE = Path("/tmp/neuro_now_playing_title")

# Анализ аудио.
SAMPLE_RATE = 22050
FFT_SIZE = 2048
HOP_SIZE = 512
BARS_COUNT = 28
EQ_MAX_HEIGHT = 72


class NeuroPanelWidget(QWidget):
    def __init__(self):
        super().__init__()

        self.old_pos = None

        # Состояние проигрывания.
        self.was_playing = False
        self.track_signature = None
        self.playback_position = 0.0
        self.last_tick = time.monotonic()

        # Анализ текущего трека.
        self.analysis_lock = threading.Lock()
        self.analysis_frames = None
        self.analysis_duration = 0.0
        self.analysis_signature = None
        self.analysis_running = False
        self.last_title_mtime_ns = None

        # Состояние эквалайзера.
        self.bar_heights = np.zeros(BARS_COUNT, dtype=float)

        self.init_ui()
        self.init_logic()

        self.widget_width = 300
        self.widget_height = 120
        self.setFixedSize(
            self.widget_width,
            self.widget_height
        )
        self.move(100, 100)

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnBottomHint |
            Qt.WindowType.Tool
        )

        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

        self.main_frame = QFrame()
        self.main_frame.setObjectName("WidgetFrame")
        self.main_frame.setStyleSheet(frame_style())
        self.layout.addWidget(self.main_frame)

        self.content_layout = QVBoxLayout(self.main_frame)
        self.content_layout.setContentsMargins(5, 5, 5, 5)
        self.content_layout.setSpacing(0)

        # Uptime.
        self.time_label = QLabel("TIME")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.time_value = QLabel("00:00:00")
        self.time_value.setStyleSheet(
            f"font-size: 16px; "
            f"color: {COLORS['pink']}; "
            f"font-weight: bold;"
        )
        self.time_value.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        # Эквалайзер.
        self.eq_widget = QWidget()
        self.eq_layout = QHBoxLayout(self.eq_widget)
        self.eq_widget.setFixedHeight(80)
        self.eq_layout.setContentsMargins(4, 4, 4, 2)
        self.eq_layout.setSpacing(1)
        self.eq_layout.setAlignment(Qt.AlignmentFlag.AlignBottom)

        self.bars = []

        for index in range(BARS_COUNT):
            bar = QFrame()
            bar.setMinimumHeight(2)
            bar.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed
            )

            color = self._spectrum_color(index)

            bar.setStyleSheet(
                f"""
                QFrame {{
                    background-color: {color};
                    border: none;
                    border-radius: 1px;
                }}
                """
            )

            self.bars.append(bar)

            self.eq_layout.addWidget(
                bar,
                1,
                Qt.AlignmentFlag.AlignBottom
            )

        # Маленькое название трека под спектром.
        self.track_title = QLabel("NO TRACK")
        self.track_title.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.track_title.setWordWrap(False)
        self.track_title.setFixedHeight(13)
        self.track_title.setStyleSheet(
            f"""
            QLabel {{
                color: {COLORS['pink_soft']};
                background: transparent;
                font-family: 'Monospace', 'DejaVu Sans Mono', 'Consolas';
                font-size: 8px;
                padding: 0 5px 2px 5px;
            }}
            """
        )

        self.content_layout.addWidget(self.time_label)
        self.content_layout.addWidget(self.time_value)
        self.content_layout.addWidget(self.eq_widget)
        self.content_layout.addWidget(self.track_title)

        self.eq_widget.hide()
        self.track_title.hide()

    def init_logic(self):
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh)
        self.status_timer.start(50)

    # =========================================================
    # AUDIO ANALYSIS
    # =========================================================

    def _spectrum_color(self, index):
        """
        Цветовая шкала эквалайзера:
        розовый -> фиолетовый -> голубой.
        """
        pink = self._hex_to_rgb(COLORS["pink"])
        purple = self._hex_to_rgb(COLORS["purple"])
        cyan = self._hex_to_rgb(COLORS["cyan"])

        position = (
            index / (BARS_COUNT - 1)
            if BARS_COUNT > 1
            else 0.0
        )

        if position < 0.5:
            t = position / 0.5
            rgb = self._lerp_color(pink, purple, t)
        else:
            t = (position - 0.5) / 0.5
            rgb = self._lerp_color(purple, cyan, t)

        return "#{:02x}{:02x}{:02x}".format(*rgb)

    @staticmethod
    def _hex_to_rgb(value):
        value = value.lstrip("#")
        return tuple(
            int(value[i:i + 2], 16)
            for i in (0, 2, 4)
        )

    @staticmethod
    def _lerp_color(c1, c2, t):
        return tuple(
            int(round(a + (b - a) * t))
            for a, b in zip(c1, c2)
        )

    def _get_track_signature(self):
        if not TEMP_TRACK.exists():
            return None

        try:
            stat = TEMP_TRACK.stat()
            return (
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
            )
        except OSError:
            return None

    def _get_track_title(self):
        # Основное название берём напрямую из Neuro Panel.
        try:
            if NOW_PLAYING_TITLE.exists():
                title = NOW_PLAYING_TITLE.read_text(
                    encoding="utf-8"
                ).strip()
                if title:
                    return title
        except Exception:
            pass

        # Резервный вариант — метаданные скачанного файла.
        if not TEMP_TRACK.exists():
            return "NO TRACK"

        if MutagenFile is not None:
            try:
                audio = MutagenFile(
                    str(TEMP_TRACK),
                    easy=True
                )

                if audio is not None:
                    tags = audio.tags or {}
                    title = tags.get("title")
                    artist = tags.get("artist")

                    if isinstance(title, list):
                        title = title[0] if title else ""

                    if isinstance(artist, list):
                        artist = artist[0] if artist else ""

                    title = str(title or "").strip()
                    artist = str(artist or "").strip()

                    if artist and title:
                        return f"{artist} — {title}"

                    if title:
                        return title
            except Exception:
                pass

        return "NOW PLAYING"

    def _start_analysis(self, signature):
        if self.analysis_running:
            return

        self.analysis_running = True

        thread = threading.Thread(
            target=self._analyze_track_worker,
            args=(signature,),
            daemon=True,
        )
        thread.start()

    def _analyze_track_worker(self, signature):
        try:
            if (
                not TEMP_TRACK.exists()
                or self._get_track_signature() != signature
            ):
                return

            command = [
                "ffmpeg",
                "-v", "error",
                "-i", str(TEMP_TRACK),
                "-f", "f32le",
                "-ac", "1",
                "-ar", str(SAMPLE_RATE),
                "pipe:1",
            ]

            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
                check=True,
            )

            samples = np.frombuffer(
                process.stdout,
                dtype=np.float32
            )

            if samples.size < FFT_SIZE:
                return

            # Усечение до полного окна.
            sample_count = (
                (samples.size - FFT_SIZE) // HOP_SIZE
            ) + 1

            frames = np.lib.stride_tricks.as_strided(
                samples,
                shape=(sample_count, FFT_SIZE),
                strides=(
                    samples.strides[0] * HOP_SIZE,
                    samples.strides[0],
                ),
                writeable=False,
            )

            window = np.hanning(FFT_SIZE).astype(
                np.float32
            )

            spectrum = np.abs(
                np.fft.rfft(
                    frames * window,
                    axis=1
                )
            )

            frequencies = np.fft.rfftfreq(
                FFT_SIZE,
                1.0 / SAMPLE_RATE
            )

            # 28 полос по логарифмической шкале.
            low_frequency = 45.0
            high_frequency = min(
                12000.0,
                SAMPLE_RATE / 2.0
            )

            edges = np.geomspace(
                low_frequency,
                high_frequency,
                BARS_COUNT + 1
            )

            bands = np.zeros(
                (sample_count, BARS_COUNT),
                dtype=np.float32
            )

            for index in range(BARS_COUNT):
                left = edges[index]
                right = edges[index + 1]

                mask = (
                    (frequencies >= left)
                    & (frequencies < right)
                )

                if np.any(mask):
                    bands[:, index] = np.mean(
                        spectrum[:, mask],
                        axis=1
                    )

            bands = np.log1p(
                bands * 8.0
            )

            # Нормализация по каждой полосе.
            low = np.percentile(
                bands,
                12,
                axis=0
            )
            high = np.percentile(
                bands,
                96,
                axis=0
            )

            bands = (
                bands - low
            ) / (
                high - low + 1e-6
            )

            bands = np.clip(
                bands,
                0.0,
                1.0
            )

            # Spectral flux — источник "ритма".
            normalized = spectrum / (
                np.mean(spectrum, axis=1, keepdims=True)
                + 1e-6
            )

            flux = np.sqrt(
                np.mean(
                    np.maximum(
                        normalized[1:] -
                        normalized[:-1],
                        0.0
                    ) ** 2,
                    axis=1,
                )
            )

            # Подавляем постоянные тихие участки.
            threshold = (
                np.median(flux)
                + 0.45 * np.std(flux)
            )

            candidate_indices = np.where(
                flux > threshold
            )[0] + 1

            # Оставляем максимум примерно раз в 120 мс,
            # чтобы мелкие всплески не превращались в кашу.
            beat_indices = []
            min_distance = max(
                1,
                int(0.12 * SAMPLE_RATE / HOP_SIZE)
            )

            for index in candidate_indices:
                if not beat_indices:
                    beat_indices.append(int(index))
                    continue

                if (
                    index - beat_indices[-1]
                    >= min_distance
                ):
                    beat_indices.append(int(index))
                elif flux[index - 1] > flux[
                    beat_indices[-1] - 1
                ]:
                    beat_indices[-1] = int(index)

            beat_times = (
                np.array(beat_indices, dtype=np.float32)
                * HOP_SIZE
                / SAMPLE_RATE
            )

            duration = (
                sample_count
                * HOP_SIZE
                / SAMPLE_RATE
            )

            analysis = {
                "bands": bands.astype(np.float32),
                "beat_times": beat_times,
                "duration": float(duration),
            }

            with self.analysis_lock:
                self.analysis_frames = analysis
                self.analysis_duration = float(duration)
                self.analysis_signature = signature

        except Exception as error:
            print(
                "NEURO EQ ANALYSIS ERROR:",
                repr(error)
            )
        finally:
            self.analysis_running = False

    # =========================================================
    # PLAYBACK POSITION
    # =========================================================

    def _initial_track_position(self):
        if not TEMP_TRACK.exists():
            return 0.0

        try:
            track_mtime = (
                TEMP_TRACK.stat().st_mtime
            )

            if PLAYING_FLAG.exists():
                flag_mtime = (
                    PLAYING_FLAG.stat().st_mtime
                )

                position = (
                    flag_mtime - track_mtime
                )

                # Если файл скачали чуть раньше запуска —
                # используем разницу как начальную позицию.
                if position >= 0:
                    return min(position, 30.0)

        except OSError:
            pass

        return 0.0

    def _update_playback_position(self, playing):
        now = time.monotonic()
        delta = max(0.0, now - self.last_tick)
        self.last_tick = now

        signature = self._get_track_signature()

        if signature != self.track_signature:
            self.track_signature = signature
            self.playback_position = (
                self._initial_track_position()
                if playing
                else 0.0
            )
            self.analysis_frames = None
            self.analysis_signature = None

            if signature is not None:
                self.track_title.setText(
                    self._elide_title(
                        self._get_track_title()
                    )
                )

        if playing:
            if not self.was_playing:
                # Возобновили воспроизведение.
                # Позицию не сбрасываем.
                pass

            self.playback_position += delta

        self.was_playing = playing

        try:
            if NOW_PLAYING_TITLE.exists():
                title_mtime = NOW_PLAYING_TITLE.stat().st_mtime_ns
                if title_mtime != self.last_title_mtime_ns:
                    self.last_title_mtime_ns = title_mtime
                    self.track_title.setText(
                        self._elide_title(
                            self._get_track_title()
                        )
                    )
        except OSError:
            pass

    def _elide_title(self, title):
        metrics = QFontMetrics(
            self.track_title.font()
        )

        width = max(
            30,
            self.widget_width - 16
        )

        return metrics.elidedText(
            title,
            Qt.TextElideMode.ElideMiddle,
            width
        )

    # =========================================================
    # UI
    # =========================================================

    def refresh(self):
        playing = PLAYING_FLAG.exists()

        self._update_playback_position(
            playing
        )

        if playing:
            self.time_label.hide()
            self.time_value.hide()
            self.eq_widget.show()
            self.track_title.show()

            signature = self.track_signature

            if (
                signature is not None
                and self.analysis_signature != signature
            ):
                self._start_analysis(signature)

            self.animate_eq()

        else:
            self.eq_widget.hide()
            self.track_title.hide()
            self.time_label.show()
            self.time_value.show()

            self.update_uptime()

    def update_uptime(self):
        boot = psutil.boot_time()

        uptime = (
            datetime.datetime.now()
            - datetime.datetime.fromtimestamp(boot)
        )

        self.time_value.setText(
            str(uptime).split('.')[0]
        )

    def animate_eq(self):
        with self.analysis_lock:
            analysis = self.analysis_frames
            analysis_signature = (
                self.analysis_signature
            )

        if (
            analysis is None
            or analysis_signature != self.track_signature
        ):
            self._animate_loading_eq()
            return

        bands = analysis["bands"]
        beat_times = analysis["beat_times"]

        duration = analysis["duration"]

        if duration > 0:
            position = min(
                self.playback_position,
                duration - 0.001
            )
        else:
            position = self.playback_position

        frame_index = int(
            position
            * SAMPLE_RATE
            / HOP_SIZE
        )

        frame_index = max(
            0,
            min(
                len(bands) - 1,
                frame_index
            )
        )

        target = (
            bands[frame_index] * EQ_MAX_HEIGHT
        )

        # Низкие полосы немного сильнее реагируют на удар.
        target *= np.linspace(
            1.15,
            0.82,
            BARS_COUNT
        )

        # Определяем силу ближайшего бита.
        pulse = 0.0

        if len(beat_times):
            index = np.searchsorted(
                beat_times,
                position,
                side="right"
            ) - 1

            if index >= 0:
                since_beat = (
                    position - beat_times[index]
                )

                # Короткий ударный импульс.
                if 0.0 <= since_beat < 0.22:
                    pulse = (
                        1.0
                        - since_beat / 0.22
                    ) ** 2

        target *= (
            1.0 + pulse * 0.75
        )

        target = np.clip(
            target,
            2.0,
            EQ_MAX_HEIGHT
        )

        # Сглаживание, чтобы движение было живым,
        # но не дёрганым.
        self.bar_heights += (
            target - self.bar_heights
        ) * 0.38

        for bar, height in zip(
            self.bars,
            self.bar_heights
        ):
            bar.setFixedHeight(
                max(2, int(height))
            )

    def _animate_loading_eq(self):
        # Не random: пока ffmpeg считает спектр,
        # просто мягкая бегущая волна.
        phase = time.monotonic() * 5.5

        target = np.array([
            10.0
            + 12.0 * (
                0.5
                + 0.5 * math.sin(
                    phase
                    + index * 0.34
                )
            )
            for index in range(BARS_COUNT)
        ])

        self.bar_heights += (
            target - self.bar_heights
        ) * 0.22

        for bar, height in zip(
            self.bars,
            self.bar_heights
        ):
            bar.setFixedHeight(
                max(2, int(height))
            )

    # =========================================================
    # DRAG & DROP
    # =========================================================

    def mousePressEvent(self, event):
        if (
            event.button()
            == Qt.MouseButton.LeftButton
        ):
            self.old_pos = (
                event.globalPosition().toPoint()
            )

    def mouseMoveEvent(self, event):
        if self.old_pos is not None:
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


if __name__ == "__main__":
    app = QApplication(sys.argv)

    widget = NeuroPanelWidget()
    widget.show()

    sys.exit(app.exec())
