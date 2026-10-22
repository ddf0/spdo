"""Сводка метрик кода и сравнение двух срезов в Markdown.

Читает файлы, которые формирует ``make metrics`` в ``reports/<срез>/``:
``cc.json``, ``mi.json``, ``raw.json``, ``hal.json`` (radon) и
``code_metrics.json`` (``tools/code_metrics.py``).

Запуск:
    python tools/metrics_report.py reports/metrics-before
    python tools/metrics_report.py reports/metrics-before reports/metrics-after
"""

import json
import sys
from pathlib import Path

#: Пороги «внимания» для перечня проблемных мест.
CC_WATCH = 6  # ранг B и выше
MI_WATCH = 65
CE_WATCH = 8
OWN_DIT_WATCH = 3


def _load(directory: Path, name: str) -> dict:
    return json.loads((directory / name).read_text(encoding="utf-8"))


def _block_name(path: str, block: dict) -> str:
    """Имя функции или метода вида ``модуль.Класс.метод``."""
    owner = f"{block['classname']}." if block.get("classname") else ""
    return f"{Path(path).stem}.{owner}{block['name']}"


def _skip(path: str) -> bool:
    """Миграции — сгенерированный код, в метриках не учитываются."""
    return "/migrations/" in path


def collect(directory: Path) -> dict:
    """Собирает показатели одного среза.

    Args:
        directory: Каталог среза метрик.

    Returns:
        Словарь показателей и перечней проблемных мест.
    """
    cc = _load(directory, "cc.json")
    blocks = [
        (
            _block_name(f, b),
            b["complexity"],
        )
        for f, items in cc.items()
        if not _skip(f) and isinstance(items, list)
        for b in items
        if b["type"] in ("function", "method")
    ]
    mi = {f: v["mi"] for f, v in _load(directory, "mi.json").items() if not _skip(f)}
    raw = [v for f, v in _load(directory, "raw.json").items() if not _skip(f)]
    hal = {f: v["total"] for f, v in _load(directory, "hal.json").items() if not _skip(f)}
    cm = _load(directory, "code_metrics.json")
    ce = {m: v["Ce"] for m, v in cm["coupling"].items()}
    dit = cm["inheritance_depth"]
    own = cm["own_inheritance_depth"]
    complexities = [c for _, c in blocks]
    return {
        "functions": len(blocks),
        "cc_avg": sum(complexities) / len(complexities),
        "cc_max": max(complexities),
        "cc_ge_watch": sum(c >= CC_WATCH for c in complexities),
        "mi_min": min(mi.values()),
        "mi_avg": sum(mi.values()) / len(mi),
        "loc": sum(r["loc"] for r in raw),
        "sloc": sum(r["sloc"] for r in raw),
        "lloc": sum(r["lloc"] for r in raw),
        "docs_pct": 100 * sum(r["multi"] + r["comments"] for r in raw) / sum(r["loc"] for r in raw),
        "hal_volume": sum(h["volume"] for h in hal.values()),
        "hal_bugs": sum(h["bugs"] for h in hal.values()),
        "hal_difficulty_max": max(h["difficulty"] for h in hal.values()),
        "ce_avg": sum(ce.values()) / len(ce),
        "ce_max": max(ce.values()),
        "dit_max": max(dit.values()),
        "own_dit_max": max(own.values()),
        "watch_cc": sorted(((n, c) for n, c in blocks if c >= CC_WATCH), key=lambda x: -x[1]),
        "watch_mi": sorted(
            (
                (Path(f).as_posix().split("src/")[-1], round(v, 1))
                for f, v in mi.items()
                if v < MI_WATCH
            ),
            key=lambda x: x[1],
        ),
        "watch_ce": sorted(((m, c) for m, c in ce.items() if c >= CE_WATCH), key=lambda x: -x[1]),
        "watch_dit": sorted(
            ((n, d) for n, d in own.items() if d >= OWN_DIT_WATCH), key=lambda x: -x[1]
        ),
    }


ROWS = [
    ("Функций и методов", "functions", "{:d}"),
    ("Цикломатическая сложность, средняя", "cc_avg", "{:.2f}"),
    ("Цикломатическая сложность, максимальная", "cc_max", "{:d}"),
    (f"Функций с CC ≥ {CC_WATCH}", "cc_ge_watch", "{:d}"),
    ("Индекс сопровождаемости, минимальный по модулю", "mi_min", "{:.1f}"),
    ("Индекс сопровождаемости, средний", "mi_avg", "{:.1f}"),
    ("Строк всего (LOC)", "loc", "{:d}"),
    ("Строк кода (SLOC)", "sloc", "{:d}"),
    ("Логических строк (LLOC)", "lloc", "{:d}"),
    ("Доля документации и комментариев, %", "docs_pct", "{:.1f}"),
    ("Объём Холстеда, суммарный", "hal_volume", "{:.0f}"),
    ("Трудность Холстеда, максимальная по модулю", "hal_difficulty_max", "{:.1f}"),
    ("Ожидаемые ошибки Холстеда, сумма", "hal_bugs", "{:.2f}"),
    ("Исходящая связность Ce, средняя", "ce_avg", "{:.2f}"),
    ("Исходящая связность Ce, максимальная", "ce_max", "{:d}"),
    ("Глубина наследования с учётом библиотек, максимальная", "dit_max", "{:d}"),
    ("Глубина собственной иерархии классов, максимальная", "own_dit_max", "{:d}"),
]


def _watch(title: str, items: list) -> list[str]:
    lines = [f"**{title}**", ""]
    lines += [f"- `{n}` — {v}" for n, v in items] or ["- нет"]
    return [*lines, ""]


def _watch_lists(m: dict) -> list[str]:
    return [
        *_watch(f"Цикломатическая сложность ≥ {CC_WATCH}", m["watch_cc"]),
        *_watch(f"Индекс сопровождаемости < {MI_WATCH}", m["watch_mi"]),
        *_watch(f"Исходящая связность Ce ≥ {CE_WATCH}", m["watch_ce"]),
        *_watch(f"Глубина собственной иерархии ≥ {OWN_DIT_WATCH}", m["watch_dit"]),
    ]


def summary(m: dict, title: str) -> str:
    """Формирует сводку одного среза."""
    lines = [f"# Метрики кода: {title}", "", "| Показатель | Значение |", "|---|---|"]
    lines += [f"| {name} | {fmt.format(m[key])} |" for name, key, fmt in ROWS]
    return "\n".join([*lines, "", "## Проблемные места", "", *_watch_lists(m)])


def compare(a: dict, b: dict) -> str:
    """Формирует сравнение двух срезов."""
    lines = [
        "# Метрики кода: до и после рефакторинга",
        "",
        "| Показатель | До | После | Изменение |",
        "|---|---|---|---|",
    ]
    for name, key, fmt in ROWS:
        delta = b[key] - a[key]
        sign = "+" if delta > 0 else ""
        lines.append(
            f"| {name} | {fmt.format(a[key])} | {fmt.format(b[key])} | {sign}{fmt.format(delta)} |"
        )
    lines += [
        "",
        "## Проблемные места до",
        "",
        *_watch_lists(a),
        "## Проблемные места после",
        "",
        *_watch_lists(b),
    ]
    return "\n".join(lines)


def main() -> None:
    """Печатает сводку одного среза или сравнение двух."""
    dirs = [Path(p) for p in sys.argv[1:]]
    if len(dirs) == 1:
        print(summary(collect(dirs[0]), dirs[0].name))
    else:
        print(compare(collect(dirs[0]), collect(dirs[1])))


if __name__ == "__main__":
    main()
