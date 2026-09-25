"""add security audit log

Revision ID: a9d0e1f2b3c4
Revises: f8c9d0e1a2b3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9d0e1f2b3c4"
down_revision: Union[str, Sequence[str], None] = "f8c9d0e1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "security_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("source_ip", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_security_audit_events_created_at", "security_audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("security_audit_events")
