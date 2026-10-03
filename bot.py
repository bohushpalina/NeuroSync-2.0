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
from indexer import add_tag, get_by_tag, get_by_name, get_recent_files, remove_tag, move_file_with_tags
from db import init_db, get_conn
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
        return f"**Данные не распознаны**\n\n`{info_text}`"

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
        status_icon = "" if days_left > 3 else ""
        if days_left <= 0: status_icon = ""

        response = [
            f"🎫 **{card_type}**",
            f"**Годен до:** `{end_date.strftime('%d.%m.%Y')}`",
            f"**Осталось:** `{max(0, days_left)} дн. {hours_left} ч.`",
            f"**Последняя поездка:** `{last_op_str}`",
            f"{status_icon} **Статус:** `{bar} {int(percent * 100)}%`",
        ]
        
        return "\n".join(response)

    except Exception as e:
        # Если что-то пошло не так в парсинге, выводим аккуратный сырой текст
        return (
            f"💳 **Данные карты:**\n"
            f"━━━━━━━━━━━━━━\n"
            f"{info_text.replace(', ', '\n')}\n"
            f"━━━━━━━━━━━━━━"
        )

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
            [KeyboardButton(text="Статус")]
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
    if not message.from_user.username or message.from_user.username.lower() != "so_me_bo_dy":
        await message.answer("Доступ запрещен. Это приватный бот.")
        return False
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
        await message.answer(f"{title}: Ничего не найдено.", reply_markup=main_kb())
        await state.clear()
        return

    msg_lines = [f"**{title}** (найдено {len(results)}):", ""]
    paths_dict = {}
    
    for i, res in enumerate(results[:15], start=1):
        path_str = res[0]
        file_name = Path(path_str).name
        paths_dict[str(i)] = path_str
        msg_lines.append(f"{i}. `{file_name}`")

    msg_lines.append("\n**Введите номер файла**, чтобы выкачать его:")
    
    await state.update_data(found_paths=paths_dict)
    await state.set_state(FileStates.waiting_for_select)
    await message.answer("\n".join(msg_lines), reply_markup=home_kb(), parse_mode="Markdown")

# --- ГЛОБАЛЬНАЯ КНОПКА ОТМЕНЫ (ДОМОЙ) ---

@dp.message(F.text.in_(["Домой", "Стоп", "Главное меню"]), ~StateFilter(NoteStates.accumulating_content))
async def cancel_any_action(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.clear()
    await message.answer("Действие отменено. Вы в главном меню.", reply_markup=main_kb())

# --- ОБРАБОТЧИКИ КОМАНД ---

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not await check_user(message): return
    init_db()
    DOWNLOAD_BASE_PATH.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    await message.answer("Система NeuroSync готова. Что делаем?", reply_markup=main_kb())

@dp.message(F.text == "Статус")
async def status_cmd(message: Message):
    if not await check_user(message): return
    
    # 1. Системный статус
    total, used, free = shutil.disk_usage("/")
    notes_count = len(list(NOTES_DIR.glob('*.md')))
    
    # 2. Метро статус (Твой код)
    await message.answer("🛰 Синхронизация с сервером метро...")
    metro_raw = await asyncio.to_thread(get_metro_info_final, MY_METRO_CARD)
    metro_styled = render_status(metro_raw)
    
    status_text = (
        f"💻 **NeuroSync OS Status**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💾 Свободно: `{free // (2**30)} GB`\n"
        f"📝 Заметок: `{notes_count} шт.`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{metro_styled}\n"
        f"© made by Tetrogame\n"
        f"━━━━━━━━━━━━━━━━━━━━"

    )
    await message.answer(status_text, parse_mode="Markdown")

# --- ЛОГИКА ФАЙЛОВОГО МЕНЕДЖЕРА (ПРОВОДНИК) ---

async def send_fm_view(event: Message | CallbackQuery, path: Path, state: FSMContext):
    try:
        items = list(path.iterdir())
    except PermissionError:
        text = f"Нет доступа к директории:\n`{path}`"
        if isinstance(event, Message):
            await event.answer(text)
        else:
            await event.answer("Отказано в доступе", show_alert=True)
        return

    folders = sorted([f for f in items if f.is_dir()], key=lambda x: x.name.lower())
    files = sorted([f for f in items if f.is_file()], key=lambda x: x.name.lower())
    
    display_items = (folders + files)[:90]
    
    map_dict = {}
    kb_buttons = []
    
    if path.parent != path:
        kb_buttons.append([InlineKeyboardButton(text="[ На уровень выше ]", callback_data="fm_up")])
        
    for i, item in enumerate(display_items):
        map_dict[str(i)] = item.name
        prefix = "[Папка]" if item.is_dir() else "[Файл]"
        kb_buttons.append([InlineKeyboardButton(text=f"{prefix} {item.name}", callback_data=f"fm_cd_{i}")])
        
    await state.update_data(fm_map=map_dict)
    markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    text = f"**Текущая директория:**\n`{path}`"
    if len(folders) + len(files) > 90:
        text += "\n*(Показаны первые 90 элементов)*"
        
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
    await message.answer("Файловый менеджер активирован. Используйте кнопки ниже для навигации.", reply_markup=home_kb())
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
    await message.answer("Введите тег для этого файла:", reply_markup=home_kb())

@dp.message(FileStates.waiting_for_upload_tag)
async def process_upload_tag(message: Message, state: FSMContext):
    await state.update_data(tag=message.text.strip())
    await state.set_state(FileStates.waiting_for_file)
    await message.answer(f"Жду файл (документ или фото) для тега '{message.text}'...", reply_markup=home_kb())

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
    smart_folders = get_smart_folders()
    target_folder = next((p for p, t in smart_folders.items() if t.lower() in tag.lower()), None)
    
    if target_folder:
        # Переносим файл встроенной логикой твоего движка indexer
        final_path = move_file_with_tags(str(temp_path), target_folder)
        if final_path:
            folder_name = Path(target_folder).name
            await message.answer(
                f"✨ Обнаружена умная папка!\n"
                f"Файл успешно перемещен в `.../{folder_name}/` и переиндексирован.", 
                reply_markup=main_kb()
            )
        else:
            await message.answer("Файл проиндексирован, но возникла ошибка при перемещении в умную папку.", reply_markup=main_kb())
    else:
        await message.answer("Файл принят, проиндексирован и сохранен в загрузки (умных папок под этот тег не найдено).", reply_markup=main_kb())
        
    await state.clear()

# --- ЛОГИКА ПОИСКА СИСТЕМНЫХ ФАЙЛОВ ---

@dp.message(F.text == "Поиск по тегу")
async def start_tag_search(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.set_state(FileStates.waiting_for_search_tag)
    await message.answer("Какой тег ищем?", reply_markup=home_kb())

@dp.message(FileStates.waiting_for_search_tag)
async def search_tag_handler(message: Message, state: FSMContext):
    results = get_by_tag(message.text.strip())
    await show_file_list(message, state, results, "Результаты по тегу")

@dp.message(F.text == "Поиск по имени")
async def start_name_search(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.set_state(FileStates.waiting_for_search_name)
    await message.answer("Введите часть названия файла:", reply_markup=home_kb())

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
            await message.answer("Отправляю файл...")
            try:
                await message.answer_document(FSInputFile(file_path_str))
                await state.clear()
                await message.answer("Готово.", reply_markup=main_kb())
            except Exception as e:
                await message.answer(f"Ошибка при отправке: {e}")
        else:
            await message.answer("Файл не найден физически на диске.")
    else:
        await message.answer("Неверный номер. Введите индекс из списка выше.")

# --- МОДУЛЬ РАБОТЫ С ЗАМЕТКАМИ ---

@dp.message(F.text == "Заметки")
async def notes_menu(message: Message, state: FSMContext):
    if not await check_user(message): return
    await state.set_state(NoteStates.waiting_for_menu_choice)
    await message.answer("**Панель заметок NeuroNotes**\nВыберите действие на клавиатуре:", reply_markup=notes_menu_kb(), parse_mode="Markdown")

@dp.message(NoteStates.waiting_for_menu_choice)
async def process_notes_menu(message: Message, state: FSMContext):
    choice = message.text.strip()
    
    if choice == "Создать заметку":
        await state.set_state(NoteStates.waiting_for_title)
        await message.answer("Введите название для новой заметки (имя файла):", reply_markup=home_kb())
        
    elif choice == "Мои заметки":
        files = sorted(NOTES_DIR.glob("*.md"), key=os.path.getmtime, reverse=True)
        if not files:
            await message.answer("В папке NeuroNotes пока нет заметок.", reply_markup=notes_menu_kb())
            return
            
        msg_lines = ["**Список заметок в системе:**", ""]
        notes_dict = {}
        
        for i, f in enumerate(files[:20], start=1):
            notes_dict[str(i)] = str(f)
            msg_lines.append(f"{i}. `{f.name}`")
            
        msg_lines.append("\n**Введите номер заметки** из текстового списка выше:")
        
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
        f"Заметка **{title}** инициализирована.\n"
        "Отправляйте любые текстовые сообщения подряд. Я буду дописывать их в буфер.\n"
        "Чтобы завершить сбор и сохранить, нажмите кнопку Домой.",
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
            await message.answer("Буфер пуст. Заметка не сохранена.", reply_markup=main_kb())
            await state.clear()
            return
            
        full_content = "\n".join(lines)
        file_path = NOTES_DIR / f"{title}.md"
        
        # ФИКС ТУТ: Добавили encoding='utf-8'
        file_path.write_text(full_content, encoding='utf-8')
        
        add_tag(str(file_path), "note")
        await message.answer(f"Заметка `{title}.md` успешно сохранена!", reply_markup=main_kb(), parse_mode="Markdown")
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
            f"**Управление заметкой:** `{Path(file_path_str).name}`\nВыберите действие на клавиатуре:",
            reply_markup=note_actions_kb(),
            parse_mode="Markdown"
        )
    else:
        await message.answer("Неверный номер заметки. Введите индекс из списка.")

@dp.message(NoteStates.waiting_for_action)
async def process_note_action(message: Message, state: FSMContext):
    choice = message.text.strip()
    data = await state.get_data()
    file_path = data.get("selected_note_path")
    
    if not file_path or not os.path.exists(file_path):
        await message.answer("Ошибка: файл больше не существует на сервере.", reply_markup=notes_menu_kb())
        await state.set_state(NoteStates.waiting_for_menu_choice)
        return
        
    if choice == "Читать":
        content = Path(file_path).read_text(encoding='utf-8')
        
        max_length = 3500
        is_truncated = len(content) > max_length
        preview = content[:max_length] if is_truncated else content
        safe_preview = preview.replace("```", "'''")

        response_text = (
            f"**Содержимое `{Path(file_path).name}`:**\n"
            f"━━━━━━━━━━━━━━\n"
            f"```\n"
            f"{safe_preview}\n"
            f"```\n"
            f"━━━━━━━━━━━━━━"
        )
        
        if is_truncated:
            response_text += f"\n**Текст обрезан!** Используйте кнопку **'Скачать'**."

        try:
            await message.answer(response_text, reply_markup=note_actions_kb(), parse_mode="Markdown")
        except Exception as e:
            import html
            safe_html = html.escape(preview)
            html_text = f"<b>Содержимое {Path(file_path).name}:</b>\n━━━━━━━━━━━━━━\n<pre>{safe_html}</pre>\n━━━━━━━━━━━━━━"
            await message.answer(html_text, reply_markup=note_actions_kb(), parse_mode="HTML")
        
    elif choice == "Править":
        await state.set_state(NoteStates.editing_content)
        # ФИКС ТУТ: Добавили encoding='utf-8'
        current_content = Path(file_path).read_text(encoding='utf-8')
        await message.answer(
            f"Текущий текст:\n`{current_content}`\n\n"
            "Отправьте ОДНО новое сообщение. Его текст полностью перезапишет старый:",
            reply_markup=home_kb(),
            parse_mode="Markdown"
        )
        
    elif choice == "Удалить":
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
            remove_tag(file_path)
            await message.answer(f"Заметка `{Path(file_path).name}` полностью удалена из NeuroNotes и базы.", reply_markup=notes_menu_kb(), parse_mode="Markdown")
        except Exception as e:
            await message.answer(f"Не удалось удалить файл: {e}", reply_markup=notes_menu_kb())
        await state.set_state(NoteStates.waiting_for_menu_choice)
        
    elif choice == "Скачать":
        await message.answer_document(FSInputFile(file_path), reply_markup=note_actions_kb())
        
    elif choice == "Назад к заметкам":
        await state.set_state(NoteStates.waiting_for_menu_choice)
        await message.answer("Возврат в меню заметок.", reply_markup=notes_menu_kb())
    else:
        await message.answer("Пожалуйста, используйте кнопки меню.")

@dp.message(NoteStates.editing_content)
async def note_edit_save(message: Message, state: FSMContext):
    data = await state.get_data()
    file_path_str = data['selected_note_path']
    
    # ФИКС ТУТ: Добавили encoding='utf-8'
    Path(file_path_str).write_text(message.text.strip(), encoding='utf-8')
    await message.answer(f"Изменения в файл `{Path(file_path_str).name}` внесены!", reply_markup=note_actions_kb(), parse_mode="Markdown")
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

if __name__ == "__main__":
    asyncio.run(main())
