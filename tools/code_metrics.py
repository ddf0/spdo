"""Метрики связности модулей и глубины наследования классов.

Дополняет radon двумя метриками из набора Visual Studio Code Metrics:

* связность (Class Coupling) — на уровне модулей: Ce — число модулей
  проекта, от которых модуль зависит (исходящие связи), Ca — число модулей,
  зависящих от него (входящие), нестабильность I = Ce / (Ca + Ce);
* глубина наследования (Depth of Inheritance) — длина цепочки базовых
  классов до ``object`` для каждого класса проекта.

Запуск: ``python tools/code_metrics.py src/spdo`` — выводит JSON в stdout.
"""

import ast
import importlib
import inspect
import json
import sys
from pathlib import Path


def module_name(path: Path, root: Path) -> str:
    """Возвращает полное имя модуля по пути к файлу."""
    rel = path.relative_to(root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path, package: str) -> set[str]:
    """Возвращает модули проекта, импортируемые файлом."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith(package)}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(package):
            found.add(node.module)
            # «from spdo.tickets import service» — импорт модуля, а не имени
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def coupling(root: Path) -> dict[str, dict[str, float]]:
    """Вычисляет Ce, Ca и нестабильность для каждого модуля пакета.

    Миграции Alembic исключаются: это сгенерированный код.
    """
    package = root.name
    files = [p for p in root.rglob("*.py") if "migrations" not in p.parts]
    modules = {module_name(p, root): p for p in files}
    deps = {m: (imports_of(p, package) & modules.keys()) - {m} for m, p in modules.items()}
    result = {}
    for m in sorted(modules):
        ce = len(deps[m])
        ca = sum(m in d for d in deps.values())
        result[m] = {"Ce": ce, "Ca": ca, "I": round(ce / (ca + ce), 2) if ca + ce else 0.0}
    return result


def inheritance_depth(root: Path, own_only: bool = False) -> dict[str, int]:
    """Вычисляет глубину наследования каждого класса пакета.

    Глубина — число классов по самой длинной цепочке базовых классов до
    ``object``. По умолчанию учитываются и классы стандартной библиотеки и
    зависимостей, как в Visual Studio; с ``own_only`` — только классы
    проекта (собственная иерархия).
    """
    sys.path.insert(0, str(root.parent))
    depth: dict[str, int] = {}
    for p in sorted(root.rglob("*.py")):
        if "migrations" in p.parts:
            continue
        name = module_name(p, root)
        module = importlib.import_module(name)
        for cls_name, cls in vars(module).items():
            if inspect.isclass(cls) and cls.__module__ == name:
                depth[f"{name}.{cls_name}"] = _depth(cls, root.name if own_only else None)
    return depth


def _depth(cls: type, package: str | None = None) -> int:
    """Длина самой длинной цепочки базовых классов до ``object``.

    Если задан ``package``, учитываются только базовые классы этого пакета.
    """
    bases = [b for b in cls.__bases__ if b is not object]
    if package is not None:
        bases = [b for b in bases if b.__module__.startswith(package)]
    return 1 + max((_depth(b, package) for b in bases), default=0)


def main() -> None:
    """Выводит метрики пакета в формате JSON."""
    root = Path(sys.argv[1]).resolve()
    report = {
        "coupling": coupling(root),
        "inheritance_depth": inheritance_depth(root),
        "own_inheritance_depth": inheritance_depth(root, own_only=True),
    }
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
