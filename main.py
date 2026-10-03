import subprocess
import os
import sys
import time
import sqlite3
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Защита от "database is locked".
# Патчим sqlite3.connect ДО импорта db/indexer, поэтому все соединения во всём
# проекте (db.get_conn, indexer и т.д.) автоматически получают:
#   * долгий timeout — SQLite сам ждёт освобождения блокировки, а не падает;
#   * WAL-режим — читатели не блокируют писателя и наоборот.
# ---------------------------------------------------------------------------
_orig_sqlite_connect = sqlite3.connect


def _patched_connect(*args, **kwargs):
    if len(args) < 2:
        kwargs.setdefault("timeout", 60)
    conn = _orig_sqlite_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error:
        pass
    return conn


sqlite3.connect = _patched_connect

from textual.containers import Horizontal, Grid, Vertical

from textual.app import App, ComposeResult
from textual.widgets import DirectoryTree, Header, Footer, Input, OptionList, Label, Button
from textual.widgets.option_list import Option
from textual.screen import ModalScreen
from rich.text import Text

from indexer import (
    scan, add_tag, remove_tag, get_by_tag, cleanup_db, 
    add_tags_bulk, remove_tags_bulk, remove_tags_by_prefix, move_file_with_tags,
    find_smart_folder
)
from db import init_db
from ns_config import COLORS, BG_RGB

# Фон экрана как hex — Textual CSS хекс-цвета понимает так же, как Qt
BG_HEX = "#{:02x}{:02x}{:02x}".format(*BG_RGB)

# -------------------------
# Модальное окно подтверждения переноса
# -------------------------
class ConfirmMoveModal(ModalScreen[bool]):
    CSS = f"""
    ConfirmMoveModal {{
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }}

    #dialog {{
        background: {COLORS['input_bg']};
        border: round {COLORS['pink_bright']};
        padding: 1;
        width: 60; 
        height: auto;
        max-height: 12; 
        layout: vertical;
    }}

    #message {{
        width: 100%;
        height: auto;
        content-align: center middle;
        text-align: center;
        margin: 1 0;
        color: {COLORS['text']};
    }}

    #btns {{
        layout: horizontal;
        height: 3;
        width: 100%;
        margin-top: 1;
    }}

    Button {{
        width: 1fr;
        margin: 0 1;
        height: 3;
    }}

    #yes {{ background: #1a3d21; color: #a3ffb4; border: none; }}
    #no {{ background: #3d1a1a; color: #ffa3a3; border: none; }}
    
    #yes:hover {{ background: #2e7d32; }}
    #no:hover {{ background: #c62828; }}
    """

    def __init__(self, filename: str, folder_path: str):
        super().__init__()
        self.filename = filename
        p = Path(folder_path)
        self.folder_name = p.name if p.name else str(p)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"Переместить [b]{self.filename}[/b]\n"
                f"в умную папку [b {COLORS['pink_bright']}] {self.folder_name} [/]?", 
                id="message"
            )
            with Horizontal(id="btns"):
                yield Button("Да", variant="success", id="yes")
                yield Button("Нет", variant="error", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

# -------------------------
# Проводник
# -------------------------
class StyledTree(DirectoryTree):
    def render_label(self, node, base_style, style):
        label = node._label.plain
        path = str(node.data.path)
        
        if node.allow_expand:
            icon = "🌸" if node.is_expanded else "📁"
            smart_tag = getattr(self.app, "smart_folders", {}).get(path)
            text = f"{icon} {label}  🧠 [{smart_tag}]" if smart_tag else f"{icon} {label}"
        else:
            suffix = node.data.path.suffix.lower()
            icons = {
                ".py": "🐍", ".js": "🩷", ".ts": "💖",
                ".json": "⚙️", ".md": "📝", ".txt": "📄",
                ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️",
                ".sql": "🗃️", ".sh": "💻",
            }
            icon = icons.get(suffix, "📄")
            tag = getattr(self.app, "file_tags", {}).get(path)
            text = f"{icon} {label}   [{tag}]" if tag else f"{icon} {label}"
            
        return Text(text, style=style)

# -------------------------
# Главное приложение
# -------------------------
class NeuroSync(App):
    CSS = f"""
Screen {{ background: {BG_HEX}; color: {COLORS['text']}; }}
Header, Footer {{ background: {COLORS['input_bg']}; color: {COLORS['pink']}; }}
#main-cont {{ layout: horizontal; padding: 1; }}
StyledTree, OptionList {{
    background: rgba(0, 0, 0, 0.65);
    color: {COLORS['text']};
    width: 1fr;
    border: round {COLORS['pink']};
}}
OptionList {{ margin-left: 1; border: round {COLORS['pink_bright']}; }}
.tree--cursor, .option-list--highlighted, .option-list--highlighted-blur {{
    background: {COLORS['pink']} !important;
    color: {COLORS['black']} !important;
    text-style: bold !important;
}}
Input {{ dock: bottom; margin: 1; background: rgba(0, 0, 0, 0.75); color: {COLORS['text']}; border: round {COLORS['border_dim']}; }}
Input:focus {{ border: round {COLORS['pink']}; }}
"""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("backspace", "go_up", "Up"),
        ("ctrl+o", "open_here", "Open File"),
        ("ctrl+d", "open_dir", "Open Dir"),
        ("t", "tag_file", "Tag"),
        ("m", "smart_tag_folder", "Smart Tag Folder"),
        ("x", "remove_tag", "Untag"),
        ("f", "find_by_tag", "Find Tag"),
        ("2", "toggle_tagged", "View/Hide"),
    ]

    def __init__(self):
        super().__init__()
        self.file_tags = {}
        self.smart_folders = {}
        self.input_mode = "tagging"
        self.tag_input = None
        self.last_path_file = Path.home() / ".neurosync_last_path"

    def _bg(self, fn, *args):
        """
        Запускает fn в обычном daemon-потоке — НЕ через @work(thread=True).

        Раньше все фоновые операции были textual-воркерами: при выходе из
        приложения (клавиша q) Textual дожидается их завершения ещё ДО того,
        как run() вернёт управление — поэтому os._exit() после run() не
        помогал, зависание было внутри самого run(). Обычный daemon-поток
        Textual вообще не отслеживает: run() возвращается сразу, не дожидаясь
        scan(Path.home()) и прочих фоновых задач, а сам процесс потом жёстко
        добивает os._exit(0).
        """
        threading.Thread(target=fn, args=args, daemon=True).start()

    def on_mount(self):
        # init_db() больше не вызывается в UI-потоке напрямую: если БД занята,
        # интерфейс не падает, а ждёт и пробует ещё раз в фоне.
        self._bg(self._startup)

    def _startup(self):
        if not self._init_db_retry():
            return
        self._bg(self._load_tags_async)
        self._bg(self._lazy_scan)

    def _init_db_retry(self, attempts: int = 15) -> bool:
        for i in range(attempts):
            try:
                init_db()
                return True
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    self.call_from_thread(self.notify, f"Ошибка БД: {e}", severity="error")
                    return False
                time.sleep(min(1 + i * 0.5, 5))
        self.call_from_thread(
            self.notify,
            "База данных занята другим процессом NeuroSync. "
            "Закройте его (pkill -f neurosync) и запустите снова.",
            severity="error",
        )
        return False

    def _lazy_scan(self):
        cleanup_db()
        scan(str(Path.home()))
        file_tags, smart_folders = self._load_tags()
        self.call_from_thread(self._apply_tags, file_tags, smart_folders)

    def _load_tags_async(self):
        file_tags, smart_folders = self._load_tags()
        self.call_from_thread(self._apply_tags, file_tags, smart_folders)

    def _load_tags(self):
        """
        Только чтение: запрос к БД + os.path.isdir() на каждую строку.
        НИКОГДА не вызывается напрямую в главном потоке — это то самое
        место, из-за которого ns/main.py зависал на несколько секунд при
        старте (и при любой операции с тегами): при большом количестве
        тегов синхронный цикл из os.path.isdir() выполнялся прямо в
        UI-потоке и блокировал отрисовку. Теперь это чистая функция без
        побочных эффектов на self/UI — её безопасно звать из любого потока.
        """
        file_tags = {}
        smart_folders = {}
        try:
            from db import get_conn
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT path, tag FROM tags")
            rows = c.fetchall()
            conn.close()

            for path, tag in rows:
                p_str = str(Path(path))
                if os.path.isdir(path):
                    smart_folders[p_str] = tag
                else:
                    file_tags[p_str] = tag
        except Exception:
            pass
        return file_tags, smart_folders

    def _apply_tags(self, file_tags, smart_folders):
        """Единственное место, которое трогает self.file_tags/self.smart_folders
        и UI — должно вызываться только в главном потоке (через
        call_from_thread), сама по себе не делает никакого I/O."""
        self.file_tags = file_tags
        self.smart_folders = smart_folders
        self.dir_tree.refresh()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self.dir_tree = StyledTree(str(Path.home()))
        self.output_view = OptionList(id="output")
        self.output_view.display = False 
        with Horizontal(id="main-cont"):
            yield self.dir_tree
            yield self.output_view
        yield Footer()

    def action_tag_file(self):
        node = self.dir_tree.cursor_node
        if not node: return
        self.input_mode = "tagging"
        self._show_input(f"Задать тег: {node.data.path.name}")

    def action_smart_tag_folder(self):
        node = self.dir_tree.cursor_node
        if not node or not node.allow_expand:
            self.notify("Нужна папка!", severity="error")
            return
        self.input_mode = "smart_tagging"
        self._show_input(f"УМНЫЙ ТЕГ: {node.data.path.name}")

    def _save_tag(self, tag):
        node = self.dir_tree.cursor_node
        if not node: return
        path = node.data.path

        if self.input_mode == "smart_tagging":
            add_tag(str(path), tag)
            self.smart_folders[str(path)] = tag
            self.dir_tree.refresh()
        elif self.input_mode == "tagging":
            target_folder = find_smart_folder(tag)
            
            if node.allow_expand:
                self._bg(self._apply_tag_recursive, path, tag, "add")
                if target_folder and str(path.parent) != target_folder:
                    self.push_screen(ConfirmMoveModal(path.name, target_folder), 
                                     lambda res: self._bg(self._move_directory_worker, str(path), target_folder) if res else None)
            else:
                add_tag(str(path), tag)
                self.file_tags[str(path)] = tag
                if target_folder and str(path.parent) != target_folder:
                    self.push_screen(ConfirmMoveModal(path.name, target_folder),
                                     lambda res: self._bg(self._move_file_worker, str(path), target_folder) if res else None)
        self.dir_tree.refresh()

    def _move_file_worker(self, old_path, target_folder):
        new_path = move_file_with_tags(old_path, target_folder)
        if new_path:
            self.file_tags.pop(old_path, None)
            file_tags, smart_folders = self._load_tags()
            self.call_from_thread(self._apply_tags, file_tags, smart_folders)
            self.call_from_thread(self.dir_tree.reload)

    def _move_directory_worker(self, old_path, target_root):
        import shutil
        from db import get_conn
        try:
            old_p, new_p = Path(old_path), Path(target_root) / Path(old_path).name
            if not new_p.exists():
                shutil.move(str(old_p), str(new_p))
                conn = get_conn()
                conn.execute("UPDATE tags SET path = ? || SUBSTR(path, ?) WHERE path LIKE ? || '%'",
                             (str(new_p), len(str(old_p)) + 1, str(old_p)))
                conn.commit()
                conn.close()
                file_tags, smart_folders = self._load_tags()
                self.call_from_thread(self._apply_tags, file_tags, smart_folders)
                self.call_from_thread(self.dir_tree.reload)
        except Exception as e: self.call_from_thread(self.notify, str(e))

    def _apply_tag_recursive(self, path, tag, action):
        pairs = [(str(f), tag) for f in path.rglob("*") if f.is_file()]
        add_tags_bulk(pairs)
        file_tags, smart_folders = self._load_tags()
        self.call_from_thread(self._apply_tags, file_tags, smart_folders)

    def action_remove_tag(self):
        node = self.dir_tree.cursor_node
        if node: self._bg(self._fast_remove_worker, str(node.data.path), node.data.path.is_dir())

    def _fast_remove_worker(self, path_str, is_dir):
        if is_dir: remove_tags_by_prefix(path_str)
        else: remove_tag(path_str)
        file_tags, smart_folders = self._load_tags()
        self.call_from_thread(self._apply_tags, file_tags, smart_folders)

    def action_find_by_tag(self):
        self.input_mode = "search"
        self._show_input("Поиск...")

    def _search_and_show(self, tag_query):
        results = get_by_tag(tag_query)
        self.call_from_thread(self.output_view.clear_options)
        if not results: return

        self.output_view.display = True
        options = []
        seen_ids = set() # ЗАЩИТА ОТ ДУБЛИКАТОВ
        
        for r in results:
            path_id = r[0]
            if path_id not in seen_ids:
                options.append(Option(f"🏷️ {Path(path_id).name}", id=path_id))
                seen_ids.add(path_id)
        
        if options:
            self.call_from_thread(self.output_view.add_options, options)
            self.call_from_thread(self.output_view.focus)

    def action_toggle_tagged(self):
        self.output_view.display = not self.output_view.display
        if self.output_view.display: self._bg(self._update_tag_list_worker)

    def _update_tag_list_worker(self):
        self.call_from_thread(self.output_view.clear_options)
        if not self.file_tags: return

        options = []
        seen_ids = set() # ЗАЩИТА ОТ ДУБЛИКАТОВ
        
        for path, tag in list(self.file_tags.items())[:1000]:
            if path not in seen_ids:
                options.append(Option(f"[{tag}] {Path(path).name}", id=path))
                seen_ids.add(path)

        if options:
            self.call_from_thread(self.output_view.add_options, options)
            self.call_from_thread(self.output_view.focus)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option.id and Path(event.option.id).exists():
            self.dir_tree.path = str(Path(event.option.id).parent)
            self.dir_tree.focus()

    def _show_input(self, placeholder):
        if self.tag_input: self.tag_input.remove()
        self.tag_input = Input(placeholder=placeholder)
        self.mount(self.tag_input)
        self.tag_input.focus()

    def on_input_submitted(self, event: Input.Submitted):
        val = event.value.strip()
        if val: self._save_tag(val) if self.input_mode != "search" else self._bg(self._search_and_show, val)
        self._close_input()

    def _close_input(self):
        if self.tag_input:
            self.tag_input.remove()
            self.tag_input = None
            self.dir_tree.focus()

    def action_go_up(self):
        p = Path(self.dir_tree.path)
        if p.parent != p: self.dir_tree.path = str(p.parent)

    def action_open_here(self):
        node = self.dir_tree.cursor_node
        if node and not node.allow_expand:
            subprocess.Popen(["xdg-open", str(node.data.path)], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    def action_open_dir(self):
        node = self.dir_tree.cursor_node
        if node:
            target = node.data.path if node.data.path.is_dir() else node.data.path.parent
            self.last_path_file.write_text(str(target))
            self.exit()

def _acquire_single_instance_lock():
    """Не даём запустить второй экземпляр — именно параллельные процессы
    (старый ещё сканирует ~/ и держит запись в БД) давали 'database is locked'."""
    import fcntl
    lock_path = Path.home() / ".neurosync.lock"
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid = ""
        try:
            lock_file.seek(0)
            pid = lock_file.read().strip()
        except OSError:
            pass
        state = ""
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/stat") as f:
                    st = f.read().rsplit(")", 1)[1].split()[0]
                state = " (приостановлен через Ctrl+Z — вернуть: fg)" if st == "T" else ""
            except OSError:
                pass
        print("NeuroSync уже запущен (другой процесс держит БД).")
        if pid.isdigit():
            print(f"Это процесс PID {pid}{state}. Завершить:  kill {pid}   (если не помогло: kill -9 {pid})")
        else:
            print("Найти процесс:  pgrep -af main.py")
        sys.exit(1)
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file  # держим ссылку, пока жив процесс


if __name__ == "__main__":
    _lock = _acquire_single_instance_lock()
    NeuroSync().run()
    # После run() интерфейс уже закрыт и терминал восстановлен Textual —
    # но сам процесс python main.py НЕ завершается, пока не доработает
    # фоновый поток _lazy_scan (cleanup_db() + scan(Path.home())). Именно
    # поэтому "ns" висел в терминале ещё ~20 секунд ПОСЛЕ выхода по "q":
    # shell ждёт завершения именно этого процесса. scan() — это фоновая
    # индексация "на будущее", она не критична прямо сейчас (в следующий
    # запуск ns она просто пройдёт заново), поэтому по выходу из TUI
    # процесс завершается принудительно, не дожидаясь её.
    os._exit(0)
