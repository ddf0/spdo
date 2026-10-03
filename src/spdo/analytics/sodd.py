"""Преобразование дополнительного набора SODD-ru в формат эталонного набора.

SODD — набор пар вопросов Stack Overflow с разметкой дубликатов; тексты
переведены на русский язык. Исходные файлы (``pairs.csv``,
``tickets_en.csv``, ``tickets_ru.csv``) не содержат заголовков, категорий
и компонентов, поэтому преобразование их формирует:

* заголовок — первое предложение русского текста, содержащее кириллицу;
* описание — русский текст, обрезанный до 4000 символов;
* компонент — технология, определяемая правилами по английскому тексту
  (ключевые слова и характерные конструкции кода);
* категорию — по компоненту.

Классификация выполняется по каждому обращению отдельно, без учёта пар,
поэтому разметка пар на неё не влияет. Признаки правил подобраны по
текстам всего корпуса (включая проверочную часть) без обращения к разметке
пар; правила распознают технологию, а не сходство обращений. Дубликаты по
построению чаще относятся к одной технологии, поэтому составляющие S_cat и
S_comp коррелируют с разметкой — это учитывается при интерпретации recall@3.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from spdo.analytics.dataset import Dataset, DatasetError, stable_holdout
from spdo.tickets.models import DESCRIPTION_MAX, TITLE_MAX

#: Доля пар каждого класса, отводимая в проверочную часть.
HOLDOUT_SHARE = 0.4
#: Вид связи эталонной разметки по классу пары SODD; ``different`` — независимые.
RELATION_KIND = {"duplicate": "duplicate", "text_similar": "related", "tag_similar": "related"}
#: Компонент, если технология не распознана.
UNKNOWN_COMPONENT = "Не определён"
#: Дополнительные графы ``tickets.csv`` набора SODD-ru.
EXTRA_FIELDS = ("source_id", "translation_review")


@dataclass(frozen=True, slots=True)
class Rule:
    """Правило распознавания компонента.

    Attributes:
        component: Наименование компонента.
        category: Категория, к которой относится компонент.
        patterns: Признаки: регулярное выражение → вес.
    """

    component: str
    category: str
    patterns: dict[str, int]


#: Правила в порядке приоритета при равенстве баллов: более узкие технологии
#: стоят раньше общих (jQuery раньше JavaScript, MySQL раньше SQL).
RULES: tuple[Rule, ...] = (
    Rule("SQL Server", "Базы данных", {r"sql server|t-sql|\bmssql|ssms": 5}),
    Rule("MySQL", "Базы данных", {r"\bmysql": 5, r"phpmyadmin": 3}),
    Rule("PostgreSQL", "Базы данных", {r"postgres|\bpsql\b|pgadmin": 5}),
    Rule("Oracle", "Базы данных", {r"\boracle\b|pl/sql|\bplsql": 5}),
    Rule("SQLite", "Базы данных", {r"sqlite": 5}),
    Rule("MongoDB", "Базы данных", {r"mongo": 5}),
    Rule(
        "SQL",
        "Базы данных",
        {
            r"\bsql\b": 3,
            r"\bselect\b[\s\S]{1,200}\bfrom\b": 3,
            r"\b(inner|left) join\b": 2,
            r"\bgroup by\b|\border by\b": 2,
            r"\binsert into\b|\bupdate \w+ set\b|\bdelete from\b": 3,
            r"\bcreate table\b|\bprimary key\b": 3,
            r"\bstored procedure|\btrigger\b": 1,
        },
    ),
    Rule("React", "Веб-интерфейс", {r"\breact\b|\bjsx\b|usestate|redux": 6}),
    Rule("Angular", "Веб-интерфейс", {r"angular|ng-model|ng-repeat": 6}),
    Rule("jQuery", "Веб-интерфейс", {r"jquery": 5, r"""\$\(['"#.]""": 3, r"\.ajax\(": 2}),
    Rule(
        "JavaScript",
        "Веб-интерфейс",
        {
            r"javascript|\bjs\b": 3,
            r"document\.|window\.|addeventlistener": 2,
            r"\bvar \w+ =|\bfunction\s*\w*\s*\(|=>\s*\{|console\.log": 2,
        },
    ),
    Rule(
        "HTML и CSS",
        "Веб-интерфейс",
        {r"\bcss\b|stylesheet|<div|<span|<html|<table|<input": 2, r"\bhtml\b|bootstrap": 1},
    ),
    Rule(
        "Node.js",
        "Серверная веб-разработка",
        {r"node\.?js|\bnpm\b|express\.js|app\.listen\(|require\('": 5},
    ),
    Rule("Laravel", "Серверная веб-разработка", {r"laravel|eloquent|\bblade\b|artisan": 6}),
    Rule("WordPress", "Серверная веб-разработка", {r"wordpress|\bwp_": 6}),
    Rule(
        "PHP",
        "Серверная веб-разработка",
        {r"\bphp\b|<\?php": 4, r"\$_(get|post|session)|\becho \$": 3},
    ),
    Rule(
        "Ruby on Rails", "Серверная веб-разработка", {r"\bruby\b|\brails\b": 5, r"<%=|\.erb\b": 4}
    ),
    Rule(
        "Android", "Мобильная разработка", {r"android": 5, r"\bactivity\b|recyclerview|gradle": 2}
    ),
    Rule(
        "iOS",
        "Мобильная разработка",
        {
            r"\bios\b|\bswift\b|xcode|objective-c|uikit|cocoa": 5,
            r"view ?controller|nsstring|@property": 3,
            r"app store|itunes connect|\bipad\b|\biphone\b|bundle identifier": 4,
        },
    ),
    Rule(
        "C# и .NET",
        "Прикладные языки",
        {
            r"c#|\.net\b|asp\.net|\blinq\b|visual studio|winforms|\bwpf\b": 4,
            r"console\.writeline|\bnamespace \w+|\bpublic (partial )?class\b.*\{": 2,
            r"\{\s*get;\s*set;\s*\}|\bdim \w+ as\b|picturebox": 4,
            r"windows forms?|form application|vb\.net|\bvb6?\b": 4,
        },
    ),
    Rule(
        "Java",
        "Прикладные языки",
        {
            r"\bjava\b|\bjvm\b|\bspring (boot|mvc|framework)\b|hibernate|maven": 4,
            r"system\.out\.print|public static void main|arraylist|hashmap|simpledateformat": 3,
            r"\bstring\[\]|\.equals\(": 1,
            r"integer\.parseint|catch \(\w*exception|\.gettext\(\)|\bjframe\b|\bswing\b": 3,
        },
    ),
    Rule(
        "C и C++",
        "Прикладные языки",
        {
            r"c\+\+|\bstd::|#include|\bcout\b|\bprintf\(|malloc|\bstruct \w+\s*\{": 4,
            r"\boperator\w*\(|\btemplate\s*<|\bint main\(": 2,
        },
    ),
    Rule(
        "Python",
        "Прикладные языки",
        {
            r"python|pandas|numpy|django|flask|matplotlib|\bpip\b": 4,
            r"\bdef \w+\(|\bimport \w+|\bprint\(|\bself\.|\.py\b": 2,
            r"\bdtypes?\b|dataframe|scikit|sklearn|tensorflow|keras|scipy": 4,
        },
    ),
    Rule(
        "R",
        "Прикладные языки",
        {
            r"\bin r\b|ggplot|data\.frame|dplyr|\bcran\b|rstudio": 5,
            r"<-\s*\w|read\.(table|csv)\(": 3,
        },
    ),
    Rule("Go", "Прикладные языки", {r"\bgolang\b|\bgo func\b|\bfmt\.print": 5}),
    Rule("Delphi", "Прикладные языки", {r"delphi|\bpascal\b": 5}),
    Rule(
        "Excel и VBA", "Офисные приложения", {r"excel|\bvba\b|spreadsheet|\bmacro\b|\bcells\(": 5}
    ),
    Rule(
        "Hadoop и Spark",
        "Инфраструктура и ОС",
        {r"hadoop|mapreduce|apache spark|pyspark|\bhdfs\b": 5},
    ),
    Rule(
        "Git",
        "Инфраструктура и ОС",
        {r"\bgit\b|github|gitlab|\bgit (commit|merge|push|pull|rebase)": 4},
    ),
    Rule("Docker", "Инфраструктура и ОС", {r"docker|kubernetes|\bk8s\b": 5}),
    Rule(
        "Linux и командная строка",
        "Инфраструктура и ОС",
        {
            r"linux|ubuntu|\bbash\b|shell script|\bsudo\b|chmod|\bgrep\b|\bsed\b|\bawk\b": 3,
            r"\bvim\b|\.vimrc|\bcron\b|\bssh\b": 3,
        },
    ),
    Rule(
        "Windows",
        "Инфраструктура и ОС",
        {
            r"\bwindows\b|powershell|\.bat\b|regedit|windows registry": 3,
            r"%\w+:|\bfor /f\b|\bcmd\.exe\b": 3,
        },
    ),
    Rule(
        "Регулярные выражения",
        "Общие вопросы программирования",
        {r"regex|regular expression|regexp": 4},
    ),
)
#: Категория для нераспознанного компонента.
FALLBACK_CATEGORY = "Общие вопросы программирования"
_COMPILED = tuple(
    (rule, tuple((re.compile(p, re.IGNORECASE), w) for p, w in rule.patterns.items()))
    for rule in RULES
)


def classify(text_en: str) -> tuple[str, str]:
    """Определяет компонент и категорию обращения по английскому тексту.

    Каждый признак учитывается один раз со своим весом; побеждает компонент
    с наибольшей суммой, при равенстве — стоящий раньше в :data:`RULES`.

    Args:
        text_en: Исходный английский текст обращения.

    Returns:
        Пара (категория, компонент); при отсутствии признаков —
        «Общие вопросы программирования» и «Не определён».
    """
    best: tuple[int, Rule] | None = None
    for rule, patterns in _COMPILED:
        score = sum(w for rx, w in patterns if rx.search(text_en))
        if score and (best is None or score > best[0]):
            best = (score, rule)
    if best is None:
        return FALLBACK_CATEGORY, UNKNOWN_COMPONENT
    return best[1].category, best[1].component


def _cut(text: str, limit: int) -> str:
    """Обрезает текст по границе слова, добавляя многоточие."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    space = head.rfind(" ")
    return (head[:space] if space > limit // 2 else head).rstrip(" ,;:") + "…"


def make_title(text_ru: str) -> str:
    """Формирует заголовок: первое предложение с кириллицей, не длиннее 200 символов.

    Args:
        text_ru: Русский текст обращения.

    Returns:
        Заголовок; если кириллицы нет — начало текста.
    """
    flat = " ".join(text_ru.split())
    sentences = re.split(r"(?<=[.?!])\s+", flat)
    title = next((s for s in sentences if re.search(r"[А-Яа-яЁё]", s)), flat)
    return _cut(title, TITLE_MAX)


def make_description(text_ru: str) -> str:
    """Формирует описание: русский текст не длиннее 4000 символов.

    Args:
        text_ru: Русский текст обращения.

    Returns:
        Описание.
    """
    return _cut(text_ru.strip(), DESCRIPTION_MAX)


def _read(path: Path) -> list[dict[str, str]]:
    """Читает исходный CSV набора SODD."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _split_pairs(pairs: list[dict[str, str]]) -> set[str]:
    """Выбирает пары проверочной части: долю каждого класса отдельно."""
    holdout: set[str] = set()
    for relation in sorted({p["relation"] for p in pairs}):
        keys = [p["pair_id"] for p in pairs if p["relation"] == relation]
        holdout |= stable_holdout(keys, HOLDOUT_SHARE)
    return holdout


def convert(raw_dir: Path) -> Dataset:
    """Преобразует исходные файлы SODD-ru в эталонный набор.

    Args:
        raw_dir: Каталог с ``pairs.csv`` и ``tickets_ru.csv``.

    Returns:
        Набор: обращения со всеми графами и группы по парам
        ``duplicate`` и ``related``; пары ``different`` образуют
        независимые обращения. Обращения вне пар относятся к настроечной части.

    Raises:
        DatasetError: Обращение входит в пары разных частей разбиения.
        OSError: Исходные файлы не читаются.
        KeyError: В исходных файлах нет обязательных граф.
    """
    pairs = _read(raw_dir / "pairs.csv")
    holdout = _split_pairs(pairs)
    split_of: dict[str, str] = {}
    groups = []
    for p in pairs:
        split = "holdout" if p["pair_id"] in holdout else "tune"
        for tid in (p["ticket_id_a"], p["ticket_id_b"]):
            if split_of.setdefault(tid, split) != split:
                raise DatasetError(f"Обращение {tid} входит в пары разных частей разбиения")
        kind = RELATION_KIND.get(p["relation"])
        if kind:
            for tid in (p["ticket_id_a"], p["ticket_id_b"]):
                groups.append({"ticket_id": tid, "group_id": p["pair_id"], "kind": kind})
    tickets = []
    for row in _read(raw_dir / "tickets_ru.csv"):
        category, component = classify(row["text_en"])
        tickets.append(
            {
                "id": row["ticket_id"],
                "title": make_title(row["text_ru"]),
                "description": make_description(row["text_ru"]),
                "category": category,
                "component": component,
                "status": "new",
                "split": split_of.get(row["ticket_id"], "tune"),
                "source_id": row.get("source_row_position", ""),
                "translation_review": str(row["translation_status"].endswith("needs_review")),
            }
        )
    return Dataset(tickets=tickets, groups=groups)


def attribution(raw_dir: Path) -> str:
    """Формирует текст атрибуции авторов по лицензии CC BY-SA.

    Args:
        raw_dir: Каталог с ``tickets_ru.csv``.

    Returns:
        Markdown со ссылкой на источник и списком авторов вопросов.
    """
    rows = _read(raw_dir / "tickets_ru.csv")
    lines = [
        "# Атрибуция",
        "",
        "Тексты обращений — вопросы Stack Overflow из набора SODD, переведённые на",
        "русский язык; распространяются по лицензии CC BY-SA. Авторы вопросов:",
        "",
    ]
    lines += [f"- {r['ticket_id']}: {r['source_author'] or 'unknown'}" for r in rows]
    return "\n".join(lines) + "\n"
