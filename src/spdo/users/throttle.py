"""Ограничение частоты попыток входа и смены пароля.

Попытки считаются в скользящем окне отдельно по ключам (имя пользователя,
IP-адрес). Пока число попыток по любому ключу не меньше порога, операция
запрещена, и пароль даже не проверяется — перебор не нагружает сервер
вычислением bcrypt.

Проверка и учёт попытки выполняются атомарно (:meth:`LoginThrottle.reserve`)
до проверки пароля: параллельные запросы не проскакивают мимо порога.
Удачная попытка затем снимается (:meth:`LoginThrottle.release`), поэтому
удачные входы с общего адреса не копятся.

Учёт ведётся в памяти процесса, поэтому рекомендуется один процесс
uvicorn (как и для поискового индекса). За обратным прокси приложение
нужно запускать с ``--proxy-headers --forwarded-allow-ips=<адрес прокси>``,
иначе все пользователи придут с адреса прокси; если это невозможно,
ограничение по адресу отключается (``SPDO_LOGIN_MAX_FAILURES_IP=0``).
Заголовок ``X-Forwarded-For`` вручную не разбирается: его подделывает клиент.
"""

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

#: Наибольшее число отслеживаемых ключей; при переполнении вытесняется самый старый.
MAX_KEYS = 10_000


class LoginThrottle:
    """Счётчик попыток по ключам.

    Attributes:
        window: Окно подсчёта и срок запрета, секунды.
    """

    def __init__(self, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        """Создаёт пустой счётчик.

        Args:
            window: Окно подсчёта, секунды.
            clock: Источник времени (подменяется в проверках).
        """
        self.window = window
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._last_prune = clock()

    def _recent(self, key: str, now: float) -> deque[float]:
        """Попытки по ключу за окно; устаревшие отбрасываются."""
        attempts = self._failures.get(key, deque())
        while attempts and now - attempts[0] >= self.window:
            attempts.popleft()
        return attempts

    def _wait(self, limits: dict[str, int], now: float) -> float | None:
        """Секунды до снятия запрета или ``None``; порог 0 — без ограничения."""
        waits = []
        for key, limit in limits.items():
            attempts = self._recent(key, now)
            if limit and len(attempts) >= limit:
                waits.append(attempts[-limit] + self.window - now)
        return max(waits) if waits else None

    def _record(self, key: str, now: float) -> None:
        """Учитывает попытку по ключу, ограничивая число ключей."""
        if key not in self._failures and len(self._failures) >= MAX_KEYS:
            self._prune(now)
            if len(self._failures) >= MAX_KEYS:
                self._failures.popitem(last=False)  # самый давно обновлённый ключ
        attempts = self._recent(key, now)
        attempts.append(now)
        self._failures[key] = attempts
        self._failures.move_to_end(key)

    def _prune(self, now: float) -> None:
        """Удаляет ключи без попыток за окно, не чаще раза в десятую часть окна."""
        if now - self._last_prune < self.window / 10:
            return
        self._last_prune = now
        for key in list(self._failures):
            if not self._recent(key, now):
                del self._failures[key]

    def retry_after(self, limits: dict[str, int]) -> float | None:
        """Через сколько секунд операция станет возможна (без учёта попытки).

        Args:
            limits: Ключ → порог числа попыток (0 — без ограничения).

        Returns:
            Секунды до снятия запрета или ``None``, если операция разрешена.
        """
        with self._lock:
            return self._wait(limits, self._clock())

    def reserve(self, limits: dict[str, int]) -> float | None:
        """Атомарно проверяет пороги и, если операция разрешена, учитывает попытку.

        Вызывается до проверки пароля. Удачную попытку затем нужно снять
        методом :meth:`release`.

        Args:
            limits: Ключ → порог числа попыток (0 — без ограничения).

        Returns:
            Секунды до снятия запрета (попытка не учтена) или ``None`` —
            попытка учтена и разрешена.
        """
        now = self._clock()
        with self._lock:
            wait = self._wait(limits, now)
            for key in limits:
                if wait is None:
                    self._record(key, now)
                elif key in self._failures:
                    # Перебор по запрещённому ключу держит его «свежим»:
                    # вытеснение при переполнении не снимет запрет.
                    self._failures.move_to_end(key)
            return wait

    def failed(self, keys: list[str]) -> None:
        """Учитывает неудачную попытку без проверки порогов.

        Args:
            keys: Ключи попытки.
        """
        now = self._clock()
        with self._lock:
            for key in keys:
                self._record(key, now)

    def release(self, reset: str, keys: list[str]) -> None:
        """Снимает учтённую удачную попытку.

        Args:
            reset: Ключ, счётчик которого сбрасывается полностью (имя
                пользователя после успешного входа).
            keys: Ключи, с которых снимается одна последняя попытка (адрес).
        """
        with self._lock:
            self._failures.pop(reset, None)
            for key in keys:
                attempts = self._failures.get(key)
                if attempts:
                    attempts.pop()
