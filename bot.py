import os
import asyncio
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import re          # <--- ДОБАВИЛИ
import requests    # <--- ДОБАВИЛИ
from datetime import datetime
from bs4 import BeautifulSoup # <--- ДОБАВИЛИ
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, 
    FSInputFile, InputProfilePhotoStatic, InlineKeyboardMarkup, 
    InlineKeyboardButton, CallbackQuery
)

# Импортируем твои функции (добавили move_file_with_tags)
from indexer import add_tag, get_by_tag, get_by_name, get_recent_files, remove_tag, move_file_with_tags, find_smart_folder
from db import init_db, get_conn
from game_status import (
    get_game_status, get_achievements, get_week_report,
    get_week_achievements, MAX_LEVEL,
)
from aiogram.exceptions import TelegramBadRequest
import ns_config
from aiogram import BaseMiddleware

# --- НАСТРОЙКИ ---
TOKEN = ns_config.TELEGRAM_BOT_TOKEN
DOWNLOAD_BASE_PATH = Path.home() / "NeuroSync_Downloads" 
NOTES_DIR = Path.home() / "NeuroNotes"
MY_METRO_CARD = ns_config.METRO_CARD_NUMBER

if not TOKEN:
    raise SystemExit("Не задан TELEGRAM_BOT_TOKEN (см. local_config.example.py)")

bot = Bot(token=TOKEN)
dp = Dispatcher()


class OwnerOnlyMiddleware(BaseMiddleware):
    """Бот умеет листать домашнюю папку — пускаем только владельца."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in ns_config.TELEGRAM_ALLOWED_USER_IDS:
            return None
        return await handler(event, data)


dp.message.outer_middleware(OwnerOnlyMiddleware())
dp.callback_query.outer_middleware(OwnerOnlyMiddleware())

# --- СТИЛЬ ВЫВОДА (монохромные символы, без цветных эмодзи и без линий) ---
# ◇ заголовок   ▾ раздел   ⟡ пункт   ▸ подпункт   ✧ акцент

def h1(title: str) -> str:
    return f"◇ **{title}**"

def h2(title: str) -> str:
    return f"▾ **{title}**"

def row(label: str, value, icon: str = "⟡") -> str:
    return f"{icon} {label}: `{value}`"

# --- ЛОГИКА МЕТРО (Tetrogame Engine) ---

def get_metro_info_final(card_number):
    session = requests.Session()
    base_url = "https://metropoliten.by/"
    headers_base = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
    }
    try:
        session.get(base_url, headers=headers_base)
        payload_number = f"%{card_number}%"
        ajax_url = f"{base_url}local/components/vosko/application/templates/.default/ajax/application.php"
        
        payload = {'FILTER[UF_NUMBER]': payload_number, 'language': 'ru'}
        ajax_headers = {
            'bx-ajax': 'true',
            'content-type': 'application/x-www-form-urlencoded',
            'referer': base_url,
            'user-agent': headers_base['User-Agent']
        }
        
        response = session.post(ajax_url, data=payload, headers=ajax_headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            container = soup.find('div', class_='container-items')
            if container:
                paragraphs = container.find_all('p')
                for p in paragraphs:
                    if 'message' not in p.get('class', []):
                        return p.get_text(strip=True)
                return container.get_text(" ", strip=True)
        return "Данные не найдены"
    except Exception as e:
        return f"Ошибка связи: {e}"

def render_status(info_text):
    # 1. Ищем все даты и время (поддерживает форматы с секундами и без)
    dates = re.findall(r'(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)', info_text)
    
    # Пытаемся вычленить тип проездного (текст до первой даты)
    type_match = re.search(r'^(.*?)(?=\s+действует|\s+до\s+|\d{2}\.)', info_text)
    card_type = type_match.group(1).strip() if type_match else "Проездной билет"

    if not dates:
        return f"{h2('Метро')}\n⟡ Данные не распознаны\n▸ `{info_text}`"

    # Обычно первая дата — окончание, вторая — последняя операция
    end_date_str = dates[0]
    last_op_str = dates[1] if len(dates) > 1 else "нет данных"

    try:
        # Приводим к формату для расчетов (отрезаем секунды если есть)
        clean_end_date = re.sub(r'(\d{2}:\d{2}):\d{2}', r'\1', end_date_str)
        end_date = datetime.strptime(clean_end_date, "%d.%m.%Y %H:%M")
        now = datetime.now()
        
        remaining_delta = end_date - now
        days_left = remaining_delta.days
        hours_left = remaining_delta.seconds // 3600

        # Расчет прогресса (базируем на стандартных 30 днях)
        total_days = 30
        percent = max(0, min(days_left / total_days, 1))
        
        # Красивый тонкий прогресс-бар
        bar_length = 12
        filled = int(bar_length * percent)
        bar = '▰' * filled + '▱' * (bar_length - filled)
        
        # Формируем итоговое сообщение
        response = [
            f"{h2('Метро')} ✧ {card_type}",
            row("Годен до", end_date.strftime('%d.%m.%Y')),
            row("Осталось", f"{max(0, days_left)} дн. {hours_left} ч."),
            row("Поездка", last_op_str),
            row("Остаток", f"{bar} {int(percent * 100)}%"),
        ]
        
        return "\n".join(response)

    except Exception as e:
        # Если что-то пошло не так в парсинге, выводим аккуратный сырой текст
        lines = "\n".join(f"▸ {part}" for part in info_text.split(", "))
        return f"{h2('Метро')} ✧ данные карты\n{lines}"

# --- СОСТОЯНИЯ (FSM) ---
class FileStates(StatesGroup):
    waiting_for_upload_tag = State()
    waiting_for_file = State()
    waiting_for_search_tag = State()
    waiting_for_search_name = State()
    waiting_for_select = State()       

class NoteStates(StatesGroup):
    waiting_for_menu_choice = State()  
    waiting_for_title = State()
    accumulating_content = State()
    waiting_for_select = State()       
    waiting_for_action = State()       
    editing_content = State()

class ExplorerStates(StatesGroup):
    browsing = State()

# --- КЛАВИАТУРЫ ---

def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Загрузить"), KeyboardButton(text="Проводник")],
            [KeyboardButton(text="Поиск по тегу"), KeyboardButton(text="Поиск по имени")],
            [KeyboardButton(text="Свежее"), KeyboardButton(text="Заметки")],
            [KeyboardButton(text="Статус"), KeyboardButton(text="Игра")]
        ],
        resize_keyboard=True
    )

def notes_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Создать заметку")],
            [KeyboardButton(text="Мои заметки")],
            [KeyboardButton(text="Домой")]
        ],
        resize_keyboard=True
    )

def note_actions_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Читать"), KeyboardButton(text="Править")],
            [KeyboardButton(text="Удалить"), KeyboardButton(text="Скачать")],
            [KeyboardButton(text="Назад к заметкам"), KeyboardButton(text="Домой")]
        ],
        resize_keyboard=True
    )

def home_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Домой")]],
        resize_keyboard=True
    )

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def check_user(message: Message):
    # Доступ уже ограничен OwnerOnlyMiddleware (по Telegram ID).
    return True

def get_smart_folders() -> dict:
    """Получает словарь умных папок {path: tag} из базы данных (только папки)"""
    smart_folders = {}
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT path, tag FROM tags")
        rows = c.fetchall()
        conn.close()
        
        for path, tag in rows:
            if os.path.isdir(path):
                smart_folders[str(Path(path))] = tag
    except Exception as e:
        print(f"Ошибка чтения умных папок: {e}")
    return smart_folders

async def show_file_list(message: Message, state: FSMContext, results: list, title: str):
    if not results:
        await message.answer(f"{h1(title)}\n⟡ ничего не найдено", reply_markup=main_kb())
        await state.clear()
        return

    msg_lines = [f"{h1(title)}", f"✧ найдено: {len(results)}", ""]
    paths_dict = {}
    
    for i, res in enumerate(results[:15], start=1):
        path_str = res[0]
        file_name = Path(path_str).name
        paths_dict[str(i)] = path_str
        msg_lines.append(f"⟡ {i} · `{file_name}`")

    msg_lines.append("\n▸ отправьте номер файла, чтобы скачать его")
    
    await state.update_data(found_paths=paths_dict)
    await state.set_state(FileStates.waiting_for_select)
    await message.answer("\n".join(msg_lines), reply_markup=home_kb(), parse_mode="Markdown")

# --- ГЛОБАЛЬНАЯ КНОПКА ОТМЕНЫ (ДОМОЙ) ---

@dp.message(F.text.in_(["Домой", "Стоп", "Главное меню"]), ~StateFilter(NoteStates.accumulating_content))
async def cancel_any_action(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.clear()
    await message.answer(f"{h1('NeuroSync')}\n▸ главное меню", reply_markup=main_kb())

# --- ОБРАБОТЧИКИ КОМАНД ---

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not await check_user(message): return
    init_db()
    DOWNLOAD_BASE_PATH.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    await message.answer(f"{h1('NeuroSync')}\n✧ система готова", reply_markup=main_kb())

@dp.message(F.text == "Статус")
async def status_cmd(message: Message):
    if not await check_user(message): return
    
    # 1. Системный статус
    total, used, free = shutil.disk_usage("/")
    notes_count = len(list(NOTES_DIR.glob('*.md')))
    
    # 2. Метро статус (Твой код)
    await message.answer("✧ Синхронизация с метро…")
    metro_raw = await asyncio.to_thread(get_metro_info_final, MY_METRO_CARD)
    metro_styled = render_status(metro_raw)
    
    status_text = (
        f"{h1('NeuroSync OS Status')}\n\n"
        f"{h2('Система')}\n"
        f"{row('Свободно', f'{free // (2**30)} GB')}\n"
        f"{row('Заметок', f'{notes_count} шт.')}\n\n"
        f"{metro_styled}"
    )
    await message.answer(status_text, parse_mode="Markdown")

# --- СТАТУС ИГРЫ (виджет NeuroGame) ---

def bar(value, maximum=100, length=12):
    value = max(0.0, min(float(value), float(maximum)))
    filled = int(length * value / maximum) if maximum else 0
    return "▰" * filled + "▱" * (length - filled)

def render_game(st: dict) -> str:
    lvl = st["level"]
    if st["xp_need"]:
        xp_val = f"{bar(min(st['xp'], st['xp_need']), st['xp_need'])} {st['xp']:.0f}/{st['xp_need']}"
    else:
        xp_val = f"{bar(1, 1)} MAX"
    hp_val = f"{bar(st['hp'])} {st['hp']:.0f}/100"
    mom_val = f"{bar(st['momentum'], 100)} {st['momentum']:.0f}% {st['momentum_state']}"

    hours, minutes = divmod(int(st["today_minutes"]), 60)
    done, total = st["achievements"]

    lines = [
        f"{h1('NeuroSync Game')} ✧ LVL {lvl}/{MAX_LEVEL}",
        "",
        h2("Состояние"),
        row("XP", xp_val),
        row("HP", hp_val),
        row("Momentum", mom_val),
        "",
        h2("Сегодня"),
        row("Фокус", f"{hours}h {minutes:02d}m"),
        row("Сессий", st["today_sessions"]),
    ]
    if st["sleep_hours"] is not None:
        lines.append(row("Сон", f"{st['sleep_hours']:g}h"))

    if st["live"]:
        h, rem = divmod(int(st["live_seconds"]), 3600)
        m, s = divmod(rem, 60)
        state = "идёт" if st["live"] == "studying" else "пауза"
        lines += ["", f"{h2('Сессия')} ✧ {state}", row("Время", f"{h:02d}:{m:02d}:{s:02d}")]

    lines += ["", row("Достижения", f"{done}/{total}", icon="✧")]
    return "\n".join(lines)

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

def _day_label(day) -> str:
    mark = "▸" if day == datetime.now().date() else " "
    return f"{mark}{WEEKDAYS[day.weekday()]} {day:%d.%m}"

def _hm(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m"

def render_work_week(report: list) -> str:
    peak = max(d["minutes"] for d in report)
    chart = "\n".join(
        f"{_day_label(d['day'])} {bar(d['minutes'], peak or 1, 10)} {_hm(d['minutes'])}"
        for d in report
    )
    total = sum(d["minutes"] for d in report)
    best = max(report, key=lambda d: d["minutes"])
    lines = [
        h1("Работа за неделю"), "",
        f"```\n{chart}\n```", "",
        row("Всего", _hm(total)),
        row("В среднем", f"{_hm(total / len(report))} в день"),
        row("Сессий", sum(d["sessions"] for d in report)),
    ]
    if best["minutes"] > 0:
        lines.append(row("Лучший день", f"{WEEKDAYS[best['day'].weekday()]} {best['day']:%d.%m} · {_hm(best['minutes'])}"))
    return "\n".join(lines)

def render_sleep_week(report: list) -> str:
    rows = []
    for d in report:
        h = d["sleep_hours"]
        if h is None:
            rows.append(f"{_day_label(d['day'])} {'·' * 10} нет данных")
        else:
            rows.append(f"{_day_label(d['day'])} {bar(h, 10, 10)} {h:g}h")
    lines = [h1("Сон за неделю"), "", "```\n" + "\n".join(rows) + "\n```", ""]
    vals = [d["sleep_hours"] for d in report if d["sleep_hours"] is not None]
    if vals:
        lines += [
            row("В среднем", f"{sum(vals) / len(vals):.1f}h"),
            row("Минимум", f"{min(vals):g}h"),
            row("Максимум", f"{max(vals):g}h"),
            row("Записано дней", f"{len(vals)} из {len(report)}"),
        ]
    else:
        lines.append("⟡ данных о сне за неделю нет")
    return "\n".join(lines)

def render_week_achievements(items: list) -> str:
    # обычный текст (без Markdown): в названиях могут быть спецсимволы
    lines = [f"◇ Достижения за неделю ✧ {len(items)}", ""]
    for day, name, desc in items:
        lines.append(f"⟡ {WEEKDAYS[day.weekday()]} {day:%d.%m} · {name}")
        lines.append(f"▸ {desc}")
    if not items:
        lines.append("⟡ новых достижений за неделю нет")
    return "\n".join(lines)

def game_menu_kb(back: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if back:
        rows.append([InlineKeyboardButton(text="◂ к статусу", callback_data="game_home")])
    rows += [
        [InlineKeyboardButton(text="◇ Работа", callback_data="game_work"),
         InlineKeyboardButton(text="◇ Сон", callback_data="game_sleep")],
        [InlineKeyboardButton(text="◇ Новые достижения", callback_data="game_week_ach"),
         InlineKeyboardButton(text="◇ Все достижения", callback_data="game_ach")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _edit(callback: CallbackQuery, text: str, markup, parse_mode=None):
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        pass  # повторное нажатие: текст не изменился

@dp.callback_query(F.data == "game_home")
async def game_home(callback: CallbackQuery):
    await callback.answer()
    st = await asyncio.to_thread(get_game_status)
    if st is None:
        await _edit(callback, f"{h1('NeuroSync Game')}\n⟡ файл состояния игры не найден", game_menu_kb(), "Markdown")
        return
    await _edit(callback, render_game(st), game_menu_kb(), "Markdown")

@dp.callback_query(F.data == "game_work")
async def game_work(callback: CallbackQuery):
    await callback.answer()
    report = await asyncio.to_thread(get_week_report)
    await _edit(callback, render_work_week(report), game_menu_kb(back=True), "Markdown")

@dp.callback_query(F.data == "game_sleep")
async def game_sleep(callback: CallbackQuery):
    await callback.answer()
    report = await asyncio.to_thread(get_week_report)
    await _edit(callback, render_sleep_week(report), game_menu_kb(back=True), "Markdown")

@dp.callback_query(F.data == "game_week_ach")
async def game_week_ach(callback: CallbackQuery):
    await callback.answer()
    items = await asyncio.to_thread(get_week_achievements)
    await _edit(callback, render_week_achievements(items), game_menu_kb(back=True))

@dp.callback_query(F.data == "game_ach")
async def game_achievements(callback: CallbackQuery):
    await callback.answer()
    items = get_achievements()
    unlocked = [(n, d) for n, d, ok in items if ok]
    lines = [f"◇ Все достижения ✧ {len(unlocked)}/{len(items)}", ""]
    for name, desc in unlocked:
        lines.append(f"⟡ {name} · {desc}")
    if not unlocked:
        lines.append("⟡ пока нет открытых")
    await _edit(callback, "\n".join(lines)[:4000], game_menu_kb(back=True))

@dp.message(F.text == "Игра")
async def game_cmd(message: Message):
    if not await check_user(message): return
    st = await asyncio.to_thread(get_game_status)
    if st is None:
        await message.answer(f"{h1('NeuroSync Game')}\n⟡ файл состояния игры не найден или повреждён")
        return
    await message.answer(render_game(st), reply_markup=game_menu_kb(), parse_mode="Markdown")

# --- ЛОГИКА ФАЙЛОВОГО МЕНЕДЖЕРА (ПРОВОДНИК) ---

async def send_fm_view(event: Message | CallbackQuery, path: Path, state: FSMContext):
    try:
        items = list(path.iterdir())
    except PermissionError:
        text = f"{h1('Проводник')}\n⟡ Нет доступа: `{path}`"
        if isinstance(event, Message):
            await event.answer(text)
        else:
            await event.answer("Отказано в доступе", show_alert=True)
        return

    # Скрытые папки и файлы (имя начинается с точки) в проводнике не показываем
    items = [f for f in items if not f.name.startswith(".")]

    folders = sorted([f for f in items if f.is_dir()], key=lambda x: x.name.lower())
    files = sorted([f for f in items if f.is_file()], key=lambda x: x.name.lower())
    
    display_items = (folders + files)[:90]
    
    map_dict = {}
    kb_buttons = []
    
    if path.parent != path:
        kb_buttons.append([InlineKeyboardButton(text="◂ на уровень выше", callback_data="fm_up")])
        
    for i, item in enumerate(display_items):
        map_dict[str(i)] = item.name
        label = f"▸ {item.name}/" if item.is_dir() else f"⟡ {item.name}"
        kb_buttons.append([InlineKeyboardButton(text=label, callback_data=f"fm_cd_{i}")])
        
    await state.update_data(fm_map=map_dict)
    markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    text = f"{h1('Проводник')}\n▸ `{path}`"
    if len(folders) + len(files) > 90:
        text += "\n✧ показаны первые 90 элементов"
        
    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")

@dp.message(F.text == "Проводник")
async def start_explorer(message: Message, state: FSMContext):
    if not await check_user(message): return
    start_path = Path.home()
    await state.set_state(ExplorerStates.browsing)
    await state.update_data(current_path=str(start_path))
    await message.answer(f"{h1('Проводник')}\n✧ навигация кнопками ниже", reply_markup=home_kb())
    await send_fm_view(message, start_path, state)

@dp.callback_query(ExplorerStates.browsing, F.data.startswith("fm_"))
async def process_explorer_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_path_str = data.get("current_path")
    
    if not current_path_str:
        await callback.answer("Сессия устарела. Начните заново.")
        return
        
    current_path = Path(current_path_str)
    action = callback.data.split("_")[1]
    
    if action == "up":
        new_path = current_path.parent
        await state.update_data(current_path=str(new_path))
        await send_fm_view(callback, new_path, state)
        
    elif action == "cd":
        index = callback.data.split("_")[2]
        fm_map = data.get("fm_map", {})
        target_name = fm_map.get(index)
        
        if not target_name:
            await callback.answer("Ошибка индекса файла.")
            return
            
        target_path = current_path / target_name
        
        if target_path.is_dir():
            await state.update_data(current_path=str(target_path))
            await send_fm_view(callback, target_path, state)
        elif target_path.is_file():
            await callback.answer("Отправка файла...")
            try:
                await callback.message.answer_document(FSInputFile(str(target_path)))
            except Exception as e:
                await callback.message.answer(f"Ошибка при отправке: {e}")
                
    await callback.answer()

# --- ЛОГИКА ЗАГРУЗКИ ФАЙЛОВ В СИСТЕМУ С АВТОСОРТИРОВКОЙ ---

@dp.message(F.text == "Загрузить")
async def start_upload(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.set_state(FileStates.waiting_for_upload_tag)
    await message.answer(f"{h1('Загрузка')}\n▸ введите тег для файла", reply_markup=home_kb())

@dp.message(FileStates.waiting_for_upload_tag)
async def process_upload_tag(message: Message, state: FSMContext):
    await state.update_data(tag=message.text.strip())
    await state.set_state(FileStates.waiting_for_file)
    await message.answer(f"{h1('Загрузка')}\n▸ тег: `{message.text.strip()}`\n⟡ жду документ или фото", reply_markup=home_kb())

@dp.message(FileStates.waiting_for_file, F.document | F.photo)
async def handle_file_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    tag = data['tag']
    
    if message.document:
        f_id, f_name = message.document.file_id, message.document.file_name
    else:
        f_id, f_name = message.photo[-1].file_id, f"photo_{message.photo[-1].file_unique_id}.jpg"

    # Сначала сохраняем во временный буфер загрузок
    temp_path = DOWNLOAD_BASE_PATH / f_name
    file = await bot.get_file(f_id)
    await bot.download_file(file.file_path, destination=str(temp_path))
    
    # Добавляем базовый тег
    add_tag(str(temp_path), tag)
    
    # УМНАЯ СОРТИРОВКА: ищем подходящую умную папку
    target_folder = find_smart_folder(tag)
    
    if target_folder:
        # Переносим файл встроенной логикой твоего движка indexer
        final_path = move_file_with_tags(str(temp_path), target_folder)
        if final_path:
            folder_name = Path(target_folder).name
            await message.answer(
                f"{h1('Файл принят')}\n"
                f"✧ умная папка: `.../{folder_name}/`\n"
                f"⟡ перемещён и переиндексирован", 
                reply_markup=main_kb()
            )
        else:
            await message.answer(f"{h1('Файл принят')}\n⟡ проиндексирован, но не удалось переместить в умную папку", reply_markup=main_kb())
    else:
        await message.answer(f"{h1('Файл принят')}\n⟡ проиндексирован, лежит в загрузках\n▸ умных папок под этот тег нет", reply_markup=main_kb())
        
    await state.clear()

# --- ЛОГИКА ПОИСКА СИСТЕМНЫХ ФАЙЛОВ ---

@dp.message(F.text == "Поиск по тегу")
async def start_tag_search(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.set_state(FileStates.waiting_for_search_tag)
    await message.answer(f"{h1('Поиск по тегу')}\n▸ какой тег ищем?", reply_markup=home_kb())

@dp.message(FileStates.waiting_for_search_tag)
async def search_tag_handler(message: Message, state: FSMContext):
    results = get_by_tag(message.text.strip())
    await show_file_list(message, state, results, "Результаты по тегу")

@dp.message(F.text == "Поиск по имени")
async def start_name_search(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.set_state(FileStates.waiting_for_search_name)
    await message.answer(f"{h1('Поиск по имени')}\n▸ введите часть названия", reply_markup=home_kb())

@dp.message(FileStates.waiting_for_search_name)
async def search_name_handler(message: Message, state: FSMContext):
    results = get_by_name(message.text.strip())
    await show_file_list(message, state, results, "Результаты поиска")

@dp.message(F.text == "Свежее")
async def recent_files_handler(message: Message, state: FSMContext):
    if not await check_user(message): return
    results = get_recent_files(10)
    await show_file_list(message, state, results, "Последние 10 файлов")

@dp.message(FileStates.waiting_for_select)
async def process_file_selection(message: Message, state: FSMContext):
    choice = message.text.strip()
    data = await state.get_data()
    paths = data.get("found_paths", {})
    
    if choice in paths:
        file_path_str = paths[choice]
        if os.path.exists(file_path_str):
            await message.answer("✧ отправляю…")
            try:
                await message.answer_document(FSInputFile(file_path_str))
                await state.clear()
                await message.answer("⟡ готово", reply_markup=main_kb())
            except Exception as e:
                await message.answer(f"Ошибка при отправке: {e}")
        else:
            await message.answer("⟡ файл не найден на диске")
    else:
        await message.answer("⟡ неверный номер, выберите из списка")

# --- МОДУЛЬ РАБОТЫ С ЗАМЕТКАМИ ---

@dp.message(F.text == "Заметки")
async def notes_menu(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.set_state(NoteStates.waiting_for_menu_choice)
    await message.answer(f"{h1('NeuroNotes')}\n▸ выберите действие на клавиатуре", reply_markup=notes_menu_kb(), parse_mode="Markdown")

@dp.message(NoteStates.waiting_for_menu_choice)
async def process_notes_menu(message: Message, state: FSMContext):
    choice = message.text.strip()
    
    if choice == "Создать заметку":
        await state.set_state(NoteStates.waiting_for_title)
        await message.answer(f"{h1('Новая заметка')}\n▸ введите название (имя файла)", reply_markup=home_kb())
        
    elif choice == "Мои заметки":
        files = sorted(NOTES_DIR.glob("*.md"), key=os.path.getmtime, reverse=True)
        if not files:
            await message.answer(f"{h1('Заметки')}\n⟡ пока пусто", reply_markup=notes_menu_kb())
            return
            
        msg_lines = [h1("Заметки"), ""]
        notes_dict = {}
        
        for i, f in enumerate(files[:20], start=1):
            notes_dict[str(i)] = str(f)
            msg_lines.append(f"⟡ {i} · `{f.name}`")
            
        msg_lines.append("\n▸ отправьте номер заметки")
        
        await state.update_data(found_notes=notes_dict)
        await state.set_state(NoteStates.waiting_for_select)
        await message.answer("\n".join(msg_lines), reply_markup=home_kb(), parse_mode="Markdown")
    else:
        await message.answer("Используйте кнопки управления на клавиатуре.")

@dp.message(NoteStates.waiting_for_title)
async def note_create_content(message: Message, state: FSMContext):
    title = message.text.strip().replace("/", "_")
    await state.update_data(note_title=title, accumulated_text=[])
    await state.set_state(NoteStates.accumulating_content)
    await message.answer(
        f"{h1(title)}\n"
        "⟡ отправляйте сообщения подряд, я допишу их в заметку\n"
        "▸ чтобы сохранить, нажмите «Домой»",
        reply_markup=home_kb(),
        parse_mode="Markdown"
    )

@dp.message(NoteStates.accumulating_content)
async def note_accumulate(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    current_lines = data.get('accumulated_text', [])
    current_lines.append(text)
    await state.update_data(accumulated_text=current_lines)

    if text in ["Домой", "Стоп", "Главное меню"]:
        title = data['note_title']
        lines = current_lines[:-1]
        
        if not lines:
            await message.answer("⟡ буфер пуст, заметка не сохранена", reply_markup=main_kb())
            await state.clear()
            return
            
        full_content = "\n".join(lines)
        file_path = NOTES_DIR / f"{title}.md"
        
        # ФИКС ТУТ: Добавили encoding='utf-8'
        file_path.write_text(full_content, encoding='utf-8')
        
        add_tag(str(file_path), "note")
        await message.answer(f"✧ заметка сохранена: `{title}.md`", reply_markup=main_kb(), parse_mode="Markdown")
        await state.clear()

@dp.message(NoteStates.waiting_for_select)
async def process_note_selection(message: Message, state: FSMContext):
    choice = message.text.strip()
    data = await state.get_data()
    notes = data.get("found_notes", {})
    
    if choice in notes:
        file_path_str = notes[choice]
        await state.update_data(selected_note_path=file_path_str)
        
        await state.set_state(NoteStates.waiting_for_action)
        await message.answer(
            f"{h1('Заметка')}\n▸ `{Path(file_path_str).name}`\n⟡ выберите действие на клавиатуре",
            reply_markup=note_actions_kb(),
            parse_mode="Markdown"
        )
    else:
        await message.answer("⟡ неверный номер, выберите из списка")

@dp.message(NoteStates.waiting_for_action)
async def process_note_action(message: Message, state: FSMContext):
    choice = message.text.strip()
    data = await state.get_data()
    file_path = data.get("selected_note_path")
    
    if not file_path or not os.path.exists(file_path):
        await message.answer("⟡ файл больше не существует", reply_markup=notes_menu_kb())
        await state.set_state(NoteStates.waiting_for_menu_choice)
        return
        
    if choice == "Читать":
        content = Path(file_path).read_text(encoding='utf-8')
        
        max_length = 3500
        is_truncated = len(content) > max_length
        preview = content[:max_length] if is_truncated else content
        safe_preview = preview.replace("```", "'''")

        response_text = (
            f"{h1(Path(file_path).name)}\n\n"
            f"```\n"
            f"{safe_preview}\n"
            f"```"
        )
        
        if is_truncated:
            response_text += "\n✧ текст обрезан, нажмите «Скачать»"

        try:
            await message.answer(response_text, reply_markup=note_actions_kb(), parse_mode="Markdown")
        except Exception as e:
            import html
            safe_html = html.escape(preview)
            html_text = f"◇ <b>{html.escape(Path(file_path).name)}</b>\n\n<pre>{safe_html}</pre>"
            await message.answer(html_text, reply_markup=note_actions_kb(), parse_mode="HTML")
        
    elif choice == "Править":
        await state.set_state(NoteStates.editing_content)
        # ФИКС ТУТ: Добавили encoding='utf-8'
        current_content = Path(file_path).read_text(encoding='utf-8')
        await message.answer(
            f"{h1('Правка')}\n`{current_content}`\n\n"
            "▸ отправьте ОДНО сообщение, оно полностью заменит старый текст",
            reply_markup=home_kb(),
            parse_mode="Markdown"
        )
        
    elif choice == "Удалить":
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
            remove_tag(file_path)
            await message.answer(f"✧ удалено: `{Path(file_path).name}`", reply_markup=notes_menu_kb(), parse_mode="Markdown")
        except Exception as e:
            await message.answer(f"⟡ не удалось удалить: {e}", reply_markup=notes_menu_kb())
        await state.set_state(NoteStates.waiting_for_menu_choice)
        
    elif choice == "Скачать":
        await message.answer_document(FSInputFile(file_path), reply_markup=note_actions_kb())
        
    elif choice == "Назад к заметкам":
        await state.set_state(NoteStates.waiting_for_menu_choice)
        await message.answer(f"{h1('NeuroNotes')}\n▸ меню заметок", reply_markup=notes_menu_kb())
    else:
        await message.answer("Пожалуйста, используйте кнопки меню.")

@dp.message(NoteStates.editing_content)
async def note_edit_save(message: Message, state: FSMContext):
    data = await state.get_data()
    file_path_str = data['selected_note_path']
    
    # ФИКС ТУТ: Добавили encoding='utf-8'
    Path(file_path_str).write_text(message.text.strip(), encoding='utf-8')
    await message.answer(f"✧ сохранено: `{Path(file_path_str).name}`", reply_markup=note_actions_kb(), parse_mode="Markdown")
    await state.set_state(NoteStates.waiting_for_action)

# --- СТАРТ БОТА ---

async def main():
    print("NeuroSync Бот инициализируется...")
    if not ns_config.TELEGRAM_ALLOWED_USER_IDS:
        raise SystemExit(
            "Не задан TELEGRAM_ALLOWED_USER_IDS: без него бот никого не пустит. "
            "Узнай свой id у @userinfobot и впиши в local_config.py"
        )
    
    icon_path = Path("icon.png")
    if icon_path.exists():
        try:
            await bot.set_my_profile_photo(photo=InputProfilePhotoStatic(photo=FSInputFile(icon_path)))
            print("Аватарка бота успешно синхронизирована с файлом icon.png!")
        except Exception as e:
            print(f"Предупреждение при обновлении аватарки: {e}")
    else:
        print("Файл icon.png не обнаружен. Пропуск обновления аватара.")

    print("NeuroSync CLI-Style Bot успешно запущен и слушает потоки!")
    await dp.start_polling(bot)

def _acquire_bot_lock():
    """Второй экземпляр бота с тем же токеном = конфликт getUpdates и 'молчащий' бот."""
    import fcntl
    lock_file = open(Path.home() / ".neurosync_bot.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = Path.home().joinpath(".neurosync_bot.lock").read_text().strip()
        except OSError:
            holder = "?"
        print(f"Бот уже запущен (PID {holder}), второй экземпляр не нужен. "
              f"Остановить: kill {holder}")
        raise SystemExit(0)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


if __name__ == "__main__":
    _bot_lock = _acquire_bot_lock()
    asyncio.run(main())
