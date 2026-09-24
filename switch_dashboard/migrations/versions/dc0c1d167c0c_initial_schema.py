"""initial_schema

Revision ID: dc0c1d167c0c
Revises: 
Create Date: 2026-09-24 16:15:13.359118

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dc0c1d167c0c'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'client_overrides' not in existing_tables:
        op.create_table('client_overrides',
            sa.Column('mac', sa.String(length=32), nullable=False),
            sa.Column('host', sa.String(length=255), nullable=True),
            sa.Column('device_type', sa.String(length=64), nullable=True),
            sa.Column('is_mini_switch', sa.Boolean(), nullable=True),
            sa.Column('passthrough_port', sa.String(length=64), nullable=True),
            sa.Column('updated_at', sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint('mac')
        )

    if 'config_settings' not in existing_tables:
        op.create_table('config_settings',
            sa.Column('key', sa.String(length=255), nullable=False),
            sa.Column('value_json', sa.Text(), nullable=False),
            sa.Column('updated_at', sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint('key')
        )

    if 'counters' not in existing_tables:
        op.create_table('counters',
            sa.Column('device_ip', sa.String(length=64), nullable=False),
            sa.Column('port', sa.String(length=64), nullable=False),
            sa.Column('tx_bytes', sa.BigInteger(), nullable=False),
            sa.Column('rx_bytes', sa.BigInteger(), nullable=False),
            sa.Column('cum_tx', sa.BigInteger(), nullable=False),
            sa.Column('cum_rx', sa.BigInteger(), nullable=False),
            sa.Column('timestamp', sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint('device_ip', 'port')
        )

    if 'device_state_cache' not in existing_tables:
        op.create_table('device_state_cache',
            sa.Column('device_ip', sa.String(length=64), nullable=False),
            sa.Column('data_json', sa.Text(), nullable=False),
            sa.Column('speeds_json', sa.Text(), nullable=True),
            sa.Column('updated_at', sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint('device_ip')
        )

    if 'devices' not in existing_tables:
        op.create_table('devices',
            sa.Column('id', sa.String(length=128), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('ip', sa.String(length=64), nullable=False),
            sa.Column('model', sa.String(length=128), nullable=True),
            sa.Column('protocol', sa.String(length=64), nullable=True),
            sa.Column('device_type', sa.String(length=64), nullable=True),
            sa.Column('role', sa.String(length=64), nullable=True),
            sa.Column('management_type', sa.String(length=64), nullable=True),
            sa.Column('port_count', sa.Integer(), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=True),
            sa.Column('username', sa.String(length=128), nullable=True),
            sa.Column('password', sa.String(length=255), nullable=True),
            sa.Column('community', sa.String(length=128), nullable=True),
            sa.Column('snmp_version', sa.String(length=16), nullable=True),
            sa.Column('parent_ip', sa.String(length=64), nullable=True),
            sa.Column('parent_port', sa.String(length=64), nullable=True),
            sa.Column('uplink_port', sa.String(length=64), nullable=True),
            sa.Column('config_json', sa.Text(), nullable=True),
            sa.Column('last_seen', sa.Float(), nullable=True),
            sa.Column('status', sa.String(length=32), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_devices_ip'), 'devices', ['ip'], unique=True)
    else:
        dev_cols = {c['name'] for c in inspector.get_columns('devices')}
        if 'device_type' not in dev_cols:
            op.add_column('devices', sa.Column('device_type', sa.String(length=64), nullable=True, server_default='switch'))
        if 'role' not in dev_cols:
            op.add_column('devices', sa.Column('role', sa.String(length=64), nullable=True, server_default='switch'))
        if 'management_type' not in dev_cols:
            op.add_column('devices', sa.Column('management_type', sa.String(length=64), nullable=True, server_default='managed'))
        if 'community' not in dev_cols:
            op.add_column('devices', sa.Column('community', sa.String(length=128), nullable=True, server_default='public'))
        if 'snmp_version' not in dev_cols:
            op.add_column('devices', sa.Column('snmp_version', sa.String(length=16), nullable=True, server_default='2c'))
        if 'parent_ip' not in dev_cols:
            op.add_column('devices', sa.Column('parent_ip', sa.String(length=64), nullable=True, server_default=''))
        if 'parent_port' not in dev_cols:
            op.add_column('devices', sa.Column('parent_port', sa.String(length=64), nullable=True, server_default=''))
        if 'uplink_port' not in dev_cols:
            op.add_column('devices', sa.Column('uplink_port', sa.String(length=64), nullable=True, server_default=''))
        if 'config_json' not in dev_cols:
            op.add_column('devices', sa.Column('config_json', sa.Text(), nullable=True, server_default='{}'))

    if 'discovered_clients' not in existing_tables:
        op.create_table('discovered_clients',
            sa.Column('mac', sa.String(length=32), nullable=False),
            sa.Column('ip', sa.String(length=64), nullable=True),
            sa.Column('hostname', sa.String(length=255), nullable=True),
            sa.Column('custom_name', sa.String(length=255), nullable=True),
            sa.Column('vendor', sa.String(length=255), nullable=True),
            sa.Column('device_type', sa.String(length=64), nullable=True),
            sa.Column('switch_ip', sa.String(length=64), nullable=True),
            sa.Column('port', sa.String(length=64), nullable=True),
            sa.Column('vlan', sa.String(length=32), nullable=True),
            sa.Column('first_seen', sa.Float(), nullable=False),
            sa.Column('last_seen', sa.Float(), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=True),
            sa.Column('is_mini_switch', sa.Integer(), nullable=True),
            sa.Column('passthrough_port', sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint('mac')
        )
        op.create_index(op.f('ix_discovered_clients_ip'), 'discovered_clients', ['ip'], unique=False)
    else:
        disc_cols = {c['name'] for c in inspector.get_columns('discovered_clients')}
        if 'is_mini_switch' not in disc_cols:
            op.add_column('discovered_clients', sa.Column('is_mini_switch', sa.Integer(), nullable=True, server_default='0'))
        if 'passthrough_port' not in disc_cols:
            op.add_column('discovered_clients', sa.Column('passthrough_port', sa.String(length=64), nullable=True, server_default='PC'))
        if 'custom_name' not in disc_cols:
            op.add_column('discovered_clients', sa.Column('custom_name', sa.String(length=255), nullable=True, server_default=''))

    if 'host_history' not in existing_tables:
        op.create_table('host_history',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('ip_address', sa.String(length=64), nullable=False),
            sa.Column('status', sa.Integer(), nullable=True),
            sa.Column('event_time', sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_host_history_ip_address'), 'host_history', ['ip_address'], unique=False)

    if 'hosts' not in existing_tables:
        op.create_table('hosts',
            sa.Column('ip_address', sa.String(length=64), nullable=False),
            sa.Column('mac_address', sa.String(length=32), nullable=True),
            sa.Column('vendor', sa.String(length=255), nullable=True),
            sa.Column('hostname', sa.String(length=255), nullable=True),
            sa.Column('ports', sa.Text(), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('status', sa.String(length=32), nullable=True),
            sa.Column('known_host', sa.Integer(), nullable=True),
            sa.Column('first_seen', sa.String(length=64), nullable=True),
            sa.Column('last_seen_online', sa.String(length=64), nullable=True),
            sa.Column('last_updated', sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint('ip_address')
        )

    if 'mac_entries' not in existing_tables:
        op.create_table('mac_entries',
            sa.Column('device_ip', sa.String(length=64), nullable=False),
            sa.Column('port', sa.String(length=64), nullable=False),
            sa.Column('mac', sa.String(length=32), nullable=False),
            sa.Column('vlan', sa.String(length=32), nullable=True),
            sa.Column('last_seen', sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint('device_ip', 'port', 'mac')
        )

    if 'metric_history' not in existing_tables:
        op.create_table('metric_history',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('device_ip', sa.String(length=64), nullable=False),
            sa.Column('port', sa.String(length=64), nullable=False),
            sa.Column('timestamp', sa.Float(), nullable=False),
            sa.Column('tx_bytes', sa.BigInteger(), nullable=False),
            sa.Column('rx_bytes', sa.BigInteger(), nullable=False),
            sa.Column('speed_tx_bps', sa.BigInteger(), nullable=False),
            sa.Column('speed_rx_bps', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('idx_metric_hist', 'metric_history', ['device_ip', 'port', 'timestamp'], unique=False)

    if 'metric_trends' not in existing_tables:
        op.create_table('metric_trends',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('device_ip', sa.String(length=64), nullable=False),
            sa.Column('port', sa.String(length=64), nullable=False),
            sa.Column('hour_timestamp', sa.Integer(), nullable=False),
            sa.Column('sample_count', sa.Integer(), nullable=False),
            sa.Column('avg_tx_bps', sa.BigInteger(), nullable=False),
            sa.Column('max_tx_bps', sa.BigInteger(), nullable=False),
            sa.Column('min_tx_bps', sa.BigInteger(), nullable=False),
            sa.Column('avg_rx_bps', sa.BigInteger(), nullable=False),
            sa.Column('max_rx_bps', sa.BigInteger(), nullable=False),
            sa.Column('min_rx_bps', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('device_ip', 'port', 'hour_timestamp', name='uq_metric_trend_device_port_hour')
        )
        op.create_index('idx_metric_trends', 'metric_trends', ['device_ip', 'port', 'hour_timestamp'], unique=False)

    if 'port_notes' not in existing_tables:
        op.create_table('port_notes',
            sa.Column('device_ip', sa.String(length=64), nullable=False),
            sa.Column('port', sa.String(length=64), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('updated_at', sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint('device_ip', 'port')
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('port_notes')
    op.drop_index('idx_metric_trends', table_name='metric_trends')
    op.drop_table('metric_trends')
    op.drop_index('idx_metric_hist', table_name='metric_history')
    op.drop_table('metric_history')
    op.drop_table('mac_entries')
    op.drop_table('hosts')
    op.drop_index(op.f('ix_host_history_ip_address'), table_name='host_history')
    op.drop_table('host_history')
    op.drop_index(op.f('ix_discovered_clients_ip'), table_name='discovered_clients')
    op.drop_table('discovered_clients')
    op.drop_index(op.f('ix_devices_ip'), table_name='devices')
    op.drop_table('devices')
    op.drop_table('device_state_cache')
    op.drop_table('counters')
    op.drop_table('config_settings')
    op.drop_table('client_overrides')
