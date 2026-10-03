import gkeepapi
from pathlib import Path

# Локальная папка заметок (отсюда rclone сам всё зальет на Google Диск)
NOTES_DIR = Path.home() / "NeuroNotes"
NOTES_DIR.mkdir(parents=True, exist_ok=True)

# Авторизация в Google Keep
USERNAME = "palina.ubuntu.26@gmail.com"
PASSWORD = "iuav tllr ofikzrtw"  # 16-значный пароль приложения из аккаунта Google

keep = gkeepapi.Keep()

print("[KEEP] Авторизация и синхронизация с сервером...")
try:
    keep.login(USERNAME, PASSWORD)
    print("[KEEP SUCCESS] Успешный вход!")
except Exception as e:
    print(f"[KEEP ERROR] Ошибка входа: {e}")
    exit(1)

# Получаем все активные (не архивные и не в корзине) заметки
notes = keep.find(archived=False, trashed=False)

count = 0
for note in notes:
    # Берем заголовок или делаем дефолтный
    title = note.title.strip() if note.title else f"Note_{note.id[:6]}"
    
    # Очищаем имя файла от запрещенных символов
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip()
    if not safe_title:
        safe_title = f"Note_{note.id[:6]}"
    
    content = note.text or ""
    
    # Если это список с чекбоксами (список покупок, тудушка)
    if hasattr(note, "items") and note.items:
        items_text = []
        for item in note.items:
            status = "[x]" if item.checked else "[ ]"
            items_text.append(f"- {status} {item.text}")
        content = "\n".join(items_text)

    # Формируем Markdown файл с префиксом [Keep]
    md_path = NOTES_DIR / f"[Keep] {safe_title}.md"
    md_content = f"# {title}\n\n{content}"
    
    md_path.write_text(md_content, encoding="utf-8")
    print(f"[SAVED] {md_path.name}")
    count += 1

print(f"\n[DONE] Скачано и сохранены в {NOTES_DIR}: {count} заметок.")
