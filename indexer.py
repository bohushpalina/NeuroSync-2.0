import os
import sqlite3
import shutil
from pathlib import Path
from db import get_conn

def cleanup_db():
    """Удаляет из базы данных записи о файлах, которых больше нет на диске."""
    conn = get_conn()
    c = conn.cursor()
    
    c.execute("SELECT path FROM files")
    all_files = c.fetchall()
    
    to_delete = [p[0] for p in all_files if not os.path.exists(p[0])]
    
    if to_delete:
        c.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in to_delete])
        c.executemany("DELETE FROM tags WHERE path = ?", [(p,) for p in to_delete])
    
    conn.commit()
    conn.close()
    return len(to_delete)

def scan(root: str):
    cleanup_db()
    conn = get_conn()
    c = conn.cursor()

    for path, _, files in os.walk(root):
        for f in files:
            full = Path(path) / f
            try:
                stat = full.stat()
            except:
                continue

            c.execute("""
            INSERT OR REPLACE INTO files
            VALUES (?, ?, ?, ?, ?)
            """, (
                str(full),
                full.name,
                full.suffix,
                stat.st_size,
                stat.st_mtime
            ))

    conn.commit()
    conn.close()

def add_tag(file_path: str, tag: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO tags VALUES (?, ?)", (file_path, tag))
    conn.commit()
    conn.close()

def get_by_tag(tag_query: str):
    conn = get_conn()
    c = conn.cursor()
    query = f"%{tag_query}%"
    c.execute("""
        SELECT f.* FROM files f
        JOIN tags t ON f.path = t.path
        WHERE t.tag LIKE ?
    """, (query,))
    results = c.fetchall()
    conn.close()
    return results

def remove_tag(file_path: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tags WHERE path = ?", (file_path,))
    conn.commit()
    conn.close()

def add_tags_bulk(pairs):
    conn = get_conn()
    c = conn.cursor()
    c.executemany("INSERT OR REPLACE INTO tags VALUES (?, ?)", pairs)
    conn.commit()
    conn.close()

def remove_tags_bulk(paths):
    conn = get_conn()
    c = conn.cursor()
    c.executemany("DELETE FROM tags WHERE path = ?", [(p,) for p in paths])
    conn.commit()
    conn.close()

def remove_tags_by_prefix(path_prefix):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tags WHERE path = ? OR path LIKE ?", 
              (path_prefix, path_prefix + os.sep + "%"))
    conn.commit()
    conn.close() 

def get_by_name(name_query):
    """Поиск файлов по всей системе (в таблице files)"""
    from db import get_conn
    conn = get_conn()
    # Ищем в таблице 'files', которая заполняется при scan()
    # Ищем и по имени файла, и по полному пути
    query = f"%{name_query}%"
    c = conn.execute("""
        SELECT path FROM files 
        WHERE name LIKE ? OR path LIKE ?
    """, (query, query))
    res = c.fetchall()
    conn.close()
    return res

def get_recent_files(limit=10):
    """Получение последних проиндексированных файлов из всей системы"""
    from db import get_conn
    conn = get_conn()
    try:
        # Сортируем по mtime (время изменения файла), чтобы видеть реально новые
        c = conn.execute("SELECT path FROM files ORDER BY mtime DESC LIMIT ?", (limit,))
        res = c.fetchall()
    except:
        res = []
    conn.close()
    return res

def move_file_with_tags(old_path_str: str, target_folder_str: str):
    """Физически перемещает файл и обновляет пути тегов в БД."""
    old_path = Path(old_path_str)
    target_folder = Path(target_folder_str)
    new_path = target_folder / old_path.name
    
    if new_path.exists() or not old_path.exists():
        return None
        
    try:
        shutil.move(old_path_str, str(new_path))
    except:
        return None
        
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE tags SET path = ? WHERE path = ?", (str(new_path), old_path_str))
    c.execute("UPDATE files SET path = ?, name = ? WHERE path = ?", (str(new_path), new_path.name, old_path_str))
    conn.commit()
    conn.close()
    return str(new_path)