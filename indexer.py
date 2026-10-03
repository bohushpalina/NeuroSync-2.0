"""
indexer.py — индекс файлов и теги NeuroSync (SQLite).

Публичный API (совместим с прежним): cleanup_db, scan, add_tag, get_by_tag,
remove_tag, add_tags_bulk, remove_tags_bulk, remove_tags_by_prefix,
get_by_name, get_recent_files, move_file_with_tags.
Новое: get_smart_folders, find_smart_folder.
"""
import os
import re
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from db import get_conn, init_db

# Папки, которые не имеет смысла индексировать (кэши, окружения, мусор).
# Хочешь индексировать что-то из этого — просто убери имя из списка.
IGNORED_DIR_NAMES = {
    ".git", ".cache", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".npm", ".cargo", ".rustup", ".gradle",
}
IGNORED_PATH_SUFFIXES = (os.sep.join((".local", "share", "Trash")),)

SCAN_BATCH_SIZE = 1000  # как часто коммитить при сканировании

_REMOVABLE_ROOTS = {"/mnt": 3, "/media": 4, "/run": 5}  # сколько частей пути = точка монтирования


# ---------------------------------------------------------------------------
# ВНУТРЕННЕЕ
# ---------------------------------------------------------------------------

def _cf(value):
    """casefold для SQL: регистронезависимый поиск работает и для кириллицы
    (штатный LIKE в SQLite регистр кириллицы не различает)."""
    return value.casefold() if isinstance(value, str) else value


@contextmanager
def _db():
    conn = get_conn()
    conn.create_function("cf", 1, _cf, deterministic=True)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_schema_lock = threading.Lock()
_schema_ready = False


def _ensure_schema():
    """Один раз за процесс: убирает дубли тегов и ставит индексы.
    Раньше INSERT OR REPLACE в tags не заменял ничего (нет ключа) и копил дубли."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        try:
            init_db()
            with _db() as conn:
                conn.execute(
                    "DELETE FROM tags WHERE rowid NOT IN "
                    "(SELECT MIN(rowid) FROM tags GROUP BY path, tag)"
                )
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_path_tag ON tags(path, tag)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime)")
            _schema_ready = True
        except Exception as e:
            print(f"[indexer] не удалось обновить схему БД (работаем как есть): {e}")


def _file_row(path: str):
    """(path, name, ext, size, mtime) или None, если это не доступный файл."""
    try:
        if not os.path.isfile(path):
            return None
        st = os.stat(path)
    except OSError:
        return None
    p = Path(path)
    return (str(p), p.name, p.suffix, st.st_size, st.st_mtime)


def _register_files(conn, paths):
    """Гарантирует, что файлы есть в таблице files (чтобы теги находились сразу,
    а не только после следующего scan)."""
    rows = [r for r in (_file_row(p) for p in paths) if r]
    if rows:
        conn.executemany("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?)", rows)


def _is_offline(path: str) -> bool:
    """True, если путь лежит на отключённом диске (/mnt/x, /media/u/x, /run/media/u/x):
    такие записи нельзя чистить, иначе отключённый диск обнулит индекс и теги."""
    p = Path(path)
    parts = p.parts
    if len(parts) < 3 or parts[0] != os.sep:
        return False
    need = _REMOVABLE_ROOTS.get(os.sep + parts[1])
    if not need:
        return False
    if os.sep + parts[1] == "/run" and (len(parts) < 3 or parts[2] != "media"):
        return False
    if len(parts) < need:
        return False
    return not os.path.ismount(Path(*parts[:need]))


def _is_gone(path: str) -> bool:
    return not os.path.exists(path) and not _is_offline(path)


# ---------------------------------------------------------------------------
# ОБСЛУЖИВАНИЕ И СКАНИРОВАНИЕ
# ---------------------------------------------------------------------------

def cleanup_db():
    """Удаляет записи (файлы И теги) о путях, которых больше нет на диске.
    Записи на отключённых дисках не трогаются."""
    _ensure_schema()
    with _db() as conn:
        file_paths = [r[0] for r in conn.execute("SELECT path FROM files")]
        tag_paths = [r[0] for r in conn.execute("SELECT DISTINCT path FROM tags")]

        dead_files = [p for p in file_paths if _is_gone(p)]
        dead_tags = [p for p in tag_paths if _is_gone(p)]

        conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in dead_files])
        conn.executemany("DELETE FROM tags WHERE path = ?", [(p,) for p in dead_tags])
    return len(dead_files)


def _skip_dir(full_path: str, name: str) -> bool:
    return name in IGNORED_DIR_NAMES or full_path.endswith(IGNORED_PATH_SUFFIXES)


def scan(root: str):
    """
    Индексирует файлы под root. Пишет в БД только новое и изменившееся и
    коммитит пачками — раньше одна гигантская транзакция держала блокировку
    на всё время скана, и бот при этом падал с 'database is locked'.
    Возвращает число добавленных/обновлённых записей.
    """
    cleanup_db()
    changed = 0
    with _db() as conn:
        # Убираем из индекса то, что теперь игнорируется (venv, .git, кэши…)
        conditions = " OR ".join(["instr(path, ?) > 0"] * (len(IGNORED_DIR_NAMES)))
        conn.execute(
            f"DELETE FROM files WHERE {conditions}",
            [os.sep + n + os.sep for n in sorted(IGNORED_DIR_NAMES)],
        )
        conn.commit()

        known = {
            path: (size, mtime)
            for path, size, mtime in conn.execute("SELECT path, size, mtime FROM files")
        }

        batch = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if not _skip_dir(os.path.join(dirpath, d), d)
            ]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if known.get(full) == (st.st_size, st.st_mtime):
                    continue  # не менялся — не пишем
                p = Path(full)
                batch.append((full, p.name, p.suffix, st.st_size, st.st_mtime))
                if len(batch) >= SCAN_BATCH_SIZE:
                    conn.executemany("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)", batch)
                    conn.commit()
                    changed += len(batch)
                    batch = []
        if batch:
            conn.executemany("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)", batch)
            changed += len(batch)
    return changed


# ---------------------------------------------------------------------------
# ТЕГИ
# ---------------------------------------------------------------------------

def add_tag(file_path: str, tag: str):
    _ensure_schema()
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO tags VALUES (?, ?)", (file_path, tag))
        _register_files(conn, [file_path])


def add_tags_bulk(pairs):
    pairs = list(pairs)
    _ensure_schema()
    with _db() as conn:
        conn.executemany("INSERT OR REPLACE INTO tags VALUES (?, ?)", pairs)
        _register_files(conn, {p for p, _ in pairs})


def remove_tag(file_path: str):
    with _db() as conn:
        conn.execute("DELETE FROM tags WHERE path = ?", (file_path,))


def remove_tags_bulk(paths):
    with _db() as conn:
        conn.executemany("DELETE FROM tags WHERE path = ?", [(p,) for p in paths])


def remove_tags_by_prefix(path_prefix):
    """Удаляет теги у пути и всего, что внутри него. Сравнение точное
    (раньше LIKE принимал '_' и '%' в имени папки за подстановочные знаки
    и мог стереть теги у чужих папок)."""
    prefix = str(path_prefix).rstrip(os.sep)
    inner = prefix + os.sep
    with _db() as conn:
        conn.execute(
            "DELETE FROM tags WHERE path = ? OR substr(path, 1, ?) = ?",
            (prefix, len(inner), inner),
        )


def get_by_tag(tag_query: str):
    """Файлы, у которых тег СОДЕРЖИТ запрос (без учёта регистра, в т.ч. кириллицы).
    Возвращает строки таблицы files, новые файлы первыми."""
    _ensure_schema()
    q = str(tag_query or "").strip().casefold()
    if not q:
        return []
    with _db() as conn:
        return conn.execute(
            """
            SELECT DISTINCT f.* FROM files f
            JOIN tags t ON f.path = t.path
            WHERE instr(cf(t.tag), ?) > 0
            ORDER BY f.mtime DESC
            """,
            (q,),
        ).fetchall()


def get_by_name(name_query):
    """Поиск файлов по имени или пути (без учёта регистра, в т.ч. кириллицы)."""
    q = str(name_query or "").strip().casefold()
    if not q:
        return []
    with _db() as conn:
        return conn.execute(
            """
            SELECT path FROM files
            WHERE instr(cf(name), ?) > 0 OR instr(cf(path), ?) > 0
            ORDER BY mtime DESC
            """,
            (q, q),
        ).fetchall()


# Что считаем «файлами пользователя» для раздела «Свежее».
# Файл показывается, если у него есть тег ИЛИ (расширение из списка ниже
# И путь без скрытых папок вроде .config/.cache/.mozilla).
# Не хватает формата — просто добавь его в список.
USER_FILE_EXTS = {
    # документы и заметки
    ".md", ".txt", ".pdf", ".doc", ".docx", ".odt", ".rtf", ".epub",
    ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp",
    # картинки, аудио, видео
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
    ".mp3", ".flac", ".wav", ".ogg", ".mp4", ".mkv", ".webm", ".mov",
    # код и проекты
    ".py", ".java", ".cpp", ".c", ".h", ".hpp", ".cs", ".js", ".ts",
    ".html", ".css", ".sql", ".json", ".yaml", ".yml", ".sh", ".tex",
    # архивы
    ".zip", ".rar", ".7z", ".tar", ".gz",
}


def _is_user_path(path: str) -> bool:
    """False, если внутри пути есть скрытая папка/файл (.config, .cache, .local…)."""
    try:
        parts = Path(path).relative_to(Path.home()).parts
    except ValueError:
        parts = Path(path).parts[1:]
    return not any(part.startswith(".") for part in parts)


def get_recent_files(limit=10):
    """Последние изменённые файлы пользователя: с тегом, либо «обычного» формата
    вне скрытых/системных папок. Служебный мусор (конфиги, логи, базы) отсекается."""
    try:
        with _db() as conn:
            tagged = {r[0] for r in conn.execute("SELECT DISTINCT path FROM tags")}
            result = []
            for (path,) in conn.execute("SELECT path FROM files ORDER BY mtime DESC"):
                if path in tagged or (
                    Path(path).suffix.lower() in USER_FILE_EXTS and _is_user_path(path)
                ):
                    result.append((path,))
                    if len(result) >= limit:
                        break
            return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# ПЕРЕМЕЩЕНИЕ
# ---------------------------------------------------------------------------

def _unique_target(target_folder: Path, name: str):
    """Путь в папке; если имя занято — 'имя (1).ext', 'имя (2).ext'…"""
    candidate = target_folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(1, 1000):
        candidate = target_folder / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return None


def _rename_prefix(conn, table: str, old: str, new: str):
    """Переписывает пути: сам old и всё внутри old/… → new/… (без LIKE)."""
    inner = old.rstrip(os.sep) + os.sep
    conn.execute(f"UPDATE {table} SET path = ? WHERE path = ?", (new, old))
    conn.execute(
        f"UPDATE {table} SET path = ? || substr(path, ?) WHERE substr(path, 1, ?) = ?",
        (new.rstrip(os.sep) + os.sep, len(inner) + 1, len(inner), inner),
    )


def move_file_with_tags(old_path_str: str, target_folder_str: str):
    """
    Физически перемещает файл (или папку) и переносит его теги и запись в индексе.
    Если в целевой папке имя занято, файл получает суффикс ' (1)' и не затирает чужой.
    Возвращает новый путь или None при ошибке.
    """
    _ensure_schema()
    old_path = Path(old_path_str)
    target_folder = Path(target_folder_str)

    if not old_path.exists() or not target_folder.is_dir():
        return None
    if old_path.parent.resolve() == target_folder.resolve():
        return str(old_path)  # уже на месте

    new_path = _unique_target(target_folder, old_path.name)
    if new_path is None:
        return None

    try:
        shutil.move(str(old_path), str(new_path))
    except (OSError, shutil.Error) as e:
        print(f"[indexer] не удалось переместить {old_path} -> {new_path}: {e}")
        return None

    old_s, new_s = str(old_path), str(new_path)
    with _db() as conn:
        _rename_prefix(conn, "tags", old_s, new_s)
        _rename_prefix(conn, "files", old_s, new_s)
        # name нужно обновить отдельно (мог измениться из-за суффикса)
        conn.execute("UPDATE files SET name = ? WHERE path = ?", (new_path.name, new_s))
        if new_path.is_file():
            _register_files(conn, [new_s])  # если файла не было в индексе — добавим
    return new_s


# ---------------------------------------------------------------------------
# УМНЫЕ ПАПКИ (общая логика для main.py и bot.py)
# ---------------------------------------------------------------------------

def _norm_tag(tag) -> str:
    """Регистр, пробелы, '-' и '_' не должны мешать совпадению тегов."""
    return re.sub(r"[\s_\-]+", " ", str(tag or "").casefold()).strip()


def get_smart_folders():
    """{путь_папки: [все её теги]} — берутся ВСЕ теги папки, а не только последний."""
    result = {}
    try:
        with _db() as conn:
            rows = conn.execute("SELECT path, tag FROM tags").fetchall()
    except Exception as e:
        print(f"[smart_folders] не удалось прочитать теги: {e}")
        return result

    is_dir_cache = {}
    for path, tag in rows:
        if path not in is_dir_cache:
            is_dir_cache[path] = os.path.isdir(path)
        if is_dir_cache[path]:
            result.setdefault(str(Path(path)), []).append(tag)
    return result


def find_smart_folder(tag):
    """
    Ищет умную папку под тег файла. Приоритет:
      1) тег папки == тег файла
      2) тег папки входит в тег файла ("web" в "web-lab10")
      3) тег файла входит в тег папки (от 3 символов)
    При равенстве приоритета побеждает более длинный (точный) тег.
    """
    q = _norm_tag(tag)
    if not q:
        return None
    best, best_score = None, 0
    for path, tags in get_smart_folders().items():
        for t in tags:
            n = _norm_tag(t)
            if not n:
                continue
            if n == q:
                score = 3000 + len(n)
            elif n in q:
                score = 2000 + len(n)
            elif len(q) >= 3 and q in n:
                score = 1000 + len(q)
            else:
                continue
            if score > best_score:
                best, best_score = path, score
    return best
