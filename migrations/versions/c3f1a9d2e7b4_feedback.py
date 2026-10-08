from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f1a9d2e7b4"
down_revision: str | None = "81cf37a246aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("trace_id", sa.String(length=64), nullable=True))
    op.create_index("ix_chat_messages_session_trace", "chat_messages", ["session_id", "trace_id"])
    op.create_table(
        "feedback",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.String(length=8), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "trace_id"),
    )
    op.create_index("ix_feedback_session_id", "feedback", ["session_id"])
    op.create_index("ix_feedback_rating_created", "feedback", ["rating", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_feedback_rating_created", table_name="feedback")
    op.drop_index("ix_feedback_session_id", table_name="feedback")
    op.drop_table("feedback")
    op.drop_index("ix_chat_messages_session_trace", table_name="chat_messages")
    op.drop_column("chat_messages", "trace_id")
