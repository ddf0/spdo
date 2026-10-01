"""Хеширование и проверка паролей.

Используется bcrypt: соль генерируется для каждого пароля и хранится в
самом хеше (требование ТУ — хеш с солью). bcrypt учитывает только первые
72 байта пароля, поэтому более длинные пароли отклоняются явно, а не
обрезаются молча.
"""

import bcrypt

from spdo.config import settings

#: Наибольшая длина пароля в байтах UTF-8, которую учитывает bcrypt.
PASSWORD_MAX_BYTES = 72
#: Наименьшая длина пароля в символах.
PASSWORD_MIN_CHARS = 8


class PasswordPolicyError(ValueError):
    """Пароль не удовлетворяет требованиям к длине."""


def check_password_policy(password: str) -> None:
    """Проверяет длину пароля.

    Args:
        password: Открытый пароль.

    Raises:
        PasswordPolicyError: Пароль короче 8 символов или длиннее 72 байт.
    """
    if len(password) < PASSWORD_MIN_CHARS:
        raise PasswordPolicyError(f"Пароль должен содержать не менее {PASSWORD_MIN_CHARS} символов")
    if len(password.encode()) > PASSWORD_MAX_BYTES:
        raise PasswordPolicyError(f"Пароль должен занимать не более {PASSWORD_MAX_BYTES} байт")


def hash_password(password: str) -> str:
    """Вычисляет хеш пароля с новой случайной солью.

    Args:
        password: Открытый пароль.

    Returns:
        Хеш в формате bcrypt (``$2b$...``).

    Raises:
        PasswordPolicyError: Пароль не удовлетворяет требованиям к длине.
    """
    check_password_policy(password)
    salt = bcrypt.gensalt(rounds=settings.password_rounds)
    return bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Сравнивает пароль с сохранённым хешем.

    Пароль длиннее 72 байт заведомо не совпадает: такой хеш не мог быть
    создан :func:`hash_password`.

    Args:
        password: Открытый пароль, введённый пользователем.
        password_hash: Сохранённый хеш.

    Returns:
        ``True``, если пароль верен.
    """
    if len(password.encode()) > PASSWORD_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


#: Хеш для выравнивания времени ответа при входе несуществующего пользователя.
DUMMY_HASH = bcrypt.hashpw(
    b"dummy-password", bcrypt.gensalt(rounds=settings.password_rounds)
).decode()
