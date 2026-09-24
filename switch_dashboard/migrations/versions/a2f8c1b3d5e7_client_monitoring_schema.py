"""client_monitoring_schema

Revision ID: a2f8c1b3d5e7
Revises: dc0c1d167c0c
Create Date: 2026-09-24 22:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2f8c1b3d5e7'
down_revision: Union[str, Sequence[str], None] = 'dc0c1d167c0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to add client active monitoring tables."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Update discovered_clients with ssid and signal_dbm
    if 'discovered_clients' in existing_tables:
        disc_cols = {c['name'] for c in inspector.get_columns('discovered_clients')}
        if 'ssid' not in disc_cols:
            op.add_column('discovered_clients', sa.Column('ssid', sa.String(length=128), nullable=True, server_default=''))
        if 'signal_dbm' not in disc_cols:
            op.add_column('discovered_clients', sa.Column('signal_dbm', sa.Integer(), nullable=True))

    # 2. client_ip_history
    if 'client_ip_history' not in existing_tables:
        op.create_table(
            'client_ip_history',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('mac', sa.String(length=32), nullable=False),
            sa.Column('ip', sa.String(length=64), nullable=False),
            sa.Column('hostname', sa.String(length=255), nullable=True, server_default=''),
            sa.Column('first_seen', sa.Float(), nullable=False),
            sa.Column('last_seen', sa.Float(), nullable=False),
            sa.Column('is_active', sa.Integer(), nullable=True, server_default='1'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('idx_client_ip_mac_active', 'client_ip_history', ['mac', 'is_active'], unique=False)
        op.create_index('idx_client_ip_lookup', 'client_ip_history', ['mac', 'ip'], unique=False)

    # 3. client_connection_history
    if 'client_connection_history' not in existing_tables:
        op.create_table(
            'client_connection_history',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('mac', sa.String(length=32), nullable=False),
            sa.Column('event_type', sa.String(length=32), nullable=False),
            sa.Column('switch_ip', sa.String(length=64), nullable=False),
            sa.Column('switch_name', sa.String(length=255), nullable=True, server_default=''),
            sa.Column('port', sa.String(length=64), nullable=False),
            sa.Column('vlan', sa.String(length=32), nullable=True, server_default='1'),
            sa.Column('ssid', sa.String(length=128), nullable=True, server_default=''),
            sa.Column('signal_dbm', sa.Integer(), nullable=True),
            sa.Column('from_switch_ip', sa.String(length=64), nullable=True),
            sa.Column('from_port', sa.String(length=64), nullable=True),
            sa.Column('connected_at', sa.Float(), nullable=False),
            sa.Column('disconnected_at', sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('idx_client_conn_mac_time', 'client_connection_history', ['mac', 'connected_at'], unique=False)
        op.create_index('idx_client_conn_event', 'client_connection_history', ['event_type', 'connected_at'], unique=False)

    # 4. client_metric_trends
    if 'client_metric_trends' not in existing_tables:
        op.create_table(
            'client_metric_trends',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('mac', sa.String(length=32), nullable=False),
            sa.Column('hour_timestamp', sa.Integer(), nullable=False),
            sa.Column('tx_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('rx_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('max_tx_bps', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('max_rx_bps', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('avg_tx_bps', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('avg_rx_bps', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('sample_count', sa.Integer(), nullable=False, server_default='1'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('mac', 'hour_timestamp', name='uq_client_metric_trends_mac_hour')
        )
        op.create_index('idx_client_trend_mac_hour', 'client_metric_trends', ['mac', 'hour_timestamp'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'client_metric_trends' in existing_tables:
        op.drop_table('client_metric_trends')
    if 'client_connection_history' in existing_tables:
        op.drop_table('client_connection_history')
    if 'client_ip_history' in existing_tables:
        op.drop_table('client_ip_history')
