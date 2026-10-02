"""Генератор документации на исходный код в разметке md2gost.

Обходит пакет ``spdo``, читает docstring в стиле Google и комментарии
``#:`` и собирает Markdown, из которого конвертер md2gost делает ``.docx``
по ГОСТ 19.106-78 (профиль ``espd``). Источник текста тот же, что у
Sphinx, поэтому HTML- и Word-версии документации не расходятся.

Запуск: ``python docs/api/gost_md.py <каталог вывода>``; результат —
``api.md`` и рисунок ``img/inheritance.png``.
"""

import enum
import importlib
import inspect
import pkgutil
import re
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sphinx.pycode import ModuleAnalyzer  # noqa: E402

import spdo  # noqa: E402  путь к исходникам добавлен строкой выше

#: Порядок пакетов в документе.
PACKAGE_ORDER = [
    "spdo",
    "spdo.db",
    "spdo.users",
    "spdo.tickets",
    "spdo.relations",
    "spdo.recommendations",
    "spdo.search",
    "spdo.analytics",
    "spdo.web",
]
#: Заголовки секций docstring, которые разбираются отдельно.
SECTIONS = {
    "Args",
    "Arguments",
    "Returns",
    "Yields",
    "Raises",
    "Attributes",
    "Модули",
    "Пакеты",
    "Команды",
}

HEADER = """---
profile: espd
title: Система интеллектуального поиска дубликатов обращений в техническую поддержку
doctitle: Документация на исходный код
designation: ""
organization: НИУ ВШЭ МИЭМ
year: 2026
letter: ""
approve:
  post: "Руководитель дисциплины\\n«Технологии разработки программного обеспечения»"
  name: ""
developers:
  - post: "Руководитель проекта,\\nинженер по тестированию"
    name: Д.С. Красавин
  - post: "Разработчик,\\nтехнический писатель"
    name: В.А. Елькин
---

# Аннотация {.notoc}

Документ содержит описание исходного кода программы СПДО версии {version}:
пакетов, модулей, классов, функций, их параметров, возвращаемых значений и
исключений. Документ сформирован автоматически из комментариев документирования
исходного кода.

[TOC]

# Общие сведения

СПДО — веб-система регистрации обращений в техническую поддержку, которая при
создании нового обращения подбирает ранее зарегистрированные похожие обращения
и ранжирует их по коэффициенту сходства

$$ S = w_1 S_{sem} + w_2 S_{lex} + w_3 S_{cat} + w_4 S_{comp} $$

где $S_{sem}$ — семантическое сходство текстов;
$S_{lex}$ — лексическое сходство текстов;
$S_{cat}$ — признак совпадения категорий;
$S_{comp}$ — признак совпадения компонентов;
$w_1$, $w_2$, $w_3$, $w_4$ — весовые коэффициенты, сумма которых равна единице.

Решение о связывании обращений принимает пользователь или оператор технической
поддержки.

Программа построена как модульный монолит: каждая подсистема — пакет Python со
своими моделями и сервисными функциями. Подсистемы обращаются друг к другу
только через сервисные функции; поисковый модуль не зависит от бизнес-модулей.

Поисковый модуль скрыт за базовым классом `SimilaritySearcher`. Производные
классы `LexicalSearcher` и `HybridSearcher` переопределяют вычисление сходства;
иерархия классов приведена на рисунке ниже.

![Иерархия классов поиска похожих обращений](img/inheritance.png){width=90}
"""


@dataclass
class Doc:
    """Разобранный docstring.

    Attributes:
        summary: Краткое описание (первый абзац).
        details: Подробное описание — абзацы после краткого.
        sections: Именованные секции: имя → список пар (имя, текст) или
            текст для ``Returns``/``Yields``.
    """

    summary: str = ""
    details: list[str] = field(default_factory=list)
    sections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def rst_to_md(text: str) -> str:
    """Переводит разметку reStructuredText docstring в разметку md2gost.

    Args:
        text: Текст с ролями Sphinx и двойными обратными кавычками.

    Returns:
        Текст с одинарными обратными кавычками для имён.
    """
    text = re.sub(r":\w+:`([^`<]+?)\s*<[^`>]+>`", r"`\1`", text)
    text = re.sub(r":\w+:`~?(?:[\w.]+\.)?([\w]+)(?:\(\))?`", r"`\1`", text)
    return re.sub(r"``([^`]+)``", r"`\1`", text)


def _is_section(line: str) -> bool:
    """Проверяет, что строка — заголовок секции docstring."""
    head = line.rstrip()
    return not line.startswith(" ") and head.endswith(":") and head[:-1] in SECTIONS


def _add_item(section: str, items: list[tuple[str, str]], line: str) -> None:
    """Добавляет строку секции: новую запись «имя: текст» или продолжение."""
    text = line.strip()
    if not text:
        return
    if section not in ("Returns", "Yields") and re.match(r"    \S", line):
        name, _, rest = text.partition(":")
        items.append((name.strip(), rest.strip()))
    elif items and section not in ("Returns", "Yields"):
        name, prev = items[-1]
        items[-1] = (name, f"{prev} {text}".strip())
    else:
        items.append(("", text))


def parse_doc(obj: object) -> Doc:
    """Разбирает docstring в стиле Google.

    Args:
        obj: Модуль, класс или функция.

    Returns:
        Краткое и подробное описание и секции.
    """
    doc = Doc()
    body: list[str] = []
    current: str | None = None
    for line in (inspect.getdoc(obj) or "").splitlines():
        if _is_section(line):
            current = line.rstrip()[:-1]
            doc.sections[current] = []
        elif current is None:
            body.append(line)
        else:
            _add_item(current, doc.sections[current], line)
    paragraphs = [rst_to_md(p.strip()) for p in "\n".join(body).split("\n\n") if p.strip()]
    if paragraphs:
        doc.summary, doc.details = paragraphs[0], paragraphs[1:]
    return doc


def paragraph(text: str) -> str:
    """Склеивает строки абзаца и переводит маркеры ``*`` в маркеры списка ``-``.

    Args:
        text: Абзац docstring.

    Returns:
        Абзац в разметке md2gost.
    """
    lines = text.splitlines()
    if any(re.match(r"\s*\* ", ln) for ln in lines):
        out: list[str] = []
        for ln in lines:
            m = re.match(r"\s*\* (.*)", ln)
            if m:
                out.append(f"- {m.group(1)}")
            elif out and out[-1].startswith("- "):
                out[-1] += " " + ln.strip()
            else:
                out.append(ln.strip())
        joined: list[str] = []
        for ln in out:
            if ln.startswith("- ") and joined and not joined[-1].startswith("- "):
                joined.append("")
            joined.append(ln)
        return "\n".join(joined)
    return " ".join(ln.strip() for ln in lines)


def lower_first(text: str) -> str:
    """Делает первую букву строчной, если это не аббревиатура."""
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        return text[0].lower() + text[1:]
    return text


def cell(text: str) -> str:
    """Готовит текст для ячейки таблицы."""
    return rst_to_md(text).replace("|", "\\|").strip() or "—"


def label(qualname: str, suffix: str) -> str:
    """Строит метку таблицы из латиницы, цифр и дефисов."""
    return "tbl:" + re.sub(r"[^A-Za-z0-9]+", "-", f"{qualname}-{suffix}").strip("-").lower()


def short_type(text: str) -> str:
    """Убирает пути модулей из аннотации типа."""
    text = text.replace("typing.", "").replace("collections.abc.", "")
    return re.sub(r"\b(?:[a-z_]\w*\.)+([A-Za-z_]\w*)", r"\1", text)


def table(title: str, tag: str, intro: str, head: tuple[str, ...], rows: list[tuple]) -> list[str]:
    """Формирует таблицу md2gost со ссылкой на неё в тексте.

    Args:
        title: Наименование таблицы.
        tag: Метка таблицы.
        intro: Начало фразы со ссылкой, например «Параметры приведены».
        head: Заголовки граф.
        rows: Строки таблицы.

    Returns:
        Строки Markdown.
    """
    lines = [f"{intro} в таблице @{tag}.", "", f"Таблица: {title} {{#{tag}}}", ""]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("|" + "---|" * len(head))
    lines += ["| " + " | ".join(cell(str(c)) for c in row) + " |" for row in rows]
    return [*lines, ""]


def describe(doc: Doc) -> list[str]:
    """Выводит краткое и подробное описание."""
    out = [paragraph(doc.summary), ""] if doc.summary else []
    for p in doc.details:
        out += [paragraph(p), ""]
    return out


def plain(annotation: object) -> object:
    """Снимает обёртку ``Annotated``: метаданные FastAPI в документации не нужны."""
    if typing.get_origin(annotation) is typing.Annotated:
        return typing.get_args(annotation)[0]
    return annotation


def signature(func: object, name: str) -> str:
    """Возвращает сигнатуру функции без путей модулей и метаданных ``Annotated``."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return f"{name}(...)"
    params = [p.replace(annotation=plain(p.annotation)) for p in sig.parameters.values()]
    return short_type(f"{name}{sig.replace(parameters=params)}")


def _arg_rows(func: object, args: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Строки таблицы параметров: имя, тип из аннотации, описание."""
    params = inspect.signature(func).parameters
    rows = []
    for pname, text in args:
        p = params.get(pname.lstrip("*"))
        has_ann = p is not None and p.annotation is not p.empty
        ann = inspect.formatannotation(plain(p.annotation)) if has_ann else ""
        rows.append((f"`{pname}`", short_type(ann) or "—", text))
    return rows


def _result_lines(doc: Doc) -> list[str]:
    """Абзацы «Возвращает: …» и «Выдаёт: …»."""
    out = []
    for sec, verb in (("Returns", "Возвращает"), ("Yields", "Выдаёт")):
        if doc.sections.get(sec):
            text = rst_to_md(" ".join(t for _, t in doc.sections[sec]))
            out += [f"{verb}: {lower_first(text)}", ""]
    return out


def function_block(func: object, qualname: str, kind: str, level: str) -> list[str]:
    """Описывает функцию или метод.

    Args:
        func: Функция.
        qualname: Полное имя для меток.
        kind: «Функция» или «Метод».
        level: Уровень заголовка (``###`` или ``####``).

    Returns:
        Строки Markdown.
    """
    name = qualname.rsplit(".", 1)[-1]
    of = "метода" if kind == "Метод" else "функции"
    doc = parse_doc(func)
    out = [f"{level} {kind} {name}", "", "```python", signature(func, name), "```", ""]
    out += describe(doc)
    args = [a for a in doc.sections.get("Args", []) if a[0]]
    if args:
        out += table(
            f"Параметры {of} {name}",
            label(qualname, "args"),
            "Входные параметры приведены",
            ("Параметр", "Тип", "Описание"),
            _arg_rows(func, args),
        )
    out += _result_lines(doc)
    raises = [(f"`{n}`", t) for n, t in doc.sections.get("Raises", []) if n]
    if raises:
        out += table(
            f"Исключения {of} {name}",
            label(qualname, "raises"),
            "Возбуждаемые исключения приведены",
            ("Исключение", "Условие"),
            raises,
        )
    return out


def _attr_rows(cls: type, doc: Doc, attr_docs: dict) -> list[tuple[str, str]]:
    """Атрибуты класса из секции ``Attributes`` и комментариев ``#:``."""
    rows = [(f"`{n}`", t) for n, t in doc.sections.get("Attributes", []) if n]
    known = {r[0] for r in rows}
    for (owner, attr), lines in attr_docs.items():
        if owner == cls.__name__ and f"`{attr}`" not in known and not attr.startswith("_"):
            rows.append((f"`{attr}`", " ".join(lines).strip()))
    return rows


def _members_table(cls: type, qual: str, rows: list[tuple[str, str]]) -> list[str]:
    """Таблица значений перечисления или атрибутов класса."""
    if issubclass(cls, enum.Enum):
        values = [(f"`{m.name}`", f"`{m.value}`") for m in cls]
        return table(
            f"Значения перечисления {cls.__name__}",
            label(qual, "values"),
            "Значения приведены",
            ("Элемент", "Значение"),
            values,
        )
    if not rows:
        return []
    return table(
        f"Атрибуты класса {cls.__name__}",
        label(qual, "attrs"),
        "Атрибуты класса приведены",
        ("Атрибут", "Описание"),
        rows,
    )


def _methods(cls: type, qual: str) -> list[str]:
    """Описания документированных открытых методов и свойств класса."""
    if issubclass(cls, enum.Enum):
        return []
    out: list[str] = []
    for name, member in cls.__dict__.items():
        func = member.fget if isinstance(member, property) else member
        func = getattr(func, "__func__", func)
        if not name.startswith("_") and inspect.isfunction(func) and func.__doc__:
            out += function_block(func, f"{qual}.{name}", "Метод", "####")
    return out


def class_block(cls: type, modname: str, attr_docs: dict) -> list[str]:
    """Описывает класс: описание, базовые классы, атрибуты, методы.

    Args:
        cls: Класс.
        modname: Имя модуля, где класс определён.
        attr_docs: Комментарии ``#:`` модуля.

    Returns:
        Строки Markdown.
    """
    qual = f"{modname}.{cls.__name__}"
    doc = parse_doc(cls)
    out = [f"### Класс {cls.__name__}", "", *describe(doc)]
    bases = [b.__name__ for b in cls.__bases__ if b is not object]
    if bases:
        word = "Базовый класс" if len(bases) == 1 else "Базовые классы"
        out += [f"{word}: {', '.join(f'`{b}`' for b in bases)}.", ""]
    out += _members_table(cls, qual, _attr_rows(cls, doc, attr_docs))
    return out + _methods(cls, qual)


def module_constants(attr_docs: dict, module: object) -> list[tuple[str, str]]:
    """Возвращает документированные константы уровня модуля."""
    return [
        (f"`{attr}`", " ".join(lines).strip())
        for (owner, attr), lines in attr_docs.items()
        if owner == "" and hasattr(module, attr)
    ]


#: Секции docstring пакета, выводимые таблицами, и суффиксы их меток.
LIST_SECTIONS = {"Модули": "modules", "Пакеты": "packages", "Команды": "commands"}


def _list_tables(modname: str, doc: Doc) -> list[str]:
    """Таблицы секций «Модули», «Пакеты», «Команды»."""
    out: list[str] = []
    for sec, suffix in LIST_SECTIONS.items():
        rows = [(f"`{n}`", t) for n, t in doc.sections.get(sec, []) if n]
        if rows:
            owner = "модуля" if sec == "Команды" else "пакета"
            title = f"{sec} {owner} {modname}"
            out += table(
                title, label(modname, suffix), f"{sec} приведены", ("Имя", "Назначение"), rows
            )
    return out


def _attr_docs(modname: str) -> dict:
    """Комментарии ``#:`` модуля или пустой словарь, если исходник недоступен."""
    try:
        return ModuleAnalyzer.for_module(modname).find_attr_docs()
    except Exception:  # noqa: BLE001 — у модуля может не быть исходника
        return {}


def _own_members(module: object, modname: str) -> list[tuple[str, object]]:
    """Открытые классы и функции, определённые в самом модуле, без псевдонимов."""
    return [
        (name, obj)
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and getattr(obj, "__module__", None) == modname
        and getattr(obj, "__name__", name) == name
    ]


def module_block(modname: str) -> list[str]:
    """Описывает модуль: назначение, константы, классы и функции.

    Описание пакета (``__init__``) выводится под заголовком пакета без
    отдельного заголовка модуля.

    Args:
        modname: Полное имя модуля.

    Returns:
        Строки Markdown.
    """
    module = importlib.import_module(modname)
    doc = parse_doc(module)
    if hasattr(module, "__path__"):
        out = [paragraph(p) + "\n" for p in doc.details]
    else:
        out = [f"## Модуль {modname}", "", *describe(doc)]
    out += _list_tables(modname, doc)
    attr_docs = _attr_docs(modname)
    consts = module_constants(attr_docs, module)
    if consts:
        out += table(
            f"Константы модуля {modname}",
            label(modname, "consts"),
            "Константы модуля приведены",
            ("Имя", "Описание"),
            consts,
        )
    for name, obj in _own_members(module, modname):
        if inspect.isclass(obj):
            out += class_block(obj, modname, attr_docs)
        elif inspect.isfunction(obj) and obj.__doc__:
            out += function_block(obj, f"{modname}.{name}", "Функция", "###")
    return out


def package_modules(package: str) -> list[str]:
    """Возвращает пакет и его непосредственные модули (без подпакетов)."""
    pkg = importlib.import_module(package)
    names = [package]
    for info in pkgutil.iter_modules(pkg.__path__, package + "."):
        if not info.ispkg:
            names.append(info.name)
    return names


def inheritance_png(path: Path) -> None:
    """Рисует диаграмму наследования поисковиков средствами graphviz."""
    dot = """digraph G { rankdir=BT; node [shape=box, fontname="Liberation Sans", fontsize=12];
      edge [arrowhead=empty];
      LexicalSearcher -> SimilaritySearcher; HybridSearcher -> SimilaritySearcher;
      SimilaritySearcher -> ABC; }"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["dot", "-Tpng", "-Gdpi=200", "-o", str(path)], input=dot.encode(), check=True)


def build(out_dir: Path) -> Path:
    """Формирует ``api.md`` и рисунок в каталоге вывода.

    Args:
        out_dir: Каталог вывода.

    Returns:
        Путь к ``api.md``.
    """
    lines = [HEADER.replace("{version}", spdo.__version__)]
    for package in PACKAGE_ORDER:
        pkg_doc = parse_doc(importlib.import_module(package))
        lines += [f"# Пакет {package}", "", paragraph(pkg_doc.summary), ""]
        # Описание самого пакета выводится под его заголовком, без «Модуль …».
        for modname in package_modules(package):
            lines += module_block(modname)
    lines += ["[ЛРИ]", ""]
    inheritance_png(out_dir / "img" / "inheritance.png")
    md = out_dir / "api.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    return md


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/api/_build/gost")))
