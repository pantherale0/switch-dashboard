"""add encrypted device secrets

Revision ID: f8c9d0e1a2b3
Revises: e7b8f9a0c1d2
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8c9d0e1a2b3"
down_revision: Union[str, Sequence[str], None] = "e7b8f9a0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_secrets",
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("device_id"),
    )


def downgrade() -> None:
    raise RuntimeError("Encrypted device-secret migration is intentionally irreversible")
