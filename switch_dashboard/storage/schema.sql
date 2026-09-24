-- switch_dashboard Database Schema (SQLite WAL Mode)

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ip TEXT UNIQUE NOT NULL,
    model TEXT DEFAULT 'Generic Model',
    protocol TEXT DEFAULT 'http_hc',
    device_type TEXT DEFAULT 'switch',
    role TEXT DEFAULT 'switch',
    management_type TEXT DEFAULT 'managed',
    port_count INTEGER DEFAULT 8,
    enabled INTEGER DEFAULT 1,
    username TEXT DEFAULT 'admin',
    password TEXT DEFAULT '',
    community TEXT DEFAULT 'public',
    snmp_version TEXT DEFAULT '2c',
    parent_ip TEXT DEFAULT '',
    parent_port TEXT DEFAULT '',
    uplink_port TEXT DEFAULT '',
    config_json TEXT,
    last_seen REAL,
    status TEXT DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS interfaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    port_index TEXT NOT NULL,
    port_name TEXT,
    link_status TEXT DEFAULT 'down',
    speed TEXT DEFAULT '',
    duplex TEXT DEFAULT '',
    flow_control TEXT DEFAULT '',
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE (device_id, port_index)
);

-- Real-time counter baselines & cumulative counters
CREATE TABLE IF NOT EXISTS counters (
    device_ip TEXT NOT NULL,
    port TEXT NOT NULL,
    tx_bytes INTEGER NOT NULL DEFAULT 0,
    rx_bytes INTEGER NOT NULL DEFAULT 0,
    cum_tx INTEGER NOT NULL DEFAULT 0,
    cum_rx INTEGER NOT NULL DEFAULT 0,
    timestamp REAL NOT NULL,
    PRIMARY KEY (device_ip, port)
);

-- Raw Time-Series History (Zabbix 'history' equivalent)
CREATE TABLE IF NOT EXISTS metric_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_ip TEXT NOT NULL,
    port TEXT NOT NULL,
    timestamp REAL NOT NULL,
    tx_bytes INTEGER NOT NULL,
    rx_bytes INTEGER NOT NULL,
    speed_tx_bps INTEGER NOT NULL DEFAULT 0,
    speed_rx_bps INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_metric_hist ON metric_history(device_ip, port, timestamp);

-- Pre-Aggregated Hourly Trends (Zabbix 'trends' equivalent)
CREATE TABLE IF NOT EXISTS metric_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_ip TEXT NOT NULL,
    port TEXT NOT NULL,
    hour_timestamp INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    avg_tx_bps INTEGER NOT NULL,
    max_tx_bps INTEGER NOT NULL,
    min_tx_bps INTEGER NOT NULL,
    avg_rx_bps INTEGER NOT NULL,
    max_rx_bps INTEGER NOT NULL,
    min_rx_bps INTEGER NOT NULL,
    UNIQUE (device_ip, port, hour_timestamp)
);
CREATE INDEX IF NOT EXISTS idx_metric_trends ON metric_trends(device_ip, port, hour_timestamp);

-- MAC Table entries
CREATE TABLE IF NOT EXISTS mac_entries (
    device_ip TEXT NOT NULL,
    port TEXT NOT NULL,
    mac TEXT NOT NULL,
    vlan TEXT DEFAULT '1',
    last_seen REAL NOT NULL,
    PRIMARY KEY (device_ip, port, mac)
);

-- Network Scanner Tables (Integrated from scanner_db.py)
CREATE TABLE IF NOT EXISTS hosts (
    ip_address TEXT PRIMARY KEY,
    mac_address TEXT,
    vendor TEXT,
    hostname TEXT,
    ports TEXT,
    note TEXT,
    status TEXT,
    known_host INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen_online TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS host_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT,
    status INTEGER,
    event_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_host_history_ip ON host_history(ip_address);

-- Key-Value store for arbitrary namespace settings & notes
CREATE TABLE IF NOT EXISTS kv_store (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

-- Cached full telemetry & cluster snapshot per device for instant startup
CREATE TABLE IF NOT EXISTS device_state_cache (
    device_ip TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    speeds_json TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);

-- Known & discovered network clients across all switch/AP forwarding tables
CREATE TABLE IF NOT EXISTS discovered_clients (
    mac TEXT PRIMARY KEY,
    ip TEXT DEFAULT '',
    hostname TEXT DEFAULT '',
    custom_name TEXT DEFAULT '',
    vendor TEXT DEFAULT '',
    device_type TEXT DEFAULT 'client',
    switch_ip TEXT DEFAULT '',
    port TEXT DEFAULT '',
    vlan TEXT DEFAULT '1',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT DEFAULT 'online',
    is_mini_switch INTEGER DEFAULT 0,
    passthrough_port TEXT DEFAULT 'PC',
    ssid TEXT DEFAULT '',
    signal_dbm INTEGER
);
CREATE INDEX IF NOT EXISTS idx_discovered_clients_ip ON discovered_clients(ip);

-- Configuration Settings Table (Single Source of Truth)
CREATE TABLE IF NOT EXISTS config_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0.0
);

-- Port Notes Table
CREATE TABLE IF NOT EXISTS port_notes (
    device_ip TEXT NOT NULL,
    port TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (device_ip, port)
);

-- Client Overrides Table
CREATE TABLE IF NOT EXISTS client_overrides (
    mac TEXT PRIMARY KEY,
    host TEXT,
    device_type TEXT,
    is_mini_switch INTEGER DEFAULT 0,
    passthrough_port TEXT DEFAULT 'PC',
    updated_at REAL NOT NULL DEFAULT 0.0
);

-- Client IP Address History
CREATE TABLE IF NOT EXISTS client_ip_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL,
    ip TEXT NOT NULL,
    hostname TEXT DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    is_active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_client_ip_mac_active ON client_ip_history(mac, is_active);
CREATE INDEX IF NOT EXISTS idx_client_ip_lookup ON client_ip_history(mac, ip);

-- Client Connection & Roaming History
CREATE TABLE IF NOT EXISTS client_connection_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL,
    event_type TEXT NOT NULL,
    switch_ip TEXT NOT NULL,
    switch_name TEXT DEFAULT '',
    port TEXT NOT NULL,
    vlan TEXT DEFAULT '1',
    ssid TEXT DEFAULT '',
    signal_dbm INTEGER,
    from_switch_ip TEXT,
    from_port TEXT,
    connected_at REAL NOT NULL,
    disconnected_at REAL
);
CREATE INDEX IF NOT EXISTS idx_client_conn_mac_time ON client_connection_history(mac, connected_at);
CREATE INDEX IF NOT EXISTS idx_client_conn_event ON client_connection_history(event_type, connected_at);

-- Client Metric Trends (Hourly Aggregated Rollups)
CREATE TABLE IF NOT EXISTS client_metric_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL,
    hour_timestamp INTEGER NOT NULL,
    tx_bytes INTEGER NOT NULL DEFAULT 0,
    rx_bytes INTEGER NOT NULL DEFAULT 0,
    max_tx_bps INTEGER NOT NULL DEFAULT 0,
    max_rx_bps INTEGER NOT NULL DEFAULT 0,
    avg_tx_bps INTEGER NOT NULL DEFAULT 0,
    avg_rx_bps INTEGER NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE (mac, hour_timestamp)
);
CREATE INDEX IF NOT EXISTS idx_client_trend_mac_hour ON client_metric_trends(mac, hour_timestamp);

