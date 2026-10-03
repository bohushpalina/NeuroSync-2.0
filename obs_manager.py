import json
import re
from urllib.parse import unquote
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from difflib import SequenceMatcher

import requests

try:
    from pptx import Presentation  # type: ignore
except ImportError:
    Presentation = None

try:
    import docx as _docx_lib  # type: ignore
except ImportError:
    _docx_lib = None

import ns_config
from ns_config import *



# ============================================================
# КОНФИГУРАЦИЯ OBSIDIAN
# ============================================================

OBSIDIAN_VAULT_PATH = Path(OBSIDIAN_VAULT).expanduser().resolve()

MAX_LINK_DEPTH = int(getattr(ns_config, "OBSIDIAN_MAX_LINK_DEPTH", 3))
MAX_GRAPH_NODES = int(getattr(ns_config, "OBSIDIAN_MAX_GRAPH_NODES", 2000))
OBS_LOG_NAME = "neuro_obsidian.log"

# Служебная метка в начале файла: показывает, что тема сгенерирована ИИ
# при дроблении. Такие файлы больше никогда не дробятся повторно, даже
# если на них случайно сослалась другая лекция.
AI_GENERATED_MARKER = "<!-- neuro:generated_topic -->"
AI_TOPIC_ALIASES_PREFIX = "<!-- neuro:topic_aliases:"
PROTECTED_TAG_PATTERN = re.compile(r"(?<!\w)#PROTECTED\b", flags=re.IGNORECASE)

class ProtectedNoteError(PermissionError):
    """Файл помечен #PROTECTED и не должен изменяться splitter'ом."""

# Аналогичная метка, но для файлов-ИСТОЧНИКОВ ("Лекция 2 ТАД.md" и т.п.),
# которые УЖЕ были раздроблены на атомарные темы хотя бы раз. Без неё
# split_note_tree считал такие файлы обычным "информативным" текстом и
# снова прогонял их через Gemini каждый раз, когда до них дотягивался
# граф ссылок ДРУГОГО корня (например "Лекция 2 ТАД (КОНСПЕКТ).md",
# ссылающаяся на те же самые разделы) — отсюда дубли атомарных файлов
# вида "ML_Pipeline-2", даже с работающей семантической дедупликацией.
FULL_CONSPECTUS_MARKER = "<!-- neuro:full_conspectus -->"

DATE_PATTERN = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
WIKILINK_PATTERN = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]+)?\]\]")
MARKDOWN_LINK_PATTERN = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")


# ============================================================
# БАЗОВЫЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_vault_path() -> Path:
    return OBSIDIAN_VAULT_PATH


def _log_change(action: str, details: str) -> None:
    """Пишет историю операций Obsidian, не меняя Markdown-файлы журнала Diary/date."""
    vault = get_vault_path()
    try:
        vault.mkdir(parents=True, exist_ok=True)
        log_path = vault / OBS_LOG_NAME
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] {action}: {details}\n")
    except Exception as exc:
        print(f"[WARN] Не удалось записать Obsidian-лог: {exc}")


def _read_note(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_protected_note(path: Path) -> bool:
    """True, если существующий Markdown-файл содержит явный тег #PROTECTED."""
    try:
        if not path.exists() or not path.is_file() or path.suffix.lower() != ".md":
            return False
        return bool(PROTECTED_TAG_PATTERN.search(_read_note(path)))
    except Exception:
        return False


def _write_note_atomic(path: Path, content: str) -> None:
    """Безопасная запись Markdown с жёсткой защитой #PROTECTED."""
    if _is_protected_note(path):
        raise ProtectedNoteError(f"Файл защищён тегом #PROTECTED: {path.name}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.neuro_tmp")

    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _safe_stem(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|]", "-", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:120] or "Untitled"


def _safe_output_filename(filename: str) -> str:
    """Безопасное имя создаваемого Obsidian-файла с обязательным .md."""
    filename = _safe_stem(filename)
    if not filename.lower().endswith(".md"):
        filename += ".md"
    return filename


def _normalize_link_target(raw_target: str) -> str:
    target = raw_target.strip()

    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()

    if "#" in target:
        target = target.split("#", 1)[0]

    if target.startswith("^"):
        return ""

    return target.strip()


def _is_external_target(target: str) -> bool:
    lower = target.lower()
    return (
        not target
        or lower.startswith(("http://", "https://", "mailto:", "obsidian://"))
        or target.startswith("#")
    )


def is_date_named(path: Path) -> bool:
    return bool(DATE_PATTERN.search(path.name))


def is_diary(path: Path) -> bool:
    return path.name.casefold() == "diary.md"


def is_always_protected(path: Path, diary_linked: Set[Path] = None) -> bool:
    resolved = path.resolve()
    return (
        is_date_named(resolved)
        or is_diary(resolved)
        or (diary_linked is not None and resolved in diary_linked)
        or _is_protected_note(resolved)
    )


# ============================================================
# ДЕДУПЛИКАЦИЯ АТОМАРНЫХ ТЕМ
# ============================================================


def _normalize_text_for_similarity(text: str) -> str:
    """Нормализует Markdown-текст для сравнения без учёта служебной разметки."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s+.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\[\[.*?\]\]", "", text)
    text = re.sub(r"\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def _atomic_paragraphs(text: str) -> List[str]:
    """Возвращает содержательные абзацы, сохраняя границы абзацев для сравнения."""
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"^#{1,6}\s+.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"#\w+", "", cleaned)
    cleaned = re.sub(r"\[\[.*?\]\]", "", cleaned)
    cleaned = re.sub(r"\[.*?\]\(.*?\)", "", cleaned)

    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", cleaned):
        paragraph = re.sub(r"\s+", " ", paragraph).strip().casefold()
        if len(paragraph) >= 60:
            paragraphs.append(paragraph)

    # Для заметок, где Markdown хранит материал одним большим блоком,
    # сравниваем также длинные предложения/фразы.
    if not paragraphs:
        normalized = _normalize_text_for_similarity(text)
        paragraphs = [
            chunk.strip()
            for chunk in re.split(r"(?<=[.!?])\s+", normalized)
            if len(chunk.strip()) >= 60
        ]

    return paragraphs


def _find_planned_atomic_match(
    item: Dict,
    planned: List[Tuple[Dict, Path]],
) -> Path | None:
    """Ищет дубль среди тем, уже запланированных в ЭТОМ же запуске."""
    new_topic = _normalize_topic_key(item.get("topic", ""))
    new_dedup_key = _normalize_topic_key(item.get("_dedup_key", ""))
    new_text = _normalize_text_for_similarity(item.get("content", ""))
    new_fragments = _atomic_paragraphs(item.get("content", ""))

    for existing_item, output_path in planned:
        existing_topic = _normalize_topic_key(existing_item.get("topic", ""))
        existing_dedup_key = _normalize_topic_key(existing_item.get("_dedup_key", ""))

        # Главный критерий: AI-кластеризация признала темы одной сущностью.
        if new_dedup_key and existing_dedup_key and new_dedup_key == existing_dedup_key:
            return output_path

        # Точное имя темы тоже всегда означает одну атомарную заметку.
        if new_topic and new_topic == existing_topic:
            return output_path

        existing_text = _normalize_text_for_similarity(existing_item.get("content", ""))
        if len(new_text) < 30 or len(existing_text) < 30:
            continue

        full_sim = SequenceMatcher(None, new_text, existing_text).ratio()
        if full_sim >= 0.92:
            return output_path

        existing_fragments = _atomic_paragraphs(existing_item.get("content", ""))
        for np in new_fragments:
            for ep in existing_fragments:
                if SequenceMatcher(None, np, ep).ratio() >= 0.96:
                    return output_path

    return None


def _flatten_generated_items(generated_by_source: Dict[Path, List[Dict]]) -> List[Tuple[str, Dict]]:
    """Возвращает все сгенерированные темы одного запуска с устойчивыми ID."""
    result = []
    counter = 0
    for source_path, items in generated_by_source.items():
        for item in items:
            counter += 1
            item["_dedup_id"] = f"t{counter}"
            result.append((f"t{counter}", item))
    return result


def _apply_dedup_groups(
    generated_by_source: Dict[Path, List[Dict]],
    groups: List[List[str]],
) -> int:
    """Присваивает одинаковым по смыслу темам единый внутренний ключ."""
    by_id = {}
    for source_path, items in generated_by_source.items():
        for item in items:
            item_id = item.get("_dedup_id")
            if item_id:
                by_id[item_id] = item

    merged = 0
    for group_index, group in enumerate(groups, start=1):
        valid_ids = [item_id for item_id in group if item_id in by_id]
        if len(valid_ids) < 2:
            continue

        # Один ключ = одна атомарная сущность.
        dedup_key = f"semantic_group_{group_index}"
        aliases = []
        for item_id in valid_ids:
            topic_name = str(by_id[item_id].get("topic", "")).strip()
            if topic_name and topic_name not in aliases:
                aliases.append(topic_name)

        for item_id in valid_ids:
            by_id[item_id]["_dedup_key"] = dedup_key
            by_id[item_id]["_dedup_aliases"] = aliases
        merged += len(valid_ids) - 1

    return merged


def _semantic_deduplicate_generated_topics(
    generated_by_source: Dict[Path, List[Dict]],
) -> int:
    """
    Семантически группирует темы ВСЕХ исходников текущего запуска.

    Это критично для пустого vault: две лекции могут породить одну и ту же
    сущность под разными именами (например, `Mean` и `Среднее арифметическое`).
    В таком случае детерминированного сравнения строк недостаточно.

    Если Gemini недоступен, операция не ломается — остаётся строгая локальная
    дедупликация по имени/тексту.
    """
    flattened = _flatten_generated_items(generated_by_source)
    if len(flattened) < 2:
        return 0

    candidates = []
    for item_id, item in flattened:
        content = _normalize_text_for_similarity(item.get("content", ""))
        candidates.append({
            "id": item_id,
            "topic": item.get("topic", ""),
            "tags": item.get("tags", []),
            "content": content[:1800],
        })

    prompt = f"""
Ты выполняешь дедупликацию атомарных тем учебного Obsidian-хранилища.

Перед тобой темы, которые были независимо извлечены из нескольких исходных файлов.
Нужно найти ТОЛЬКО те темы, которые обозначают ОДНУ И ТУ ЖЕ конкретную сущность,
термин, определение, метод или показатель.

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Разные формулировки одного понятия нужно объединять.
2. Русское и английское название одного понятия нужно объединять, если содержание совпадает.
   Например `Mean`, `Arithmetic mean`, `Среднее арифметическое` = ОДНА тема.
3. Нельзя объединять просто связанные темы. Например `Mean` и `Median` — РАЗНЫЕ темы.
4. Нельзя объединять родительскую тему и её частный случай.
5. Если сомневаешься — НЕ объединяй. Лучше оставить две темы, чем ошибочно слить разные знания.
6. Внутри одной группы должно быть минимум 2 ID.
7. Каждый ID может находиться максимум в одной группе.

Темы:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Верни СТРОГО JSON: 
{{
  "groups": [["t1", "t7"], ["t3", "t12", "t19"]]
}}
Если дублей нет, верни {{"groups": []}}.
"""

    try:
        result = _gemini_json(
            prompt,
            "Ты строгий классификатор дублей. Ты объединяешь только одну и ту же сущность, а не просто похожие или связанные темы."
        )
        groups = result.get("groups", []) if isinstance(result, dict) else []
        if not isinstance(groups, list):
            return 0
        merged = _apply_dedup_groups(generated_by_source, groups)
        if merged:
            _log_change("SEMANTIC_DEDUP", f"merged={merged}; candidates={len(flattened)}")
        return merged
    except Exception as exc:
        _log_change("SEMANTIC_DEDUP_FALLBACK", str(exc))
        return 0


def _iter_existing_atomic_notes(vault: Path, exclude: Set[Path] = None):
    excluded = {p.resolve() for p in (exclude or set())}

    for note in vault.rglob("*.md"):
        note = note.resolve()
        if not note.is_file() or note.name.startswith(".") or note in excluded:
            continue

        try:
            text = _read_note(note)
        except Exception:
            continue

        # Только файлы, которые действительно создавались нашим splitter'ом.
        # Это не даст обычным конспектам внезапно стать "атомарным дублем".
        if not is_ai_generated_topic(text):
            continue

        yield note, text


def _find_existing_atomic_match(
    content: str,
    topic: str,
    vault: Path,
    exclude: Set[Path] = None,
) -> Path | None:
    """
    Ищет уже существующую атомарную тему по ВСЕМУ vault.

    Это намеренно глобальный антидубль: атомарное знание едино во всём
    хранилище. Поэтому при обработке второй лекции темы, уже созданные
    первой лекцией, переиспользуются и не превращаются в «Тема-2».
    Податомарки после нормализации subtopics проходят этот же путь.
    """
    """
    Ищет уже существующую AI-сгенерированную атомарную заметку.

    Приоритеты:
    1. почти полный дубль текста (>=95%);
    2. практически идентичный содержательный абзац (>=99%);
    3. одинаковое имя темы + высокое сходство текста (>=85%).

    Важно: обычные лекции и исходники не считаются атомарными кандидатами.
    """
    norm_new = _normalize_text_for_similarity(content)
    norm_topic = _normalize_topic_key(topic)
    filename_key = _normalize_topic_key(Path(_safe_output_filename(topic)).stem)
    new_fragments = _atomic_paragraphs(content)

    best_match = None
    best_score = 0.0
    best_reason = ""

    for note, note_text in _iter_existing_atomic_notes(vault, exclude):
        # 0. Алиасы, сохранённые при предыдущем семантическом объединении.
        # Например, атомарка `Среднее арифметическое` может помнить `Mean`.
        existing_aliases = _extract_topic_aliases(note_text)
        if norm_topic and norm_topic in existing_aliases:
            _log_change(
                "REUSE_EXISTING_TOPIC",
                f"{topic!r} -> {note.relative_to(vault)} (совпадение алиаса темы)",
            )
            return note

        # 1. Одинаковая атомарная тема — никогда не создаём второй файл.
        existing_heading_match = re.search(r"^#\s+(.+?)\s*$", note_text, flags=re.MULTILINE)
        existing_topic = (
            existing_heading_match.group(1).strip()
            if existing_heading_match
            else note.stem
        )
        same_topic = norm_topic and (
            norm_topic == _normalize_topic_key(existing_topic)
            or filename_key == _normalize_topic_key(note.stem)
        )

        if same_topic:
            _log_change(
                "REUSE_EXISTING_TOPIC",
                f"{topic!r} -> {note.relative_to(vault)} (та же атомарная тема)",
            )
            return note

        norm_existing = _normalize_text_for_similarity(note_text)
        if len(norm_existing) < 30:
            continue

        if len(norm_new) < 30:
            continue

        # 1. Сильный дубль всего содержимого.
        full_sim = SequenceMatcher(None, norm_new, norm_existing).ratio()
        if full_sim >= 0.95 and full_sim > best_score:
            best_match = note
            best_score = full_sim
            best_reason = f"полное сходство {full_sim:.1%}"

        # 2. Ключевой абзац совпадает практически полностью.
        existing_fragments = _atomic_paragraphs(note_text)
        if new_fragments and existing_fragments:
            for new_fragment in new_fragments:
                for existing_fragment in existing_fragments:
                    fragment_sim = SequenceMatcher(
                        None, new_fragment, existing_fragment
                    ).ratio()
                    if fragment_sim >= 0.99 and fragment_sim > best_score:
                        best_match = note
                        best_score = fragment_sim
                        best_reason = f"совпадение абзаца {fragment_sim:.1%}"

        # 3. То же название + заметно похожее содержание.
        if norm_topic == _normalize_topic_key(note.stem) and full_sim >= 0.85:
            if full_sim > best_score:
                best_match = note
                best_score = full_sim
                best_reason = f"та же тема, сходство {full_sim:.1%}"

    if best_match is not None:
        _log_change(
            "REUSE_EXISTING_TOPIC",
            f"{topic!r} -> {best_match.relative_to(vault)} ({best_reason})",
        )

    return best_match


# ============================================================
# ПОИСК OBSIDIAN-ФАЙЛА
# ============================================================

def _tokenize_name(text: str) -> List[str]:
    text = Path(text).stem.casefold()
    return [token for token in re.findall(r"[a-zа-яё0-9]+", text) if token]


def _semantic_name_score(query: str, path: Path) -> int:
    query_tokens = set(_tokenize_name(query))
    name_tokens = set(_tokenize_name(path.name))

    if not query_tokens or not name_tokens:
        return 0

    score = len(query_tokens & name_tokens) * 20

    # Убираем шумовые русские предлоги/союзы, чтобы:
    # "Лекция 1 по СУБД" и "Лекция 1 СУБД" считались одним именем.
    stop_words = {
        "по", "для", "из", "в", "во", "на", "и", "или", "с", "со",
        "к", "ко", "о", "об", "от", "до", "за", "у", "а"
    }
    q_meaningful = query_tokens - stop_words
    n_meaningful = name_tokens - stop_words

    if q_meaningful:
        overlap = len(q_meaningful & n_meaningful)
        score += overlap * 25
        if overlap == len(q_meaningful):
            score += 30

    query_stem = Path(query).stem.casefold().strip()
    name_stem = path.stem.casefold().strip()

    if query_stem == name_stem:
        score += 100
    elif query_stem in name_stem or name_stem in query_stem:
        score += 45

    return score


def resolve_note(note_ref: str, base_dir: Path = None) -> Path:
    """
    Находит заметку:
    1) по точному пути/имени;
    2) по имени в vault;
    3) по смысловому совпадению имени, например:
       "Лекция 1 по СУБД.md" -> "Лекция 1 СУБД.md".
    """
    if not note_ref:
        raise FileNotFoundError("Не указано имя Obsidian-файла.")

    vault = get_vault_path()
    raw = note_ref.strip().strip("`\"'")
    raw_path = Path(raw)

    if raw.startswith("~"):
        candidate = raw_path.expanduser()
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    if raw_path.is_absolute() and raw_path.exists() and raw_path.is_file():
        return raw_path.resolve()

    if base_dir is not None:
        local_candidates = [
            (base_dir / raw).with_suffix(".md") if raw_path.suffix == "" else base_dir / raw,
            base_dir / raw,
        ]
        for candidate in local_candidates:
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()

    exact_candidates = []
    if raw_path.suffix.lower() == ".md":
        exact_candidates.append(vault / raw)
    else:
        exact_candidates.extend([vault / f"{raw}.md", vault / raw])

    for candidate in exact_candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    all_notes = [
        note.resolve()
        for note in vault.rglob("*.md")
        if note.is_file() and not note.name.startswith(".")
    ]

    raw_casefold = raw.casefold()
    exact = [p for p in all_notes if p.name.casefold() == raw_casefold]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise FileNotFoundError(
            f"Найдено несколько Obsidian-файлов с именем '{note_ref}':\n"
            + "\n".join(f"- {p.relative_to(vault)}" for p in exact[:20])
        )

    # Умное совпадение имени.
    # Числа в запросе различают файлы ("Лекция 99" — не "Лекция 3 КС"):
    # если в запросе есть число, а в имени файла его нет, файл не кандидат.
    query_numbers = {t for t in _tokenize_name(raw) if t.isdigit()}

    scored = []
    for note in all_notes:
        if query_numbers and not query_numbers <= set(_tokenize_name(note.name)):
            continue
        score = _semantic_name_score(raw, note)
        if score > 0:
            scored.append((score, note))

    scored.sort(key=lambda item: (-item[0], str(item[1]).casefold()))

    if scored:
        best_score = scored[0][0]
        best = [note for score, note in scored if score == best_score]

        # Требуем достаточно сильное смысловое совпадение.
        if best_score >= 45:
            if len(best) == 1:
                return best[0]

            relative_list = "\n".join(
                f"- {p.relative_to(vault)}" for p in best[:20]
            )
            raise FileNotFoundError(
                f"Запрос '{note_ref}' подходит сразу к нескольким Obsidian-файлам:\n"
                f"{relative_list}\nУкажи чуть точнее, какой файл нужен."
            )

    raise FileNotFoundError(
        f"Obsidian-файл '{note_ref}' не найден в хранилище."
    )


# ============================================================
# ГРАФ WIKILINK / MARKDOWN-LINK
# ============================================================

def extract_links(text: str) -> List[str]:
    links = []

    for match in WIKILINK_PATTERN.finditer(text):
        target = _normalize_link_target(match.group(1))
        if "|" in target:
            target = target.split("|", 1)[0].strip()
        if target and not _is_external_target(target):
            links.append(target)

    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        target = _normalize_link_target(match.group(2))
        if target and not _is_external_target(target):
            links.append(target)

    return list(dict.fromkeys(links))


def _normalize_note_title(text: str) -> str:
    """Нормализует имя заметки для надежного логического сравнения."""
    value = unquote(str(text)).strip().strip("<>").strip()
    value = value.replace("\\", "/")
    if value.lower().endswith(".md"):
        value = value[:-3]
    value = value.casefold()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\wа-яё0-9]+", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _link_name_score(target_name: str, note: Path) -> int:
    """
    Оценивает, насколько имя файла похоже на цель ссылки.
    Нужен для случаев, когда Obsidian-ссылка логически совпадает с файлом,
    но имя отличается пунктуацией, скобками или небольшими служебными словами.
    """
    target_norm = _normalize_note_title(target_name)
    note_norm = _normalize_note_title(note.name)

    if not target_norm or not note_norm:
        return 0

    if target_norm == note_norm:
        return 1000

    target_tokens = set(re.findall(r"[\wа-яё0-9]+", target_norm, flags=re.IGNORECASE))
    note_tokens = set(re.findall(r"[\wа-яё0-9]+", note_norm, flags=re.IGNORECASE))

    stop_words = {
        "по", "для", "из", "в", "во", "на", "и", "или", "с", "со",
        "к", "ко", "о", "об", "от", "до", "за", "у", "а"
    }

    target_meaningful = target_tokens - stop_words
    note_meaningful = note_tokens - stop_words

    if not target_meaningful or not note_meaningful:
        return 0

    overlap = len(target_meaningful & note_meaningful)

    score = overlap * 30

    if target_meaningful.issubset(note_meaningful):
        score += 45

    if note_meaningful.issubset(target_meaningful):
        score += 35

    if target_norm in note_norm or note_norm in target_norm:
        score += 25

    return score


def resolve_link(target: str, source_path: Path) -> Path:
    """
    Разрешает Obsidian-ссылку максимально надежно:
    1) exact relative/absolute path;
    2) exact имя/стем по всему vault;
    3) логическое совпадение имени без влияния пунктуации/скобок;
    4) fallback по пересечению значимых слов.
    """
    clean_target = _normalize_link_target(target)

    # Поддержка [[Файл|Отображаемое имя]]
    if "|" in clean_target:
        clean_target = clean_target.split("|", 1)[0].strip()

    clean_target = unquote(clean_target).strip()

    if not clean_target or _is_external_target(clean_target):
        raise FileNotFoundError("Ссылка внешняя или пустая.")

    vault = get_vault_path()
    target_path = Path(clean_target)

    candidates = []

    if target_path.is_absolute():
        candidates.append(target_path)
    else:
        candidates.append(source_path.parent / target_path)
        candidates.append(vault / target_path)

    normalized = []
    for candidate in candidates:
        candidate = candidate.resolve()

        if candidate.suffix.lower() != ".md":
            normalized.append(candidate.with_suffix(".md"))

        normalized.append(candidate)

    for candidate in normalized:
        if candidate.exists() and candidate.is_file():
            try:
                candidate.resolve().relative_to(vault)
            except ValueError:
                continue
            return candidate.resolve()

    # Точное совпадение имени/стема по всему vault.
    target_name = target_path.name.casefold()
    target_stem = target_path.stem.casefold()

    all_files = [
        p.resolve()
        for p in vault.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    ]

    exact_matches = []
    for note in all_files:
        if note.name.casefold() == target_name or note.stem.casefold() == target_stem:
            exact_matches.append(note)

    # Если есть единственный точный файл — используем его.
    if len(exact_matches) == 1:
        return exact_matches[0]

    if len(exact_matches) > 1:
        # Предпочитаем markdown, если дубликатов несколько.
        md_matches = [p for p in exact_matches if p.suffix.lower() == ".md"]
        if len(md_matches) == 1:
            return md_matches[0]

    # Логическое совпадение имени.
    scored = []
    for note in all_files:
        score = _link_name_score(clean_target, note)
        if score > 0:
            scored.append((score, note))

    scored.sort(key=lambda item: (-item[0], str(item[1]).casefold()))

    if scored:
        best_score = scored[0][0]
        best = [note for score, note in scored if score == best_score]

        # Достаточно сильное совпадение.
        if best_score >= 75:
            if len(best) == 1:
                return best[0]

            md_best = [p for p in best if p.suffix.lower() == ".md"]
            if len(md_best) == 1:
                return md_best[0]

            raise FileNotFoundError(
                f"Ссылка [[{target}]] подходит сразу к нескольким файлам:\n"
                + "\n".join(f"- {p.relative_to(vault)}" for p in best[:20])
                + "\n"
                "Уточни путь или имя."
            )

    raise FileNotFoundError(
        f"Не удалось разрешить ссылку [[{target}]] в vault."
    )


def _read_links(path: Path) -> List[Tuple[str, Path]]:
    try:
        text = _read_note(path)
    except Exception:
        return []

    result = []

    for raw_link in extract_links(text):
        try:
            target = resolve_link(raw_link, path)
            result.append((raw_link, target))
        except FileNotFoundError:
            continue

    return result


# ============================================================
# НЕПРИКОСНОВЕННОСТЬ DIARY И ДАТ
# ============================================================

def collect_diary_linked_files() -> Set[Path]:
    """
    Все файлы, достижимые из Diary.md по ссылкам.
    Циклы безопасны: каждый узел проходит только один раз.
    """
    vault = get_vault_path()
    protected: Set[Path] = set()

    diary_files = [
        p.resolve()
        for p in vault.rglob("*.md")
        if is_diary(p)
    ]

    stack = list(diary_files)
    visited: Set[Path] = set()

    while stack:
        current = stack.pop().resolve()

        if current in visited:
            continue

        visited.add(current)
        protected.add(current)

        for _, target in _read_links(current):
            target = target.resolve()

            if target not in visited:
                stack.append(target)

    return protected


SUPPORTED_ATTACHMENT_SUFFIXES = {".pptx"}


def _extract_pptx_text(path: Path) -> str:
    if Presentation is None:
        raise RuntimeError(
            "Библиотека python-pptx не установлена. Поставь: pip install python-pptx"
        )

    prs = Presentation(str(path))
    lines: List[str] = []

    for slide_index, slide in enumerate(prs.slides, start=1):
        slide_lines: List[str] = []

        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if text:
                        slide_lines.append(text)

            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells)
                    if row_text.strip(" |"):
                        slide_lines.append(row_text)

        if slide_lines:
            lines.append(f"## Слайд {slide_index}")
            lines.extend(slide_lines)

        if slide.has_notes_slide:
            notes_text = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes_text:
                if not slide_lines:
                    lines.append(f"## Слайд {slide_index}")
                lines.append(f"_Заметки докладчика:_ {notes_text}")

        if slide_lines or (slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip()):
            lines.append("")

    return "\n".join(lines).strip()


def _extract_docx_text(path: Path) -> str:
    """
    Rule 9: если первоисточник — .docx (Word), сохраняем его текст целиком
    (включая таблицы), а не только пересказ, прежде чем дробить на атомарки.
    """
    if _docx_lib is None:
        raise RuntimeError(
            "Библиотека python-docx не установлена. Поставь: pip install python-docx"
        )

    document = _docx_lib.Document(str(path))
    lines: List[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            lines.append(text)

    for table in document.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip(" |"):
                lines.append(row_text)

    return "\n".join(lines).strip()


def _materialize_attachment_source(attachment_path: Path) -> Path:
    """
    Реальный текст лекции нередко лежит не в .md, а во вложении (pptx),
    на которое .md-файл ссылается через ![[...]]. Раньше такие вложения
    просто отбрасывались как "не .md", и дробить было нечего.

    Теперь: если рядом со вложением ещё нет .md-двойника — извлекаем текст
    из вложения и создаём такой .md-файл (с тем же именем). Дальше он
    обрабатывается как любой другой информативный файл: дробится на темы,
    сохраняет полный конспект и т.д. Сам исходный pptx не трогаем.
    """
    md_path = attachment_path.with_suffix(".md")

    if md_path.exists():
        return md_path

    suffix = attachment_path.suffix.lower()

    # Rule 9 требует поддержку doc/pptx/docx/ppt. Форматы с современным
    # библиотечным парсером (.pptx, .docx) поддерживаются здесь напрямую,
    # даже если локальный ns_config.SUPPORTED_ATTACHMENT_SUFFIXES ещё не
    # обновлён под них. Легаси-бинарные .doc/.ppt (не Office Open XML)
    # без дополнительных внешних конвертеров надёжно не читаются —
    # такие файлы стоит пересохранить в .docx/.pptx перед загрузкой.
    locally_supported = {".pptx", ".docx"}
    known_suffixes = set(SUPPORTED_ATTACHMENT_SUFFIXES) | locally_supported

    if suffix not in known_suffixes:
        raise FileNotFoundError(
            f"формат {suffix or '(без расширения)'} пока не поддерживается для извлечения текста "
            "(легаси .doc/.ppt надёжно не читаются — пересохрани как .docx/.pptx)"
        )

    if suffix == ".pptx":
        extracted = _extract_pptx_text(attachment_path)
    elif suffix == ".docx":
        extracted = _extract_docx_text(attachment_path)
    else:
        extracted = ""

    if not extracted.strip():
        raise FileNotFoundError(f"в {attachment_path.name} не нашлось текста для извлечения")

    content = (
        f"<!-- neuro:extracted_from={attachment_path.name} -->\n\n"
        f"# {attachment_path.stem}\n\n{extracted}\n"
    )
    _write_note_atomic(md_path, content)
    _log_change("EXTRACT_ATTACHMENT_TEXT", f"{attachment_path.name} -> {md_path.name}")
    return md_path


def collect_link_graph(
    root: Path,
    diary_linked: Set[Path],
    max_depth: int = MAX_LINK_DEPTH,
    extraction_log: List[str] = None,
) -> List[Path]:
    """
    Возвращает корневой файл и связанные с ним файлы до max_depth.
    Защищенные Diary/date-файлы никогда не попадают в результат.
    Ссылки на поддерживаемые вложения (pptx) автоматически превращаются
    в обычные .md-файлы через _materialize_attachment_source и участвуют
    в графе как полноценные узлы.
    Циклы графа безопасны благодаря visited.
    """
    result = []
    visited: Set[Path] = set()
    queue = [(root.resolve(), 0)]

    while queue and len(visited) < MAX_GRAPH_NODES:
        current, depth = queue.pop(0)

        if current in visited:
            continue

        visited.add(current)

        if is_always_protected(current, diary_linked):
            continue

        if current.suffix.lower() != ".md":
            if not current.exists():
                continue

            try:
                current = _materialize_attachment_source(current)
            except Exception as exc:
                if extraction_log is not None:
                    extraction_log.append(f"{current.name}: {exc}")
                continue

            if current in visited:
                continue
            visited.add(current)

        if not current.exists():
            continue

        result.append(current)

        if depth >= max_depth:
            continue

        for _, target in _read_links(current):
            if target not in visited:
                queue.append((target, depth + 1))

    return result


# ============================================================
# ОПРЕДЕЛЕНИЕ LINK-ONLY ФАЙЛА
# ============================================================

def is_link_only_note(text: str) -> bool:
    """
    True, если файл фактически состоит только из заголовков/ссылок,
    пустых строк и служебных markdown-элементов, то есть не хранит
    самостоятельную информацию.
    """
    work = text

    work = re.sub(r"```.*?```", "", work, flags=re.DOTALL)
    work = re.sub(r"`[^`]*`", "", work)

    work = WIKILINK_PATTERN.sub("", work)
    work = MARKDOWN_LINK_PATTERN.sub("", work)

    work = re.sub(r"^#{1,6}\s*.*$", "", work, flags=re.MULTILINE)
    work = re.sub(r"^\s*[-*+]\s*$", "", work, flags=re.MULTILINE)
    work = re.sub(r"^\s*[-*+]\s+", "", work, flags=re.MULTILINE)
    work = re.sub(r"^\s*>\s*", "", work, flags=re.MULTILINE)
    work = re.sub(r"<!--.*?-->", "", work, flags=re.DOTALL)

    stripped = work.strip()

    if not stripped:
        return bool(extract_links(text))

    return False


def is_ai_generated_topic(text: str) -> bool:
    """
    True, если файл — это атомарная тема, которую ИИ уже создал при
    прошлом дроблении. Такие файлы пропускаются при повторном split,
    даже если на них ссылается граф другой лекции.
    """
    return text.lstrip().startswith(AI_GENERATED_MARKER)


def is_full_conspectus(text: str) -> bool:
    """
    True, если файл уже был раздроблен хотя бы раз и стал "полным
    конспектом" (исходный текст + секция '## Атомарные темы'). Такой
    файл больше НИКОГДА не отдаётся на повторное дробление, даже если
    до него дотянется граф ссылок совсем другого корня.
    """
    return FULL_CONSPECTUS_MARKER in text[:200]


def _extract_topic_aliases(text: str) -> Set[str]:
    """Читает скрытые названия/синонимы темы из атомарной заметки."""
    aliases: Set[str] = set()
    for line in text.splitlines():
        if line.startswith(AI_TOPIC_ALIASES_PREFIX):
            raw = line[len(AI_TOPIC_ALIASES_PREFIX):].strip()
            if raw.endswith("-->"):
                raw = raw[:-3].strip()
            for alias in raw.split("|"):
                alias = alias.strip()
                if alias:
                    aliases.add(_normalize_topic_key(alias))
    return aliases


def _extract_topic_aliases_display(text: str) -> List[str]:
    """Как _extract_topic_aliases, но возвращает алиасы в исходном написании
    (без casefold) — нужно, когда алиасы отдаются человеку/модели, а не
    используются как ключ для сравнения."""
    aliases: List[str] = []
    for line in text.splitlines():
        if line.startswith(AI_TOPIC_ALIASES_PREFIX):
            raw = line[len(AI_TOPIC_ALIASES_PREFIX):].strip()
            if raw.endswith("-->"):
                raw = raw[:-3].strip()
            for alias in raw.split("|"):
                alias = alias.strip()
                if alias and alias not in aliases:
                    aliases.append(alias)
    return aliases


def _extract_inline_topic_aliases(text: str, topic_name: str = "") -> Set[str]:
    """Извлекает очевидные сокращения вида «Полное название (СКС)»."""
    aliases: Set[str] = set()
    topic_name = str(topic_name or "").strip()
    if not topic_name:
        return aliases

    topic_pattern = _build_loose_phrase_pattern(topic_name).pattern
    pattern = re.compile(
        rf"{topic_pattern}\s*\(([А-ЯЁA-Z][А-ЯЁA-Z0-9/-]{{1,11}})\)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        alias = match.group(1).strip()
        if alias and len(alias) >= 2:
            aliases.add(_normalize_topic_key(alias))
    return aliases


def _extract_visible_aliases(text: str) -> Set[str]:
    """Извлекает алиасы из видимой строки «Также известно как: ...»."""
    aliases: Set[str] = set()
    for match in re.finditer(
        r"Также известно как:\s*(.+?)(?:_|\n|$)", text, flags=re.IGNORECASE
    ):
        for alias in re.split(r",|;|/", match.group(1)):
            alias = alias.strip(" _.-")
            if alias:
                aliases.add(_normalize_topic_key(alias))
    return aliases


def _topic_lookup_aliases(note_text: str, topic_name: str, note_stem: str) -> Set[str]:
    """Полный набор логических имён заметки для индекса разрешения ссылок."""
    aliases = {
        _normalize_topic_key(topic_name),
        _normalize_topic_key(note_stem),
    }
    aliases.update(_extract_topic_aliases(note_text))
    aliases.update(_extract_visible_aliases(note_text))
    aliases.update(_extract_inline_topic_aliases(note_text, topic_name))
    return {key for key in aliases if key}


def _find_existing_note_for_target_alias(
    target: str,
    referencing_file: Path,
    global_topic_lookup: Dict[str, Path],
) -> Path | None:
    """Ищет уже существующий файл по сокращению/алиасу, не создавая дубль."""
    key = _normalize_topic_key(target)
    direct = global_topic_lookup.get(key) if key else None
    if direct is not None and direct.exists() and not _is_effectively_empty_note(direct):
        return direct.resolve()

    # Пытаемся найти полную форму прямо рядом со ссылкой: «Полное название (СКС)».
    try:
        local_text = _read_note(referencing_file)
    except Exception:
        local_text = ""

    acronym = re.escape(str(target).strip())
    candidates: List[str] = []
    for match in re.finditer(
        rf"([^\n.;:(){{}}]{{5,140}}?)\s*\(({acronym})\)",
        local_text,
        flags=re.IGNORECASE,
    ):
        candidate = re.sub(r"^[-*•\s]+", "", match.group(1)).strip()
        if candidate:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            resolved = resolve_link(candidate, referencing_file)
            if resolved.exists() and not _is_effectively_empty_note(resolved):
                return resolved.resolve()
        except Exception:
            continue

    # Если полная форма находится не в текущем файле, ищем её по всему vault.
    # Это закрывает реальный случай: отдельного файла «СКС» нет, зато где-то
    # есть «[Структурированная кабельная система](<Структурированная кабельная
    # система>) (СКС)». В таком случае ссылка должна указывать на реальный
    # русский файл полной формы, а не запускать создание «СКС.md».
    if re.fullmatch(r"[А-ЯЁA-Z][А-ЯЁA-Z0-9/-]{1,11}", str(target).strip()):
        vault = get_vault_path()
        for note in vault.rglob("*.md"):
            if not note.is_file() or note.name.startswith("."):
                continue
            try:
                text = _read_note(note)
            except Exception:
                continue

            # Наиболее надёжная форма: [Полное название](<Полное название>) (СКС).
            for match in re.finditer(
                rf"\[([^\]]{{5,140}})\]\(<([^>]+)>\)\s*\(({acronym})\)",
                text,
                flags=re.IGNORECASE,
            ):
                candidate = match.group(2).strip()
                if not candidate:
                    continue
                try:
                    resolved = resolve_link(candidate, note)
                except Exception:
                    continue
                if resolved.exists() and not _is_effectively_empty_note(resolved):
                    return resolved.resolve()

            for match in re.finditer(
                rf"([^\n.;:(){{}}]{{5,140}}?)\s*\(({acronym})\)",
                text,
                flags=re.IGNORECASE,
            ):
                candidate = re.sub(r"^[-*•\s]+", "", match.group(1)).strip()
                candidate = re.sub(r"^\[([^\]]+)\]\(<[^>]+>\)$", r"\1", candidate).strip()
                if not candidate:
                    continue
                try:
                    resolved = resolve_link(candidate, note)
                except Exception:
                    continue
                if resolved.exists() and not _is_effectively_empty_note(resolved):
                    return resolved.resolve()
    return None


def ensure_study_tag(path: Path) -> bool:
    if _is_protected_note(path):
        return False

    text = _read_note(path)

    if re.search(r"(?<!\w)#STUDY\b", text, flags=re.IGNORECASE):
        return False

    new_text = f"#STUDY\n\n{text.lstrip()}"
    _write_note_atomic(path, new_text)
    _log_change("ADD_STUDY_TAG", f"{path.relative_to(get_vault_path())}")
    return True


# ============================================================
# GEMINI
# ============================================================

def _gemini_text(prompt: str, system_instruction: str) -> str:
    url = ns_config.gemini_url()

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
    }

    last_response = None
    for attempt in range(3):
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=90,
        )
        last_response = response

        if response.status_code not in (429, 500, 502, 503, 504):
            break

        if attempt < 2:
            import time
            time.sleep(2 * (attempt + 1))

    data = last_response.json()

    if last_response.status_code != 200:
        error_message = data.get("error", {}).get("message", last_response.text)
        raise RuntimeError(f"Gemini API [{last_response.status_code}]: {error_message}")

    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini не вернул candidates.")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise RuntimeError("Gemini не вернул текстовый ответ.")

    return parts[0].get("text", "").strip()


def _gemini_json(prompt: str, system_instruction: str):
    raw = _gemini_text(prompt, system_instruction)
    clean = raw.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gemini вернул невалидный JSON:\n{raw[:2000]}"
        ) from exc


# ============================================================
# РАЗБИЕНИЕ ОДНОГО ФАЙЛА
# ============================================================

def split_note_with_ai(
    path: Path,
    available_context: List[str],
) -> List[Dict]:
    content = _read_note(path)
    prompt = f"""
Ты работаешь как архитектор учебной базы знаний в Obsidian.

Твоя задача — НЕ просто пересказать исходный файл и НЕ просто разбить его по
заголовкам. Твоя задача — построить из исходного материала МАКСИМАЛЬНО
ПОЛНУЮ, НЕИЗБЫТОЧНУЮ и СВЯЗНУЮ АТОМАРНУЮ структуру знаний.

========================
1. ИСТОЧНИК — ЕДИНСТВЕННЫЙ ИСТОЧНИК ИСТИНЫ
========================

Имя исходного файла:
{path.name}

Полный исходный материал:
--- BEGIN SOURCE ---
{content}
--- END SOURCE ---

Используй ТОЛЬКО сведения, реально присутствующие в исходном материале.

НЕЛЬЗЯ:
- придумывать отсутствующие факты;
- дополнять материал знаниями из своей памяти;
- подменять содержание источника общими знаниями;
- приписывать понятию свойства, которых источник не сообщает;
- создавать атомарный файл только потому, что некоторый термин общеизвестен.

МОЖНО:
- нормализовать формулировки;
- объединять синонимы;
- переводить обычную техническую лексику на русский;
- менять порядок предложений внутри одной темы ради ясности;
- связывать темы через [[wikilinks]];
- выносить самостоятельные понятия в отдельные файлы;
- объединять несколько фрагментов ИЗ ЭТОГО ЖЕ ИСТОЧНИКА в одну тему, если они
  относятся к одной и той же сущности.

========================
2. ГЛАВНАЯ ЦЕЛЬ: ПОЛНОЕ ПОКРЫТИЕ ИСХОДНИКА
========================

КРИТИЧЕСКИ ВАЖНО:

Не останавливайся после того, как нашёл "очевидные" основные темы.

Сначала МОЛЧА, ВНУТРИ СЕБЯ, проведи полный инвентаризационный проход по всему
исходному тексту.

На этом проходе найди КАЖДОЕ самостоятельное понятие, для которого в источнике
есть хотя бы одно из следующего:

1) явное определение;
2) объяснение "что это такое";
3) объяснение назначения или функции;
4) описание устройства, компонента, технологии, метода или механизма;
5) отдельная характеристика;
6) отдельный принцип работы;
7) отдельные преимущества / недостатки / особенности;
8) отдельный классификационный элемент, которому посвящено собственное описание;
9) именованный тип / подтип / разновидность, которому источник даёт
   самостоятельное содержание;
10) термин, который источник определяет внутри описания другой темы;
11) отдельный объект, сущность или процесс, необходимый для понимания материала.

После инвентаризации сопоставь этот полный список с темами, которые ты собираешься
вернуть.

Твоя внутренняя проверка перед ответом должна звучать примерно так:

"Я просмотрел весь исходник.
Для каждого самостоятельного понятия либо существует отдельная тема,
либо я могу обоснованно объяснить, почему отдельная тема НЕ нужна."

НЕ ПОЛАГАЙСЯ ТОЛЬКО НА ЗАГОЛОВКИ И РАЗДЕЛЫ ИСХОДНИКА.

Определения, данные внутри абзаца, таблицы, списки, примеры с отдельным описанием,
подпункты и замечания тоже анализируй.

========================
3. ЧТО СЧИТАТЬ АТОМАРНОЙ ТЕМОЙ
========================

Один атомарный файл = одна самостоятельная сущность знания.

Самостоятельной темой являются, например:

- конкретное устройство;
- конкретный сетевой компонент;
- конкретный тип топологии;
- конкретный протокол;
- конкретная технология;
- конкретная среда передачи;
- конкретный адрес/идентификатор;
- конкретный процесс;
- конкретная служба;
- конкретный метод;
- конкретное определение;
- конкретный классификационный элемент;
- конкретный термин, которому источник реально даёт содержание.

Проверка:

Если тему можно взять из исходника и задать ей вопрос
"Что это?", "Для чего это?", "Как это работает?", "Какие у него свойства?",
и источник даёт самостоятельный ответ — это сильный кандидат на отдельный файл.

========================
4. ОЧЕНЬ ВАЖНО: НЕ СЖИМАЙ РАЗНОИМЕННЫЕ ПОДТИПЫ
========================

Если источник перечисляет несколько конкретных разновидностей и каждой даёт
хотя бы небольшое самостоятельное описание, КАЖДАЯ разновидность должна иметь
СВОЙ атомарный файл.

Например:

Топология:
- Полносвязная топология
- Общая шина
- Кольцевая топология
- Звезда
- Дерево

не превращать только в:

"Классификация сетевых топологий.md"

Нужно:

- "Полносвязная топология.md"
- "Топология Общая шина.md"
- "Кольцевая топология.md"
- "Топология Звезда.md"
- "Иерархическое дерево.md"

А общий файл-классификация допустим ТОЛЬКО дополнительно.

То же самое относится к:

- видам сетевого оборудования;
- видам сред передачи;
- видам кабелей;
- разновидностям интерфейсов;
- видам соединений;
- методам;
- типам архитектур;
- типам моделей;
- любым другим перечисленным разновидностям.

========================
5. ТЕРМИН ВНУТРИ ДРУГОЙ ТЕМЫ
========================

КРИТИЧЕСКИ ВАЖНО:

Если термин определён внутри описания другого понятия, он не должен потеряться.

Например:

"MAC-адрес" определён внутри описания сетевой карты.

Тогда должны существовать отдельно:

- Сетевая карта
- MAC-адрес

А внутри текста "Сетевая карта" должна появиться ссылка:

[[MAC-адрес]]

Аналогично:

Маршрутизатор может содержать отдельное определение
маршрутизации → "Маршрутизация" становится отдельной темой, ЕСЛИ источник
действительно раскрывает её как самостоятельное понятие.

Необходимо искать такие вложенные определения ВО ВСЕХ абзацах, а не только
в заголовках.

========================
6. ГРАНИЦА МЕЖДУ "ОТДЕЛЬНАЯ ТЕМА" И "ПРОСТО ПРИМЕР"
========================

НЕ СОЗДАВАЙ отдельную тему только потому, что слово встретилось.

Например:

"Сервер, ноутбук, смартфон и телевизор относятся к конечным устройствам."

Если источник только перечисляет их как примеры и ничего отдельно про каждый
не объясняет, НЕ создавай четыре атомарки.

Но если источник пишет:

"Сервер — устройство, предоставляющее ..."
"Ноутбук — ..."
"Смартфон — ..."

тогда это уже самостоятельные темы.

Критерий:
Нужно отделять "упомянутый пример" от "раскрытого понятия".

========================
7. ОБЩАЯ ТЕМА И ПОДТЕМЫ
========================

Не создавай искусственные обзорные файлы только для группировки.

Пример плохой структуры:

"Сетевое оборудование"
    -> всё оборудование описано только внутри него

Правильнее:

"Маршрутизатор"
"Коммутатор"
"Концентратор"
"Мост"
"Шлюз"

А общий обзор допустим как дополнительная связующая тема.

Поле "subtopics" используй ТОЛЬКО если основная тема действительно крупная
и имеет независимые объёмные подтемы.

Очень важно:
subtopic не является способом обойти глобальную дедупликацию.

Каждая подтема должна рассматриваться как полноценная атомарная сущность
и сравниваться с уже существующими темами.

========================
8. ГЛОБАЛЬНАЯ ДЕДУПЛИКАЦИЯ
========================

ЭТО ОЧЕНЬ ВАЖНО ДЛЯ ОБРАБОТКИ ВТОРОЙ, ТРЕТЬЕЙ И ПОСЛЕДУЮЩИХ ЛЕКЦИЙ.

Другие темы, уже существующие или создаваемые рядом:
{chr(10).join(f"- {item}" for item in available_context[:200]) or "- список пока пуст"}

Рассматривай этот список как уже существующую ГЛОБАЛЬНУЮ БАЗУ сущностей,
а не как список "тем только этой лекции".

Если новое понятие обозначает ту же сущность, что уже существующая тема,
НЕ создавай новую тему.

Пример:

Первая лекция:
"Структурированная кабельная система"

Вторая лекция:
"СКС"

Третья лекция:
"Structured Cabling System"

Это ОДНА сущность, а не три файла.

Алиасы:
- СКС
- Structured Cabling System

Основное русское имя:
"Структурированная кабельная система"

То же самое для:

"Router" = "Маршрутизатор" = "роутер"

"Switch" = "Коммутатор" = "свитч"

"Network node" = "Сетевой узел" = "узел"

"Network interface card" = "Сетевая карта" = "NIC"

"Computer network" = "Компьютерная сеть"

Если существующая тема уже есть, новая генерация должна концептуально
ПРИВЯЗЫВАТЬСЯ К НЕЙ, а не создавать дубль.

ЗАПРЕЩЕНО создавать искусственные варианты:

- Тема
- Тема-2
- Тема-3
- Тема (новая)
- Тема 2

только потому, что источник второй лекции снова описывает ту же сущность.

========================
9. СЕМАНТИЧЕСКОЕ СЛИЯНИЕ
========================

Считай одинаковыми одну и ту же сущность, если различается только:

- язык;
- регистр;
- пунктуация;
- дефис;
- падежная форма;
- сокращение;
- полное название / аббревиатура;
- русское / английское название;
- общеупотребительный синоним;
- техническое сокращение.

Но НЕ объединяй разные сущности только потому, что они связаны.

Например:

"Коммутатор" != "Маршрутизатор"

"Физическая топология" != "Логическая топология"

"Сеть" != "Сетевой узел"

"Витая пара" != "Коаксиальный кабель"

"Среда передачи данных" != "Оптоволокно"

"Сетевые сервисы" != "Сетевые процессы"

Связанность НЕ является основанием для объединения.

========================
10. АЛИАСЫ
========================

Для каждой темы собери ВСЕ реально встреченные в источнике варианты названия.

Особенно обязательно учитывать:

- сокращения;
- аббревиатуры;
- английское название;
- русскую полную форму;
- русское краткое название;
- профессиональное разговорное название;
- варианты из скобок;
- варианты вида "Полное название (СКС)".

Пример:

topic:
"Структурированная кабельная система"

aliases:
[
  "СКС",
  "Structured Cabling System"
]

Не создавай отдельный файл только для алиаса.

========================
11. ЯЗЫК
========================

ВСЕ:
- topic;
- filename;
- видимая часть content;
- описания;
- определения;
- пояснения;

пиши НА РУССКОМ.

Оригинальные английские слова можно сохранять ТОЛЬКО как:

- аббревиатуры;
- названия протоколов;
- стандарты;
- языки;
- ключевые слова;
- общепринятые технические обозначения;
- имена собственные.

Обычные слова обязательно переводи.

Примеры:

Router -> Маршрутизатор
Switch -> Коммутатор
Hub -> Концентратор
Bridge -> Сетевой мост
Gateway -> Шлюз
Network node -> Сетевой узел
Network interface card -> Сетевая карта
End devices -> Оконечные устройства
Intermediary devices -> Промежуточные устройства
Transmission media -> Среда передачи данных

НЕ используйте английское название как основное только потому, что Gemini
к нему привык.

========================
12. ИМЯ ФАЙЛА
========================

КРИТИЧЕСКИ ВАЖНО:

Поле "filename" должно соответствовать КАНОНИЧЕСКОМУ русскому "topic".

Не используй случайное имя, сгенерированное от английского исходного термина.

Правильно:

topic: "Компьютерная сеть"
filename: "Компьютерная сеть.md"

topic: "Сетевой узел"
filename: "Сетевой узел.md"

topic: "Маршрутизатор"
filename: "Маршрутизатор.md"

topic: "Структурированная кабельная система"
filename: "Структурированная кабельная система.md"

Неправильно:

topic: "Компьютерная сеть"
filename: "Computer_network.md"

Неправильно:

topic: "Маршрутизатор"
filename: "Router.md"

========================
13. WIKILINKS
========================

Внутри content ссылки должны быть обычными Obsidian wikilinks:

[[Компьютерная сеть]]
[[Сетевой узел]]
[[Маршрутизатор]]

НЕ используй:

[Компьютерная сеть](<Компьютерная сеть>)
[Сетевой узел](<Сетевой узел>)

если речь идёт о внутренней теме базы знаний.

Первое содержательное упоминание отдельной темы в тексте должно по возможности
иметь [[wikilink]].

Ссылка должна вести на КАНОНИЧЕСКОЕ ИМЯ темы, а не на английский алиас,
если существует русская каноническая тема.

Например, даже если в источнике написано:

"Router"

в атомарном тексте лучше:

"[[Маршрутизатор]] — специализированное устройство..."

а "Router" сохранить в aliases.

========================
14. НЕ СОЗДАВАЙ БИТЫЕ ССЫЛКИ
========================

НИКОГДА не создавай ссылку на тему, для которой одновременно выполняются оба
условия:

1) она не существует среди уже известных тем;
2) она не будет создана текущим ответом.

Каждый [[wikilink]] должен ссылаться либо:

- на существующую тему;
- либо на тему из возвращаемого результата.

НЕ создавай пустые ссылки.

НЕ создавай ссылки вида:

[Структурированная кабельная система](<Структурированная кабельная система>)

в качестве замены wikilink.

НЕ создавай [[СКС]], если каноническая тема:
[[Структурированная кабельная система]]

НЕ создавай [[Router]], если каноническая тема:
[[Маршрутизатор]]

Английские названия могут находиться в aliases, но не должны становиться
случайными отдельными ссылками.

========================
15. ЗАЩИЩЁННЫЕ ФАЙЛЫ
========================

В существующей базе могут находиться файлы с тегом:

#PROTECTED

Такие файлы считаются НЕПРИКОСНОВЕННЫМИ.

НИКОГДА не предлагай:
- переименовать их;
- изменить их content;
- заменить их заголовок;
- переписать их ссылки;
- удалить их;
- объединить их с другой темой;
- создать поверх них новый файл.

Если существующая #PROTECTED-тема семантически соответствует новой теме,
ИСПОЛЬЗУЙ ЕЁ КАК УЖЕ СУЩЕСТВУЮЩУЮ СУЩНОСТЬ.

То есть:

новая лекция
   ↓
та же сущность
   ↓
существующий #PROTECTED файл

а НЕ новый дубль.

#PROTECTED означает "не изменять", а не "игнорировать".

========================
16. СОХРАНЕНИЕ СОДЕРЖАНИЯ
========================

Не выбрасывай содержательную информацию.

Если исходный фрагмент относится к определённой атомарной теме, он должен
попасть в content этой темы либо быть логично распределён между несколькими
темами без потери смысла.

Не копируй один и тот же большой фрагмент полностью в несколько атомарок.

Правило:

ОДНА мысль / определение / описание = ОДНА основная тема.

Другие связанные темы получают wikilink, а не дублирование всего текста.

========================
17. КАЧЕСТВО АТОМИЗАЦИИ
========================

После первого внутреннего разбиения выполни ещё одну молчаливую проверку:

ПРОСМОТРИ ИСХОДНИК СНОВА.

Поищи места:

- "— это";
- "является";
- "представляет собой";
- "используется для";
- "предназначен для";
- "служит для";
- "характеризуется";
- "бывает";
- "выделяют";
- "различают";
- "существуют";
- "тип";
- "вид";
- "разновидность";
- "компонент";
- "элемент";
- "процесс";
- "сервис";
- "технология";
- "метод";
- "протокол";
- "адрес";
- "интерфейс";
- "устройство";
- "среда".

Это НЕ означает, что каждое предложение с таким словом обязательно создаёт
новую тему.

Это означает: ещё раз проверь, не спряталось ли внутри абзаца самостоятельное
понятие, которое ты пропустил.

========================
18. ПРОВЕРКА ПОЛНОТЫ ПЕРЕД ОТВЕТОМ
========================

Перед формированием JSON проведи внутреннюю проверку:

А. Все явно определённые сущности найдены?
Б. Все названные разновидности с собственным описанием выделены?
В. Вложенные определения не потеряны?
Г. Синонимы объединены?
Д. Повторяющиеся сущности из других тем НЕ создаются заново?
Е. У каждой отдельной темы есть самостоятельный смысл?
Ж. Нет ли случайного обзорного файла вместо нескольких атомарных?
З. Нет ли темы, созданной только потому, что термин был вскользь упомянут?
И. Нет ли wikilink на несуществующую тему?
К. Нет ли английского имени там, где есть нормальный русский эквивалент?
Л. Не затрагивается ли #PROTECTED?
М. Подтемы тоже прошли эти проверки?

Только после этой проверки формируй JSON.

========================
19. ВАЖНЫЙ БАЛАНС: ПОЛНОТА ≠ БЕСКОНЕЧНОЕ ДРОБЛЕНИЕ
========================

Твоя цель НЕ в том, чтобы создать как можно больше файлов.

Твоя цель:

МАКСИМАЛЬНОЕ ПОКРЫТИЕ СОДЕРЖАТЕЛЬНЫХ СУЩНОСТЕЙ
+
МИНИМУМ ДУБЛЕЙ
+
МИНИМУМ ЛОЖНЫХ ТЕМ
+
СВЯЗНЫЙ ГРАФ.

Не дроби:

- предложения;
- отдельные факты;
- численные параметры;
- примеры;
- обычные существительные;
- случайные упоминания;

если для них нет самостоятельного понятия.

Но НИКОГДА не оставляй в родительском файле самостоятельное определение
только потому, что оно "короткое".

Короткость определения НЕ является основанием оставить его внутри другой темы.

========================
20. ФОРМАТ ВЫВОДА
========================

Верни ТОЛЬКО валидный JSON-массив.

[
  {{
    "topic": "Каноническое русское название темы",
    "filename": "Каноническое русское название темы.md",
    "aliases": ["Синоним", "Аббревиатура", "Оригинальное название"],
    "tags": ["#тег1", "#тег2", "#определение"],
    "related_topics": [],
    "content": "Полный Markdown-текст атомарной темы с [[wikilinks]]",
    "subtopics": [
      {{
        "topic": "Каноническое русское название подтемы",
        "filename": "Каноническое русское название подтемы.md",
        "aliases": [],
        "tags": ["#тег"],
        "related_topics": [],
        "content": "Полный Markdown-текст подтемы"
      }}
    ]
  }}
]

Обязательные поля:
topic
filename
aliases
tags
related_topics
content
subtopics

Если subtopics не нужны — верни [].

Если aliases не нужны — верни [].

Если related_topics не нужны — верни [].

========================
21. ИТОГОВАЯ МЫСЛИТЕЛЬНАЯ СХЕМА
========================

Работай в таком внутреннем порядке:

ШАГ 1.
Полностью прочитать исходник.

ШАГ 2.
Составить полный скрытый инвентарь самостоятельных понятий.

ШАГ 3.
Для каждого понятия определить:
новая тема / существующая тема / алиас существующей темы / просто пример.

ШАГ 4.
Разделить понятия на атомарные сущности.

ШАГ 5.
Объединить семантические дубли.

ШАГ 6.
Проверить существующие темы и не создавать повторов.

ШАГ 7.
Проверить #PROTECTED и никогда не планировать изменение защищённых файлов.

ШАГ 8.
Назначить каждому понятию КАНОНИЧЕСКОЕ РУССКОЕ название.

ШАГ 9.
Построить filename ИМЕННО из canonical topic.

ШАГ 10.
Создать content на русском с wikilinks на канонические темы.

ШАГ 11.
Повторно проверить исходник на пропущенные определения и подтипы.

ШАГ 12.
Повторно проверить ссылки, дубли, aliases и язык.

ШАГ 13.
Только после этого вернуть JSON.

Главное правило:

НЕ "сделай несколько тем".

А:

"ПОСТРОЙ ПОЛНЫЙ НАБОР АТОМАРНЫХ СУЩНОСТЕЙ, КОТОРЫЕ РЕАЛЬНО РАСКРЫТЫ
В ИСХОДНИКЕ, И ПРИВЯЖИ ИХ К УЖЕ СУЩЕСТВУЮЩЕЙ ГЛОБАЛЬНОЙ БАЗЕ,
НЕ СОЗДАВАЯ ДУБЛЕЙ И НЕ СОЗДАВАЯ БИТЫХ ССЫЛОК."
"""

    system_instruction = (
        "Ты архитектор учебной базы знаний Obsidian. "
        "Твоя задача — строить полную атомарную структуру знаний из исходного "
        "материала, а не просто пересказывать текст. "
        "Сначала внутренне инвентаризируй все самостоятельно раскрытые понятия, "
        "включая определения, функции, процессы, устройства, технологии, "
        "классификационные элементы и именованные подтипы, даже если они находятся "
        "внутри описания другой темы. Затем сопоставь их с существующей глобальной "
        "базой и не создавай семантических дублей. "
        "Одинаковая сущность под разными русскими, английскими, сокращёнными и "
        "полными названиями должна быть одной темой с aliases. "
        "Обычную техническую лексику пиши по-русски. "
        "filename всегда должен соответствовать каноническому русскому topic. "
        "Используй канонические русские [[wikilinks]], а не markdown-ссылки. "
        "Не создавай ссылки на отсутствующие темы. "
        "Не изменяй и не переименовывай темы, помеченные #PROTECTED. "
        "Не выдумывай сведения, которых нет в источнике. "
        "Не дроби случайные упоминания, но и не оставляй самостоятельные "
        "определения внутри родительской темы только потому, что они короткие. "
        "Перед выдачей JSON повторно проверь исходник на пропущенные определения, "
        "подтипы, вложенные понятия, дубли и битые ссылки. "
        "Верни строго валидный JSON требуемой структуры."
    )

    result = _gemini_json(prompt, system_instruction)

    if not isinstance(result, list) or not result:
        raise RuntimeError("Gemini не вернул список тематических файлов.")

    valid = []

    for item in result:
        normalized = _normalize_generated_item(item)
        if normalized is None:
            continue

        # Rule 6 применяется не только на уровне промпта (модель иногда
        # игнорирует текстовую просьбу), но и здесь, на уровне кода:
        # если контент всё равно оказался не на русском, просим Gemini
        # перевести его отдельным вызовом, прежде чем принять тему.
        normalized = _enforce_russian_language_safe(normalized)
        normalized = _strip_self_links(normalized)

        # Податомарные файлы (rule 4/7): разворачиваем в отдельные записи
        # того же вида, помечая родительскую тему для последующей склейки
        # ссылок в split_note_tree.
        raw_subtopics = item.get("subtopics", []) if isinstance(item, dict) else []
        if not isinstance(raw_subtopics, list):
            raw_subtopics = []

        subtopic_items = []
        for raw_sub in raw_subtopics:
            normalized_sub = _normalize_generated_item(raw_sub)
            if normalized_sub is None:
                continue
            normalized_sub = _enforce_russian_language_safe(normalized_sub)
            normalized_sub = _strip_self_links(normalized_sub)
            normalized_sub["_parent_topic"] = normalized["topic"]
            subtopic_items.append(normalized_sub)

        normalized["_subtopics"] = [sub["topic"] for sub in subtopic_items]
        valid.append(normalized)
        valid.extend(subtopic_items)

    if not valid:
        raise RuntimeError(f"Gemini не создал ни одной валидной темы для {path.name}.")

    return valid


def _is_mostly_russian(text: str) -> bool:
    """
    Грубая эвристика rule 6: считаем кириллицу против латиницы среди буквенных
    символов. Редкие термины/аббревиатуры на латинице (rule 6.2) не собьют
    порог — их обычно на порядок меньше, чем русского текста вокруг них.
    """
    cyrillic = len(re.findall(r"[а-яА-ЯёЁ]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    total = cyrillic + latin

    if total < 20:
        # Слишком мало буквенного текста, чтобы делать выводы (например,
        # контент почти целиком — формула/код/список терминов).
        return True

    return (cyrillic / total) >= 0.6


def _looks_like_established_term(name: str) -> bool:
    """
    Больше не используется для решения "переводить или нет" (см.
    _topic_is_russian_or_established и то, почему это было ошибкой), но
    оставлена как содержательная подсказка внутри промпта перевода —
    называет модели саму границу словами, а не только примерами.
    """
    stripped = name.strip()
    if not stripped:
        return False
    if re.search(r"[a-z]", stripped):
        return False
    return bool(re.search(r"[A-Z]", stripped))


# Жёсткие русские канонические имена для часто встречающихся сетевых тем.
# Ключи нормализуются через _normalize_topic_key, поэтому работают для
# "Network_node", "Network-node" и "Network node" одинаково.
_RUSSIAN_TOPIC_OVERRIDES = {
    "computer network": "Компьютерная сеть",
    "network node": "Сетевой узел",
    "node": "Сетевой узел",
    "communication line": "Линия связи",
    "communication channel": "Канал связи",
    "network interface card": "Сетевая карта",
    "transceiver": "Приёмо-передатчик",
    "mac address": "MAC-адрес",
    "interface": "Интерфейс",
    "physical interface": "Физический интерфейс",
    "logical interface": "Логический интерфейс",
    "network cables": "Сетевые кабели",
    "straight through cable": "Прямой кабель",
    "crossover cable": "Перекрёстный кабель",
    "console cable": "Консольный кабель",
    "peripheral devices": "Периферийные устройства",
    "end devices": "Конечные устройства",
    "intermediary devices": "Промежуточные устройства",
    "router": "Маршрутизатор",
    "switch": "Коммутатор",
    "hub": "Концентратор",
    "bridge": "Мост",
    "gateway": "Шлюз",
    "media": "Среда передачи данных",
    "transmission media": "Среда передачи данных",
    "software": "Программное обеспечение",
    "network services": "Сетевые сервисы",
    "network processes": "Сетевые процессы",
    "active network equipment": "Активное сетевое оборудование",
    "passive network equipment": "Пассивное сетевое оборудование",
    "ether": "Эфир",
    "fiber optics": "Оптоволокно",
    "fiber optic": "Оптоволокно",
    "power line communication": "Связь по линиям электропередачи",
    "plc": "Связь по линиям электропередачи",
    "network connectivity problems": "Проблемы связи нескольких компьютеров",
    "network topology": "Сетевая топология",
    "mesh topology": "Полносвязная топология",
    "bus topology": "Общая шина",
    "ring topology": "Кольцевая топология",
    "star topology": "Звезда",
    "tree topology": "Дерево",
    "structured cabling system": "Структурированная кабельная система",
    # "SCS" — транслит русской аббревиатуры СКС, а не международный термин
    # (см. промпт перевода). Аббревиатуры теперь проходят проверку по форме,
    # поэтому этот случай нужно закрепить явно, чтобы русификация не пропала.
    "scs": "СКС",
    "ddl": "Язык определения данных",
    "dml": "Язык обработки данных",
    "dcl": "Язык управления данными",
}

# Устоявшиеся обозначения и имена собственные, которые допустимо оставить
# латиницей. Обычные английские слова сюда НЕ попадают.
#
# ВАЖНО: это уже НЕ закрытый белый список. Раньше любая аббревиатура вне
# списка (CIDR, VLAN, NAT, OSPF...) объявлялась "английским названием",
# дважды уходила на перевод, а потом _enforce_russian_language бросал
# RuntimeError и ВЕСЬ исходник (лекция целиком) не давал ни одной атомарки.
# Теперь список — лишь быстрый путь; настоящие аббревиатуры распознаются
# структурно (_looks_like_acronym_token), а обычные слова по-прежнему
# уходят на русификацию.
_ESTABLISHED_LATIN_TOPIC_NAMES = {
    "api", "arp", "arpanet", "css", "dhcp", "dns", "docker", "ftp",
    "html", "http", "https", "icmp", "ieee", "ip", "json", "lan",
    "mac", "rest", "select", "soap", "sql", "ssh", "ssl", "tcp", "udp",
    "tls", "usb", "wan", "wi fi", "xsd", "xml",
    "xml api", "tcp ip", "ieee 802 3",
    # сети
    "cidr", "vlsm", "vlan", "nat", "pat", "ospf", "bgp", "rip", "eigrp",
    "stp", "rstp", "vpn", "qos", "acl", "cli", "gui", "isp", "ipv4",
    "ipv6", "ipsec", "mpls", "snmp", "smtp", "pop3", "imap", "telnet",
    "ethernet", "bluetooth", "wlan", "pan", "man", "osi", "tftp", "ntp",
    "lldp", "cdp", "vtp", "hsrp", "vrrp", "sdn", "nfv", "iot", "csma cd",
    "csma ca", "crc", "ttl", "mtu", "mss", "asn", "dscp", "lacp",
    # ПО / инструменты
    "linux", "unix", "windows", "python", "git", "cisco", "wireshark",
    "ubuntu", "kubernetes",
}

# Аббревиатура: 2–8 символов, только заглавные латинские буквы и цифры,
# допускается хвост версии вида "v4"/"v6" (IPv4, OSPFv2) и цифровой хвост
# (POP3). Обычные английские слова в Title Case ("Router", "Subnet") сюда
# не попадают, поэтому русификация для них работает как раньше.
_ACRONYM_TOKEN_RE = re.compile(r"^(?=(?:.*[A-Z]){2})[A-Z][A-Z0-9]{1,7}(?:v[0-9]+)?$")
_IPV_TOKEN_RE = re.compile(r"^IPv[0-9]+$")


def _looks_like_acronym_token(token: str) -> bool:
    """True для настоящих международных аббревиатур: CIDR, VLAN, IPv6, OSPFv2."""
    token = str(token or "").strip()
    if not token:
        return False
    if token.casefold() in _ESTABLISHED_LATIN_TOPIC_NAMES:
        return True
    if _IPV_TOKEN_RE.match(token):
        return True
    return bool(_ACRONYM_TOKEN_RE.match(token)) and len(token) <= 10


def _latin_part_is_established(part: str) -> bool:
    """Одна латинская часть названия (TCP/IP, Wi-Fi, VLAN, IEEE) — допустима?"""
    whole = re.sub(r"[^A-Za-z0-9]+", " ", part).strip().casefold()
    if whole in _ESTABLISHED_LATIN_TOPIC_NAMES:
        return True
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*|[0-9]+", part)
    alpha_tokens = [t for t in tokens if re.search(r"[A-Za-z]", t)]
    if not alpha_tokens:
        return False
    return all(_looks_like_acronym_token(t) for t in alpha_tokens)


def _canonicalize_topic_name(topic: str) -> str:
    """Возвращает жёстко каноническое русское имя, когда оно известно."""
    raw = str(topic or "").strip()
    if not raw:
        return raw
    key = _normalize_topic_key(raw)
    return _RUSSIAN_TOPIC_OVERRIDES.get(key, raw)


def _topic_is_russian_or_established(topic: str) -> bool:
    """
    Проверяет именно ЗАГОЛОВОК темы.

    Ключевое правило:
    - чистое русское название — да;
    - смешанное название допустимо только если латинские куски являются
      настоящими международными обозначениями (MAC-адрес, IP-сеть, Маска CIDR);
    - чистая аббревиатура (CIDR, VLAN, OSPF, IPv6) — да, даже если её нет в
      списке: аббревиатуры определяются по форме, а не по белому списку;
    - чистое английское/транслитерированное обычное слово — НЕТ
      (оно по-прежнему отправляется на русификацию);
    - известные названия из _RUSSIAN_TOPIC_OVERRIDES считаются корректными,
      даже если на входе было английское имя.
    """
    raw = str(topic or "").strip()
    if not raw:
        return False

    has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", raw))
    latin_parts = re.findall(r"[A-Za-z]+(?:[-/][A-Za-z0-9]+)*", raw)

    if not latin_parts:
        return True

    # Чистое латинское название: допустимо, только если ВСЕ его части —
    # устоявшиеся обозначения или аббревиатуры.
    if not has_cyrillic:
        normalized = re.sub(r"[^A-Za-z0-9]+", " ", raw).strip().casefold()
        if normalized in _ESTABLISHED_LATIN_TOPIC_NAMES:
            return True
        # Название из нескольких слов ("Network node") аббревиатурой быть
        # не может — проверяем каждое слово отдельно, все должны быть
        # аббревиатурами ("TCP IP", "IEEE 802.3").
        words = [w for w in re.split(r"\s+", raw) if re.search(r"[A-Za-z]", w)]
        return bool(words) and all(_latin_part_is_established(w) for w in words)

    # Смешанное название: каждая латинская часть должна быть допустимой.
    # Исключение: одиночная латинская буква (классификатор вида "Класс B",
    # "Уровень N", "Тип A") — не английское слово, пропускаем.
    for part in latin_parts:
        token = part.replace("-", " ").replace("/", " ").strip().casefold()
        if len(token) == 1:
            continue
        if _latin_part_is_established(part):
            continue
        return False
    return True



def _translate_item_to_russian(item: Dict) -> Dict:
    """
    Отдельный, прицельный вызов Gemini: переводит уже сгенерированную тему
    на русский, если основной проход правило 6 проигнорировал. Оригинальное
    название (если оно было переведено) уходит в aliases — иначе ссылки на
    эту тему из текста на языке оригинала перестанут резолвиться.
    """
    prompt = f"""
Атомарная Obsidian-заметка ниже написана не на русском языке (или сильно
смешана с другим языком, включая НАЗВАНИЕ темы), а по правилам все атомарные
файлы должны быть на русском.

Тема: {item['topic']}
Текст:
{item['content']}

Перепиши:
- переведи текст ПО СМЫСЛУ на русский, не копируй оригинальные предложения;
- ОБЯЗАТЕЛЬНО переведи и название темы, если оно не на русском — это касается
  ОБЫЧНЫХ переводимых слов ("Router" -> "Маршрутизатор", "Switch" ->
  "Коммутатор", "Network node" -> "Сетевой узел", "Peripheral devices" ->
  "Периферийные устройства", "End devices" -> "Оконечные устройства"). Не
  прикрывайся "это термин" для обычных слов, у которых есть стандартный
  русский эквивалент в любом учебнике по сетям/программированию;
- ОСОБЫЙ СЛУЧАЙ (часто встречается и ЛЕГКО ПРОПУСКАЕТСЯ): название может
  быть НЕ английским словом, а русским словом/фразой, ЗАПИСАННЫМ ЛАТИНСКИМИ
  БУКВАМИ ("транслитом") — например "Vychislitelnaya moshnost" (это
  "Вычислительная мощность"), "Globalnaya set" (это "Глобальная сеть"), "SCS"
  как транслит русской аббревиатуры "СКС" (Структурированная Кабельная
  Система) — а НЕ настоящий международный термин. Такие названия тоже
  ОБЯЗАТЕЛЬНО нужно вернуть на нормальный русский (кириллицей), а не оставлять
  латиницей под видом "аббревиатуры";
- НЕ переводи только НАСТОЯЩИЕ международные аббревиатуры/протоколы/стандарты,
  которые реально используются в этом виде в русскоязычной литературе без
  перевода (TCP/IP, WAN, LAN, MAC, XSD, SOAP, HTTP, USB, IEEE 802.3, ARPANET) —
  их оставь как есть. Проверяй себя вопросом: "используют ли ЭТО СЛОВО ИМЕННО
  В ТАКОМ ВИДЕ русскоязычные инженеры/учебники, или это просто латиница вместо
  кириллицы для русского понятия?" — если сомневаешься, переводи;
- если название темы было переведено/исправлено — оригинальное написание
  верни в aliases, иначе ссылки на эту тему из текста на языке оригинала
  (или из старого транслита) перестанут находить файл;
- ничего не выдумывай сверх исходного текста, ничего содержательного не теряй.

Верни СТРОГО JSON-объект:
{{
  "topic": "Название на русском (или настоящая аббревиатура/протокол)",
  "aliases": ["Оригинальное название, если переводили"],
  "content": "Текст на русском"
}}
"""
    system_instruction = (
        "Ты переводишь и нормализуешь атомарные заметки Obsidian на русский "
        "язык. Переводишь ОБЫЧНЫЕ слова и составные термины ('Network node', "
        "'Router', 'Peripheral devices') всегда — 'это техническое слово' не "
        "повод оставить его на английском. Отдельно проверяешь, не является ли "
        "латинское название транслитом русского слова ('SCS' вместо 'СКС', "
        "'Globalnaya set' вместо 'Глобальная сеть') — такое тоже переводишь на "
        "кириллицу. Не переводишь только настоящие международные "
        "аббревиатуры/протоколы, реально принятые в русской литературе "
        "без перевода (TCP/IP, WAN, MAC, ARPANET). Не теряешь фактов. "
        "Отвечаешь строго JSON."
    )

    result = _gemini_json(prompt, system_instruction)

    if not isinstance(result, dict):
        return item

    new_topic = str(result.get("topic", "")).strip() or item["topic"]
    new_content = str(result.get("content", "")).strip() or item["content"]

    extra_aliases = result.get("aliases", [])
    if not isinstance(extra_aliases, list):
        extra_aliases = []

    merged_aliases = list(item.get("aliases", []))
    for alias in extra_aliases:
        alias = str(alias).strip()
        if alias and alias not in merged_aliases:
            merged_aliases.append(alias)

    if item["topic"] != new_topic and item["topic"] not in merged_aliases:
        merged_aliases.append(item["topic"])

    updated = dict(item)
    updated["topic"] = new_topic
    updated["content"] = new_content
    updated["aliases"] = merged_aliases

    # Имя файла раньше оставалось от старого (английского) topic — теперь
    # пересобираем его из нового русского названия, иначе тема переведена,
    # а .md-файл всё равно называется "Router.md".
    if new_topic != item["topic"]:
        updated["filename"] = _safe_output_filename(new_topic)

    return updated


_LATIN_TO_CYRILLIC_MULTI = [
    ("shch", "щ"), ("Shch", "Щ"), ("SHCH", "Щ"),
    ("sch", "щ"), ("Sch", "Щ"),
    ("iya", "ия"), ("Iya", "Ия"),
    ("iye", "ие"), ("Iye", "Ие"),
    ("yu", "ю"), ("Yu", "Ю"), ("YU", "Ю"),
    ("ya", "я"), ("Ya", "Я"), ("YA", "Я"),
    ("yo", "ё"), ("Yo", "Ё"),
    ("zh", "ж"), ("Zh", "Ж"), ("ZH", "Ж"),
    ("kh", "х"), ("Kh", "Х"), ("KH", "Х"),
    ("ts", "ц"), ("Ts", "Ц"), ("TS", "Ц"),
    ("ch", "ч"), ("Ch", "Ч"), ("CH", "Ч"),
    ("sh", "ш"), ("Sh", "Ш"), ("SH", "Ш"),
    ("ye", "е"), ("Ye", "Е"),
    ("iy", "ий"), ("Iy", "Ий"),
    ("yy", "ый"),
    ("'", ""), ("’", ""), ("ʹ", ""),
]

_LATIN_TO_CYRILLIC_SINGLE = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е",
    "z": "з", "i": "и", "j": "й", "y": "ы", "k": "к", "l": "л",
    "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с",
    "t": "т", "u": "у", "f": "ф", "h": "х", "c": "ц", "q": "к",
    "w": "в", "x": "кс",
    "A": "А", "B": "Б", "V": "В", "G": "Г", "D": "Д", "E": "Е",
    "Z": "З", "I": "И", "J": "Й", "Y": "Ы", "K": "К", "L": "Л",
    "M": "М", "N": "Н", "O": "О", "P": "П", "R": "Р", "S": "С",
    "T": "Т", "U": "У", "F": "Ф", "H": "Х", "C": "Ц", "Q": "К",
    "W": "В", "X": "Кс",
}


def _transliterate_latin_to_cyrillic(text: str) -> str:
    """
    Детерминированная (не через LLM) транслитерация латиницы в кириллицу —
    ГАРАНТИРОВАННЫЙ последний рубеж на случай, когда Gemini дважды подряд
    не замечает, что название темы — это не английское слово, а русская
    фраза, просто записанная латиницей ("Topologiya_seti" вместо
    "Топология сети"). Не претендует на идеальную грамотность (это не
    перевод, а механическая транслитерация), зато гарантированно убирает
    латиницу вместо того, чтобы молча её оставить.
    """
    normalized = re.sub(r"[_\-]+", " ", text)
    result = []
    i = 0
    n = len(normalized)

    while i < n:
        matched = False
        for pattern, repl in _LATIN_TO_CYRILLIC_MULTI:
            if normalized[i:i + len(pattern)] == pattern:
                result.append(repl)
                i += len(pattern)
                matched = True
                break
        if matched:
            continue
        ch = normalized[i]
        result.append(_LATIN_TO_CYRILLIC_SINGLE.get(ch, ch))
        i += 1

    return re.sub(r"\s+", " ", "".join(result)).strip()


def _enforce_russian_language(item: Dict) -> Dict:
    """
    Жёсткий постпроцессор rule 6.

    Важные инварианты:
    1) каноническое имя темы определяется из topic, а НЕ из filename Gemini;
    2) обычные английские слова в названии запрещены;
    3) известные сетевые английские названия принудительно получают русское имя;
    4) если модель не перевела название, файл не должен молча выйти английским;
    5) оригинальное название сохраняется как скрытый alias для старых ссылок.
    """
    updated = dict(item)
    original_topic = str(updated.get("topic", "")).strip()
    topic = _canonicalize_topic_name(original_topic)

    aliases = [
        str(alias).strip()
        for alias in updated.get("aliases", [])
        if str(alias).strip()
    ]

    if topic and topic != original_topic and original_topic not in aliases:
        aliases.append(original_topic)

    updated["topic"] = topic
    updated["aliases"] = aliases

    content_ok = _is_mostly_russian(updated.get("content", ""))
    topic_ok = _topic_is_russian_or_established(topic)

    # Даже если topic уже русский, filename НИКОГДА не берём из ответа Gemini.
    # Имя файла всегда строится из канонического topic.
    def _sync_filename(obj: Dict) -> Dict:
        obj = dict(obj)
        obj["filename"] = _safe_output_filename(obj.get("topic", ""))
        return obj

    updated = _sync_filename(updated)

    if topic_ok and content_ok:
        return updated

    # Первый проход: смысловой перевод через Gemini.
    try:
        translated = _translate_item_to_russian(updated)
    except Exception as exc:
        translated = updated
        _log_change("LANGUAGE_TRANSLATE_FAILED", f"{original_topic!r}: {exc}")

    translated = dict(translated)
    translated_topic_before_map = str(translated.get("topic", "")).strip()
    translated_topic = _canonicalize_topic_name(translated_topic_before_map)

    merged_aliases = list(updated.get("aliases", []))
    for alias in translated.get("aliases", []) or []:
        alias = str(alias).strip()
        if alias and alias not in merged_aliases:
            merged_aliases.append(alias)

    if translated_topic_before_map and translated_topic_before_map != translated_topic:
        if translated_topic_before_map not in merged_aliases:
            merged_aliases.append(translated_topic_before_map)

    translated["topic"] = translated_topic
    translated["aliases"] = merged_aliases
    translated = _sync_filename(translated)

    if (
        _topic_is_russian_or_established(translated["topic"])
        and _is_mostly_russian(translated.get("content", ""))
    ):
        _log_change("LANGUAGE_AUTO_TRANSLATED", translated["topic"])
        return translated

    # Второй, специально более строгий запрос.
    try:
        retried = _translate_item_to_russian(translated)
        retried = dict(retried)
        retried_original = str(retried.get("topic", "")).strip()
        retried_topic = _canonicalize_topic_name(retried_original)

        retried_aliases = list(merged_aliases)
        for alias in retried.get("aliases", []) or []:
            alias = str(alias).strip()
            if alias and alias not in retried_aliases:
                retried_aliases.append(alias)

        if retried_original and retried_original != retried_topic and retried_original not in retried_aliases:
            retried_aliases.append(retried_original)

        retried["topic"] = retried_topic
        retried["aliases"] = retried_aliases
        retried = _sync_filename(retried)

        if _topic_is_russian_or_established(retried["topic"]):
            translated = retried
    except Exception as exc:
        _log_change("LANGUAGE_TRANSLATE_RETRY_FAILED", f"{original_topic!r}: {exc}")

    # Последний детерминированный рубеж только для русских слов, записанных
    # латиницей. Для неизвестного настоящего английского слова нельзя
    # безопасно "угадывать" перевод транслитерацией — в таком случае лучше
    # остановить создание конкретной атомарки, чем записать английскую тему.
    topic_now = str(translated.get("topic", "")).strip()
    if not _topic_is_russian_or_established(topic_now):
        mapped = _canonicalize_topic_name(topic_now)
        if mapped != topic_now:
            translated["topic"] = mapped
        else:
            looks_transliterated = (
                "_" in topic_now
                or "-" in topic_now
            )
            if looks_transliterated and re.search(r"[A-Za-z]", topic_now):
                cyrillic_topic = _transliterate_latin_to_cyrillic(topic_now).strip()
                if re.search(r"[А-Яа-яЁё]", cyrillic_topic):
                    if topic_now not in translated.get("aliases", []):
                        translated["aliases"] = list(translated.get("aliases", [])) + [topic_now]
                    translated["topic"] = cyrillic_topic

    translated = _sync_filename(translated)

    # МЯГКИЙ финал вместо RuntimeError. Раньше здесь стояло жёсткое
    # исключение: одна неопознанная тема (например, аббревиатура) обрушивала
    # split_note_with_ai для ВСЕГО исходника, и лекция не давала ни одной
    # атомарки. Все попытки русификации выше (override-словарь, два прохода
    # через Gemini, детерминированная транслитерация) выполняются как и
    # раньше; если после них тема всё же осталась латиницей — сохраняем её
    # как есть, оригинал остаётся в aliases, а vault-wide проверка
    # (repair_english_atomic_topics_vault_wide) попробует ещё раз позже.
    if not _topic_is_russian_or_established(translated.get("topic", "")):
        _log_change(
            "LANGUAGE_SOFT_ACCEPT_TOPIC",
            f"{original_topic!r} -> {translated.get('topic', original_topic)!r}",
        )

    if not _is_mostly_russian(translated.get("content", "")):
        _log_change(
            "LANGUAGE_SOFT_ACCEPT_CONTENT",
            f"{translated.get('topic', original_topic)!r}",
        )

    _log_change("LANGUAGE_ENFORCED", translated.get("topic", original_topic))
    return translated


def _enforce_russian_language_safe(item: Dict) -> Dict:
    """
    Обёртка над _enforce_russian_language: любая неожиданная ошибка
    русификации ОДНОЙ темы не должна ронять весь исходник. В худшем случае
    тема проходит без правки (оригинальное название — в aliases).
    """
    try:
        return _enforce_russian_language(item)
    except Exception as exc:
        _log_change(
            "LANGUAGE_ENFORCE_FAILED",
            f"{str(item.get('topic', ''))!r}: {exc}",
        )
        return item



def _strip_self_links(item: Dict) -> Dict:
    """
    Защита от промаха модели: иногда она всё равно оформляет [[Тема]] внутри
    файла с той же темой (обычно на первом упоминании собственного
    названия, буквально следуя правилу "ссылка при первом упоминании
    понятия"). Ссылка на саму себя бесполезна и, что хуже, малейшее
    расхождение форм записи (регистр, вид дефиса, лишний пробел) превращает
    её в "пустую ссылку" — Obsidian создаёт заглушку рядом вместо перехода
    в тот же файл. Снимаем скобки, оставляя видимый текст как обычную прозу.
    """
    topic_key = _normalize_topic_key(item.get("topic", ""))
    content = item.get("content", "")

    if not topic_key or not content:
        return item

    def _replace(match: re.Match) -> str:
        target = match.group(1)
        display = match.group(2) if match.group(2) is not None else target
        if _normalize_topic_key(target) == topic_key:
            return display
        return match.group(0)

    new_content = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", _replace, content)

    if new_content == content:
        return item

    updated = dict(item)
    updated["content"] = new_content
    return updated


def _normalize_generated_item(item) -> Dict | None:
    """Нормализует ответ Gemini. filename полностью контролируется кодом."""
    if not isinstance(item, dict):
        return None

    topic = str(item.get("topic", "")).strip()
    # Никогда не доверяем Gemini в части имени файла.
    topic = _canonicalize_topic_name(topic)
    filename = _safe_output_filename(topic)

    tags = item.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    tags = [str(tag).strip() for tag in tags if str(tag).strip()]

    aliases = item.get("aliases", [])
    if not isinstance(aliases, list):
        aliases = []
    aliases = [str(alias).strip() for alias in aliases if str(alias).strip()]

    related_topics = item.get("related_topics", [])
    if not isinstance(related_topics, list):
        related_topics = []
    related_topics = [
        str(topic_name).strip()
        for topic_name in related_topics
        if str(topic_name).strip()
    ]

    body = str(item.get("content", "")).strip()

    if not topic or not body:
        return None

    return {
        "topic": topic,
        "filename": filename,
        "aliases": aliases,
        "tags": tags,
        "related_topics": related_topics,
        "content": body,
    }



def _normalize_topic_key(text: str) -> str:
    # БАГ: ссылки/имена файлов часто через подчёркивание или дефис
    # ("Адресация_узлов_сети", "Network-node"), а в естественном тексте
    # источника то же самое пишется с пробелом ("Адресация узлов сети").
    # Без этой нормализации такие пары считались РАЗНЫМИ темами — отсюда
    # и ложные "висячие" ссылки на реально существующие файлы, и дубли.
    text = re.sub(r"[_\-]+", " ", text)
    return re.sub(r"\s+", " ", text.strip().casefold())


def _build_loose_phrase_pattern(phrase: str) -> re.Pattern:
    """
    Та же проблема (подчёркивание/дефис vs пробел), но для поиска ПО СЫРОМУ
    ТЕКСТУ, а не по нормализованным ключам: имя файла/ссылка может быть
    "Адресация_узлов_сети", а естественный текст источника пишет то же самое
    как "адресация узлов сети". Ищем фразу, трактуя пробел/подчёркивание/
    дефис как взаимозаменяемые разделители слов.
    """
    words = [w for w in re.split(r"[\s_\-]+", phrase.strip()) if w]
    if not words:
        return re.compile(r"(?!x)x")  # заведомо непроходимый паттерн для пустой фразы
    return re.compile(
        r"[\s_\-]+".join(re.escape(word) for word in words),
        flags=re.IGNORECASE,
    )


def _unique_output_path(folder: Path, filename: str, reserved: Set[Path]) -> Path:
    filename = _safe_output_filename(filename)
    base = folder / filename

    if (
        not is_diary(base)
        and not is_date_named(base)
        and base.resolve() not in reserved
        and not base.exists()
    ):
        return base

    stem = base.stem
    suffix = base.suffix
    number = 2

    while True:
        candidate = folder / f"{stem}-{number}{suffix}"

        if candidate.resolve() not in reserved and not candidate.exists():
            return candidate

        number += 1


def _render_tags(tags: List[str]) -> str:
    clean = []

    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue

        if not tag.startswith("#"):
            tag = "#" + tag

        tag = re.sub(r"\s+", "-", tag)
        tag = re.sub(r"[^\w#\-А-Яа-яЁё]", "", tag)

        if tag and tag not in clean:
            clean.append(tag)

    if "#STUDY" in clean:
        clean.remove("#STUDY")

    return " ".join(clean)


def _render_note(
    item: Dict,
    link_targets: List[Tuple[str, Path]],
    source_path: Path = None,
    subtopic_targets: List[Tuple[str, Path]] = None,
    parent_target: Tuple[str, Path] = None,
) -> str:
    tags_line = _render_tags(item.get("tags", []))

    parts = [AI_GENERATED_MARKER]

    # Скрытая метка синонимов используется механикой дедупликации и не
    # обязана совпадать 1-в-1 с видимым списком ниже.
    dedup_aliases = list(item.get("_dedup_aliases", []) or item.get("aliases", []))
    for inferred_alias in _extract_inline_topic_aliases(item.get("content", ""), item.get("topic", "")):
        if inferred_alias and inferred_alias not in dedup_aliases:
            dedup_aliases.append(inferred_alias)
    if dedup_aliases:
        clean_dedup_aliases = []
        for alias in dedup_aliases:
            alias = str(alias).strip().replace("|", " ")
            if alias and alias not in clean_dedup_aliases:
                clean_dedup_aliases.append(alias)
        if clean_dedup_aliases:
            parts.append(f"{AI_TOPIC_ALIASES_PREFIX}{'|'.join(clean_dedup_aliases)} -->")

    if tags_line:
        parts.append(tags_line)

    parts.append(f"# {item['topic']}")

    # Видимые синонимы тоже держим русскими. Оригинальные английские/транслитовые
    # варианты остаются только в скрытом AI_TOPIC_ALIASES_PREFIX, чтобы старые
    # ссылки продолжали резолвиться, но не торчали в тексте заметки.
    visible_aliases = []
    topic_key = _normalize_topic_key(item.get("topic", ""))
    for alias in item.get("aliases", []):
        alias = str(alias).strip()
        if not alias or _normalize_topic_key(alias) == topic_key:
            continue
        if not _topic_is_russian_or_established(alias):
            mapped_alias = _canonicalize_topic_name(alias)
            if mapped_alias != alias:
                alias = mapped_alias
        if (
            alias
            and _topic_is_russian_or_established(alias)
            and _normalize_topic_key(alias) != topic_key
            and alias not in visible_aliases
        ):
            visible_aliases.append(alias)

    if visible_aliases:
        parts.append("")
        parts.append(f"_Также известно как: {', '.join(visible_aliases)}_")

    parts.append("")
    parts.append(item["content"].strip())

    # Rule 4.2: связь податомарного файла с основной темой.
    if parent_target is not None:
        parent_name, parent_path = parent_target
        parts.append("")
        parts.append(f"Основная тема: [[{parent_path.stem}]]")

    if source_path is not None:
        parts.append("")
        parts.append(f"Источник (полный конспект): [[{source_path.stem}]]")

    # Rule 4.1/4.2: структурные ссылки на подтемы — отдельным списком,
    # это нормально для иерархии "тема -> подтемы", а не "упоминание вскользь".
    if subtopic_targets:
        parts.append("")
        parts.append("## Подтемы")
        for _, target in subtopic_targets:
            parts.append(f"- [[{target.stem}]]")

    # Rule 3.3/8: "Связанные файлы" — только для тех связей, которые не
    # удалось естественно встроить как [[wikilink]] прямо в текст темы.
    body_for_check = item["content"]
    extra_links = [
        (name, target)
        for name, target in link_targets
        if f"[[{target.stem}]]" not in body_for_check and f"[[{target.stem}|" not in body_for_check
    ]

    if extra_links:
        parts.append("")
        parts.append("## Связанные файлы")

        for _, target in extra_links:
            parts.append(f"- [[{target.stem}]]")

    return "\n".join(parts).strip() + "\n"


def _apply_recursive_cross_link_pass(
    created_files: List[Path],
    global_topic_lookup: Dict[str, Path],
    progress_callback: Optional[Callable[[str], None]] = None,
) -> int:
    """
    Rule 5: рекурсивный проход по графу вновь созданных в этом запуске файлов.
    Ищет в тексте каждого файла упоминания ДРУГИХ известных тем, которые ещё
    не оформлены как [[wikilink]], и подставляет ссылку в место первого
    упоминания (rule 3). Ничего не удаляет и не создаёт новых файлов —
    только добавляет ссылки там, где смысловая связь уже есть в тексте.
    """
    if not created_files:
        return 0

    if progress_callback is not None:
        try:
            progress_callback(
                f"Прохожу по связям между {len(created_files)} созданными файлами (rule 5)..."
            )
        except Exception:
            pass

    # Сначала длинные/конкретные названия тем, чтобы "XML Schema" не
    # проглатывалось более коротким совпадением "XML". Матчим не только по
    # имени файла, но и по алиасам (rule 6.5/2.1) — иначе переведённая на
    # русский тема никогда не подхватит своё упоминание оригинальным
    # (нерусским) названием в исходном тексте.
    unique_paths = {path.resolve(): path for path in global_topic_lookup.values()}
    topic_entries: List[Tuple[str, Path, str]] = []  # (display_name_для_поиска, файл, каноническое_имя)

    for target_path in unique_paths.values():
        canonical = target_path.stem
        topic_entries.append((canonical, target_path, canonical))

        try:
            note_text = _read_note(target_path)
        except Exception:
            note_text = ""

        for alias_key in _extract_topic_aliases(note_text):
            if alias_key and alias_key != canonical.casefold():
                topic_entries.append((alias_key, target_path, canonical))

    topic_entries.sort(key=lambda entry: len(entry[0]), reverse=True)

    updated = 0

    for file_path in created_files:
        if _is_protected_note(file_path):
            continue
        try:
            text = _read_note(file_path)
        except Exception:
            continue

        existing_links = {
            _normalize_topic_key(name)
            for name in extract_links(text)
            if _normalize_topic_key(name)
        }
        new_text = text
        changed = False

        for display_name, target_path, canonical_name in topic_entries:
            if target_path.resolve() == file_path.resolve():
                continue
            canonical_key = _normalize_topic_key(canonical_name)
            display_key = _normalize_topic_key(display_name)
            if canonical_key in existing_links or display_key in existing_links:
                continue
            if len(display_name) < 3:
                continue

            phrase_pattern = _build_loose_phrase_pattern(display_name).pattern
            pattern = re.compile(
                r"(?<![\[\wА-Яа-яЁё])" + phrase_pattern + r"(?![\]\wА-Яа-яЁё])",
                flags=re.IGNORECASE,
            )
            match = pattern.search(new_text)
            if not match:
                continue

            start, end = match.span()

            # ПО ТРЕБОВАНИЮ: никаких pipe-ссылок [[Файл|текст]] — только
            # чистое [[Тема]]. Раньше pipe использовался, чтобы сохранить
            # видимый текст при совпадении по алиасу — но это ЖЕ было
            # причиной, почему английские слова продолжали появляться в
            # тексте, даже когда сам файл давно переименован на русский
            # ([[Маршрутизатор|Router]] всё ещё ПОКАЗЫВАЕТ "Router"). Теперь
            # подставляем канонический русский текст темы целиком.
            replacement = f"[[{canonical_name}]]"

            new_text = new_text[:start] + replacement + new_text[end:]
            existing_links.add(canonical_key)
            changed = True

        if changed and new_text != text:
            try:
                _write_note_atomic(file_path, new_text)
                _log_change(
                    "RECURSIVE_CROSS_LINK",
                    str(file_path.relative_to(get_vault_path())),
                )
                updated += 1
            except Exception:
                continue

    return updated


def _collect_topic_excerpts(
    target_name: str,
    original_text_by_source: Dict[Path, str],
    extra_texts: List[Tuple[str, str]] = None,
    window: int = 500,
    max_excerpts_per_source: int = 3,
) -> List[Tuple[str, str]]:
    """
    Ищет упоминания target_name во ВСЕХ первоисточниках текущего запуска
    (не только в том файле, где встретилась ссылка) и вырезает вокруг каждого
    совпадения окно текста — сырой материал для досоздания атомарного файла.

    extra_texts — дополнительный материал сверх сырых исходников: содержимое
    уже созданных в этом запуске атомарных файлов. Тема иногда реально
    раскрыта не в исходном конспекте дословно, а в прозе другого атомарного
    файла (например, мимоходом объяснена внутри соседней темы) — раньше
    поиск смотрел только в сыром тексте источника и такие случаи упускал,
    из-за чего реально находимая в тексте информация не подхватывалась.
    """
    if not target_name.strip():
        return []

    pattern = _build_loose_phrase_pattern(target_name)
    excerpts: List[Tuple[str, str]] = []

    for source_path, text in original_text_by_source.items():
        matches = list(pattern.finditer(text))[:max_excerpts_per_source]
        for match in matches:
            start = max(0, match.start() - window)
            end = min(len(text), match.end() + window)
            snippet = text[start:end].strip()
            if snippet:
                excerpts.append((source_path.name, snippet))

    for label, text in (extra_texts or []):
        matches = list(pattern.finditer(text))[:max_excerpts_per_source]
        for match in matches:
            start = max(0, match.start() - window)
            end = min(len(text), match.end() + window)
            snippet = text[start:end].strip()
            if snippet:
                excerpts.append((label, snippet))

    return excerpts


def _generate_missing_atomic_topic(
    target_name: str,
    excerpts: List[Tuple[str, str]],
) -> Dict | None:
    """
    Rule 3.2 наоборот: собирает ОДИН атомарный файл под тему, на которую уже
    сослались как на [[target_name]], строго из отрывков первоисточников
    этого запуска. Если по отрывкам реального материала нет — Gemini честно
    отвечает found:false, и файл не создаётся (не выдумываем факты).
    """
    excerpt_text = "\n\n".join(
        f"--- Отрывок из {source_name} ---\n{snippet}"
        for source_name, snippet in excerpts[:12]
    )

    prompt = f"""
На тему "{target_name}" уже сослались как на [[{target_name}]] из других
атомарных файлов, но отдельного файла для неё пока нет. Ниже — ВСЕ отрывки,
где эта тема встречается в первоисточниках текущего запуска:

{excerpt_text}

Если в отрывках выше ДЕЙСТВИТЕЛЬНО есть содержательная информация именно по
теме "{target_name}" (определение, объяснение, структура и т.п.), а не только
случайное упоминание слова без раскрытия — собери из неё ОДИН атомарный
Markdown-файл:
- пиши на русском языке (кроме устоявшихся терминов/аббревиатур без хорошего
  русского эквивалента);
- не выдумывай факты сверх того, что реально есть в отрывках;
- НЕ добавляй в content новые [[wikilinks]] — этот файл закрывает уже
  существующую голую ссылку, а не плодит новые связи.

Если содержательной информации по теме в отрывках нет (упоминание только
вскользь) — верни ровно {{"found": false}}.

Верни СТРОГО JSON-объект, без markdown-разметки вокруг:
{{
  "found": true,
  "topic": "Название темы",
  "filename": "Topic_name.md",
  "aliases": [],
  "tags": ["#tag1", "#tag2"],
  "related_topics": [],
  "content": "Markdown-текст на русском"
}}
"""

    system_instruction = (
        "Ты аккуратный редактор атомарной базы знаний Obsidian. Пишешь только "
        "на русском языке (кроме устоявшихся терминов), никогда не выдумываешь "
        "факты сверх предоставленных отрывков и честно отвечаешь found:false, "
        "если материала недостаточно. Соблюдай формат JSON буквально."
    )

    result = _gemini_json(prompt, system_instruction)

    if not isinstance(result, dict) or not result.get("found"):
        return None

    normalized = _normalize_generated_item(result)
    if normalized is None:
        return None

    normalized = _enforce_russian_language_safe(normalized)
    normalized = _strip_self_links(normalized)
    normalized["filename"] = _safe_output_filename(normalized["topic"])

    # ЖЁСТКОЕ ПРАВИЛО: ни одного пустого/тонкого атомарного файла. Gemini
    # иногда отвечает found:true на круговом материале (например, отрывки —
    # это просто соседние упоминания САМОЙ ссылки в других файлах, без
    # реального раскрытия темы) и выдаёт формально непустой, но фактически
    # пересказывающий одну фразу файл. Такое не считается "нашли материал" —
    # лучше честно снять ссылку, чем создать пустышку.
    if _meaningful_text_length(normalized.get("content", "")) < 120:
        return None

    return normalized


def _meaningful_text_length(text: str) -> int:
    """
    Длина "мяса" текста без служебных меток/заголовков/строк-из-одних-тегов —
    общий измеритель для двух разных проверок: "файл фактически пуст"
    (заглушка Obsidian) и "Gemini сказал found:true, но там кот наплакал"
    (круговая/тонкая генерация в repair-проходе).
    """
    meaningful = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    meaningful = re.sub(r"^#{1,6}\s.*$", "", meaningful, flags=re.MULTILINE)
    meaningful = re.sub(r"^\s*(#[^\s#]+\s*)+$", "", meaningful, flags=re.MULTILINE)
    return len(meaningful.strip())


def _is_effectively_empty_note(path: Path) -> bool:
    """
    Obsidian по умолчанию создаёт пустую .md-заглушку при переходе по
    несуществующей ссылке. Формально файл "существует", resolve_note его
    находит и ссылка выглядит рабочей (подсвечена, не красная) — но
    содержательного текста там нет. Именно это стояло за жалобой "ссылка
    вроде есть, а файл пустой": находясь якобы "резолвнутой", такая
    заглушка никогда не попадала в repair.
    """
    try:
        text = _read_note(path)
    except Exception:
        return True

    return _meaningful_text_length(text) < 40


def _replace_wikilinks_target(text: str, raw_target: str, replacement_target: str) -> Tuple[str, int]:
    """Заменяет [[target]] и [[target|display]] без чувствительности к регистру."""
    pattern = re.compile(
        r"\[\[" + re.escape(raw_target) + r"(?:\|[^\]]*)?\]\]",
        flags=re.IGNORECASE,
    )
    return pattern.subn(lambda _m: f"[[{replacement_target}]]", text)


def _remove_empty_wikilinks(text: str) -> str:
    """Убирает физически пустые wikilinks, включая варианты с пробелами/pipe."""
    text = re.sub(r"\[\[\s*(?:\|\s*)?\]\]", "", text)
    # Пустая markdown-ссылка без изображения.
    text = re.sub(r"(?<!\!)\[\s*\]\(\s*[^)]*\)", "", text)
    return text


def _atomic_topic_lookup(vault: Path) -> Dict[str, Path]:
    """Строит индекс canonical topic + filename + hidden aliases -> atomics."""
    lookup: Dict[str, Path] = {}
    for note, note_text in _iter_existing_atomic_notes(vault):
        heading_match = re.search(r"^#\s+(.+?)\s*$", note_text, flags=re.MULTILINE)
        topic_name = heading_match.group(1).strip() if heading_match else note.stem
        for key in (
            _normalize_topic_key(topic_name),
            _normalize_topic_key(note.stem),
            *_extract_topic_aliases(note_text),
        ):
            if key and key not in lookup:
                lookup[key] = note.resolve()
    return lookup


def _link_dangling_target_to_existing_file(
    existing_path: Path,
    target_name: str,
    new_item: Dict,
    referencing_files: List[Path],
    global_topic_lookup: Dict[str, Path],
) -> None:
    """
    Нашли, что тема уже реально раскрыта в другом файле хранилища
    (semantic-match) — не плодим новый файл, а: 1) добавляем текст ссылки
    (и переведённое имя темы, если оно другое) в алиасы существующего файла,
    чтобы ВПРЕДЬ такие ссылки сразу резолвились; 2) сами ссылки в
    ссылающихся файлах переводим на реальное (русское) имя файла — чистым
    [[Тема]], без pipe-синтаксиса.
    """
    try:
        existing_text = _read_note(existing_path)
    except Exception:
        existing_text = ""

    new_aliases = list(_extract_topic_aliases_display(existing_text))
    for candidate in (target_name, new_item.get("topic", "")):
        candidate = candidate.strip()
        if candidate and candidate not in new_aliases and _normalize_topic_key(candidate) != _normalize_topic_key(existing_path.stem):
            new_aliases.append(candidate)

    if new_aliases and existing_text:
        cleaned = re.sub(
            rf"^{re.escape(AI_TOPIC_ALIASES_PREFIX)}.*-->\s*\n?",
            "",
            existing_text,
            flags=re.MULTILINE,
        )
        alias_line = f"{AI_TOPIC_ALIASES_PREFIX}{'|'.join(new_aliases)} -->\n"

        if cleaned.startswith(AI_GENERATED_MARKER):
            insert_at = len(AI_GENERATED_MARKER)
            updated_text = cleaned[:insert_at] + "\n" + alias_line + cleaned[insert_at:].lstrip("\n")
        else:
            updated_text = alias_line + cleaned

        try:
            _write_note_atomic(existing_path, updated_text)
        except Exception:
            pass

    for ref_file in referencing_files:
        try:
            text = _read_note(ref_file)
            # Каноническая ссылка всегда чистая: [[РусскоеИмя]], без pipe.
            fixed, _ = _replace_wikilinks_target(text, target_name, existing_path.stem)
            fixed = _remove_empty_wikilinks(fixed)
            if fixed != text:
                _write_note_atomic(ref_file, fixed)
        except Exception:
            continue

    global_topic_lookup[_normalize_topic_key(target_name)] = existing_path
    global_topic_lookup[_normalize_topic_key(new_item.get("topic", ""))] = existing_path


def _repair_dangling_topic_links(
    created_files: List[Path],
    global_topic_lookup: Dict[str, Path],
    original_text_by_source: Dict[Path, str],
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[int, int, int]:
    """
    Нет смысла ссылаться [[X]] на атомарный файл X, который никогда не был
    создан — читатель попадёт в пустоту. После того как все файлы этого
    запуска записаны (и прошёл rule 5 — рекурсивная перелинковка), здесь мы:
      1. находим "висячие" [[ссылки]] среди файлов, созданных в этом запуске;
      2. для каждой уникальной темы ищем реальный материал по ВСЕМ
         первоисточникам этого запуска (_collect_topic_excerpts);
      3. если материал нашёлся — создаём для темы полноценный атомарный файл;
      4. если материала нигде нет — снимаем [[...]], чтобы не оставлять
         битую ссылку на несуществующий файл.
    Возвращает (создано_новых_файлов, привязано_к_существующим, разлинковано_битых_ссылок).

    Каждая уникальная висячая тема — это ЕЩЁ ОДИН вызов Gemini, поэтому на
    больших ветках с обилием перекрёстных ссылок этот проход может ощутимо
    прибавить времени к общей обработке — отсюда progress_callback.
    """
    def _report(message: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(message)
        except Exception:
            pass

    if not created_files:
        return 0, 0, 0

    dangling_targets: Dict[str, List[Path]] = {}
    empty_stub_paths: Dict[str, Path] = {}

    for file_path in created_files:
        if _is_protected_note(file_path):
            continue
        try:
            text = _read_note(file_path)
        except Exception:
            continue

        for target in extract_links(text):
            key = _normalize_topic_key(target)

            resolved_path = global_topic_lookup.get(key)

            if resolved_path is None:
                resolved_path = _find_existing_note_for_target_alias(
                    target, file_path, global_topic_lookup
                )

            if resolved_path is None:
                try:
                    resolved_path = resolve_note(target)
                except Exception:
                    resolved_path = None

            if resolved_path is not None:
                if not _is_effectively_empty_note(resolved_path):
                    if _normalize_topic_key(target) != _normalize_topic_key(resolved_path.stem):
                        try:
                            old_text = _read_note(file_path)
                            fixed, count = _replace_wikilinks_target(
                                old_text, target, resolved_path.stem
                            )
                            fixed = _remove_empty_wikilinks(fixed)
                            if count and fixed != old_text:
                                _write_note_atomic(file_path, fixed)
                        except Exception:
                            pass
                    global_topic_lookup.setdefault(key, resolved_path.resolve())
                    continue  # реально резолвится и там есть содержание

                # Резолвится, но это пустая заглушка (Obsidian создаёт такую
                # сама при клике на несуществующую ссылку) — считаем её
                # дальше как "висячую" и запоминаем путь, чтобы заполнить
                # РОВНО этот файл, а не наплодить рядом ещё один.
                empty_stub_paths[target] = resolved_path

            dangling_targets.setdefault(target, []).append(file_path)

    if not dangling_targets:
        return 0, 0, 0

    _report(f"Найдено висячих ссылок без файла: {len(dangling_targets)}. Ищу материал по каждой...")

    # Дополнительный материал для поиска: содержимое уже созданных в этом
    # запуске атомарных файлов (не только сырые первоисточники) — тема
    # иногда реально раскрыта в прозе соседней темы, а не дословно в
    # исходном конспекте.
    extra_texts: List[Tuple[str, str]] = []
    for created_path in created_files:
        try:
            extra_texts.append((created_path.name, _read_note(created_path)))
        except Exception:
            continue

    created_count = 0
    linked_existing_count = 0
    unlinked_count = 0

    _report(f"Проверяю {len(dangling_targets)} висячих ссылок и сопоставляю их с существующими темами...")

    for target_name, referencing_files in dangling_targets.items():
        excerpts = _collect_topic_excerpts(target_name, original_text_by_source, extra_texts=extra_texts)

        new_item = None
        if excerpts:
            try:
                new_item = _generate_missing_atomic_topic(target_name, excerpts)
            except Exception:
                new_item = None

        if new_item is not None:
            # ГЛАВНОЕ ТРЕБОВАНИЕ: прежде чем плодить новый файл, проверяем
            # ВСЁ хранилище (а не только темы этого запуска) на семантическое
            # совпадение — та же логика, что и обычная дедупликация при
            # генерации. Раньше висячая ссылка "[[Компьютерная сеть]]" не
            # находила уже существующий "Network.md" (нет точного совпадения
            # по имени/алиасу) и создавала рядом пустышку/дубль, хотя тема
            # уже реально раскрыта в другом файле хранилища.
            existing_match = _find_existing_atomic_match(
                new_item["content"],
                new_item["topic"],
                get_vault_path(),
            )

            if existing_match is not None:
                _link_dangling_target_to_existing_file(
                    existing_match, target_name, new_item, referencing_files, global_topic_lookup
                )
                _log_change(
                    "REPAIR_DANGLING_LINK_MATCHED_EXISTING",
                    f"[[{target_name}]] -> {existing_match.relative_to(get_vault_path())} "
                    f"(семантическое совпадение, новый файл не создан)",
                )
                linked_existing_count += 1
                continue

            existing_stub = empty_stub_paths.get(target_name)

            if existing_stub is not None:
                # Заполняем ту же самую заглушку, которую Obsidian уже
                # создал при клике на мёртвую ссылку — не плодим "Тема-2".
                output_path = existing_stub
            else:
                reserved = {p.resolve() for p in global_topic_lookup.values()}
                output_path = _unique_output_path(get_vault_path(), new_item["filename"], reserved)

            rendered = _render_note(new_item, [])
            _write_note_atomic(output_path, rendered)

            created_files.append(output_path)
            global_topic_lookup[_normalize_topic_key(new_item["topic"])] = output_path
            global_topic_lookup[_normalize_topic_key(target_name)] = output_path

            _log_change(
                "REPAIR_DANGLING_LINK_CREATED",
                f"{output_path.relative_to(get_vault_path())} <- [[{target_name}]] "
                f"из {len(referencing_files)} файл(ов)",
            )
            created_count += 1

            # Каноническое имя файла может отличаться от текста ссылки
            # (например, модель линковала синонимом) — правим сами ссылки.
            if _normalize_topic_key(new_item["topic"]) != _normalize_topic_key(target_name):
                for ref_file in referencing_files:
                    try:
                        text = _read_note(ref_file)
                        fixed, _ = _replace_wikilinks_target(
                            text, target_name, output_path.stem
                        )
                        fixed = _remove_empty_wikilinks(fixed)
                        if fixed != text:
                            _write_note_atomic(ref_file, fixed)
                    except Exception:
                        continue
        else:
            for ref_file in referencing_files:
                try:
                    text = _read_note(ref_file)
                    fixed, count = _replace_wikilinks_target(text, target_name, target_name)
                    # [[X|X]] / любые варианты, для которых нет файла, превращаем
                    # в обычный текст X вместо мёртвой Obsidian-ссылки.
                    display_pattern = re.compile(
                        r"\[\[" + re.escape(target_name) + r"(?:\|([^\]]*))?\]\]",
                        flags=re.IGNORECASE,
                    )
                    fixed, _ = display_pattern.subn(
                        lambda m: (m.group(1) or target_name).strip(),
                        fixed,
                    )
                    fixed = _remove_empty_wikilinks(fixed)
                    if fixed != text:
                        _write_note_atomic(ref_file, fixed)
                        unlinked_count += max(1, count)
                except Exception:
                    continue

            _log_change(
                "REPAIR_DANGLING_LINK_REMOVED",
                f"[[{target_name}]] — материала не нашлось ни в одном первоисточнике этого запуска",
            )

    return created_count, linked_existing_count, unlinked_count


def _render_full_conspectus(original_path: Path, original_text: str, created_files: List[Path]) -> str:
    """
    Сохраняет исходный конспект целиком. Ссылки на атомарные темы теперь
    ТОЛЬКО инлайновые — вставляются прямо в текст на месте упоминания через
    rule 5 (см. _apply_recursive_cross_link_pass), а не отдельным списком
    внизу файла: по прямому требованию — "всё должно быть в тексте как
    гиперссылки", а не как индекс после конспекта.

    Помечает файл FULL_CONSPECTUS_MARKER — это ЕДИНСТВЕННОЕ, что защищает
    такие файлы от повторного дробления при следующих запусках split
    (см. is_full_conspectus и проверку в split_note_tree).
    """
    text = original_text.strip()

    # Убираем нашу же метку, если она уже стоит (например, при повторном
    # запуске починки/дедупа) — чтобы не задваивалась.
    text = text.replace(FULL_CONSPECTUS_MARKER, "").strip()

    # Убираем СТАРЫЙ индекс атомарных тем, если конспект был обработан ДО
    # этого правила (он больше не нужен — все ссылки теперь внутри текста).
    text = re.split(r"(?m)^##\s+Атомарные темы\s*$", text, maxsplit=1)[0].rstrip()

    if not re.search(r"(?<!\w)#STUDY\b", text, flags=re.IGNORECASE):
        text = f"#STUDY\n\n{text}"

    return f"{FULL_CONSPECTUS_MARKER}\n{text}".strip() + "\n"


# ============================================================
# ОСНОВНАЯ ОПЕРАЦИЯ: РАЗДРОБИТЬ
# ============================================================

def _protected_reason(path: Path, diary_linked: Set[Path]) -> str:
    resolved = path.resolve()
    if is_diary(resolved):
        return "Diary.md"
    if is_date_named(resolved):
        return "имя содержит дату YYYY-MM-DD"
    if resolved in diary_linked:
        return "в цепочке ссылок из Diary.md"
    if _is_protected_note(resolved):
        return "тег #PROTECTED"
    return ""


def _diagnose_root_links(root: Path) -> List[str]:
    """
    Построчная диагностика по каждой ссылке в корневом файле: удалось ли её
    разрешить, ведёт ли она на .md или на вложение (и получилось ли из него
    извлечь текст). Нужна, чтобы не гадать вслепую, почему граф пуст.
    """
    try:
        text = _read_note(root)
    except Exception as exc:
        return [f"не удалось прочитать файл: {exc}"]

    raw_links = extract_links(text)
    if not raw_links:
        return ["в файле не найдено ни одной ссылки [[...]] или [текст](...)"]

    lines = []

    for raw in raw_links:
        try:
            target = resolve_link(raw, root)
        except FileNotFoundError as exc:
            lines.append(f"[[{raw}]] → не найдено: {exc}")
            continue

        if target.suffix.lower() == ".md":
            status = "OK" if target.exists() else "путь разрешился, но файла нет"
            lines.append(f"[[{raw}]] → {status}: {target.name}")
            continue

        try:
            materialized = _materialize_attachment_source(target)
            lines.append(
                f"[[{raw}]] → вложение {target.name}, текст извлечён в {materialized.name}"
            )
        except Exception as exc:
            lines.append(f"[[{raw}]] → вложение {target.name}, извлечь текст не удалось: {exc}")

    return lines


def split_note_tree(note_ref: str, progress_callback: Optional[Callable[[str], None]] = None) -> str:
    def _report(message: str) -> None:
        # Живой лог этапов для UI. Сам процесс не должен падать из-за
        # проблем с виджетом — поэтому любая ошибка колбэка проглатывается.
        if progress_callback is None:
            return
        try:
            progress_callback(message)
        except Exception:
            pass

    vault = get_vault_path()
    _log_change("SPLIT_REQUEST", f"root={note_ref}")
    _report(f"Ищу файл «{note_ref}» в хранилище...")

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    try:
        root = resolve_note(note_ref)
    except Exception as exc:
        return f"Не удалось найти исходный Obsidian-файл: {exc}"

    _report(f"Файл найден: {root.name}. Строю граф связанных ссылок...")

    diary_linked = collect_diary_linked_files()

    if is_always_protected(root, diary_linked):
        reason = _protected_reason(root, diary_linked)
        if reason:
            return f"Файл {root.name} защищён: {reason}."
        return f"Файл {root.name} защищён и не будет изменён."

    extraction_log: List[str] = []
    files_to_process = collect_link_graph(root, diary_linked, extraction_log=extraction_log)

    if not files_to_process:
        return "После применения защиты Diary/date не осталось файлов для обработки."

    _report(f"В ветке найдено файлов для проверки: {len(files_to_process)}.")

    protected_skipped = 0
    protected_files: Set[Path] = set()
    graph_scan = [(root.resolve(), 0)]
    graph_seen: Set[Path] = set()

    while graph_scan:
        current, depth = graph_scan.pop(0)

        if current in graph_seen:
            continue

        graph_seen.add(current)

        if depth > 0 and is_always_protected(current, diary_linked):
            protected_skipped += 1
            protected_files.add(current.resolve())
            continue

        if depth >= MAX_LINK_DEPTH or not current.exists():
            continue

        for _, target in _read_links(current):
            if target not in graph_seen:
                graph_scan.append((target, depth + 1))

    created_files: List[Path] = []
    processed_files: List[Path] = []
    study_files: List[Path] = []
    failures: List[str] = []

    available_topics: List[str] = []
    root_skip_reason = None

    # Сначала помечаем link-only файлы и не отдаем их на дробление.
    # Также пропускаем файлы, которые сам ИИ уже создал как атомарные темы
    # при прошлом дроблении — их нельзя дробить повторно, даже если на них
    # случайно сослалась другая лекция.
    informative_files = []
    original_text_by_source: Dict[Path, str] = {}
    already_generated_skipped = 0
    link_only_skipped = 0

    for path in files_to_process:
        is_root = path.resolve() == root.resolve()
        try:
            text = _read_note(path)

            if is_ai_generated_topic(text):
                already_generated_skipped += 1
                if is_root:
                    root_skip_reason = (
                        "этот файл уже является атомарной темой, сгенерированной ИИ ранее, "
                        "и не дробится повторно"
                    )
                continue

            if is_full_conspectus(text):
                already_generated_skipped += 1
                if is_root:
                    root_skip_reason = (
                        "этот файл уже является ПОЛНЫМ КОНСПЕКТОМ после прошлого дробления "
                        "и не дробится повторно — иначе Gemini заново генерировал бы атомарные "
                        "темы с чуть другими названиями, и вместо переиспользования плодились бы "
                        "дубли вроде 'Тема-2', 'Тема-3'"
                    )
                continue

            if is_link_only_note(text):
                link_only_skipped += 1
                if ensure_study_tag(path):
                    study_files.append(path)
                if is_root:
                    root_skip_reason = (
                        "в файле нет собственного текста — он состоит только из ссылок/заголовков "
                        "(похож на файл-оглавление). Дробить нечего: если реальный текст лежит в "
                        "связанных файлах глубже по ссылкам, проверь, что они прописаны как "
                        "[[wikilink]] или ![[embed]] и находятся не дальше "
                        f"{MAX_LINK_DEPTH} уровней ссылок от корня"
                    )
                continue

            informative_files.append(path)
            original_text_by_source[path] = text
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
            if is_root:
                root_skip_reason = f"не удалось прочитать файл: {exc}"

    _report(
        f"Отфильтровано информативных файлов для дробления: {len(informative_files)} "
        f"(пропущено файлов-оглавлений: {link_only_skipped}, уже атомарных: {already_generated_skipped})."
    )

    # Генерируем тематики по одному файлу, чтобы не раздувать один запрос.
    generated_by_source: Dict[Path, List[Dict]] = {}
    total_informative = len(informative_files)

    for idx, path in enumerate(informative_files, start=1):
        _report(f"[{idx}/{total_informative}] Отправляю в Gemini: {path.name}...")
        try:
            generated = split_note_with_ai(path, available_topics)

            for item in generated:
                available_topics.append(item["topic"])

            generated_by_source[path] = generated
            _report(f"[{idx}/{total_informative}] Готово: {path.name} → тем: {len(generated)}.")
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
            _report(f"[{idx}/{total_informative}] Ошибка на {path.name}: {exc}")

    # Сначала семантически дедуплицируем ВСЕ темы всех исходников текущего запуска.
    # Это работает даже в полностью пустом vault, где ещё нет готовых атомарок.
    _report("Провожу семантическую дедупликацию тем этого запуска...")
    semantic_merged = _semantic_deduplicate_generated_topics(generated_by_source)

    # Затем сопоставляем каждую новую тему с уже существующими атомарными
    # заметками и с темами, запланированными раньше в этом же запуске.
    all_output_entries: List[Tuple[Path, Dict, Path, bool]] = []
    generated_source_entries: List[Tuple[Path, List[Tuple[Dict, Path, bool]]]] = []
    reused_files: List[Path] = []
    reused_seen: Set[Path] = set()
    reserved_global: Set[Path] = set()
    planned_atomic_items: List[Tuple[Dict, Path]] = []

    planned_reuse_count = 0

    for source_path, generated in generated_by_source.items():
        output_entries = []

        for item in generated:
            # Антидубль работает одинаково для обычных тем и податомарок:
            # после flatten subtopics каждая податомарка попадает сюда как
            # обычный item и проверяется и внутри текущего запуска, и по всему vault.
            # Поэтому повторный запуск второй лекции привяжет совпадающую тему
            # к уже существующей атомарке первой лекции, а не создаст Topic-2.
            # Сначала ищем дубль среди тем, уже запланированных в этом же запуске.
            # Это не считается "существующей атомаркой": файл появился только сейчас.
            planned_match = _find_planned_atomic_match(item, planned_atomic_items)
            existing_path = planned_match
            is_existing_reuse = False

            if existing_path is None:
                existing_path = _find_existing_atomic_match(
                    content=item["content"],
                    topic=item["topic"],
                    vault=vault,
                    exclude=set(generated_by_source.keys()),
                )
                is_existing_reuse = existing_path is not None

            if existing_path is not None:
                output_path = existing_path.resolve()
                is_reused = True
                if is_existing_reuse and output_path not in reused_seen:
                    reused_files.append(output_path)
                    reused_seen.add(output_path)
                elif planned_match is not None:
                    planned_reuse_count += 1
            else:
                output_path = _unique_output_path(
                    source_path.parent,
                    item["filename"],
                    reserved_global,
                )
                reserved_global.add(output_path.resolve())
                is_reused = False

            output_entries.append((item, output_path, is_reused))
            all_output_entries.append(
                (source_path, item, output_path, is_reused)
            )

            planned_atomic_items.append((item, output_path))

        generated_source_entries.append((source_path, output_entries))

    # Глобальный индекс включает НЕ ТОЛЬКО темы текущего запуска, но и уже
    # существующие атомарные заметки. Поэтому related_topics тоже не плодит
    # дублей, а ссылается на найденный существующий файл.
    global_topic_lookup: Dict[str, Path] = {}

    for note, note_text in _iter_existing_atomic_notes(vault):
        heading_match = re.search(r"^#\s+(.+?)\s*$", note_text, flags=re.MULTILINE)
        topic_name = heading_match.group(1).strip() if heading_match else note.stem
        for alias_key in _topic_lookup_aliases(note_text, topic_name, note.stem):
            global_topic_lookup.setdefault(alias_key, note)


    for _, item, output_path, _ in all_output_entries:
        key = _normalize_topic_key(item["topic"])
        global_topic_lookup.setdefault(key, output_path)

        for alias in item.get("aliases", []):
            alias_key = _normalize_topic_key(alias)
            if alias_key:
                global_topic_lookup.setdefault(alias_key, output_path)

    # Записываем тематические файлы уже с глобальными логическими связями.
    _report(f"Записываю атомарные файлы для {len(generated_source_entries)} исходников...")

    for source_path, output_entries in generated_source_entries:
        try:
            source_topic_links: List[Path] = []
            source_created: List[Path] = []

            # Локальная карта тема -> файл для этого исходника: нужна, чтобы
            # связать основную тему с её податомарными файлами (rule 4),
            # даже если они ещё не попали в общий словарь тем.
            local_topic_lookup: Dict[str, Path] = {
                _normalize_topic_key(local_item["topic"]): local_output_path
                for local_item, local_output_path, _ in output_entries
            }

            for item, output_path, is_reused in output_entries:
                links = []

                for related_topic in item.get("related_topics", []):
                    target = global_topic_lookup.get(
                        _normalize_topic_key(related_topic)
                    )

                    if (
                        target is not None
                        and target.resolve() != output_path.resolve()
                    ):
                        links.append((related_topic, target))

                # Rule 4: ссылки основной темы на её податомарные файлы.
                subtopic_targets = []
                for subtopic_name in item.get("_subtopics", []):
                    target = local_topic_lookup.get(_normalize_topic_key(subtopic_name))
                    if target is not None and target.resolve() != output_path.resolve():
                        subtopic_targets.append((subtopic_name, target))

                # Rule 4: обратная ссылка податомарного файла на основную тему.
                parent_target = None
                parent_topic = item.get("_parent_topic")
                if parent_topic:
                    parent_path = local_topic_lookup.get(_normalize_topic_key(parent_topic))
                    if parent_path is not None and parent_path.resolve() != output_path.resolve():
                        parent_target = (parent_topic, parent_path)

                if not is_reused:
                    rendered = _render_note(
                        item,
                        links,
                        source_path=source_path,
                        subtopic_targets=subtopic_targets,
                        parent_target=parent_target,
                    )
                    _write_note_atomic(output_path, rendered)

                    created_files.append(output_path)
                    source_created.append(output_path)
                    _log_change(
                        "CREATE_TOPIC",
                        f"{output_path.relative_to(get_vault_path())} <- {source_path.relative_to(get_vault_path())}"
                    )
                else:
                    # Существующую атомарную заметку НЕ перезаписываем.
                    # Источник просто получает ссылку на неё.
                    _log_change(
                        "LINK_EXISTING_TOPIC",
                        f"{source_path.relative_to(get_vault_path())} -> {output_path.relative_to(get_vault_path())}"
                    )

                if output_path.resolve() not in source_topic_links:
                    source_topic_links.append(output_path.resolve())

            # Только после полной успешной записи всех частей исходник
            # сохраняет свой полный текст (конспект целиком не теряется!)
            # и дополняется #STUDY-меткой и ссылками на атомарные темы.
            full_conspectus_content = _render_full_conspectus(
                source_path,
                original_text_by_source[source_path],
                source_topic_links,
            )
            _write_note_atomic(source_path, full_conspectus_content)
            _log_change(
                "REWRITE_AS_FULL_CONSPECTUS",
                f"{source_path.relative_to(get_vault_path())}; создано тем: {len(source_created)}"
            )

            # Раньше сам исходный конспект в rule 5 не попадал, и его "живой"
            # текст с определениями так и оставался просто текстом — ссылки
            # были только отдельным списком "## Атомарные темы" в конце.
            # Добавляя исходник в created_files, он проходит тот же
            # рекурсивный проход по связям, что и остальные файлы: реальные
            # упоминания понятий ПРЯМО В ТЕКСТЕ конспекта превращаются в
            # [[ссылки]] на уже созданные атомарные темы, а не только
            # перечисляются отдельным блоком.
            created_files.append(source_path)

            processed_files.append(source_path)

        except Exception as exc:
            failures.append(
                f"{source_path.name}: ошибка записи результатов: {exc}"
            )

    # Rule 5: дополнительный проход по вновь созданным файлам — добавляет
    # ссылки на упомянутые в тексте темы, которые не попали в текст на
    # первом проходе.
    for _, item, output_path, _ in all_output_entries:
        key = _normalize_topic_key(item["topic"])
        global_topic_lookup.setdefault(key, output_path)

    cross_linked_count = _apply_recursive_cross_link_pass(
        created_files, global_topic_lookup, progress_callback=progress_callback
    )

    repaired_created, repaired_linked_existing, repaired_unlinked = _repair_dangling_topic_links(
        created_files, global_topic_lookup, original_text_by_source, progress_callback=progress_callback
    )

    # БАГ: repair-проход создаёт НОВЫЕ атомарные файлы (часто больше десятка
    # за раз — почти всё, что реально описано в тексте, но не попало в
    # первый проход атомизации). Если на этом остановиться, они физически
    # не могут быть связаны с текстом: перелинковка (rule 5) выше уже
    # прошла ДО того, как эти файлы появились. Отсюда огромный конспект с
    # 2-3 ссылками при десятках реально существующих тем. Прогоняем rule 5
    # ещё раз — теперь уже со всеми, включая только что дозданные, файлами.
    if repaired_created:
        cross_linked_count += _apply_recursive_cross_link_pass(
            created_files, global_topic_lookup, progress_callback=progress_callback
        )

    _report("Обработка завершена, собираю итоговый отчёт...")

    report = [
        "OBSIDIAN SPLIT COMPLETED",
        "",
        f"Vault: {vault}",
        f"Корневой файл: {root.name}",
        f"Глубина обхода ссылок: {MAX_LINK_DEPTH}",
        f"Файлов в графе (корень + связанные по ссылкам): {len(files_to_process)}",
        f"Обработано информативных файлов: {len(processed_files)}",
        f"Создано новых тематических файлов: {len(created_files)}",
        f"Переиспользовано существующих атомарных файлов: {len(reused_files)}",
        f"Семантически объединено дублей в этом запуске: {semantic_merged}",
        f"Тем сведено к уже запланированным атомаркам: {planned_reuse_count}",
        f"Файлов дополнено ссылками на повторном проходе (rule 5): {cross_linked_count}",
        f"Досоздано атомарных файлов для висячих ссылок (по всем первоисточникам): {repaired_created}",
        f"Привязано к уже существующим темам хранилища (без создания дублей): {repaired_linked_existing}",
        f"Битых ссылок разлинковано (материала не нашлось нигде): {repaired_unlinked}",
        f"Помечено #STUDY (link-only): {len(study_files)}",
        f"Защищённых/неизменённых файлов в цепочке: {len(protected_files)}",
    ]

    if link_only_skipped:
        report.append(f"Файлов-оглавлений без своего текста (пропущено дробление): {link_only_skipped}")

    if already_generated_skipped:
        report.append(f"Пропущено уже сгенерированных ИИ тем (не дробим повторно): {already_generated_skipped}")

    if protected_skipped:
        report.append(f"Защищённых файлов пропущено: {protected_skipped}")

    if not processed_files and not created_files:
        report.append("")
        if root_skip_reason:
            report.append(f"⚠ Корневой файл не был раздроблен: {root_skip_reason}.")
        elif informative_files and failures:
            # Текст в файлах есть — дробление упало с ошибкой. Раньше здесь
            # ошибочно выводилось "нет собственного текста", что маскировало
            # реальную причину.
            report.append(
                "⚠ Файлы с текстом найдены, но дробление завершилось ошибкой — "
                "причина в разделе «Проблемы» ниже."
            )
        else:
            report.append(
                "⚠ Ни один файл в этой ветке (корень и связанные по ссылкам) "
                "не содержит собственного текста для дробления."
            )

        if extraction_log:
            report.append("")
            report.append("Не удалось извлечь текст из вложений:")
            report.extend(f"- {msg}" for msg in extraction_log)

        if len(files_to_process) <= 1 and not (informative_files and failures):
            report.append("")
            report.append("Диагностика ссылок в корневом файле:")
            report.extend(f"  • {line}" for line in _diagnose_root_links(root))

    if processed_files:
        report.append("")
        report.append(f"Полных конспектов обновлено: {len(processed_files)}")

    if study_files:
        report.append(f"Link-only файлов помечено #STUDY: {len(study_files)}")

    if reused_files:
        report.append(f"Переиспользовано существующих атомарок: {len(reused_files)}")

    if failures:
        report.append("")
        report.append("Проблемы:")
        report.extend(f"- {failure}" for failure in failures)

    final_report = "\n".join(report)
    _log_change(
        "SPLIT_FINISHED",
        f"root={root.name}; processed={len(processed_files)}; created={len(created_files)}; study={len(study_files)}"
    )
    return final_report


# ============================================================
# ПОИСК НУЖНЫХ ССЫЛОК В ДОКУМЕНТЕ
# ============================================================

def _link_contexts(path: Path) -> List[Dict]:
    text = _read_note(path)
    lines = text.splitlines()

    contexts = []

    for index, line in enumerate(lines):
        raw_targets = []

        raw_targets.extend(
            _normalize_link_target(match.group(1))
            for match in WIKILINK_PATTERN.finditer(line)
        )

        raw_targets.extend(
            _normalize_link_target(match.group(2))
            for match in MARKDOWN_LINK_PATTERN.finditer(line)
        )

        raw_targets = [
            item for item in raw_targets
            if item and not _is_external_target(item)
        ]

        if not raw_targets:
            continue

        nearest_heading = ""
        for heading_line in reversed(lines[:index + 1]):
            if re.match(r"^\s*#{1,6}\s+", heading_line):
                nearest_heading = re.sub(r"^\s*#{1,6}\s+", "", heading_line).strip()
                break

        for raw_target in raw_targets:
            target_path = None

            try:
                target_path = resolve_link(raw_target, path)
            except FileNotFoundError:
                pass

            target_content = ""
            if target_path and target_path.exists():
                try:
                    target_content = _read_note(target_path)[:2500]
                except Exception:
                    target_content = ""

            contexts.append(
                {
                    "raw_link": raw_target,
                    "source_line": line.strip(),
                    "heading": nearest_heading,
                    "target_path": str(target_path) if target_path else "",
                    "target_name": target_path.name if target_path else raw_target,
                    "target_content": target_content,
                }
            )

    return contexts


def find_links_in_note(note_ref: str, query: str) -> str:
    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    try:
        source = resolve_note(note_ref)
    except Exception as exc:
        return f"Не удалось найти Obsidian-документ: {exc}"

    diary_linked = collect_diary_linked_files()

    if is_always_protected(source, diary_linked):
        # Защита относится к изменению. Поиск разрешаем.
        protection_note = (
            "Документ защищён от изменений, но его ссылки можно безопасно искать."
        )
    else:
        protection_note = ""

    _log_change("FIND_REQUEST", f"source={source.relative_to(vault)}; query={query}")
    contexts = _link_contexts(source)

    if not contexts:
        return f"В документе {source.name} не найдено локальных Obsidian-ссылок."

    serialized = json.dumps(
        contexts[:120],
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""
Пользователь ищет данные в Obsidian.

Документ:
{source.name}

Запрос пользователя:
{query}

Ниже перечислены ссылки, найденные в документе, контекст строки/заголовка
и начало целевого файла.

{serialized}

Задача:
1. Найди только те ссылки, которые действительно могут вести к нужным данным.
2. Не придумывай ссылки, которых нет в списке.
3. Для каждой подходящей ссылки объясни, почему она относится к запросу.
4. Покажи имя целевого файла и исходную Obsidian-ссылку.
5. Если подходящих ссылок нет, прямо так и скажи.
6. Учитывай смысл, а не только совпадение слов.

Ответь на русском языке.
"""

    system_instruction = (
        "Ты — навигатор учебного Obsidian-хранилища. "
        "Ты находишь уже существующие связи и никогда не придумываешь новые."
    )

    try:
        answer = _gemini_text(prompt, system_instruction)
    except Exception as exc:
        return f"Ошибка поиска ссылок через Gemini: {exc}"

    prefix = f"[OBSIDIAN FIND] {source.name}\n"
    if protection_note:
        prefix += f"{protection_note}\n"

    return prefix + "\n" + answer


# ============================================================
# ПОИСК ОПРЕДЕЛЕНИЯ ПОНЯТИЯ ПО ВСЕМУ VAULT
# ============================================================

def _first_tags_line(text: str) -> str:
    for line in text.splitlines()[:3]:
        clean = line.strip()
        if clean.startswith("#"):
            return clean
    return ""


def _definition_candidates(term: str) -> List[Tuple[int, Path]]:
    """
    Ищет заметки, которые с наибольшей вероятностью содержат определение
    искомого термина: смотрим на совпадение имени файла/заголовка, на теги
    (особенно #определение) и на упоминание термина в первых строках текста.
    """
    vault = get_vault_path()
    term_tokens = set(_tokenize_name(term))

    candidates = []

    for note in vault.rglob("*.md"):
        if not note.is_file() or note.name.startswith("."):
            continue

        try:
            text = _read_note(note)
        except Exception:
            continue

        if is_ai_generated_topic(text):
            # именно эти файлы и есть атомарные определения — им положен бонус
            pass

        score = _semantic_name_score(term, note)

        tags_line = _first_tags_line(text)
        tag_tokens = set(re.findall(r"[a-zа-яё0-9]+", tags_line.casefold()))
        overlap = len(term_tokens & tag_tokens)
        score += overlap * 30

        has_definition_tag = bool(
            re.search(r"(?<!\w)#определение\b", text, flags=re.IGNORECASE)
        )
        if has_definition_tag and (overlap or score >= 20):
            score += 25

        # Бонус только если уже есть совпадение по имени/тегам. Раньше +10
        # получали ВСЕ атомарные файлы при любом запросе, и несуществующий
        # термин отвечал "нашлось несколько похожих статей" (список всех тем).
        if score > 0 and is_ai_generated_topic(text):
            score += 10  # атомарная тема — обычно и есть точное определение

        if score > 0:
            candidates.append((score, note.resolve()))

    candidates.sort(key=lambda item: (-item[0], str(item[1]).casefold()))
    return candidates


def find_definition(term: str) -> str:
    """
    Возвращает готовое определение понятия прямо из Obsidian, дословно
    подцепляя сохранённый текст файла — без пересказа от ИИ.
    """
    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    term = term.strip()
    if not term:
        return "Не указан термин, для которого нужно найти определение."

    candidates = _definition_candidates(term)

    if not candidates:
        return f"В Obsidian не нашлось файла с определением «{term}»."

    best_score = candidates[0][0]
    best = [note for score, note in candidates if score == best_score]

    if len(best) > 1 and best_score < 60:
        relative_list = "\n".join(f"- [[{p.stem}]]" for p in best[:10])
        _log_change("DEFINE_AMBIGUOUS", f"term={term}; matches={len(best)}")
        return (
            f"По запросу «{term}» нашлось несколько похожих статей в Obsidian:\n"
            f"{relative_list}\nУточни, какая именно нужна."
        )

    note = best[0]

    try:
        content = _read_note(note)
    except Exception as exc:
        return f"Не удалось прочитать файл {note.name}: {exc}"

    content = content.replace(AI_GENERATED_MARKER, "").strip()

    _log_change("DEFINE_REQUEST", f"term={term}; matched={note.name}")

    return f"[OBSIDIAN DEFINE] [[{note.stem}]]\n\n{content}"


# ============================================================
# ОБЗОР СТРУКТУРЫ OBSIDIAN
# ============================================================

def _local_incoming_counts(notes: List[Path]) -> Dict[Path, int]:
    incoming = {note.resolve(): 0 for note in notes}
    note_set = set(incoming)

    for source in notes:
        for _, target in _read_links(source):
            target = target.resolve()
            if target in note_set:
                incoming[target] += 1

    return incoming


def _logical_branch_roots(notes: List[Path]) -> List[Path]:
    """
    Определяет корни логических веток:
    - #STUDY + локальные ссылки — главный признак;
    - иначе файл с локальными ссылками и без входящих ссылок.
    """
    linked_notes = [note for note in notes if _read_links(note)]
    incoming = _local_incoming_counts(notes)

    study_roots = [
        note for note in linked_notes
        if re.search(r"(?<!\w)#study\b", _read_note(note), flags=re.IGNORECASE)
    ]

    if study_roots:
        return sorted(
            list(dict.fromkeys(study_roots)),
            key=lambda p: p.name.casefold()
        )

    roots = [
        note for note in linked_notes
        if incoming.get(note.resolve(), 0) == 0
    ]

    return sorted(
        list(dict.fromkeys(roots)),
        key=lambda p: p.name.casefold()
    )


def _collect_logical_branch(root: Path) -> List[Tuple[Path, int]]:
    result = []
    visited: Set[Path] = set()
    queue = [(root.resolve(), 0)]

    while queue and len(visited) < MAX_GRAPH_NODES:
        current, depth = queue.pop(0)
        current = current.resolve()

        if current in visited:
            continue
        visited.add(current)

        if not current.exists() or current.suffix.lower() != ".md":
            continue

        result.append((current, depth))

        if depth >= MAX_LINK_DEPTH:
            continue

        for _, target in _read_links(current):
            target = target.resolve()
            if target not in visited:
                queue.append((target, depth + 1))

    return result


def obsidian_overview() -> str:
    """
    Кратко показывает ЛОГИЧЕСКУЮ структуру vault по ссылкам.
    Физические папки не используются для определения веток.
    """
    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    all_notes = sorted(
        [
            p.resolve()
            for p in vault.rglob("*.md")
            if p.is_file() and not p.name.startswith(".")
        ],
        key=lambda p: str(p).casefold()
    )

    if not all_notes:
        return f"В Obsidian vault нет Markdown-файлов: {vault}"

    diary_linked = collect_diary_linked_files()
    roots = _logical_branch_roots(all_notes)
    branches = []
    claimed: Set[Path] = set()

    for root in roots:
        branch = _collect_logical_branch(root)
        if not branch:
            continue

        branches.append((root, branch))
        claimed.update(path for path, _ in branch)

    orphan_notes = [note for note in all_notes if note not in claimed]

    lines = [
        "OBSIDIAN OVERVIEW",
        "",
        f"Vault: {vault}",
        f"Всего Markdown-файлов: {len(all_notes)}",
        f"Логических веток: {len(branches)}",
    ]

    for root, branch in branches:
        protected = sum(
            1 for path, _ in branch
            if is_always_protected(path, diary_linked)
        )

        lines.append("")
        protection_suffix = f"; защищено: {protected}" if protected else ""
        lines.append(
            f"📚 {root.stem} — {len(branch)} материалов{protection_suffix}"
        )

        for path, depth in branch[:25]:
            marker = " [PROTECTED]" if is_always_protected(path, diary_linked) else ""

            prefix = "   " + "   " * depth + "└─ "
            summary = ""

            try:
                content = _read_note(path)
                for raw_line in content.splitlines():
                    clean = raw_line.strip()
                    if not clean:
                        continue
                    if clean.startswith("<!--") or clean.startswith("#STUDY") or clean.startswith("#"):
                        continue
                    if clean.startswith("[[") or clean.startswith("["):
                        continue
                    summary = re.sub(r"\s+", " ", clean)
                    break
            except Exception:
                pass

            if summary:
                lines.append(f"{prefix}{path.stem}{marker} — {summary[:180]}")
            else:
                lines.append(f"{prefix}{path.stem}{marker}")

        if len(branch) > 25:
            lines.append(f"   └─ … и ещё {len(branch) - 25}")

    if orphan_notes:
        lines.append("")
        lines.append(f"📄 Вне найденных логических веток — {len(orphan_notes)} файлов")
        for note in orphan_notes[:20]:
            marker = " [PROTECTED]" if is_always_protected(note, diary_linked) else ""
            lines.append(f"   └─ {note.name}{marker}")

        if len(orphan_notes) > 20:
            lines.append(f"   └─ … и ещё {len(orphan_notes) - 20}")

    protected_total = sum(
        1 for note in all_notes
        if is_always_protected(note, diary_linked)
    )
    lines.append("")
    lines.append(
        f"🛡 Защищены от изменений: {protected_total} "
        "(Diary.md, даты YYYY-MM-DD и файлы с тегом #PROTECTED)."
    )

    _log_change(
        "OVERVIEW_REQUEST",
        f"notes={len(all_notes)}; branches={len(branches)}; orphan={len(orphan_notes)}"
    )

    return "\n".join(lines)


# ============================================================
# РАЗОВАЯ ПОЧИНКА: ЗАЩИТА УЖЕ СУЩЕСТВУЮЩИХ ПОЛНЫХ КОНСПЕКТОВ
# ============================================================

def repair_full_conspectus_protection() -> str:
    """
    Файлы, которые стали "полными конспектами" ДО того, как появился
    FULL_CONSPECTUS_MARKER, не защищены от повторного дробления — именно
    из-за них плодились дубли атомарных тем при обработке смежных
    лекций/конспектов. Эта функция находит такие файлы (есть секция
    '## Атомарные темы', но нет маркера) и задним числом их защищает.
    Сам текст и список тем не трогает — только добавляет маркер.
    """
    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    protected_now: List[Path] = []

    for note in vault.rglob("*.md"):
        if not note.is_file() or note.name.startswith("."):
            continue

        try:
            text = _read_note(note)
        except Exception:
            continue

        has_section = bool(re.search(r"(?m)^##\s+Атомарные темы\s*$", text))

        if (
            not has_section
            or is_full_conspectus(text)
            or is_ai_generated_topic(text)
            or _is_protected_note(note)
        ):
            continue

        new_text = f"{FULL_CONSPECTUS_MARKER}\n{text.strip()}\n"

        try:
            _write_note_atomic(note, new_text)
        except Exception as exc:
            continue

        _log_change("BACKFILL_FULL_CONSPECTUS_MARKER", str(note.relative_to(vault)))
        protected_now.append(note)

    if not protected_now:
        return "Все полные конспекты уже защищены от повторного дробления — чинить нечего."

    report = [
        f"Задним числом защищено от повторного дробления: {len(protected_now)}",
        "",
    ]
    report.extend(f"- [[{note.stem}]]" for note in protected_now)
    report.append("")
    report.append(
        "⚠ Уже созданные дублирующиеся атомарные .md-файлы (например 'Тема', 'Тема-2', "
        "'Тема-3'...) автоматически не удалялись и не сливались — они могут отличаться "
        "формулировками. Просмотри их вручную и оставь только нужные."
    )

    return "\n".join(report)


# ============================================================
# ЕДИНАЯ ТОЧКА ВХОДА
# ============================================================

def _make_compact_progress_callback(
    callback: Optional[Callable[[str], None]],
) -> Optional[Callable[[str], None]]:
    """Показывает в UI только крупные этапы; подробный audit остаётся в логе."""
    if callback is None:
        return None

    last = {"value": ""}

    def emit(message: str) -> None:
        msg = str(message or "").strip()
        if not msg or msg == last["value"]:
            return
        if msg.startswith("[["):
            return
        if any(token in msg for token in (
            "Прошу Gemini собрать атомарный файл",
            "Материала нет, разлинковываю",
            "Перевожу тему ",
        )):
            return

        match = re.match(r"^\[(\d+)/(\d+)\] Отправляю в Gemini: (.+?)\.\.\.$", msg)
        if match:
            msg = f"Анализ исходников: {match.group(1)}/{match.group(2)} — {match.group(3)}"
        elif "Готово:" in msg and "→ тем:" in msg:
            return

        last["value"] = str(message).strip()
        try:
            callback(msg)
        except Exception:
            pass

    return emit


def obsidian_split(note_ref: str, progress_callback: Optional[Callable[[str], None]] = None) -> str:
    """
    Единая точка входа для split.

    После локального дробления автоматически выполняются:
    1) vault-wide перевод старых/пропущенных английских атомарных тем;
    2) vault-wide ремонт висячих ссылок и пустых Obsidian-заглушек.

    Поэтому успешный split не оставляет после себя англоязычные имена файлов
    или ссылки на пустоту только потому, что проблема была создана в старой
    части хранилища.
    """
    compact_callback = _make_compact_progress_callback(progress_callback)

    base_report = split_note_tree(note_ref, progress_callback=compact_callback)

    if compact_callback is not None:
        compact_callback("Финальная проверка: язык атомарок и ссылки...")

    language_report = repair_english_atomic_topics_vault_wide(
        progress_callback=compact_callback
    )
    dangling_report = repair_dangling_links_vault_wide(
        progress_callback=compact_callback
    )

    return "\n\n--- ФИНАЛЬНАЯ ПРОВЕРКА VAULT ---\n\n".join(
        [base_report, language_report, dangling_report]
    )


def obsidian_find(note_ref: str, query: str) -> str:
    return find_links_in_note(note_ref, query)


def obsidian_define(term: str) -> str:
    return find_definition(term)


def repair_dangling_links_vault_wide(
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Та же логика, что и repair-проход в конце split_note_tree, но по ВСЕМУ
    хранилищу, а не только по файлам одного запуска. Нужна, потому что
    висячие ссылки, созданные ДО того как появился repair-проход (или из
    другой ветки, обработанной в другой раз), никогда не попадали в
    created_files текущего запуска и так и оставались битыми навсегда.
    """
    def _report(message: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(message)
        except Exception:
            pass

    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    _report("Собираю индекс всех атомарных тем и синонимов по хранилищу...")

    global_topic_lookup: Dict[str, Path] = {}

    for note, note_text in _iter_existing_atomic_notes(vault):
        heading_match = re.search(r"^#\s+(.+?)\s*$", note_text, flags=re.MULTILINE)
        topic_name = heading_match.group(1).strip() if heading_match else note.stem
        for alias_key in _topic_lookup_aliases(note_text, topic_name, note.stem):
            global_topic_lookup.setdefault(alias_key, note)


    all_notes: List[Path] = []
    original_text_by_source: Dict[Path, str] = {}

    for note in vault.rglob("*.md"):
        if not note.is_file() or note.name.startswith("."):
            continue

        try:
            text = _read_note(note)
        except Exception:
            continue

        all_notes.append(note)

        # Полные конспекты хранят исходный текст источника целиком — это и
        # есть "первоисточники" для repair-поиска в масштабе всего vault.
        if is_full_conspectus(text):
            original_text_by_source[note] = text

    _report(
        f"Файлов в хранилище: {len(all_notes)}, из них полных конспектов "
        f"(первоисточников): {len(original_text_by_source)}. Ищу висячие ссылки..."
    )

    created_count, linked_existing_count, unlinked_count = _repair_dangling_topic_links(
        all_notes, global_topic_lookup, original_text_by_source, progress_callback=progress_callback
    )

    if created_count == 0 and linked_existing_count == 0 and unlinked_count == 0:
        return "Висячих ссылок по всему хранилищу не найдено — чинить нечего."

    return (
        "РЕМОНТ ВИСЯЧИХ ССЫЛОК ПО ВСЕМУ ХРАНИЛИЩУ\n\n"
        f"Досоздано атомарных файлов для найденных ссылок: {created_count}\n"
        f"Привязано к уже существующим темам хранилища (без создания дублей): {linked_existing_count}\n"
        f"Битых ссылок разлинковано (материала не нашлось нигде): {unlinked_count}"
    )


def _rename_atomic_topic_everywhere(vault: Path, old_stem: str, new_stem: str) -> int:
    """
    Переименовывает ВСЕ входящие [[old_stem]] / [[old_stem|текст]] ссылки по
    всему хранилищу на чистое [[new_stem]] — без pipe-синтаксиса, даже если
    у старой ссылки уже был свой pipe-текст. По требованию: никаких
    [[Тема|EnglishWord]] — pipe-текст как раз и был тем местом, где
    английское слово оставалось видимым в прозе уже ПОСЛЕ переименования
    файла на русский.
    """
    if _normalize_topic_key(old_stem) == _normalize_topic_key(new_stem):
        return 0

    old_key = _normalize_topic_key(old_stem)
    pattern = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]", flags=re.IGNORECASE)

    updated = 0

    for note in vault.rglob("*.md"):
        if not note.is_file() or note.name.startswith(".") or _is_protected_note(note):
            continue

        try:
            text = _read_note(note)
        except Exception:
            continue

        def _replace(match: re.Match) -> str:
            target = match.group(1).strip()
            if _normalize_topic_key(target) != old_key:
                return match.group(0)
            # По требованию — без pipe-синтаксиса вообще.
            return f"[[{new_stem}]]"

        new_text, count = pattern.subn(_replace, text)

        if count and new_text != text:
            try:
                _write_note_atomic(note, new_text)
                updated += 1
            except Exception:
                continue

    return updated


def repair_english_atomic_topics_vault_wide(
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Ретроактивно чинит атомарные файлы, созданные ДО того как rule 6
    (обязательный русский) стал проверяться на уровне кода: находит темы,
    чьё название всё ещё на английском и не является устоявшимся термином,
    переводит topic+content через _translate_item_to_russian, переименовывает
    файл и правит ВСЕ входящие ссылки по хранилищу на новое имя — старый
    (непереведённый) текст ссылки сохраняется как отображаемый (pipe-текст),
    чтобы неless менять саму прозу конспектов.

    Структурные хвосты файла (Источник/Основная тема/## Подтемы/
    ## Связанные файлы) не переводятся и не трогаются — только заголовок,
    строка синонимов и содержательное тело между ними.
    """
    def _report(message: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(message)
        except Exception:
            pass

    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    # Снимок ДО начала переименований — сама генерация ленивая (rglob),
    # мутировать файлы во время обхода небезопасно.
    candidates = [
        (note, note_text)
        for note, note_text in _iter_existing_atomic_notes(vault)
        if not _is_protected_note(note)
    ]

    _report(f"Проверяю язык темы у {len(candidates)} атомарных файлов...")

    TAIL_MARKERS = (
        "\nОсновная тема:",
        "\nИсточник (полный конспект):",
        "\n## Подтемы",
        "\n## Связанные файлы",
    )

    translated_count = 0
    content_fixed_count = 0
    relinked_count = 0
    failures: List[str] = []

    for note, note_text in candidates:
        if _is_protected_note(note):
            continue
        heading_match = re.search(r"^#\s+(.+?)\s*$", note_text, flags=re.MULTILINE)
        if not heading_match:
            continue

        topic = heading_match.group(1).strip()
        rest = note_text[heading_match.end():]

        canonical_topic = _canonicalize_topic_name(topic)
        if (
            canonical_topic == topic
            and _topic_is_russian_or_established(topic)
            and _is_mostly_russian(rest)
        ):
            continue  # уже каноническое русское имя (или легитимная аббревиатура)

        _report(f"Перевожу тему «{topic}» ({note.name})...")

        tail_split = len(rest)
        for marker in TAIL_MARKERS:
            idx = rest.find(marker)
            if idx != -1:
                tail_split = min(tail_split, idx)

        body = rest[:tail_split]
        tail = rest[tail_split:]

        # Уже отрендеренную строку синонимов убираем — соберём заново из
        # обновлённого списка aliases, чтобы не задвоить её.
        body = re.sub(r"^\s*_Также известно как:.*?_\s*\n*", "", body, flags=re.MULTILINE).strip()

        pseudo_item = {
            "topic": topic,
            "content": body,
            "aliases": _extract_topic_aliases_display(note_text),
        }

        try:
            translated = _enforce_russian_language(pseudo_item)
        except Exception as exc:
            failures.append(f"{note.name}: ошибка перевода — {exc}")
            continue

        translated = _strip_self_links(translated)
        translated["filename"] = _safe_output_filename(translated["topic"])

        new_topic = translated.get("topic", topic).strip()

        if not new_topic or _normalize_topic_key(new_topic) == _normalize_topic_key(topic):
            # Название не поменялось (устоявшийся термин/аббревиатура), но
            # текст мог быть переведён: раньше такой результат молча
            # выбрасывался, и английский текст оставался в файле навсегда.
            # Теперь переписываем файл на месте — без переименования.
            new_body = str(translated.get("content", "")).strip()
            if (
                new_body
                and new_body != body
                and _is_mostly_russian(new_body)
                and not _is_mostly_russian(body)
            ):
                alias_line_match = re.search(
                    r"^\s*_Также известно как:.*?_\s*$",
                    rest[:tail_split],
                    flags=re.MULTILINE,
                )
                rebuilt = note_text[: heading_match.end(1)] + "\n"
                if alias_line_match:
                    rebuilt += "\n" + alias_line_match.group(0).strip() + "\n"
                rebuilt += "\n" + new_body + "\n" + tail
                try:
                    _write_note_atomic(note, rebuilt.strip() + "\n")
                    _log_change("LANGUAGE_REPAIR_CONTENT", note.stem)
                    content_fixed_count += 1
                except Exception as exc:
                    failures.append(f"{note.name}: ошибка записи перевода текста — {exc}")
            continue

        old_stem = note.stem
        new_stem = Path(_safe_output_filename(new_topic)).stem

        reserved = {n.resolve() for n, _ in candidates if n.resolve() != note.resolve()}
        new_path = _unique_output_path(note.parent, f"{new_stem}.md", reserved)

        clean_aliases = []
        for alias in translated.get("aliases", []):
            alias = str(alias).strip().replace("|", " ")
            if alias and alias not in clean_aliases:
                clean_aliases.append(alias)

        # Голову файла (маркер + теги) переносим как есть, только строку
        # алиасов и заголовок пересобираем заново.
        head = note_text[: heading_match.start()]
        head = re.sub(
            rf"^{re.escape(AI_TOPIC_ALIASES_PREFIX)}.*-->\s*\n?",
            "",
            head,
            flags=re.MULTILINE,
        )
        if clean_aliases:
            head += f"{AI_TOPIC_ALIASES_PREFIX}{'|'.join(clean_aliases)} -->\n"

        new_note_text = head + f"# {new_topic}\n"
        if clean_aliases:
            new_note_text += f"\n_Также известно как: {', '.join(clean_aliases)}_\n"
        new_note_text += "\n" + translated.get("content", body).strip() + "\n" + tail

        try:
            _write_note_atomic(new_path, new_note_text.strip() + "\n")

            if new_path.resolve() != note.resolve():
                note.unlink(missing_ok=True)

            renamed_refs = _rename_atomic_topic_everywhere(vault, old_stem, new_path.stem)

            _log_change(
                "LANGUAGE_REPAIR_RENAMED",
                f"{old_stem} -> {new_path.stem}; входящих ссылок поправлено: {renamed_refs}",
            )
            translated_count += 1
            relinked_count += renamed_refs
        except Exception as exc:
            failures.append(f"{note.name}: ошибка записи/переименования — {exc}")
            continue

    if translated_count == 0 and content_fixed_count == 0 and not failures:
        return "Англоязычных атомарных тем не найдено — переводить нечего."

    lines = [
        f"Русские названия атомарок: переименовано {translated_count}, ссылок поправлено {relinked_count}."
    ]
    if content_fixed_count:
        lines.append(f"Текст переведён на русский в файлах с неизменным названием: {content_fixed_count}.")

    if failures:
        lines.append("")
        lines.append(f"Не удалось обработать: {len(failures)}")
        lines.extend(f"- {line}" for line in failures[:20])

    return "\n".join(lines)


def strip_legacy_conspectus_index_vault_wide() -> str:
    """
    Полные конспекты защищены от повторного дробления (is_full_conspectus),
    поэтому их старый хвост "## Атомарные темы" сам собой никогда не
    убирался — split больше их не трогает. Чистая текстовая правка без
    единого вызова Gemini: просто отрезает этот блок у всех уже
    существующих конспектов разом.
    """
    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    cleaned = 0

    for note in vault.rglob("*.md"):
        if not note.is_file() or note.name.startswith("."):
            continue

        try:
            text = _read_note(note)
        except Exception:
            continue

        if not is_full_conspectus(text) or _is_protected_note(note):
            continue

        new_text = re.split(r"(?m)^##\s+Атомарные темы\s*$", text, maxsplit=1)[0].rstrip()

        if new_text != text.rstrip():
            try:
                _write_note_atomic(note, new_text.strip() + "\n")
                cleaned += 1
            except Exception:
                continue

    if cleaned == 0:
        return "Старых индексов '## Атомарные темы' в конспектах не найдено — чистить нечего."

    return f"Убран хвост '## Атомарные темы' у {cleaned} конспект(ов) — ссылки на темы теперь только внутри текста."


def obsidian_repair_duplicates(progress_callback: Optional[Callable[[str], None]] = None) -> str:
    parts = [
        repair_full_conspectus_protection(),
        strip_legacy_conspectus_index_vault_wide(),
        # Сначала переводим/переименовываем атомарки, затем одним проходом
        # чинить ссылки уже на окончательные русские имена.
        repair_english_atomic_topics_vault_wide(progress_callback=progress_callback),
        repair_dangling_links_vault_wide(progress_callback=progress_callback),
    ]
    return "\n\n---\n\n".join(parts)


# ============================================================
# ОБЗОР ОДНОЙ ЛОГИЧЕСКОЙ ВЕТКИ
# ============================================================

def _branch_candidates(query: str) -> List[Path]:
    """Ищет возможные корни логической ветки по имени, #STUDY и ссылкам."""
    vault = get_vault_path()
    if not query.strip():
        return []

    candidates = []

    for note in vault.rglob("*.md"):
        if not note.is_file() or note.name.startswith("."):
            continue

        try:
            text = _read_note(note)
        except Exception:
            continue

        score = _semantic_name_score(query, note)
        links = _read_links(note)

        if links:
            score += min(len(links), 15) * 2

        if re.search(r"(?<!\w)#study\b", text, flags=re.IGNORECASE):
            score += 35

        # Индексная запись с коротким содержанием и большим числом ссылок
        # сильнее похожа на корень ветки.
        if links and len(text) < 5000:
            score += 8

        # Слова запроса в первых строках/заголовке.
        query_tokens = [
            t for t in re.findall(r"[a-zа-яё0-9]+", query.casefold())
            if len(t) > 1
        ]
        first_part = text[:5000].casefold()
        score += sum(5 for token in query_tokens if token in first_part)

        if score > 0:
            candidates.append((score, note.resolve()))

    candidates.sort(key=lambda item: (-item[0], str(item[1]).casefold()))
    return [note for _, note in candidates[:10]]


def obsidian_branch_overview(branch_query: str) -> str:
    """
    Показывает содержимое логической ветки по названию темы.
    Ветки определяются по ссылкам между Markdown-файлами, а не по физическим папкам.
    Файлы не изменяются.
    """
    vault = get_vault_path()

    if not vault.exists():
        return f"Obsidian vault не найден: {vault}"

    candidates = _branch_candidates(branch_query)
    if not candidates:
        return f"Логическая ветка по запросу '{branch_query}' не найдена в Obsidian."

    # Выбираем лучший кандидат как корень ветки.
    root = candidates[0].resolve()
    diary_linked = collect_diary_linked_files()

    # Для обзора не применяем защиту как фильтр: защищённые файлы можно читать.
    # Но не строим бесконечные циклы и ограничиваем глубину.
    result = []
    visited: Set[Path] = set()
    queue = [(root, 0)]

    while queue and len(result) < MAX_GRAPH_NODES:
        current, depth = queue.pop(0)
        current = current.resolve()

        if current in visited:
            continue
        visited.add(current)

        if not current.exists() or current.suffix.lower() != ".md":
            continue

        result.append((current, depth))

        if depth >= MAX_LINK_DEPTH:
            continue

        for _, target in _read_links(current):
            target = target.resolve()
            if target not in visited:
                queue.append((target, depth + 1))

    # Отдельно считаем реальные локальные материалы.
    protected_count = sum(
        1 for path, _ in result
        if is_always_protected(path, diary_linked)
    )

    lines = [
        "OBSIDIAN BRANCH",
        "",
        f"Запрос: {branch_query}",
        f"Корень ветки: {root.name}",
        f"Материалов в ветке: {len(result)}",
        f"Максимальная глубина: {MAX_LINK_DEPTH}",
    ]

    for path, depth in result:
        prefix = "   " + "   " * depth + "└─ "
        marker = " [PROTECTED]" if is_always_protected(path, diary_linked) else ""
        lines.append(f"{prefix}{path.stem}{marker}")

        # Для корня и небольших веток показываем краткую первую содержательную строку.
        if depth <= 1:
            try:
                text = _read_note(path)
                summary = ""
                for raw_line in text.splitlines():
                    clean = raw_line.strip()
                    if not clean:
                        continue
                    if clean.startswith("<!--") or clean.startswith("#STUDY") or clean.startswith("#"):
                        continue
                    if clean.startswith("[[") or clean.startswith("["):
                        continue
                    summary = re.sub(r"\s+", " ", clean)
                    break
                if summary:
                    lines.append(f"{prefix}   {summary[:180]}")
            except Exception:
                pass

    if protected_count:
        lines.append("")
        lines.append(f"🛡 Защищено от изменений: {protected_count}")

    _log_change(
        "BRANCH_OVERVIEW_REQUEST",
        f"query={branch_query}; root={root.name}; nodes={len(result)}"
    )

    return "\n".join(lines)
