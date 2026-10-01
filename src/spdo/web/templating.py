"""Окружение шаблонов Jinja2 и пути к статике."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from spdo.users.deps import csrf_token
from spdo.users.models import Role

#: Каталог шаблонов страниц.
TEMPLATES_DIR = Path(__file__).parent / "templates"
#: Каталог статических файлов.
STATIC_DIR = Path(__file__).parent / "static"

#: Названия ролей для интерфейса.
ROLE_LABELS = {Role.USER: "пользователь", Role.OPERATOR: "оператор", Role.ADMIN: "администратор"}

#: Шаблонизатор приложения; в шаблонах доступны ``csrf_token(request)`` и ``role_labels``.
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["role_labels"] = ROLE_LABELS
