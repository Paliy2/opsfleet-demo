from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a6d3c1e5f20"
down_revision: str | None = "5b8e2c7d9a14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pii_tokens",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("info_type", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", "info_type", "ordinal"),
        sa.UniqueConstraint("session_id", "fingerprint"),
    )
    op.create_table(
        "pii_lookups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("token", sa.String(length=48), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("matches", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pii_lookups_user_created", "pii_lookups", ["user_id", "created_at"])
    op.create_index("ix_pii_lookups_session_id", "pii_lookups", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_pii_lookups_session_id", table_name="pii_lookups")
    op.drop_index("ix_pii_lookups_user_created", table_name="pii_lookups")
    op.drop_table("pii_lookups")
    op.drop_table("pii_tokens")
