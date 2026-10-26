"""Окружение шаблонов Jinja2 и пути к статике."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from spdo.config import settings
from spdo.tickets.models import TicketStatus
from spdo.users.deps import csrf_token
from spdo.users.models import Role

#: Каталог шаблонов страниц.
TEMPLATES_DIR = Path(__file__).parent / "templates"
#: Каталог статических файлов.
STATIC_DIR = Path(__file__).parent / "static"

#: Названия ролей для интерфейса.
ROLE_LABELS = {Role.USER: "пользователь", Role.OPERATOR: "оператор", Role.ADMIN: "администратор"}

#: Названия статусов обращения для интерфейса.
STATUS_LABELS = {
    TicketStatus.NEW: "Новое",
    TicketStatus.IN_PROGRESS: "В работе",
    TicketStatus.RESOLVED: "Решено",
    TicketStatus.CLOSED: "Закрыто",
    TicketStatus.CLOSED_DUPLICATE: "Закрыто как дубликат",
}

#: Названия предложенного вида связи (значения ``LinkKind`` модуля ``search``).
KIND_LABELS = {"duplicate": "Возможный дубликат", "related": "Возможно связанное"}


#: Часовой пояс дат в интерфейсе (имя проверено при чтении настроек).
DISPLAY_TZ = ZoneInfo(settings.display_timezone)


def local_time(value: datetime) -> str:
    """Форматирует дату в часовом поясе интерфейса.

    Args:
        value: Дата из БД; без часового пояса считается датой в UTC.

    Returns:
        Строка вида ``ДД.ММ.ГГГГ ЧЧ:ММ``.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")


#: Шаблонизатор приложения; в шаблонах доступны
#: ``csrf_token(request)``, ``role_labels`` и фильтр ``local_time``.
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["role_labels"] = ROLE_LABELS
templates.env.filters["local_time"] = local_time
