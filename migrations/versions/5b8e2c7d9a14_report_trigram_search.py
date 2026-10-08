from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b8e2c7d9a14"
down_revision: str | None = "c3f1a9d2e7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column(
        "reports",
        sa.Column(
            "search_text",
            sa.Text(),
            sa.Computed("title || ' ' || body", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_reports_search_trgm",
        "reports",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_reports_search_trgm", table_name="reports")
    op.drop_column("reports", "search_text")
