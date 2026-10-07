"""Schema constraints of the initial migration."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from spdo.recommendations.models import Recommendation, SearchSession, Verdict
from spdo.relations.models import Link, RelationKind
from spdo.search.base import LinkKind
from spdo.search.models import SearchIndexEntry
from spdo.tickets.models import Solution, SolutionRating, Ticket, TicketStatus


def make_ticket(session: Session, rows: dict[str, int], title: str = "t") -> Ticket:
    t = Ticket(
        title=title,
        description="d",
        author_id=rows["user"],
        category_id=rows["category"],
        component_id=rows["component"],
    )
    session.add(t)
    session.flush()
    return t


def expect_integrity(session: Session, obj: object) -> None:
    with session.begin_nested():
        session.add(obj)
        with pytest.raises(IntegrityError):
            session.flush()


def test_ticket_defaults(session: Session, base_rows: dict[str, int]) -> None:
    t = make_ticket(session, base_rows)
    session.refresh(t)
    assert t.status is TicketStatus.NEW
    assert t.confidential is False
    assert t.created_at is not None


@pytest.mark.parametrize(("title", "description"), [("", "d"), ("t", ""), ("t", "x" * 4001)])
def test_ticket_length_checks(
    session: Session, base_rows: dict[str, int], title: str, description: str
) -> None:
    t = Ticket(
        title=title,
        description=description,
        author_id=base_rows["user"],
        category_id=base_rows["category"],
        component_id=base_rows["component"],
    )
    with session.begin_nested(), pytest.raises((IntegrityError, DataError)):
        session.add(t)
        session.flush()


def test_link_rules(session: Session, base_rows: dict[str, int]) -> None:
    a, b, c = (make_ticket(session, base_rows, n) for n in "abc")
    uid = base_rows["user"]

    expect_integrity(
        session, Link(source_id=a.id, target_id=a.id, kind=RelationKind.RELATED, author_id=uid)
    )

    session.add(Link(source_id=a.id, target_id=b.id, kind=RelationKind.DUPLICATE, author_id=uid))
    session.flush()

    # one link per pair regardless of direction and kind
    expect_integrity(
        session, Link(source_id=b.id, target_id=a.id, kind=RelationKind.RELATED, author_id=uid)
    )
    # at most one main ticket
    expect_integrity(
        session, Link(source_id=a.id, target_id=c.id, kind=RelationKind.DUPLICATE, author_id=uid)
    )

    session.add(Link(source_id=c.id, target_id=a.id, kind=RelationKind.RELATED, author_id=uid))
    session.flush()


def test_solution_and_rating_uniqueness(session: Session, base_rows: dict[str, int]) -> None:
    t = make_ticket(session, base_rows)
    uid = base_rows["user"]
    s = Solution(ticket_id=t.id, text="fix", author_id=uid)
    session.add(s)
    session.flush()
    assert t.solution is s

    expect_integrity(session, Solution(ticket_id=t.id, text="again", author_id=uid))

    session.add(SolutionRating(solution_id=s.id, user_id=uid, helped=True))
    session.flush()
    expect_integrity(session, SolutionRating(solution_id=s.id, user_id=uid, helped=False))


def test_session_and_recommendations(session: Session, base_rows: dict[str, int]) -> None:
    t = make_ticket(session, base_rows)
    ss = SearchSession(
        user_id=base_rows["user"],
        query_text="q",
        mode="lexical",
        model_version="tfidf-1",
        params={"p1": 0.8, "p2": 0.6, "n": 10},
    )
    ss.recommendations.append(
        Recommendation(ticket_id=t.id, position=1, score=0.9, suggested_kind=LinkKind.DUPLICATE)
    )
    session.add(ss)
    session.flush()
    rec = session.scalars(select(Recommendation)).one()
    assert rec.verdict is Verdict.PENDING
    assert ss.params["n"] == 10

    bad = [
        Recommendation(
            session_id=ss.id, ticket_id=t.id, position=2, score=0.7, suggested_kind=LinkKind.RELATED
        ),
        Recommendation(
            session_id=ss.id,
            ticket_id=t.id,
            position=11,
            score=0.7,
            suggested_kind=LinkKind.RELATED,
        ),
        Recommendation(
            session_id=ss.id, ticket_id=t.id, position=1, score=1.5, suggested_kind=LinkKind.RELATED
        ),
    ]
    for r in bad:
        expect_integrity(session, r)


def test_search_index_vector(session: Session, base_rows: dict[str, int]) -> None:
    t = make_ticket(session, base_rows)
    e = SearchIndexEntry(
        ticket_id=t.id,
        text="t d",
        author_id=base_rows["user"],
        embedding=[0.1] * 312,
        model_version="m",
    )
    session.add(e)
    session.flush()
    session.refresh(e)
    assert len(e.embedding) == 312

    with session.begin_nested(), pytest.raises(DataError):
        e.embedding = [0.1] * 3
        session.flush()


def test_verdict_consistency(session: Session, base_rows: dict[str, int]) -> None:
    from datetime import UTC, datetime

    t = make_ticket(session, base_rows)
    ss = SearchSession(
        user_id=base_rows["user"], query_text="q", mode="lexical", model_version="v", params={}
    )
    session.add(ss)
    session.flush()
    kw = {"session_id": ss.id, "ticket_id": t.id, "score": 0.9, "suggested_kind": LinkKind.DUPLICATE}
    expect_integrity(session, Recommendation(position=1, verdict=Verdict.REJECTED, **kw))
    expect_integrity(
        session, Recommendation(position=2, verdict_at=datetime.now(UTC), verdict_by=base_rows["user"], **kw)
    )
    ok = Recommendation(
        position=3, verdict=Verdict.REJECTED, verdict_at=datetime.now(UTC), verdict_by=base_rows["user"], **kw
    )
    session.add(ok)
    session.flush()


def test_action_log_blocks_ticket_delete(session: Session, base_rows: dict[str, int]) -> None:
    from spdo.analytics.models import ActionKind, ActionLogEntry

    t = make_ticket(session, base_rows)
    session.add(ActionLogEntry(user_id=base_rows["user"], action=ActionKind.STATUS_CHANGED, ticket_id=t.id))
    session.flush()
    with session.begin_nested(), pytest.raises(IntegrityError):
        session.delete(t)
        session.flush()


def test_server_defaults_for_raw_insert(session: Session, base_rows: dict[str, int]) -> None:
    from sqlalchemy import text

    row = session.execute(
        text(
            "insert into tickets(title, description, author_id, category_id, component_id)"
            " values ('t', 'd', :u, :c, :m) returning status, confidential"
        ),
        {"u": base_rows["user"], "c": base_rows["category"], "m": base_rows["component"]},
    ).one()
    assert tuple(row) == ("new", False)
