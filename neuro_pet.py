import sys
import os
import json
import time
import random
from pathlib import Path
from datetime import datetime

from ns_config import *

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
)
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QPixmap, QTransform, QCursor


os.environ["QT_QPA_PLATFORM"] = "xcb"


# ============================================================
# CONFIG
# ============================================================

PET_DIR = Path(__file__).parent / "pet"

PET_SPRITES = {
    "IDLE": PET_DIR / "image_part_001.png",
    "HAPPY": PET_DIR / "image_part_002.png",
    "SLEEP": PET_DIR / "image_part_003.png",
    "SITTING": PET_DIR / "image_part_004.png",
    "WALKING": PET_DIR / "image_part_005.png",
    "CURIOUS": PET_DIR / "image_part_006.png",
    "SHY": PET_DIR / "image_part_007.png",
    "HUNGRY": PET_DIR / "image_part_008.png",
    "EXCITED": PET_DIR / "image_part_009.png",
}

PET_SIZE = 120

FEED_HAPPY_SECONDS = 20 * 60
HUNGER_DEPLETION_SECONDS = 6 * 60 * 60  # Сытость падает до нуля за 6 часов

SITTING_AFTER_SECONDS = 2 * 60
SHY_AFTER_SECONDS = 5

EXCITED_SECONDS = 15
WAKE_EXCITED_SECONDS = 5

WAKE_START_HOUR = 8
SLEEP_START_HOUR = 23

PET_DATA_FILE = Path.home() / ".neurosync_pet.json"

# Прогулки стали значительно реже:
# первая прогулка после старта — через 8-15 минут,
# после каждой прогулки следующая — ещё через 10-20 минут.
WANDER_INITIAL_MIN_SECONDS = 8 * 60
WANDER_INITIAL_MAX_SECONDS = 15 * 60
WANDER_NEXT_MIN_SECONDS = 10 * 60
WANDER_NEXT_MAX_SECONDS = 20 * 60

# Отвлечение на курсор во время ходьбы
CURSOR_DISTRACT_RADIUS = 220  # px, курсор должен быть настолько близко
CURSOR_DISTRACT_CHECK_CHANCE = 0.02  # шанс "заметить" курсор за один тик (~100мс)
CURSOR_DISTRACT_DURATION = (2.0, 5.0)  # сек, сколько питомец пялится на курсор

# Иногда (нечасто) вместо случайной точки питомец решает побегать за курсором
CHASE_CURSOR_CHANCE = 0.2
CHASE_TARGET_REFRESH_SECONDS = 0.5  # как часто обновлять цель, догоняя курсор

# Клик мышкой по питомцу = "погладить". Двигаться при этом почти не должен,
# иначе это будет считаться перетаскиванием.
CLICK_MAX_MOVEMENT = 6
CLICK_MAX_DURATION = 0.35
CLICK_REACTION_SECONDS = (3.0, 6.0)

# Если погладить слишком много раз подряд и быстро — питомцу становится
# многовато внимания, и он на время смущается (SHY) вместо радости.
PET_COMBO_WINDOW_SECONDS = 4.0
PET_COMBO_OVERSTIM_THRESHOLD = 4
OVERSTIM_REACTION_SECONDS = (4.0, 7.0)

# ------------------------------------------------------------
# ГОЛОД
# ------------------------------------------------------------
# Сытость падает линейно и предсказуемо с момента кормления —
# без скачков в полночь или ровно в 14:00 (как было раньше).
# Ниже PECKISH_THRESHOLD питомец не сразу "голодает", а сперва
# какое-то время "выпрашивает еду" — это выглядит куда логичнее,
# чем мгновенный скачок IDLE -> HUNGRY.
PECKISH_THRESHOLD = 25.0
PECKISH_HOLD_SECONDS = (4.0, 9.0)  # как долго держится одна "просящая" поза

# Если покормить питомца, который и так почти не голоден — он всё равно
# рад, но не так сильно и не так долго, как по-настоящему голодный.
OVERFEED_SATIETY_THRESHOLD = 70.0
OVERFEED_HAPPY_FACTOR = 0.4

# Спонтанная радость: иногда питомцу просто хорошо без причины.
SPONTANEOUS_JOY_INTERVAL_SECONDS = (5 * 60, 12 * 60)
SPONTANEOUS_JOY_DURATION_SECONDS = (2.0, 4.0)

# Сонливость перед сном: в последние N минут перед SLEEP_START_HOUR
# питомец быстрее садится от скуки и быстрее смущается от долгого
# наведения курсора — он попросту устал.
DROWSY_BEFORE_SLEEP_MINUTES = 30
DROWSY_SITTING_FACTOR = 0.3
DROWSY_SHY_FACTOR = 0.5


# ============================================================
# ОБЩАЯ ЛОГИКА ГОЛОДА/СНА
# Вынесена из класса в функции модуля, чтобы этим же кормлением
# мог управлять другой процесс (например, панелька neuropanel_widget.py) —
# оба читают/пишут один и тот же PET_DATA_FILE.
# ============================================================

def load_last_fed():
    if not PET_DATA_FILE.exists():
        now = time.time()
        save_last_fed(now)
        return now

    try:
        with open(PET_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return float(data.get("last_fed", time.time()))

    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        now = time.time()
        save_last_fed(now)
        return now


def save_last_fed(timestamp):
    try:
        PET_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

        with open(PET_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"last_fed": float(timestamp)},
                f,
                indent=2,
            )

    except OSError as e:
        print(f"NeuroPet JSON write error: {e}")


def get_hunger_percentage(last_fed):
    # Чистый, предсказуемый спад: сытость падает линейно с момента
    # кормления и нигде не "обнуляется" скачком (раньше это происходило
    # в полночь и ровно в 14:00, что выглядело нелогично и пугающе резко).
    elapsed = max(0.0, time.time() - last_fed)
    hunger = 100.0 - (elapsed / HUNGER_DEPLETION_SECONDS * 100.0)

    return max(0.0, min(100.0, hunger))


def is_sleep_time():
    hour = datetime.now().hour
    return hour >= SLEEP_START_HOUR or hour < WAKE_START_HOUR


class NeuroPetWidget(QWidget):
    def __init__(self):
        super().__init__()

        self.old_pos = None
        self.current_state = "IDLE"
        self.facing_left = False

        self.last_fed = load_last_fed()
        self._last_fed_check = time.monotonic()
        self.happy_until = 0.0
        self.hover_started = None
        self.last_interaction = time.monotonic()

        self.excited_until = time.monotonic() + EXCITED_SECONDS
        self.wake_excited_until = 0.0

        # Параметры для самостоятельной ходьбы
        self.is_wandering = False
        self.is_chasing_cursor = False
        self.target_pos = None
        self.distracted_until = 0.0
        self.next_chase_target_update = 0.0
        self.next_wandering_check = time.monotonic() + random.randint(
            WANDER_INITIAL_MIN_SECONDS, WANDER_INITIAL_MAX_SECONDS
        )

        # Клик (погладить питомца) отдельно от перетаскивания
        self.press_pos = None
        self.press_time = None
        self.click_reaction_until = 0.0
        self.click_reaction_state = "EXCITED"
        self.pet_combo = 0
        self.last_pet_time = 0.0

        # "Выпрашивание еды" перед тем, как стать по-настоящему HUNGRY
        self.peckish_state = "SITTING"
        self.peckish_until = 0.0

        # Спонтанные моменты радости без повода
        self.spontaneous_until = 0.0
        self.next_spontaneous_check = time.monotonic() + random.uniform(
            *SPONTANEOUS_JOY_INTERVAL_SECONDS
        )

        self.init_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_state)
        self.timer.start(100)  # Увеличили частоту таймера для плавной ходьбы (10 раз в секунду)

        self.update_state()

    # ========================================================
    # DATA
    # ========================================================
    # load_last_fed / save_last_fed теперь живут как функции модуля
    # (см. начало файла) — их же использует neuropanel_widget.py.

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
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)

        self.pet_label = QLabel(self)
        self.pet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pet_label.setGeometry(0, 0, PET_SIZE, PET_SIZE)
        self.pet_label.setStyleSheet(
            "background: transparent; border: none;"
        )
        self.pet_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )

        self.setFixedSize(PET_SIZE, PET_SIZE)

    # ========================================================
    # PET STATE
    # ========================================================

    def get_hunger_percentage(self):
        return get_hunger_percentage(self.last_fed)

    def is_sleep_time(self):
        return is_sleep_time()

    def set_state(self, state):
        if state not in PET_SPRITES:
            state = "IDLE"

        self.current_state = state
        sprite = PET_SPRITES[state]

        if not sprite.exists():
            self.pet_label.clear()
            return

        pixmap = QPixmap(str(sprite))

        if pixmap.isNull():
            self.pet_label.clear()
            return

        pixmap = pixmap.scaled(
            PET_SIZE,
            PET_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

        if self.facing_left:
            pixmap = pixmap.transformed(
                QTransform().scale(-1, 1),
                Qt.TransformationMode.FastTransformation,
            )

        self.pet_label.setPixmap(pixmap)

    def update_state(self):
        now = time.time()
        current_time = datetime.now()

        # Кормёжка теперь может произойти из другого процесса (кнопка
        # FEED переехала в neuropanel_widget.py) — периодически (раз в
        # ~2 секунды, а не каждый тик в 100мс, чтобы не дёргать диск)
        # перечитываем last_fed из общего файла. Если он подрос — значит
        # питомца покормили снаружи, реагируем так же, как при
        # локальном feed_pet().
        if time.monotonic() - self._last_fed_check >= 2.0:
            self._last_fed_check = time.monotonic()
            fresh_last_fed = load_last_fed()

            if fresh_last_fed > self.last_fed + 1:
                was_satiety = get_hunger_percentage(self.last_fed)
                self.last_fed = fresh_last_fed
                self.happy_until = time.monotonic() + self._feed_happy_bonus(was_satiety)
                self.peckish_until = 0.0
            else:
                self.last_fed = fresh_last_fed

        # Сон имеет абсолютный приоритет, прерывает ходьбу
        if self.is_sleep_time():
            self.is_wandering = False
            self.is_chasing_cursor = False
            self.target_pos = None
            self.set_state("SLEEP")
            return

        # Реакция на поглаживание (клик мышкой) — приоритетнее прогулки/скуки
        if time.monotonic() < self.click_reaction_until:
            self.is_wandering = False
            self.is_chasing_cursor = False
            self.target_pos = None
            self.set_state(self.click_reaction_state)
            return

        # Обработка самостоятельной ходьбы днем
        if self.old_pos is None:  # Если мышку не тянут
            cursor_pos = QCursor.pos()
            pet_center = self.pos() + QPoint(PET_SIZE // 2, PET_SIZE // 2)
            dist_to_cursor = (
                (cursor_pos.x() - pet_center.x()) ** 2
                + (cursor_pos.y() - pet_center.y()) ** 2
            ) ** 0.5

            # Пока идёт куда-то, питомец может ненадолго "залипнуть" на курсор
            if (
                self.is_wandering
                and time.monotonic() >= self.distracted_until
                and dist_to_cursor <= CURSOR_DISTRACT_RADIUS
                and random.random() < CURSOR_DISTRACT_CHECK_CHANCE
            ):
                self.distracted_until = time.monotonic() + random.uniform(
                    *CURSOR_DISTRACT_DURATION
                )
                self.facing_left = cursor_pos.x() < pet_center.x()

            if self.is_wandering and time.monotonic() < self.distracted_until:
                self.set_state("CURIOUS")
                return

            if self.is_wandering and self.target_pos is not None:
                # Если питомец решил побегать за курсором - обновляем цель,
                # чтобы он его действительно "преследовал"
                if (
                    self.is_chasing_cursor
                    and time.monotonic() >= self.next_chase_target_update
                ):
                    self.next_chase_target_update = (
                        time.monotonic() + CHASE_TARGET_REFRESH_SECONDS
                    )
                    screen = QApplication.primaryScreen().geometry()
                    self.target_pos = QPoint(
                        max(0, min(screen.width() - PET_SIZE, cursor_pos.x() - PET_SIZE // 2)),
                        max(0, min(screen.height() - PET_SIZE, cursor_pos.y() - PET_SIZE // 2)),
                    )

                current_pos = self.pos()
                dx = self.target_pos.x() - current_pos.x()
                dy = self.target_pos.y() - current_pos.y()
                distance = (dx ** 2 + dy ** 2) ** 0.5

                # Направление взгляда при ходьбе:
                # Вправо и вверх/вниз/прямо -> смотрит направо (facing_left = False)
                # Влево и вверх/вниз/прямо -> смотрит налево (facing_left = True)
                if abs(dx) > 1:
                    self.facing_left = (dx > 0)

                if distance < 3:
                    # Дошли до цели (или догнали курсор)
                    self.is_wandering = False
                    self.is_chasing_cursor = False
                    self.target_pos = None
                    self.next_wandering_check = time.monotonic() + random.randint(
                        WANDER_NEXT_MIN_SECONDS, WANDER_NEXT_MAX_SECONDS
                    )
                else:
                    # Шаг к цели
                    step = 2.0
                    new_x = current_pos.x() + int((dx / distance) * step)
                    new_y = current_pos.y() + int((dy / distance) * step)
                    self.move(new_x, new_y)
                    self.set_state("WALKING")
                    return
            else:
                # Проверяем, не пора ли начать новую прогулку
                if time.monotonic() > self.next_wandering_check:
                    self.is_chasing_cursor = random.random() < CHASE_CURSOR_CHANCE

                    if self.is_chasing_cursor:
                        screen = QApplication.primaryScreen().geometry()
                        self.target_pos = QPoint(
                            max(0, min(screen.width() - PET_SIZE, cursor_pos.x() - PET_SIZE // 2)),
                            max(0, min(screen.height() - PET_SIZE, cursor_pos.y() - PET_SIZE // 2)),
                        )
                        self.next_chase_target_update = (
                            time.monotonic() + CHASE_TARGET_REFRESH_SECONDS
                        )
                    else:
                        screen = QApplication.primaryScreen().geometry()
                        margin = 150
                        rand_x = random.randint(margin, screen.width() - margin)
                        rand_y = random.randint(margin, screen.height() - margin)
                        self.target_pos = QPoint(rand_x, rand_y)

                    self.is_wandering = True
                    return

        if current_time.hour == WAKE_START_HOUR and current_time.minute < 5:
            if self.wake_excited_until == 0.0:
                self.wake_excited_until = time.monotonic() + WAKE_EXCITED_SECONDS

        if time.monotonic() < self.wake_excited_until:
            self.set_state("EXCITED")
            return

        if time.monotonic() < self.excited_until:
            self.set_state("EXCITED")
            return

        # Ручное перетаскивание мышей
        if self.old_pos is not None:
            self.set_state("EXCITED")  # Другой спрайт при ручном переносе
            return

        if now < self.happy_until:
            self.set_state("HAPPY")
            return

        hunger_pct = self.get_hunger_percentage()

        if hunger_pct <= 0.0:
            self.set_state("HUNGRY")
            return

        if hunger_pct <= PECKISH_THRESHOLD:
            # Уже проголодался, но ещё не критично — вместо резкого
            # прыжка в HUNGRY питомец какое-то время "выпрашивает еду",
            # ерзая между разными позами.
            if time.monotonic() >= self.peckish_until:
                self.peckish_state = random.choice(["SITTING", "CURIOUS", "EXCITED"])
                self.peckish_until = time.monotonic() + random.uniform(*PECKISH_HOLD_SECONDS)
            self.set_state(self.peckish_state)
            return

        # Определяем, не пора ли уже "устать" перед сном
        minutes_now = current_time.hour * 60 + current_time.minute
        minutes_to_sleep = (SLEEP_START_HOUR * 60) - minutes_now
        is_drowsy = 0 <= minutes_to_sleep <= DROWSY_BEFORE_SLEEP_MINUTES

        sitting_after = SITTING_AFTER_SECONDS * (DROWSY_SITTING_FACTOR if is_drowsy else 1.0)
        shy_after = SHY_AFTER_SECONDS * (DROWSY_SHY_FACTOR if is_drowsy else 1.0)

        if self.hover_started is not None:
            hovered_for = time.monotonic() - self.hover_started

            if hovered_for >= shy_after:
                self.set_state("SHY")
            else:
                self.set_state("CURIOUS")
            return

        # Иногда питомцу просто хорошо без повода
        if time.monotonic() < self.spontaneous_until:
            self.set_state("EXCITED")
            return

        if time.monotonic() > self.next_spontaneous_check:
            self.spontaneous_until = time.monotonic() + random.uniform(
                *SPONTANEOUS_JOY_DURATION_SECONDS
            )
            self.next_spontaneous_check = time.monotonic() + random.uniform(
                *SPONTANEOUS_JOY_INTERVAL_SECONDS
            )
            self.set_state("EXCITED")
            return

        idle_for = time.monotonic() - self.last_interaction

        if idle_for >= sitting_after:
            self.set_state("SITTING")
            return

        self.set_state("IDLE")

    def _feed_happy_bonus(self, satiety_before_feeding):
        """Сколько секунд радости даёт кормление — меньше, если и так был сыт."""
        if satiety_before_feeding >= OVERFEED_SATIETY_THRESHOLD:
            return FEED_HAPPY_SECONDS * OVERFEED_HAPPY_FACTOR
        return FEED_HAPPY_SECONDS

    def feed_pet(self):
        if self.is_sleep_time():
            return

        now = time.time()
        was_satiety = self.get_hunger_percentage()
        self.last_fed = now
        self.last_interaction = time.monotonic()
        save_last_fed(now)

        self.happy_until = time.monotonic() + self._feed_happy_bonus(was_satiety)
        self.peckish_until = 0.0
        self.set_state("HAPPY")

    # ========================================================
    # HOVER
    # ========================================================

    def enterEvent(self, event):
        self.hover_started = time.monotonic()
        self.last_interaction = time.monotonic()

        if not self.is_sleep_time():
            self.update_state()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover_started = None
        self.last_interaction = time.monotonic()

        if not self.is_sleep_time():
            self.update_state()
        super().leaveEvent(event)

    # ========================================================
    # DRAG + DIRECTION
    # ========================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.old_pos = event.globalPosition().toPoint()
            self.press_pos = self.old_pos
            self.press_time = time.monotonic()
            self.last_interaction = time.monotonic()
            self.is_wandering = False  # Прерываем авто-ходьбу при клике/переносе
            self.is_chasing_cursor = False
        elif event.button() == Qt.MouseButton.RightButton:
            # ПКМ по питомцу — тоже способ его "погладить"
            self._react_to_petting()

    def mouseMoveEvent(self, event):
        if self.old_pos is None:
            return

        current_pos = event.globalPosition().toPoint()
        delta = current_pos - self.old_pos

        if delta.x() > 1:
            self.facing_left = True
        elif delta.x() < -1:
            self.facing_left = False

        self.move(
            self.x() + delta.x(),
            self.y() + delta.y(),
        )

        self.old_pos = current_pos
        self.last_interaction = time.monotonic()

        if self.is_sleep_time():
            self.set_state("SLEEP")
        else:
            self.set_state("EXCITED")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.press_pos is not None:
            release_pos = event.globalPosition().toPoint()
            moved = (release_pos - self.press_pos).manhattanLength()
            duration = time.monotonic() - (self.press_time or time.monotonic())

            # Если мышку почти не двигали и отпустили быстро — это клик
            # (погладили питомца), а не перетаскивание.
            if moved <= CLICK_MAX_MOVEMENT and duration <= CLICK_MAX_DURATION:
                self._react_to_petting()

        self.old_pos = None
        self.press_pos = None
        self.press_time = None
        self.last_interaction = time.monotonic()

        if self.is_sleep_time():
            self.set_state("SLEEP")
        else:
            self.update_state()

    def _react_to_petting(self):
        """Реакция на клик/ПКМ — питомца погладили."""
        if self.is_sleep_time():
            return

        now_m = time.monotonic()
        self.last_interaction = now_m
        self.is_wandering = False
        self.is_chasing_cursor = False
        self.target_pos = None

        # Считаем "комбо" быстрых поглаживаний подряд
        if now_m - self.last_pet_time <= PET_COMBO_WINDOW_SECONDS:
            self.pet_combo += 1
        else:
            self.pet_combo = 1
        self.last_pet_time = now_m

        if self.pet_combo >= PET_COMBO_OVERSTIM_THRESHOLD:
            # Перегладили — питомцу нужно немного личного пространства
            self.click_reaction_state = "SHY"
            self.click_reaction_until = now_m + random.uniform(*OVERSTIM_REACTION_SECONDS)
            self.pet_combo = 0
        else:
            self.click_reaction_state = random.choice(["EXCITED", "HAPPY", "CURIOUS"])
            self.click_reaction_until = now_m + random.uniform(*CLICK_REACTION_SECONDS)

        self.set_state(self.click_reaction_state)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    pet = NeuroPetWidget()
    # Получаем геометрию экрана
    screen = QApplication.primaryScreen().geometry()

    # Левый нижний угол (с небольшим отступом от левого края и панели снизу)
    margin_x = 50
    margin_bottom = 120

    # Вычисляем координаты Y для левого нижнего угла
    pet_x = margin_x
    pet_y = screen.height() - PET_SIZE - margin_bottom

    # Перемещаем кролика
    pet.move(pet_x, pet_y + 125)

    pet.show()
    pet.lower()

    sys.exit(app.exec())
