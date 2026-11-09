"""Профилирование обработки запросов СПДО на заполненной БД.

Запросы сценариев (подбор, реестр, карточка, массовые обращения)
выполняются через ``TestClient`` в одном процессе с приложением; БД и режим
поиска задаются как обычно (``SPDO_DATABASE_URL``, ``SPDO_SEARCH_MODE``),
пароль пользователей — ``SPDO_LOAD_PASSWORD``. Перед замером поиск
прогревается синхронно, затем каждый сценарий выполняется дважды:

* с ``cProfile`` — точная статистика по функциям (``<сценарий>.prof``,
  ``<сценарий>.txt`` с первыми строками по суммарному и собственному
  времени);
* с ``pyinstrument`` — дерево вызовов (``<сценарий>.html`` и
  ``<сценарий>-tree.txt``).

Синхронные обработчики FastAPI выполняются в пуле потоков, а профилировщики
Python видят только свой поток, поэтому профилируется каждый вызов в пуле
(подменяются ``run_in_threadpool`` и вход и выход зависимостей-генераторов,
например сессии БД) и профили затем объединяются. Асинхронные промежуточные
обработчики (сессия, заголовки) и сам сервер не профилируются; их долю
показывает сравнение суммарного времени профиля со временем запросов без
профилировщика (``timings.txt``). Профилируемые запросы обращаются к другим
номерам обращений, чем прогревочные, чтобы не все данные были в кеше.

Пример::

    SPDO_LOAD_PASSWORD=... uv run python tools/profile_app.py reports/lab4/profile
"""

import argparse
import cProfile
import functools
import io
import os
import pstats
import re
import statistics
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

os.environ.setdefault("SPDO_SEARCH_WARM_UP", "false")  # прогрев выполняется ниже синхронно

import fastapi.concurrency as fastapi_concurrency  # noqa: E402
import fastapi.dependencies.utils as fastapi_deps  # noqa: E402
import fastapi.routing as fastapi_routing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyinstrument import Profiler  # noqa: E402
from pyinstrument.renderers import ConsoleRenderer, HTMLRenderer  # noqa: E402
from pyinstrument.session import Session as ProfileSession  # noqa: E402

from spdo.db.session import SessionLocal  # noqa: E402
from spdo.main import app  # noqa: E402
from spdo.search.factory import get_searcher  # noqa: E402

#: Запросы подбора: заголовок и описание, как при наборе текста.
QUERIES = (
    ("Не подключается VPN", "При подключении из дома выдаёт ошибку 809"),
    ("Принтер печатает пустые листы", "МФУ на третьем этаже выдаёт чистую бумагу"),
    ("Не приходит почта", "Письма от внешних адресатов перестали доходить"),
    ("Забыл пароль от учётной записи", "Сброс пароля не приходит на почту"),
    ("Wi-Fi постоянно отваливается", "Сеть пропадает каждые пять минут"),
    ("Проектор не видит ноутбук", "Подключаю по HDMI, нет сигнала"),
)
#: Адреса реестра с фильтрами и полнотекстовым поиском.
REGISTRY = (
    "/tickets",
    "/tickets?status=new",
    "/tickets?q=%D0%BF%D1%80%D0%B8%D0%BD%D1%82%D0%B5%D1%80",
    "/tickets?q=vpn&status=closed",
    "/tickets?page=40",
)
#: Поле CSRF-токена в форме входа и на страницах.
FORM_TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
META_TOKEN = re.compile(r'name="csrf-token" content="([^"]+)"')
#: Строк статистики cProfile в отчёте.
TOP = 30


class ThreadpoolProfiler:
    """Профилирует вызовы, которые FastAPI выполняет в пуле потоков.

    Attributes:
        kind: ``cprofile``, ``pyinstrument`` или ``None`` (без профилирования).
    """

    def __init__(self) -> None:
        """Подменяет ``run_in_threadpool`` в модулях FastAPI."""
        self.kind: str | None = None
        self._lock = threading.Lock()
        self._cprofiles: list[cProfile.Profile] = []
        self._sessions: list[ProfileSession] = []
        original = fastapi_routing.run_in_threadpool

        async def run(func: Callable, *args: object, **kwargs: object) -> object:
            return await original(functools.partial(self._call, func), *args, **kwargs)

        fastapi_routing.run_in_threadpool = run
        fastapi_deps.run_in_threadpool = run
        self._patch_generator_dependencies()

    def _patch_generator_dependencies(self) -> None:
        """Профилирует вход и выход синхронных зависимостей-генераторов."""
        original = fastapi_concurrency.contextmanager_in_threadpool
        profiler = self

        class Profiled:
            def __init__(self, cm: object) -> None:
                self._cm = cm

            def __enter__(self) -> object:
                return profiler._call(self._cm.__enter__)

            def __exit__(self, *exc: object) -> object:
                return profiler._call(self._cm.__exit__, *exc)

        def wrapped(cm: object) -> object:
            return original(Profiled(cm))

        fastapi_concurrency.contextmanager_in_threadpool = wrapped
        fastapi_deps.contextmanager_in_threadpool = wrapped

    def _call(self, func: Callable, *args: object, **kwargs: object) -> object:
        """Выполняет вызов под профилировщиком текущего вида."""
        if self.kind == "cprofile":
            prof = cProfile.Profile()
            try:
                return prof.runcall(func, *args, **kwargs)
            finally:
                with self._lock:
                    self._cprofiles.append(prof)
        if self.kind == "pyinstrument":
            profiler = Profiler(interval=0.0005)
            profiler.start()
            try:
                return func(*args, **kwargs)
            finally:
                profiler.stop()
                with self._lock:
                    self._sessions.append(profiler.last_session)
        return func(*args, **kwargs)

    def start(self, kind: str | None) -> None:
        """Начинает сбор профилей указанного вида."""
        self._cprofiles, self._sessions, self.kind = [], [], kind

    def _captured(self, items: list) -> None:
        """Проверяет, что подмена сработала и вызовы были перехвачены."""
        if not items:
            raise RuntimeError("Вызовы в пуле потоков не перехвачены: подмена FastAPI не сработала")

    def stats(self) -> pstats.Stats:
        """Объединённая статистика cProfile."""
        self._captured(self._cprofiles)
        first, *rest = self._cprofiles
        stats = pstats.Stats(first)
        for prof in rest:
            stats.add(prof)
        return stats

    def session(self) -> ProfileSession:
        """Объединённая сессия pyinstrument."""
        self._captured(self._sessions)
        return functools.reduce(ProfileSession.combine, self._sessions)


def login(client: TestClient, username: str, password: str) -> str:
    """Входит в систему и возвращает CSRF-токен страниц.

    Raises:
        RuntimeError: Вход не выполнен.
    """
    client.cookies.clear()
    form = FORM_TOKEN.search(client.get("/login").text)
    if form is None:
        raise RuntimeError("На странице входа нет CSRF-токена")
    data = {"username": username, "password": password, "csrf_token": form.group(1)}
    client.post("/login", data=data)
    home = client.get("/")
    # Без входа главная перенаправляет на /login; CSRF-токен есть и там.
    meta = META_TOKEN.search(home.text)
    if home.url.path == "/login" or meta is None:
        raise RuntimeError(f"Вход пользователя {username} не выполнен")
    return meta.group(1)


def scenarios(args: argparse.Namespace) -> dict[str, tuple[str, Callable[[TestClient, str, int]]]]:
    """Сценарии: имя → (пользователь, функция запроса номер ``i``)."""

    def search(client: TestClient, csrf: str, i: int):
        title, description = QUERIES[i % len(QUERIES)]
        body = {"title": title, "description": description}
        return client.post("/api/search", json=body, headers={"X-CSRF-Token": csrf})

    return {
        "search": (args.user, search),
        "registry": (args.operator, lambda c, _, i: c.get(REGISTRY[i % len(REGISTRY)])),
        "card": (args.operator, lambda c, _, i: c.get(f"/tickets/{1 + i * 97 % args.tickets}")),
        "incidents": (args.operator, lambda c, _, i: c.get("/incidents")),
    }


def run(
    client: TestClient, request: Callable, csrf: str, count: int, offset: int = 0
) -> list[float]:
    """Выполняет ``count`` запросов (номера ``offset`` …) и возвращает их время, мс.

    Raises:
        RuntimeError: Ответ не 200.
    """
    times = []
    for i in range(offset, offset + count):
        start = time.perf_counter()
        response = request(client, csrf, i)
        times.append((time.perf_counter() - start) * 1000)
        if response.status_code != 200:
            raise RuntimeError(f"Ответ {response.status_code}: {response.text[:200]}")
    return times


def write_cprofile(stats: pstats.Stats, out: Path, name: str) -> None:
    """Сохраняет статистику cProfile: двоичный профиль и текстовый отчёт."""
    stats.dump_stats(out / f"{name}.prof")
    text = io.StringIO()
    stats.stream = text
    stats.strip_dirs()
    text.write(f"=== {name}: по суммарному времени (cumulative)\n")
    stats.sort_stats("cumulative").print_stats(TOP)
    text.write(f"=== {name}: по собственному времени (tottime)\n")
    stats.sort_stats("tottime").print_stats(TOP)
    (out / f"{name}.txt").write_text(text.getvalue(), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Профилирует сценарии и сохраняет отчёты.

    Returns:
        Код завершения.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="каталог отчётов")
    parser.add_argument("--user", default="user1", help="пользователь для подбора")
    parser.add_argument("--operator", default="operator", help="оператор для страниц")
    parser.add_argument("--requests", type=int, default=60, help="запросов на сценарий")
    parser.add_argument("--tickets", type=int, default=5000, help="наибольший номер обращения")
    parser.add_argument("--scenario", nargs="*", help="сценарии (по умолчанию все)")
    args = parser.parse_args(argv)
    password = os.environ.get("SPDO_LOAD_PASSWORD")
    if not password:
        print("Ошибка: задайте SPDO_LOAD_PASSWORD", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    profiler = ThreadpoolProfiler()
    with SessionLocal() as session:
        get_searcher().warm_up(session)
    timings = []
    with TestClient(app) as client:
        for name, (username, request) in scenarios(args).items():
            if args.scenario and name not in args.scenario:
                continue
            csrf = login(client, username, password)
            profiler.start(None)
            run(client, request, csrf, 3)  # прогрев кешей
            times = run(client, request, csrf, args.requests)
            p95 = statistics.quantiles(times, n=20)[-1]
            timings.append(
                f"{name:10} n={len(times)} mean_ms={statistics.mean(times):.1f} "
                f"p50_ms={statistics.median(times):.1f} p95_ms={p95:.1f} max_ms={max(times):.1f}"
            )
            print(timings[-1])
            profiler.start("cprofile")
            run(client, request, csrf, args.requests, offset=args.requests)
            write_cprofile(profiler.stats(), args.out, name)
            profiler.start("pyinstrument")
            run(client, request, csrf, args.requests, offset=2 * args.requests)
            session = profiler.session()
            (args.out / f"{name}.html").write_text(HTMLRenderer().render(session), "utf-8")
            tree = ConsoleRenderer(unicode=True, color=False, show_all=False).render(session)
            (args.out / f"{name}-tree.txt").write_text(tree, encoding="utf-8")
    (args.out / "timings.txt").write_text("\n".join(timings) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
