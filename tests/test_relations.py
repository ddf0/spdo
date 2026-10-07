"""Relations: duplicate/related links, cycle ban, groups, permissions, concurrency."""

import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from spdo.analytics.models import ActionKind, ActionLogEntry
from spdo.relations import service as rel
from spdo.relations.models import RelationKind as K
from spdo.tickets import service as tickets
from spdo.tickets.access import TicketNotFoundError
from spdo.tickets.models import ChangeKind
from spdo.tickets.models import TicketStatus as S
from spdo.tickets.transitions import TransitionError
from spdo.users.models import Role, User


def mk_user(session: Session, name: str, role: Role) -> User:
    u = User(username=name, full_name=name, role=role, password_hash="x")
    session.add(u)
    session.flush()
    return u


@pytest.fixture
def p(session: Session, base_rows):
    author = session.get(User, base_rows["user"])
    ns = SimpleNamespace(
        author=author,
        other=mk_user(session, "other", Role.USER),
        op=mk_user(session, "op1", Role.OPERATOR),
        cat=base_rows["category"],
        comp=base_rows["component"],
    )

    def new(owner=None, title="t"):
        return tickets.create_ticket(
            session,
            owner or author,
            title=title,
            description="d",
            category_id=ns.cat,
            component_id=ns.comp,
        )

    ns.new = new
    return ns


def test_duplicate_closes_source_and_records_everything(session: Session, p) -> None:
    main, dup = p.new(), p.new()
    link = rel.create_link(session, p.author, source_id=dup.id, target_id=main.id, kind=K.DUPLICATE)
    assert link.target_id == main.id and link.author_id == p.author.id
    assert dup.status is S.CLOSED_DUPLICATE and main.status is S.NEW
    for t, other in ((dup, main), (main, dup)):
        links = [h for h in tickets.get_history(session, p.op, t.id) if h.kind is ChangeKind.LINK]
        assert [h.new_value for h in links] == [f"duplicate:{other.id}"]
    log = session.scalars(
        select(ActionLogEntry).where(ActionLogEntry.action == ActionKind.LINK_CREATED)
    ).all()
    assert [(e.ticket_id, e.other_ticket_id) for e in log] == [(dup.id, main.id)]
    assert rel.list_links(session, p.author, main.id) == [link]
    assert rel.duplicates_of(session, main.id) == [dup.id]
    assert rel.root_main(session, dup.id) == main.id and rel.root_main(session, main.id) == main.id


def test_related_keeps_statuses(session: Session, p) -> None:
    a, b = p.new(), p.new()
    rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.RELATED)
    assert a.status is S.NEW and b.status is S.NEW
    assert rel.duplicates_of(session, b.id) == []


def test_self_and_existing_links(session: Session, p) -> None:
    a, b = p.new(), p.new()
    with pytest.raises(rel.SelfLinkError):
        rel.create_link(session, p.op, source_id=a.id, target_id=a.id, kind=K.RELATED)
    rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.RELATED)
    with pytest.raises(rel.LinkExistsError):
        rel.create_link(session, p.op, source_id=b.id, target_id=a.id, kind=K.RELATED)
    with pytest.raises(rel.LinkExistsError):
        rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.DUPLICATE)


def test_direct_cycle(session: Session, p) -> None:
    a, b = p.new(), p.new()
    rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.DUPLICATE)
    # b is a main; making it a duplicate of its own duplicate is a cycle
    with pytest.raises(rel.CycleError):
        rel.create_link(session, p.op, source_id=b.id, target_id=a.id, kind=K.DUPLICATE)


def test_indirect_cycle_detected_with_path(session: Session, p) -> None:
    a, b, c = p.new(), p.new(), p.new()
    rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.DUPLICATE)
    # build chain a -> b -> c through raw links to bypass root redirection, then test c -> a
    session.execute(
        text(
            "insert into links(source_id, target_id, kind, author_id) values (:s, :t, 'duplicate', :u)"
        ),
        {"s": b.id, "t": c.id, "u": p.op.id},
    )
    assert rel.main_chain(session, a.id) == [a.id, b.id, c.id]
    with pytest.raises(rel.CycleError, match=rf"{c.id} → {a.id} образует цикл") as err:
        rel.create_link(session, p.op, source_id=c.id, target_id=a.id, kind=K.DUPLICATE)
    assert str(b.id) not in str(err.value).replace(str(c.id), "").replace(str(a.id), "")


def test_target_duplicate_redirects_to_root(session: Session, p) -> None:
    root, mid, new = p.new(), p.new(), p.new()
    rel.create_link(session, p.op, source_id=mid.id, target_id=root.id, kind=K.DUPLICATE)
    link = rel.create_link(session, p.op, source_id=new.id, target_id=mid.id, kind=K.DUPLICATE)
    assert link.target_id == root.id
    assert rel.duplicates_of(session, root.id) == sorted([mid.id, new.id])
    last = [h for h in tickets.get_history(session, p.op, new.id) if h.kind is ChangeKind.LINK][-1]
    assert f"выбрано {mid.id}" in last.comment


def test_already_duplicate(session: Session, p) -> None:
    a, b, c = p.new(), p.new(), p.new()
    rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.DUPLICATE)
    with pytest.raises((rel.LinkExistsError, TransitionError)):
        rel.create_link(session, p.op, source_id=a.id, target_id=c.id, kind=K.DUPLICATE)


def test_resolved_source_cannot_become_duplicate(session: Session, p) -> None:
    a, b = p.new(), p.new()
    tickets.change_status(session, p.op, a.id, S.IN_PROGRESS)
    tickets.publish_solution(session, p.op, a.id, "fix")
    with pytest.raises(TransitionError):
        rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.DUPLICATE)
    assert rel.list_links(session, p.op, a.id) == []


def test_permissions_and_confidentiality(session: Session, p) -> None:
    mine = p.new()
    foreign = p.new(owner=p.other)
    secret = p.new(owner=p.other)
    tickets.set_confidential(session, p.op, secret.id, True)
    # user may not use someone else's ticket as source
    with pytest.raises(TicketNotFoundError):
        rel.create_link(session, p.author, source_id=foreign.id, target_id=mine.id, kind=K.RELATED)
    # but may link own ticket to a foreign non-confidential one
    rel.create_link(session, p.author, source_id=mine.id, target_id=foreign.id, kind=K.RELATED)
    # confidential foreign target is hidden
    other_mine = p.new()
    with pytest.raises(TicketNotFoundError):
        rel.create_link(
            session, p.author, source_id=other_mine.id, target_id=secret.id, kind=K.DUPLICATE
        )
    # operator can
    rel.create_link(session, p.op, source_id=other_mine.id, target_id=secret.id, kind=K.DUPLICATE)
    with pytest.raises(TicketNotFoundError):
        rel.create_link(session, p.op, source_id=999999, target_id=mine.id, kind=K.RELATED)
    with pytest.raises(TicketNotFoundError):
        rel.list_links(session, p.other, mine.id)


def test_record_link_unknown_ticket(session: Session, p) -> None:
    with pytest.raises(TicketNotFoundError):
        tickets.record_link(session, p.op, 999999, 1, "related")


# --- real concurrency ----------------------------------------------------


@pytest.fixture
def committed(engine: Engine):
    """Committed users/tickets visible to independent connections; wiped afterwards."""
    with Session(engine) as s:
        u = User(username="race-op", full_name="r", role=Role.OPERATOR, password_hash="x")
        s.add(u)
        s.flush()
        cat = s.execute(
            text("insert into categories(name, description) values ('race-c', '') returning id")
        ).scalar_one()
        comp = s.execute(
            text("insert into components(name) values ('race-m') returning id")
        ).scalar_one()
        a = tickets.create_ticket(
            s, u, title="a", description="d", category_id=cat, component_id=comp
        )
        b = tickets.create_ticket(
            s, u, title="b", description="d", category_id=cat, component_id=comp
        )
        s.commit()
        ids = SimpleNamespace(user=u.id, a=a.id, b=b.id)
    yield ids
    with engine.begin() as c:
        c.execute(
            text(
                "truncate action_log, ticket_history, links, tickets, users, categories, components cascade"
            )
        )


def test_concurrent_opposite_duplicates_serialized(engine: Engine, committed) -> None:
    errors: list[BaseException] = []
    started = threading.Event()

    s1 = Session(engine)
    op1 = s1.get(User, committed.user)
    rel.create_link(
        s1, op1, source_id=committed.a, target_id=committed.b, kind=K.DUPLICATE
    )  # holds the lock

    def second() -> None:
        with Session(engine) as s2:
            op2 = s2.get(User, committed.user)
            started.set()
            try:
                rel.create_link(
                    s2, op2, source_id=committed.b, target_id=committed.a, kind=K.DUPLICATE
                )
                s2.commit()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    t = threading.Thread(target=second)
    t.start()
    started.wait(5)
    time.sleep(0.5)  # second transaction is now waiting for the advisory lock
    assert t.is_alive()
    s1.commit()
    s1.close()
    t.join(10)
    assert len(errors) == 1 and isinstance(errors[0], rel.CycleError | rel.LinkExistsError)
    with Session(engine) as s:
        assert len(s.scalars(select(text("1")).select_from(text("links"))).all()) == 1



def test_source_with_duplicates_cannot_become_duplicate(session: Session, p) -> None:
    s_, d, t = p.new(), p.new(), p.new()
    rel.create_link(session, p.op, source_id=d.id, target_id=s_.id, kind=K.DUPLICATE)
    with pytest.raises(rel.HasDuplicatesError):
        rel.create_link(session, p.op, source_id=s_.id, target_id=t.id, kind=K.DUPLICATE)
    assert s_.status is S.NEW


def test_redirect_to_hidden_root_is_refused(session: Session, p) -> None:
    secret_root = p.new(owner=p.other)
    public_dup = p.new(owner=p.other)
    rel.create_link(session, p.op, source_id=public_dup.id, target_id=secret_root.id, kind=K.DUPLICATE)
    tickets.set_confidential(session, p.op, secret_root.id, True)
    mine = p.new()
    with pytest.raises(TicketNotFoundError) as err:
        rel.create_link(session, p.author, source_id=mine.id, target_id=public_dup.id, kind=K.DUPLICATE)
    assert str(secret_root.id) not in str(err.value).replace(str(public_dup.id), "")
    assert mine.status is S.NEW
    assert rel.list_links(session, p.author, mine.id) == []


def test_unknown_recommendation_is_integrity_error(session: Session, p) -> None:
    a, b = p.new(), p.new()
    with pytest.raises(rel.LinkIntegrityError):
        rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.RELATED, recommendation_id=999999)
    assert rel.list_links(session, p.op, a.id) == []


def test_chain_depth_guard(session: Session, p, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = p.new(), p.new()
    rel.create_link(session, p.op, source_id=a.id, target_id=b.id, kind=K.DUPLICATE)
    monkeypatch.setattr(rel, "MAX_CHAIN_DEPTH", 1)
    with pytest.raises(rel.RelationError, match="длиннее"):
        rel.main_chain(session, a.id)


def test_concurrent_opposite_related_no_deadlock(
    engine: Engine, committed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both transactions lock their own source first, then insert: must not deadlock."""
    barrier = threading.Barrier(2, timeout=5)
    original = tickets.get_link_target

    def paused(session, actor, ticket_id):  # runs right after the source row lock
        barrier.wait()
        return original(session, actor, ticket_id)

    monkeypatch.setattr(tickets, "get_link_target", paused)
    errors: list[BaseException] = []

    def worker(src: int, dst: int) -> None:
        with Session(engine) as s:
            op = s.get(User, committed.user)
            try:
                rel.create_link(s, op, source_id=src, target_id=dst, kind=K.RELATED)
                s.commit()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(committed.a, committed.b)),
        threading.Thread(target=worker, args=(committed.b, committed.a)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not any(t.is_alive() for t in threads)
    assert len(errors) == 1 and isinstance(errors[0], rel.LinkExistsError), errors
