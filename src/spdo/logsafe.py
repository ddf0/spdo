"""Фильтр журналов, скрывающий пароли и токены.

ТЗ запрещает записывать пароли и токены доступа в журналы событий.
Пароли не логируются кодом приложения намеренно; фильтр — вторая линия
защиты на случай, если секрет окажется в тексте сообщения библиотеки
(строка запроса, заголовок, текст исключения).
"""

import logging
import re

#: Шаблон пары «ключ=значение» или «ключ: значение» с секретом. Ключ может
#: содержать секретное слово внутри (``password_hash``, ``X-CSRF-Token``);
#: значение — строка в кавычках, схема с токеном (``Bearer xyz``) или слово.
SECRET_RE = re.compile(
    r"(?P<key>[\w-]*(?:password|passwd|token|secret|session|cookie|authorization)[\w-]*)"
    r"(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+\S+|[^\s&\"',;]+)",
    re.IGNORECASE,
)
#: Замена значения секрета.
MASK = "***"


def redact(text: str) -> str:
    """Заменяет значения секретов в строке маской.

    Args:
        text: Исходная строка.

    Returns:
        Строка, в которой значения паролей, токенов и cookie скрыты.
    """
    return SECRET_RE.sub(lambda m: f"{m['key']}{m['sep']}{MASK}", text)


class RedactSecretsFilter(logging.Filter):
    """Фильтр, маскирующий секреты в сообщении и трассировке записи.

    Сообщение форматируется заранее, затем маскируется; аргументы
    записи очищаются, чтобы обработчики не подставили их повторно.
    Трассировка исключения форматируется в ``exc_text`` и тоже маскируется.
    """

    #: Форматтер для преобразования исключения в текст.
    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        """Маскирует секреты в записи.

        Args:
            record: Запись журнала.

        Returns:
            Всегда ``True`` — запись не отбрасывается.
        """
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(
    logger_names: tuple[str, ...] = ("", "uvicorn", "uvicorn.access", "uvicorn.error"),
) -> None:
    """Подключает фильтр к обработчикам указанных журналов.

    Фильтр ставится на обработчики, а не на сами журналы, чтобы
    действовать и на записи, пришедшие от дочерних журналов.

    Args:
        logger_names: Имена журналов; пустая строка — корневой журнал.
    """
    secret_filter = RedactSecretsFilter()
    for name in logger_names:
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactSecretsFilter) for f in handler.filters):
                handler.addFilter(secret_filter)
