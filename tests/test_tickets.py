"""Tickets: registration, transitions, solutions, history, drafts, dictionaries."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from spdo.analytics.models import ActionKind, ActionLogEntry
from spdo.tickets import dictionaries, drafts, service
from spdo.tickets.access import PermissionDeniedError, TicketNotFoundError, TicketValidationError
from spdo.tickets.models import ChangeKind, HistoryEntry
from spdo.tickets.models import TicketStatus as S
from spdo.tickets.transitions import (
    TRANSITIONS,
    TransitionError,
    Trigger,
    allowed_targets,
    check_transition,
)
from spdo.users.models import Role, User


def mk_user(session: Session, name: str, role: Role) -> User:
    u = User(username=name, full_name=name, role=role, password_hash="x")
    session.add(u)
    session.flush()
    return u


@pytest.fixture
def people(session: Session, base_rows):
    return SimpleNamespace(
        author=session.get(User, base_rows["user"]),
        other=mk_user(session, "other", Role.USER),
        op=mk_user(session, "op1", Role.OPERATOR),
        admin=mk_user(session, "adm1", Role.ADMIN),
        cat=base_rows["category"],
        comp=base_rows["component"],
    )


def new_ticket(session: Session, p, **kw):
    data = {
        "title": " Не печатает принтер ",
        "description": "Ошибка 0x01",
        "category_id": p.cat,
        "component_id": p.comp,
    }
    data.update(kw)
    return service.create_ticket(session, p.author, **data)


# --- transitions table -----------------------------------------------------


def test_table_matches_spec() -> None:
    expected = {
        (S.NEW, S.IN_PROGRESS),
        (S.NEW, S.CLOSED_DUPLICATE),
        (S.IN_PROGRESS, S.CLOSED_DUPLICATE),
        (S.IN_PROGRESS, S.RESOLVED),
        (S.NEW, S.CLOSED),
        (S.IN_PROGRESS, S.CLOSED),
        (S.RESOLVED, S.CLOSED),
        (S.RESOLVED, S.IN_PROGRESS),
        (S.CLOSED, S.IN_PROGRESS),
    }
    assert set(TRANSITIONS) == expected


@pytest.mark.parametrize("frm", list(S))
@pytest.mark.parametrize("to", list(S))
def test_everything_else_forbidden(frm, to) -> None:
    if (frm, to) in TRANSITIONS:
        return
    for trig in Trigger:
        with pytest.raises(TransitionError):
            check_transition(frm, to, trig, "comment")


def test_comment_and_trigger_rules() -> None:
    with pytest.raises(TransitionError, match="комментарий"):
        check_transition(S.NEW, S.CLOSED, Trigger.OPERATOR, "  ")
    with pytest.raises(TransitionError, match="событием"):
        check_transition(S.IN_PROGRESS, S.RESOLVED, Trigger.OPERATOR)
    check_transition(S.RESOLVED, S.CLOSED, Trigger.EXPIRY)
    assert allowed_targets(S.NEW, Trigger.OPERATOR) == [S.IN_PROGRESS, S.CLOSED]


# --- registration and visibility -------------------------------------------


def test_create_ticket(session: Session, people) -> None:
    t = new_ticket(session, people)
    assert (
        t.status is S.NEW and t.title == "Не печатает принтер" and t.author_id == people.author.id
    )
    hist = service.get_history(session, people.author, t.id)
    assert [(h.kind, h.new_value) for h in hist] == [(ChangeKind.CREATED, "new")]


@pytest.mark.parametrize(
    "kw",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"description": ""},
        {"description": "x" * 4001},
        {"category_id": 999999},
        {"component_id": 999999},
    ],
)
def test_create_ticket_validation(session: Session, people, kw) -> None:
    with pytest.raises(TicketValidationError):
        new_ticket(session, people, **kw)


def test_create_from_draft_removes_it(session: Session, people) -> None:
    d = drafts.save_draft(session, people.author, title="t")
    new_ticket(session, people, draft_id=d.id)
    assert drafts.list_drafts(session, people.author) == []


def test_foreign_draft_not_removed(session: Session, people) -> None:
    d = drafts.save_draft(session, people.other, title="t")
    new_ticket(session, people, draft_id=d.id)
    assert len(drafts.list_drafts(session, people.other)) == 1


def test_visibility(session: Session, people) -> None:
    t = new_ticket(session, people)
    assert service.get_ticket(session, people.author, t.id) is t
    assert service.get_ticket(session, people.op, t.id) is t
    with pytest.raises(TicketNotFoundError):
        service.get_ticket(session, people.other, t.id)
    with pytest.raises(TicketNotFoundError):
        service.get_history(session, people.other, t.id)
    with pytest.raises(TicketNotFoundError):
        service.get_ticket(session, people.op, 999999)


# --- lifecycle -------------------------------------------------------------


def test_full_lifecycle(session: Session, people) -> None:
    t = new_ticket(session, people)
    with pytest.raises(PermissionDeniedError):
        service.change_status(session, people.author, t.id, S.IN_PROGRESS)
    service.change_status(session, people.op, t.id, S.IN_PROGRESS)

    with pytest.raises(TicketValidationError):
        service.publish_solution(session, people.op, t.id, "  ")
    sol = service.publish_solution(session, people.op, t.id, "Переустановить драйвер")
    assert t.status is S.RESOLVED and sol.text == "Переустановить драйвер"

    with pytest.raises(PermissionDeniedError):
        service.confirm_resolution(session, people.op, t.id)
    service.confirm_resolution(session, people.author, t.id)
    assert t.status is S.CLOSED

    with pytest.raises(TransitionError):
        service.change_status(session, people.op, t.id, S.IN_PROGRESS)
    service.change_status(session, people.op, t.id, S.IN_PROGRESS, "Проблема вернулась")
    sol2 = service.publish_solution(session, people.op, t.id, "Заменить картридж")
    assert sol2.id == sol.id and sol2.text == "Заменить картридж"

    hist = service.get_history(session, people.op, t.id)
    statuses = [(h.old_value, h.new_value) for h in hist if h.kind is ChangeKind.STATUS]
    assert statuses == [
        ("new", "in_progress"),
        ("in_progress", "resolved"),
        ("resolved", "closed"),
        ("closed", "in_progress"),
        ("in_progress", "resolved"),
    ]
    sols = [(h.old_value, h.new_value) for h in hist if h.kind is ChangeKind.SOLUTION]
    assert sols == [
        (None, "Переустановить драйвер"),
        ("Переустановить драйвер", "Заменить картридж"),
    ]
    assert all(h.author_id for h in hist)
    reopen = next(h for h in hist if h.old_value == "closed")
    assert reopen.comment == "Проблема вернулась" and reopen.author_id == people.op.id

    log = session.scalars(select(ActionLogEntry).where(ActionLogEntry.ticket_id == t.id)).all()
    assert len(log) == 5 and all(e.action is ActionKind.STATUS_CHANGED for e in log)


def test_solution_requires_in_progress(session: Session, people) -> None:
    t = new_ticket(session, people)
    with pytest.raises(TransitionError):
        service.publish_solution(session, people.op, t.id, "x")
    assert t.solution is None


def test_close_without_solution_requires_comment(session: Session, people) -> None:
    t = new_ticket(session, people)
    with pytest.raises(TransitionError):
        service.change_status(session, people.op, t.id, S.CLOSED)
    service.change_status(session, people.op, t.id, S.CLOSED, "Не воспроизводится")
    assert t.status is S.CLOSED


def test_resolved_and_duplicate_not_via_change_status(session: Session, people) -> None:
    t = new_ticket(session, people)
    for target in (S.RESOLVED, S.CLOSED_DUPLICATE):
        with pytest.raises(TransitionError):
            service.change_status(session, people.op, t.id, target, "c")


def test_close_as_duplicate(session: Session, people) -> None:
    main = new_ticket(session, people)
    dup = new_ticket(session, people)
    service.close_as_duplicate(session, people.author, dup.id, main.id)
    assert dup.status is S.CLOSED_DUPLICATE
    last = service.get_history(session, people.op, dup.id)[-1]
    assert last.comment == f"Дубликат обращения {main.id}"
    with pytest.raises(TransitionError):
        service.close_as_duplicate(session, people.author, dup.id, main.id)
    with pytest.raises(TicketNotFoundError):
        service.close_as_duplicate(session, people.other, main.id, dup.id)


def test_confidentiality(session: Session, people) -> None:
    t = new_ticket(session, people)
    with pytest.raises(PermissionDeniedError):
        service.set_confidential(session, people.author, t.id, True)
    service.set_confidential(session, people.op, t.id, True, "Персональные данные")
    service.set_confidential(session, people.op, t.id, True)  # no-op, no history
    assert t.confidential is True
    hist = [
        h
        for h in service.get_history(session, people.op, t.id)
        if h.kind is ChangeKind.CONFIDENTIALITY
    ]
    assert [(h.old_value, h.new_value, h.comment) for h in hist] == [
        ("false", "true", "Персональные данные")
    ]


# --- close expired ---------------------------------------------------------


def resolve(session: Session, p, days_ago: float):
    t = new_ticket(session, p)
    service.change_status(session, p.op, t.id, S.IN_PROGRESS)
    service.publish_solution(session, p.op, t.id, "решение")
    when = datetime.now(UTC) - timedelta(days=days_ago)
    session.execute(
        update(HistoryEntry)
        .where(HistoryEntry.ticket_id == t.id, HistoryEntry.new_value == "resolved")
        .values(created_at=when)
    )
    return t


def test_close_expired(session: Session, people) -> None:
    old = resolve(session, people, 20)
    fresh = resolve(session, people, 1)
    now = datetime.now(UTC)
    assert [t.id for t in service.find_expired(session, 14, now)] == [old.id]

    with pytest.raises(PermissionDeniedError):
        service.close_expired(session, people.op, 14, now)
    assert service.close_expired(session, people.admin, 14, now) == [old.id]
    session.refresh(old)
    session.refresh(fresh)
    assert old.status is S.CLOSED and fresh.status is S.RESOLVED
    last = service.get_history(session, people.admin, old.id)[-1]
    assert last.author_id == people.admin.id and "14" in last.comment
    assert service.close_expired(session, people.admin, 14, now) == []


def test_reopened_then_resolved_uses_latest_resolution(session: Session, people) -> None:
    t = resolve(session, people, 30)
    service.change_status(session, people.op, t.id, S.IN_PROGRESS, "снова")
    service.publish_solution(session, people.op, t.id, "новое решение")  # resolved now
    assert service.find_expired(session, 14, datetime.now(UTC)) == []


# --- drafts ----------------------------------------------------------------


def test_drafts(session: Session, people) -> None:
    d = drafts.save_draft(session, people.author, title="Прин", category_id=people.cat)
    d2 = drafts.save_draft(
        session, people.author, draft_id=d.id, title="Принтер", description="не печатает"
    )
    assert d2.id == d.id and d2.title == "Принтер" and d2.category_id is None
    assert drafts.get_draft(session, people.author, d.id) is d
    with pytest.raises(TicketNotFoundError):
        drafts.get_draft(session, people.other, d.id)
    with pytest.raises(TicketNotFoundError):
        drafts.save_draft(session, people.other, draft_id=d.id, title="hijack")
    with pytest.raises(TicketValidationError):
        drafts.save_draft(session, people.author, title="x" * 201)
    drafts.save_draft(session, people.author, title="", description="")  # empty draft allowed
    assert len(drafts.list_drafts(session, people.author)) == 2
    drafts.delete_draft(session, people.author, d.id)
    assert len(drafts.list_drafts(session, people.author)) == 1


# --- dictionaries ----------------------------------------------------------


def test_dictionaries(session: Session, people) -> None:
    with pytest.raises(PermissionDeniedError):
        dictionaries.create_category(session, people.op, "Сеть")
    c = dictionaries.create_category(session, people.admin, " Сеть ", "VPN, Wi-Fi")
    assert c.name == "Сеть"
    with pytest.raises(TicketValidationError, match="уже есть"):
        dictionaries.create_category(session, people.admin, "Сеть")
    with pytest.raises(TicketValidationError):
        dictionaries.create_component(session, people.admin, "  ")
    m = dictionaries.create_component(session, people.admin, "Принтеры")
    assert "Сеть" in [x.name for x in dictionaries.list_categories(session)]
    assert "Принтеры" in [x.name for x in dictionaries.list_components(session)]

    dictionaries.rename_category(session, people.admin, c.id, "Сети", "все сети")
    assert c.name == "Сети" and c.description == "все сети"
    dictionaries.rename_component(session, people.admin, m.id, "Печать")
    # conflict on rename rolls back only the rename
    with pytest.raises(TicketValidationError):
        dictionaries.rename_category(session, people.admin, c.id, "c")  # base_rows category
    assert c.name == "Сети"
    with pytest.raises(TicketValidationError):
        dictionaries.rename_component(session, people.admin, m.id, "m")
    with pytest.raises(TicketNotFoundError):
        dictionaries.rename_category(session, people.admin, 999999, "x")
    with pytest.raises(TicketNotFoundError):
        dictionaries.rename_component(session, people.admin, 999999, "x")
    # session is still usable after the rolled-back savepoint
    assert dictionaries.create_component(session, people.admin, "Сканеры").id


# --- review fixes ----------------------------------------------------------


def test_lock_rereads_stale_object(session: Session, people) -> None:
    from spdo.tickets.models import Ticket

    t = new_ticket(session, people)
    assert t.status is S.NEW
    # another transaction changed the row; the identity-map object is stale
    session.execute(update(Ticket).where(Ticket.id == t.id).values(status="closed"), execution_options={"synchronize_session": False})
    assert t.status is S.NEW
    locked = service.get_ticket(session, people.op, t.id, lock=True)
    assert locked.status is S.CLOSED
    with pytest.raises(TransitionError):
        service.change_status(session, people.op, t.id, S.IN_PROGRESS)  # closed->in_progress needs comment


def test_history_ordered_by_id_not_date(session: Session, people) -> None:
    t = new_ticket(session, people)
    service.change_status(session, people.op, t.id, S.IN_PROGRESS)
    # all entries of one transaction share now(); push the later one back in time
    session.execute(update(HistoryEntry).where(HistoryEntry.ticket_id == t.id, HistoryEntry.kind == ChangeKind.STATUS).values(created_at=datetime(2000, 1, 1, tzinfo=UTC)))
    kinds = [h.kind for h in service.get_history(session, people.op, t.id)]
    assert kinds == [ChangeKind.CREATED, ChangeKind.STATUS]


def test_confirm_by_stranger_is_not_found(session: Session, people) -> None:
    t = resolve(session, people, 0)
    with pytest.raises(TicketNotFoundError):
        service.confirm_resolution(session, people.other, t.id)


def test_duplicate_of_itself(session: Session, people) -> None:
    t = new_ticket(session, people)
    with pytest.raises(TicketValidationError):
        service.close_as_duplicate(session, people.author, t.id, t.id)


def test_draft_unknown_dictionary(session: Session, people) -> None:
    with pytest.raises(TicketValidationError, match="Категория"):
        drafts.save_draft(session, people.author, category_id=999999)
    with pytest.raises(TicketValidationError, match="Компонент"):
        drafts.save_draft(session, people.author, component_id=999999)


def test_dictionary_names_case_insensitive(session: Session, people) -> None:
    c = dictionaries.create_category(session, people.admin, "Сеть")
    with pytest.raises(TicketValidationError):
        dictionaries.create_category(session, people.admin, "сЕТЬ")
    dictionaries.rename_category(session, people.admin, c.id, "СЕТЬ")  # own name, other case
    assert c.name == "СЕТЬ"
    m = dictionaries.create_component(session, people.admin, "Почта")
    with pytest.raises(TicketValidationError):
        dictionaries.create_component(session, people.admin, "почта")
    with pytest.raises(TicketValidationError):
        dictionaries.rename_component(session, people.admin, m.id, "M")  # base_rows "m"


@pytest.mark.parametrize("bad", ["0", "-5", "x"])
def test_cli_days_must_be_positive(bad: str, capsys) -> None:
    from spdo.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["close-expired", "--admin", "a", "--days", bad])
    assert build_parser().parse_args(["close-expired", "--dry-run"]).admin is None
