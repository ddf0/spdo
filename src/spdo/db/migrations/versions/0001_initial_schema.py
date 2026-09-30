"""Начальная схема БД.

Ревизия: 0001
Предыдущая:
Создана: 2026-09-29 23:59:22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применяет изменения схемы."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        sa.UniqueConstraint("name", name=op.f("uq_categories_name")),
    )
    op.create_table(
        "components",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_components")),
        sa.UniqueConstraint("name", name=op.f("uq_components_name")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column(
            "role",
            sa.Enum("user", "operator", "admin", name="user_role"),
            server_default="user",
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "ticket_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=4000), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("component_id", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_ticket_drafts_author_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_ticket_drafts_category_id_categories"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_ticket_drafts_component_id_components"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_drafts")),
    )
    op.create_index(
        op.f("ix_ticket_drafts_author_id"), "ticket_drafts", ["author_id"], unique=False
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=4000), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "new", "in_progress", "resolved", "closed", "closed_duplicate", name="ticket_status"
            ),
            server_default="new",
            nullable=False,
        ),
        sa.Column("confidential", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("component_id", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 4000", name=op.f("ck_tickets_description_len")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name=op.f("ck_tickets_title_len")
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name=op.f("fk_tickets_author_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], name=op.f("fk_tickets_category_id_categories")
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["components.id"], name=op.f("fk_tickets_component_id_components")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index("ix_tickets_author_id", "tickets", ["author_id"], unique=False)
    op.create_index("ix_tickets_category_id", "tickets", ["category_id"], unique=False)
    op.create_index("ix_tickets_component_id", "tickets", ["component_id"], unique=False)
    op.create_index("ix_tickets_status", "tickets", ["status"], unique=False)
    op.create_table(
        "action_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "link_created", "recommendation_rejected", "status_changed", name="action_kind"
            ),
            nullable=False,
        ),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("other_ticket_id", sa.Integer(), nullable=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["other_ticket_id"], ["tickets.id"], name=op.f("fk_action_log_other_ticket_id_tickets")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_action_log_ticket_id_tickets")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_action_log_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_log")),
    )
    op.create_index("ix_action_log_created_at", "action_log", ["created_at"], unique=False)
    op.create_index("ix_action_log_ticket_id", "action_log", ["ticket_id"], unique=False)
    op.create_table(
        "search_index",
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("component_id", sa.Integer(), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("confidential", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("embedding", Vector(312), nullable=True),
        sa.Column("model_version", sa.String(length=200), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_search_index_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ticket_id", name=op.f("pk_search_index")),
    )
    op.create_table(
        "search_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("component_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=200), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidates_count", sa.SmallInteger(), nullable=False),
        sa.Column("failed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_search_sessions_category_id_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_search_sessions_component_id_components"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_search_sessions_ticket_id_tickets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_search_sessions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_sessions")),
    )
    op.create_index(
        "ix_search_sessions_created_at", "search_sessions", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_search_sessions_ticket_id"), "search_sessions", ["ticket_id"], unique=False
    )
    op.create_index(
        op.f("ix_search_sessions_user_id"), "search_sessions", ["user_id"], unique=False
    )
    op.create_table(
        "solutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name=op.f("fk_solutions_author_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_solutions_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_solutions")),
        sa.UniqueConstraint("ticket_id", name=op.f("uq_solutions_ticket_id")),
    )
    op.create_table(
        "ticket_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "created",
                "status",
                "confidentiality",
                "category",
                "component",
                "solution",
                "link",
                name="change_kind",
            ),
            nullable=False,
        ),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name=op.f("fk_ticket_history_author_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ticket_history_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_history")),
    )
    op.create_index(
        "ix_ticket_history_ticket_id_created_at",
        "ticket_history",
        ["ticket_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "recommendations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("s_sem", sa.Float(), nullable=False),
        sa.Column("s_lex", sa.Float(), nullable=False),
        sa.Column("s_cat", sa.Float(), nullable=False),
        sa.Column("s_comp", sa.Float(), nullable=False),
        sa.Column(
            "suggested_kind", sa.Enum("duplicate", "related", name="link_kind"), nullable=False
        ),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column(
            "verdict",
            sa.Enum("pending", "accepted", "rejected", name="verdict"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("verdict_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "(verdict = 'pending') = (verdict_at IS NULL AND verdict_by IS NULL)",
            name=op.f("ck_recommendations_verdict_consistent"),
        ),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 10", name=op.f("ck_recommendations_position_range")
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 1", name=op.f("ck_recommendations_score_range")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["search_sessions.id"],
            name=op.f("fk_recommendations_session_id_search_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_recommendations_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verdict_by"], ["users.id"], name=op.f("fk_recommendations_verdict_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendations")),
        sa.UniqueConstraint(
            "session_id", "position", name=op.f("uq_recommendations_session_id_position")
        ),
        sa.UniqueConstraint(
            "session_id", "ticket_id", name=op.f("uq_recommendations_session_id_ticket_id")
        ),
    )
    op.create_index("ix_recommendations_ticket_id", "recommendations", ["ticket_id"], unique=False)
    op.create_table(
        "solution_ratings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("solution_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("helped", sa.Boolean(), nullable=False),
        sa.Column(
            "rated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["solution_id"],
            ["solutions.id"],
            name=op.f("fk_solution_ratings_solution_id_solutions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_solution_ratings_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_solution_ratings")),
        sa.UniqueConstraint(
            "solution_id", "user_id", name=op.f("uq_solution_ratings_solution_id_user_id")
        ),
    )
    op.create_table(
        "links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("duplicate", "related", name="relation_kind"), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("recommendation_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source_id <> target_id", name=op.f("ck_links_not_self")),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], name=op.f("fk_links_author_id_users")),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["recommendations.id"],
            name=op.f("fk_links_recommendation_id_recommendations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["tickets.id"],
            name=op.f("fk_links_source_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["tickets.id"],
            name=op.f("fk_links_target_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_links")),
        sa.UniqueConstraint("recommendation_id", name=op.f("uq_links_recommendation_id")),
    )
    op.create_index("ix_links_source_id", "links", ["source_id"], unique=False)
    op.create_index("ix_links_target_id", "links", ["target_id"], unique=False)
    op.create_index(
        "uq_links_one_main",
        "links",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'duplicate'"),
    )
    op.create_index(
        "uq_links_pair",
        "links",
        [
            sa.literal_column("least(source_id, target_id)"),
            sa.literal_column("greatest(source_id, target_id)"),
        ],
        unique=True,
    )


def downgrade() -> None:
    """Откатывает изменения схемы.

    Расширение ``vector`` не удаляется: им могут пользоваться другие схемы.
    """
    op.drop_index("uq_links_pair", table_name="links")
    op.drop_index(
        "uq_links_one_main", table_name="links", postgresql_where=sa.text("kind = 'duplicate'")
    )
    op.drop_index("ix_links_target_id", table_name="links")
    op.drop_index("ix_links_source_id", table_name="links")
    op.drop_table("links")
    op.drop_table("solution_ratings")
    op.drop_index("ix_recommendations_ticket_id", table_name="recommendations")
    op.drop_table("recommendations")
    op.drop_index("ix_ticket_history_ticket_id_created_at", table_name="ticket_history")
    op.drop_table("ticket_history")
    op.drop_table("solutions")
    op.drop_index(op.f("ix_search_sessions_user_id"), table_name="search_sessions")
    op.drop_index(op.f("ix_search_sessions_ticket_id"), table_name="search_sessions")
    op.drop_index("ix_search_sessions_created_at", table_name="search_sessions")
    op.drop_table("search_sessions")
    op.drop_table("search_index")
    op.drop_index("ix_action_log_ticket_id", table_name="action_log")
    op.drop_index("ix_action_log_created_at", table_name="action_log")
    op.drop_table("action_log")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_index("ix_tickets_component_id", table_name="tickets")
    op.drop_index("ix_tickets_category_id", table_name="tickets")
    op.drop_index("ix_tickets_author_id", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index(op.f("ix_ticket_drafts_author_id"), table_name="ticket_drafts")
    op.drop_table("ticket_drafts")
    op.drop_table("users")
    op.drop_table("components")
    op.drop_table("categories")
    op.execute("DROP TYPE verdict")
    op.execute("DROP TYPE link_kind")
    op.execute("DROP TYPE relation_kind")
    op.execute("DROP TYPE change_kind")
    op.execute("DROP TYPE action_kind")
    op.execute("DROP TYPE ticket_status")
    op.execute("DROP TYPE user_role")
