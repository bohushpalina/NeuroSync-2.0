# ai_sorter.py
# ИИ-сортировщик файлов NeuroOS: раскладывает файлы в целевой папке
# по "умным папкам" пачками через Gemini, тегируя каждый файл.
# Вся конфигурация (ключ API, модель, игнор-лист, размер пачки) — в ns_config.py.

import json
import requests
from pathlib import Path
from datetime import datetime

# Импортируем системные функции проекта
from indexer import add_tag, move_file_with_tags
from db import get_conn

import ns_config

# ============================================================
# КОНФИГ
# Всё вынесено в ns_config.py — здесь только ссылки на него,
# чтобы не плодить рассинхрон между ai_sorter.py и llm.py.
# ============================================================

DEFAULT_IGNORE = ns_config.DEFAULT_IGNORE


def ai_sort_folder(target_folder_path: str, ignore_files: tuple = DEFAULT_IGNORE):
    """
    Сортирует файлы в папке с помощью ИИ порциями по ns_config.SORT_CHUNK_SIZE файлов.
    :param target_folder_path: Путь к целевой директории
    :param ignore_files: Кортеж с именами файлов, которые нельзя трогать
    """
    target_dir = Path(target_folder_path).expanduser().resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        return f"Ошибка: Папка {target_folder_path} не найдена."

    # Приводим все игнорируемые файлы к нижнему регистру для надежной проверки
    ignore_set = {name.lower().strip() for name in ignore_files}

    # 1. ЗАЩИТА: Собираем файлы, принудительно отсекая скрытые и файлы из кортежа игнорирования
    files = [
        f.name for f in target_dir.iterdir()
        if f.is_file()
        and not f.name.startswith('.')
        and f.name.lower().strip() not in ignore_set
    ]

    if not files:
        return "Папка пуста (или в ней остались только защищенные файлы), разгребать нечего."

    # 2. Сканируем БД на наличие уже существующих умных папок с тегами
    existing_smart_folders = {}
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT path, tag FROM tags")
        for path_str, tag_str in c.fetchall():
            p = Path(path_str)
            if p.is_dir() and target_dir in p.parents:
                existing_smart_folders[tag_str.lower().strip()] = p
        conn.close()
    except Exception as e:
        print(f"[WARN] Не удалось подгрузить существующие теги папок из БД: {e}")

    # === НАСТРОЙКА БАТЧИНГА (ПАКЕТНОЙ ОБРАБОТКИ) ===
    chunk_size = ns_config.SORT_CHUNK_SIZE  # Оптимальный размер пачки, чтобы не превысить Max Output Tokens
    file_chunks = [files[i:i + chunk_size] for i in range(0, len(files), chunk_size)]

    moved_count = 0
    report_lines = []

    print(f">> Всего найдено файлов для сортировки: {len(files)}. Разбито на пачки: {len(file_chunks)}")

    # === ФОРМИРУЕМ СПИСОК СУЩЕСТВУЮЩИХ ПАПОК ДЛЯ ПРОМПТА ===
    existing_folders_list = []
    for tag, path in existing_smart_folders.items():
        existing_folders_list.append(f"- Папка: {path.name} (базовый тег: {tag})")

    existing_folders_context = "\n".join(existing_folders_list) if existing_folders_list else "Существующих папок пока нет, нужно создать новые."

    # API URL — собирается через ns_config.gemini_url(), ключ/модель/версия не дублируются
    url = ns_config.gemini_url()
    headers = {"Content-Type": "application/json"}

    # Цикл обработки пачек файлов
    for chunk_index, chunk in enumerate(file_chunks, start=1):
        print(f"\n>> Обработка пачки {chunk_index}/{len(file_chunks)} ({len(chunk)} файлов)...")

        # 3. Динамически формируем промпт для текущей пачки
        prompt = f"""
        Ты — умный системный интегратор NeuroOS. Твоя задача — рассортировать предоставленные файлы по логичным категориям (умным папкам) внутри текущей директории {target_dir.resolve()}.

        СТРОГИЕ ПРАВИЛА СОРТИРОВКИ И ТЕГИРОВАНИЯ:
        0. АГРЕССИВНОЕ ПЕРЕИСПОЛЬЗОВАНИЕ (КРИТИЧЕСКИ ВАЖНО): Ты ОБЯЗАН использовать папки из списка выше, если файл хоть как-то подходит по смыслу. НЕ ПЛОДИ синонимы! Если есть папка 'Academic_Labs', пихай все лабы, рефераты и учебу туда, не создавай 'Education' или 'University_Study'.
        1. НАЗВАНИЯ ПАПОК: Придумай короткие лаконичные названия для папок на английском языке в стиле разметки системных директорий (например: Web_Dev, Analytics, Machine_Learning, Docs, System_Scripts).
        2. БАЗОВЫЙ УМНЫЙ ТЕГ: Каждая папка неявно имеет свой базовый короткий тег (lowercase, без спецсимволов). Например, для папки "Web_Dev" базовый тег — "web", для "Machine_Learning" — "ml".
        3. ПРАВИЛО НАСЛЕДОВАНИЯ ТЕГОВ (КРИТИЧЕСКИ ВАЖНО): Каждый файл внутри папки должен получить 2-3 тега.
        - Как минимум ОДИН из тегов файла ОБЯЗАН включать в себя базовый тег папки как подстроку (например, если базовая папка "web", то тег файла должен быть "web-project", "react-web" или "web-lab10"). Это необходимо для работы алгоритма поиска подстрок `t.lower() in tag.lower()`.
        - Остальные теги файла могут быть более специфичными (технология, тип файла, контекст), на английском или русском языке.

        Список файлов для анализа и сортировки:
        {', '.join(chunk)}

        Пример правильного связывания:
        Если файл "index.js" ты кладешь в папку "Web_Dev" (базовый тег "web"), то его теги могут быть: ["web-frontend", "javascript", "lab10"].

        Верни ответ СТРОГО в формате чистого JSON (без разметки маркдауна ```json и без лишнего текста) in виде списка словарей:
        [
        {{
            "file": "имя_файла.ext",
            "new_folder": "Имя_Папки_На_Английском",
            "folder_tag": "базовый_тег_папки",
            "tags": ["тег-с-включением-базового", "доп_тег1", "доп_тег2"]
        }}
        ]
        """

        payload = {
            "contents": [{"parts": [{"text": prompt}]}]
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)

            if response.status_code != 200:
                print(f"[ERROR] Ошибка API Gemini на пачке {chunk_index} [{response.status_code}]: {response.text}")
                continue

            res_json = response.json()
            if 'candidates' in res_json and res_json['candidates']:
                text_response = res_json['candidates'][0]['content']['parts'][0]['text']
            else:
                print(f"[WARN] Пустой ответ от API для пачки {chunk_index}")
                continue

            clean_json = text_response.replace("```json", "").replace("```", "").strip()
            instructions = json.loads(clean_json)

            # Инвентаризация файлов на диске (обновляем каждый раз, так как файлы улетают)
            current_disk_files = {f.name.lower().strip(): f for f in target_dir.iterdir() if f.is_file()}

            for item in instructions:
                file_name = item.get("file", "").strip().strip("'\"")
                folder_name = item.get("new_folder", "Unsorted").strip().strip("'\"")
                folder_tag = item.get("folder_tag", folder_name.lower()).strip().strip("'\"")
                tags = item.get("tags", [])

                if not file_name or file_name.lower().strip() in ignore_set:
                    continue

                file_key = file_name.lower().strip()
                real_file_path = None

                if (target_dir / file_name).exists():
                    real_file_path = target_dir / file_name
                elif file_key in current_disk_files:
                    real_file_path = current_disk_files[file_key]

                if real_file_path and real_file_path.exists():
                    folder_tag_key = folder_tag.lower().strip()

                    if folder_tag_key in existing_smart_folders and existing_smart_folders[folder_tag_key].exists():
                        new_folder_path = existing_smart_folders[folder_tag_key]
                        folder_name = new_folder_path.name
                    else:
                        new_folder_path = target_dir / folder_name
                        new_folder_path.mkdir(exist_ok=True)
                        add_tag(str(new_folder_path), folder_tag)
                        existing_smart_folders[folder_tag_key] = new_folder_path

                    print(f"[PROCESS] Перемещаю: {real_file_path.name} -> {folder_name}/")

                    actual_new_path = move_file_with_tags(str(real_file_path), str(new_folder_path))
                    if not actual_new_path:
                        actual_new_path = str(new_folder_path / real_file_path.name)

                    if tags:
                        clean_tags = [t.strip() for t in tags if t.strip()]
                        if clean_tags:
                            single_combined_tag = "-".join(clean_tags)
                            add_tag(str(actual_new_path), single_combined_tag)
                            tag_report = single_combined_tag
                        else:
                            tag_report = "без_тегов"
                    else:
                        tag_report = "без_тегов"

                    report_lines.append(f"  ➔ {real_file_path.name} улетел в {folder_name}/ (Тег: {tag_report})")
                    moved_count += 1
                else:
                    print(f"[WARN] Файл не найден на диске в текущей итерации: '{file_name}'")

        except Exception as batch_error:
            print(f"[ERROR] Критический сбой при обработке пачки {chunk_index}: {batch_error}")
            continue

    # === ЗАВЕРШЕНИЕ СЕССИИ СОРТИРОВКИ ===
    log_output = "\n".join(report_lines)

    if moved_count > 0:
        log_file_path = target_dir / ns_config.NEURO_LOG_NAME
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            with open(log_file_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"\n=========================================\n")
                log_file.write(f"СЕССИЯ СОРТИРОВКИ ИИ: {current_time}\n")
                log_file.write(f"Успешно обработано файлов: {moved_count}\n")
                log_file.write(f"-----------------------------------------\n")
                log_file.write(log_output + "\n")
            print(f"[SUCCESS] Итоговый отчет успешно записан в {log_file_path}")
        except Exception as e:
            print(f"[ERROR] Не удалось записать лог в файл {ns_config.NEURO_LOG_NAME}: {e}")

    return (
        f"Успех! Робот разгреб файлы. Всего перемещено: {moved_count}.\n"
        f"История изменений записана в файл {ns_config.NEURO_LOG_NAME} внутри целевой папки.\n\n"
        f"Детальный отчет:\n{log_output if log_output else 'Ни один файл не был перемещен.'}"
    )


if __name__ == "__main__":
    test_path = "~/Право"
    print(ai_sort_folder(test_path))
