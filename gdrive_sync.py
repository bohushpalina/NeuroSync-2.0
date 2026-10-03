import subprocess
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Точный путь к папке с заметками
NOTES_DIR = Path.home() / "NeuroNotes"

# Оптимизированные флаги для максимального ускорения rclone
_RCLONE_COMMON_FLAGS = [
    "--update",
    "--fast-list",               # Скачивает список файлов за 1 запрос к API
    "--transfers", "16",          # Параллельные потоки передачи файлов
    "--checkers", "16",           # Параллельная проверка файлов
    "--drive-use-trash=false",    # Быстрое удаление файлов в обход корзины
    "-v",
    "--exclude", "*.pyc",
    "--exclude", "__pycache__/**",
    "--exclude", ".git/**",
    "--contimeout", "5s",
    "--timeout", "15s",
    "--retries", "1",
]


def run_rclone_sync() -> bool:
    """
    ОТПРАВКА (push): заливает заметки из ~/NeuroNotes/ на Google Диск.
    """
    if not NOTES_DIR.exists():
        print(f"[SYNC UP ERROR] Папка заметок {NOTES_DIR} не найдена!")
        return False

    cmd = [
        "rclone",
        "copy",
        str(NOTES_DIR),
        "gdrive:NeuroSync/Notes",
        *_RCLONE_COMMON_FLAGS,
    ]

    print(f"[SYNC UP] Синхронизирую заметки из {NOTES_DIR} на Google Диск...")
    try:
        process = subprocess.Popen(cmd)
        process.wait()

        if process.returncode == 0:
            print("[SYNC UP SUCCESS] Заметки успешно улетели на Google Диск!")
            return True

        print(f"[SYNC UP ERROR] Код ошибки: {process.returncode}")
        return False
    except Exception as e:
        print(f"[SYNC UP ERROR] Ошибка: {e}")
        return False


def extract_text_from_docx(docx_path: Path) -> str:
    """Извлекает текст из .docx файла."""
    try:
        with zipfile.ZipFile(docx_path) as docx:
            xml_content = docx.read('word/document.xml')
            tree = ET.fromstring(xml_content)
            namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            
            paragraphs = []
            for paragraph in tree.findall('.//w:p', namespaces):
                texts = [node.text for node in paragraph.findall('.//w:t', namespaces) if node.text]
                if texts:
                    paragraphs.append(''.join(texts))
            
            return '\n'.join(paragraphs)
    except Exception as e:
        print(f"[DOCX ERROR] Не удалось прочитать {docx_path.name}: {e}")
        return ""


def _delete_remote_file(remote_filename: str):
    """Удаляет файл с Google Диска, чтобы не плодить дубликаты (.docx и .md)."""
    cmd = [
        "rclone",
        "deletefile",
        f"gdrive:NeuroSync/Notes/{remote_filename}",
        *_RCLONE_COMMON_FLAGS,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[CLEANUP] Удален старый файл с Google Диска: {remote_filename}")
    except Exception as e:
        print(f"[CLEANUP ERROR] Не удалось удалить {remote_filename} с Диска: {e}")


def _convert_files_to_md():
    """
    Конвертирует .txt и .docx в .md, удаляет локальный исходник 
    и стирает оригинал (.docx / .txt) с Google Диска.
    """
    if not NOTES_DIR.exists():
        return

    files_to_convert = list(NOTES_DIR.glob("*.txt")) + list(NOTES_DIR.glob("*.docx"))

    for file_path in files_to_convert:
        original_remote_name = file_path.name
        try:
            # 1. Извлечение содержимого
            if file_path.suffix.lower() == '.docx':
                content = extract_text_from_docx(file_path)
            else:
                content = file_path.read_text(encoding="utf-8")
            
            # 2. Очистка расширений
            clean_name = file_path.name
            for ext in ['.docx', '.txt']:
                if clean_name.lower().endswith(ext):
                    clean_name = clean_name[:-len(ext)]
            
            if clean_name.lower().endswith('.md'):
                clean_name = clean_name[:-3]
            
            # 3. Создание Markdown-версии
            md_path = NOTES_DIR / f"{clean_name}.md"
            md_content = f"# {clean_name}\n\n{content}"
            md_path.write_text(md_content, encoding="utf-8")
            
            # 4. Удаление локального файла
            file_path.unlink()
            print(f"[CONVERT] Преобразовано: {original_remote_name} -> {md_path.name}")
            
            # 5. Удаление старого файла с Google Диска
            _delete_remote_file(original_remote_name)

        except Exception as e:
            print(f"[CONVERT ERROR] Ошибка при обработке файла {original_remote_name}: {e}")


def run_rclone_sync_down() -> bool:
    """
    ЗАГРУЗКА (pull): подтягивает изменения с Google Диска на этот компьютер.
    """
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        "rclone",
        "copy",
        "gdrive:NeuroSync/Notes",
        str(NOTES_DIR),
        *_RCLONE_COMMON_FLAGS,
    ]

    print(f"[SYNC DOWN] Подтягиваю заметки с Google Диска в {NOTES_DIR}...")
    try:
        process = subprocess.Popen(cmd)
        process.wait()

        if process.returncode == 0:
            print("[SYNC DOWN SUCCESS] Заметки с Google Диска подтянуты!")
            _convert_files_to_md()
            return True

        print(f"[SYNC DOWN ERROR] Код ошибки: {process.returncode}")
        return False
    except Exception as e:
        print(f"[SYNC DOWN ERROR] Ошибка: {e}")
        return False


def sync_up_in_background(on_done=None):
    """Фоновая отправка локальных заметок на Google Диск (push)."""
    def _run():
        ok = run_rclone_sync()
        if on_done:
            on_done(ok)

    threading.Thread(target=_run, daemon=True).start()


def sync_down_in_background(on_done=None):
    """Фоновая загрузка заметок с Google Диска на комп (pull)."""
    def _run():
        ok = run_rclone_sync_down()
        if on_done:
            on_done(ok)

    threading.Thread(target=_run, daemon=True).start()


def sync_in_background(on_done=None):
    """Алиас для sync_up_in_background."""
    sync_up_in_background(on_done)


if __name__ == "__main__":
    run_rclone_sync()
