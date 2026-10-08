from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "81cf37a246aa"
down_revision: str | None = "4d664a96db80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "golden_trios",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("approach", sa.Text(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', question || ' ' || approach)", persisted=True),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_golden_trios_embedding",
        "golden_trios",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_golden_trios_search", "golden_trios", ["search_vector"], postgresql_using="gin"
    )
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column(
        "chat_sessions", sa.Column("summary", sa.Text(), server_default="", nullable=False)
    )
    op.add_column(
        "chat_sessions",
        sa.Column("summarized_through_id", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("analysis_state", sa.JSON(), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "analysis_state")
    op.drop_column("chat_sessions", "summarized_through_id")
    op.drop_column("chat_sessions", "summary")
    op.drop_table("user_preferences")
    op.drop_index("ix_golden_trios_search", table_name="golden_trios")
    op.drop_index("ix_golden_trios_embedding", table_name="golden_trios")
    op.drop_table("golden_trios")
