"""add authentication session tables

Revision ID: e7b8f9a0c1d2
Revises: a2f8c1b3d5e7
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b8f9a0c1d2"
down_revision: Union[str, Sequence[str], None] = "a2f8c1b3d5e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_subject", "auth_sessions", ["subject"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "oidc_login_transactions",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_token_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("state_hash"),
    )
    op.create_index("ix_oidc_login_transactions_expires_at", "oidc_login_transactions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("oidc_login_transactions")
    op.drop_table("auth_sessions")
