"""Вход в СПДО для нагрузочных сценариев wrk.

Выполняет вход по имени и паролю и печатает переменные окружения для
сценариев ``tools/load/*.lua``: cookie сессии и CSRF-токен. Пароль
берётся из переменной окружения ``SPDO_LOAD_PASSWORD``, чтобы не попадать
в историю команд. Используется только стандартная библиотека.

Пример::

    eval "$(SPDO_LOAD_PASSWORD=... python tools/load/session.py URL operator)"
"""

import os
import re
import shlex
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

#: Поле CSRF-токена в форме входа.
FORM_TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
#: CSRF-токен страницы для запросов из сценариев.
META_TOKEN = re.compile(r'name="csrf-token" content="([^"]+)"')


def login(base: str, username: str, password: str) -> tuple[str, str]:
    """Входит в систему и возвращает cookie сессии и CSRF-токен.

    Args:
        base: Адрес приложения без завершающей косой черты.
        username: Имя для входа.
        password: Пароль.

    Returns:
        Заголовок ``Cookie`` и CSRF-токен сессии.

    Raises:
        RuntimeError: Вход не удался.
    """
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = opener.open(f"{base}/login").read().decode()
    form = FORM_TOKEN.search(page)
    if form is None:
        raise RuntimeError("На странице входа нет CSRF-токена")
    data = urllib.parse.urlencode(
        {"username": username, "password": password, "csrf_token": form.group(1)}
    ).encode()
    opener.open(f"{base}/login", data=data).read()
    home = opener.open(f"{base}/").read().decode()
    meta = META_TOKEN.search(home)
    if meta is None:
        raise RuntimeError("Вход не выполнен: проверьте имя и пароль")
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    return cookie, meta.group(1)


def main(argv: list[str]) -> int:
    """Печатает ``export`` переменных ``SPDO_COOKIE`` и ``SPDO_CSRF``.

    Args:
        argv: Адрес приложения и имя пользователя.

    Returns:
        Код завершения.
    """
    if len(argv) != 2 or "SPDO_LOAD_PASSWORD" not in os.environ:
        print("Использование: SPDO_LOAD_PASSWORD=... session.py <адрес> <имя>", file=sys.stderr)
        return 2
    try:
        cookie, csrf = login(argv[0].rstrip("/"), argv[1], os.environ["SPDO_LOAD_PASSWORD"])
    except (OSError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"export SPDO_COOKIE={shlex.quote(cookie)} SPDO_CSRF={shlex.quote(csrf)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
