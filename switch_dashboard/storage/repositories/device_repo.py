import json
import time
import logging
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Tuple, Generator

from sqlalchemy import select, delete, update, text
from sqlalchemy.orm import Session

from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.engine import get_db_session
from switch_dashboard.storage.models.config import DeviceConfig
from switch_dashboard.storage.models.mac import MacEntry
from switch_dashboard.storage.models.metrics import DeviceStateCache
from switch_dashboard.storage.models.client import DiscoveredClient
from switch_dashboard.storage.models.client_monitoring import (
    ClientConnectionHistory,
    ClientIpHistory,
    ClientMetricTrend,
)

logger = logging.getLogger("switch_dashboard.storage.device_repo")


class DeviceRepository:
    def __init__(self, db=None, db_url: Optional[str] = None):
        self.db = db or get_db()
        self.db_url = db_url

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        if self.db and hasattr(self.db, "session"):
            with self.db.session() as session:
                yield session
        else:
            with get_db_session(self.db_url) as session:
                yield session

    def upsert_device(self, sw: Dict[str, Any], last_seen: Optional[float] = None, status: str = "online"):
        ip = sw["ip"]
        dev_id = str(sw.get("id", ip))
        name = sw.get("name", ip)
        model = sw.get("model", "Generic Model")
        protocol = sw.get("protocol", "http_hc")
        device_type = sw.get("device_type", "switch")
        role = sw.get("role", "switch")
        management_type = sw.get("management_type", "managed")
        enabled = bool(sw.get("enabled", True))
        port_count = int(sw.get("port_count", 8))
        username = sw.get("username", "admin")
        password = sw.get("password", "")
        community = sw.get("community", "public")
        snmp_version = sw.get("snmp_version", "2c")
        parent_ip = sw.get("parent_ip", "")
        parent_port = sw.get("parent_port", "")
        uplink_port = sw.get("uplink_port", "")
        cfg_json = json.dumps(sw)

        with self._get_session() as session:
            device = session.execute(select(DeviceConfig).where(DeviceConfig.ip == ip)).scalar_one_or_none()
            if device:
                device.name = name
                device.model = model
                device.protocol = protocol
                device.device_type = device_type
                device.role = role
                device.management_type = management_type
                device.enabled = enabled
                device.port_count = port_count
                device.username = username
                device.password = password
                device.community = community
                device.snmp_version = snmp_version
                device.parent_ip = parent_ip
                device.parent_port = parent_port
                device.uplink_port = uplink_port
                device.config_json = cfg_json
                if last_seen is not None:
                    device.last_seen = last_seen
                device.status = status
            else:
                device = DeviceConfig(
                    id=dev_id,
                    name=name,
                    ip=ip,
                    model=model,
                    protocol=protocol,
                    device_type=device_type,
                    role=role,
                    management_type=management_type,
                    enabled=enabled,
                    port_count=port_count,
                    username=username,
                    password=password,
                    community=community,
                    snmp_version=snmp_version,
                    parent_ip=parent_ip,
                    parent_port=parent_port,
                    uplink_port=uplink_port,
                    config_json=cfg_json,
                    last_seen=last_seen or 0.0,
                    status=status,
                )
                session.add(device)

    def get_all_devices(self) -> List[Dict[str, Any]]:
        with self._get_session() as session:
            rows = session.execute(select(DeviceConfig).order_by(DeviceConfig.name.asc())).scalars().all()
            result = []
            for r in rows:
                d = {
                    "id": r.id,
                    "name": r.name,
                    "ip": r.ip,
                    "model": r.model,
                    "protocol": r.protocol,
                    "device_type": r.device_type,
                    "role": r.role,
                    "management_type": r.management_type,
                    "enabled": 1 if r.enabled else 0,
                    "port_count": r.port_count,
                    "username": r.username,
                    "password": r.password,
                    "community": r.community,
                    "snmp_version": r.snmp_version,
                    "parent_ip": r.parent_ip,
                    "parent_port": r.parent_port,
                    "uplink_port": r.uplink_port,
                    "config_json": r.config_json,
                    "last_seen": r.last_seen,
                    "status": r.status,
                }
                if r.config_json:
                    try:
                        d["config"] = json.loads(r.config_json)
                    except Exception:
                        d["config"] = {}
                else:
                    d["config"] = {}
                result.append(d)
            return result

    def get_device_by_ip(self, ip: str) -> Optional[Dict[str, Any]]:
        with self._get_session() as session:
            r = session.execute(select(DeviceConfig).where(DeviceConfig.ip == ip)).scalar_one_or_none()
            if not r:
                return None
            d = {
                "id": r.id,
                "name": r.name,
                "ip": r.ip,
                "model": r.model,
                "protocol": r.protocol,
                "device_type": r.device_type,
                "role": r.role,
                "management_type": r.management_type,
                "enabled": 1 if r.enabled else 0,
                "port_count": r.port_count,
                "username": r.username,
                "password": r.password,
                "community": r.community,
                "snmp_version": r.snmp_version,
                "parent_ip": r.parent_ip,
                "parent_port": r.parent_port,
                "uplink_port": r.uplink_port,
                "config_json": r.config_json,
                "last_seen": r.last_seen,
                "status": r.status,
            }
            if r.config_json:
                try:
                    d["config"] = json.loads(r.config_json)
                except Exception:
                    d["config"] = {}
            else:
                d["config"] = {}
            return d

    def update_mac_table(self, ip: str, mac_entries: List[Dict[str, Any]], timestamp: float):
        if not ip:
            return

        # Deduplicate entries by (port, mac) to prevent duplicate primary keys in batch insert
        deduped: Dict[Tuple[str, str], str] = {}
        for entry in mac_entries:
            if not isinstance(entry, dict):
                continue
            port = str(entry.get("port", "")).strip()
            mac = str(entry.get("mac", "")).replace("-", ":").upper().strip()
            if not port or not mac or mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                continue
            key = (port, mac)
            vlan = str(entry.get("vlan", "1"))
            # Prefer non-default VLAN if already seen
            if key in deduped:
                if deduped[key] in ("1", "") and vlan not in ("1", ""):
                    deduped[key] = vlan
            else:
                deduped[key] = vlan

        with self._get_session() as session:
            try:
                session.execute(delete(MacEntry).where(MacEntry.device_ip == ip))
                for (port, mac), vlan in deduped.items():
                    session.add(MacEntry(device_ip=ip, port=port, mac=mac, vlan=vlan, last_seen=timestamp))
            except Exception as e:
                logger.warning(f"Batch MAC update failed for {ip} ({e}); recovering safely...")
                try:
                    for (port, mac), vlan in deduped.items():
                        session.merge(MacEntry(device_ip=ip, port=port, mac=mac, vlan=vlan, last_seen=timestamp))
                except Exception as ex:
                    logger.error(f"Fallback MAC table update failed for {ip}: {ex}")

    def get_mac_table(self, ip: str) -> List[Dict[str, Any]]:
        with self._get_session() as session:
            rows = session.execute(
                select(MacEntry).where(MacEntry.device_ip == ip).order_by(MacEntry.port.asc())
            ).scalars().all()
            return [{"port": r.port, "mac": r.mac, "vlan": r.vlan} for r in rows]

    def save_all_cached_device_states(self, results: Dict[str, Any], speeds: Dict[str, Any]):
        """Persists full device and cluster snapshots (including Proxmox nodes/guests) to the database."""
        now = time.time()
        with self._get_session() as session:
            for ip, data in results.items():
                if not ip or not isinstance(data, dict):
                    continue
                sw_speeds = speeds.get(ip, {})
                cached = session.get(DeviceStateCache, ip)
                if cached:
                    cached.data_json = json.dumps(data)
                    cached.speeds_json = json.dumps(sw_speeds)
                    cached.updated_at = now
                else:
                    session.add(DeviceStateCache(
                        device_ip=ip,
                        data_json=json.dumps(data),
                        speeds_json=json.dumps(sw_speeds),
                        updated_at=now
                    ))

    def load_all_cached_device_states(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Loads all cached device snapshots from the database for instant warm boot."""
        cached_data = {}
        cached_speeds = {}
        with self._get_session() as session:
            rows = session.execute(select(DeviceStateCache)).scalars().all()
            for row in rows:
                ip = row.device_ip
                try:
                    cached_data[ip] = json.loads(row.data_json)
                except Exception:
                    pass
                try:
                    cached_speeds[ip] = json.loads(row.speeds_json)
                except Exception:
                    pass
        return cached_data, cached_speeds

    def upsert_discovered_clients_batch(self, clients: List[Dict[str, Any]]):
        """Persists discovered/active network clients across all switches/APs to the database."""
        if not clients:
            return
        now = time.time()
        with self._get_session() as session:
            for c in clients:
                mac = str(c.get("mac") or c.get("id") or "").replace("-", ":").upper()
                if not mac or mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                    continue
                ip = str(c.get("ip") or "")
                hostname = str(c.get("host") or c.get("hostname") or c.get("name") or "")
                vendor = str(c.get("vendor") or "")
                dev_type = str(c.get("device_type") or c.get("type") or "client")
                switch_ip = str(c.get("switch_ip") or c.get("last_seen_ip") or c.get("parent_ap") or "")
                port = str(c.get("port") or c.get("last_seen_port") or "")
                vlan = str(c.get("vlan") or "1")
                status = str(c.get("status") or "online")
                first_seen = float(c.get("first_seen") or now)
                last_seen = float(c.get("last_seen_time") or c.get("last_seen") or now)
                ssid = str(c.get("ssid") or "")
                raw_sig = c.get("signal_dbm") if c.get("signal_dbm") is not None else c.get("signal")
                signal_dbm = None
                if raw_sig not in (None, ""):
                    try:
                        signal_dbm = int(float(raw_sig))
                    except (ValueError, TypeError):
                        signal_dbm = None

                client = session.get(DiscoveredClient, mac)
                if client:
                    if ip != "":
                        client.ip = ip
                    if hostname != "" and not hostname.startswith("Client "):
                        client.hostname = hostname
                    if vendor != "":
                        client.vendor = vendor
                    if dev_type != "client":
                        client.device_type = dev_type
                    if switch_ip != "":
                        client.switch_ip = switch_ip
                    if port != "":
                        client.port = port
                    if ssid != "":
                        client.ssid = ssid
                    if signal_dbm is not None:
                        client.signal_dbm = signal_dbm
                    client.vlan = vlan
                    client.last_seen = last_seen
                    client.status = status
                else:
                    session.add(DiscoveredClient(
                        mac=mac,
                        ip=ip,
                        hostname=hostname,
                        vendor=vendor,
                        device_type=dev_type,
                        switch_ip=switch_ip,
                        port=port,
                        vlan=vlan,
                        first_seen=first_seen,
                        last_seen=last_seen,
                        status=status,
                        ssid=ssid,
                        signal_dbm=signal_dbm,
                    ))

    def get_all_discovered_clients(self) -> Dict[str, Dict[str, Any]]:
        """Returns all known discovered clients keyed by normalized MAC."""
        clients = {}
        with self._get_session() as session:
            rows = session.execute(
                select(DiscoveredClient).order_by(DiscoveredClient.last_seen.desc())
            ).scalars().all()
            for row in rows:
                d = {
                    "mac": row.mac,
                    "ip": row.ip,
                    "hostname": row.hostname,
                    "custom_name": row.custom_name,
                    "vendor": row.vendor,
                    "device_type": row.device_type,
                    "switch_ip": row.switch_ip,
                    "port": row.port,
                    "vlan": row.vlan,
                    "first_seen": row.first_seen,
                    "last_seen": row.last_seen,
                    "status": row.status,
                    "is_mini_switch": row.is_mini_switch,
                    "passthrough_port": row.passthrough_port,
                    "ssid": getattr(row, "ssid", "") or "",
                    "signal_dbm": getattr(row, "signal_dbm", None),
                }
                mac_norm = row.mac.replace(":", "").replace("-", "").upper()
                clients[mac_norm] = d
        return clients

    def update_discovered_client_meta(
        self,
        mac: str,
        host: Optional[str] = None,
        hostname: Optional[str] = None,
        ip: Optional[str] = None,
        device_type: Optional[str] = None,
        is_mini_switch: Optional[bool] = None,
        passthrough_port: Optional[str] = None,
    ):
        """Updates user custom host name, discovered hostname, ip, device type, or passthrough setting in discovered_clients."""
        mac_formatted = str(mac).replace("-", ":").upper()
        if len(mac_formatted) == 12 and ":" not in mac_formatted:
            mac_formatted = ":".join(mac_formatted[i:i+2] for i in range(0, 12, 2))
        with self._get_session() as session:
            client = session.get(DiscoveredClient, mac_formatted)
            if client:
                if host is not None:
                    client.custom_name = host
                    client.hostname = host
                if hostname is not None:
                    client.hostname = hostname
                if ip is not None:
                    client.ip = ip
                if device_type is not None:
                    client.device_type = device_type
                if is_mini_switch is not None:
                    client.is_mini_switch = 1 if is_mini_switch else 0
                if passthrough_port is not None:
                    client.passthrough_port = passthrough_port

    def update_discovered_client_passthrough(self, mac: str, is_mini_switch: bool, passthrough_port: str = "PC"):
        """Updates user custom is_mini_switch / passthrough state in discovered_clients."""
        mac_formatted = str(mac).replace("-", ":").upper()
        if len(mac_formatted) == 12 and ":" not in mac_formatted:
            mac_formatted = ":".join(mac_formatted[i:i+2] for i in range(0, 12, 2))
        val = 1 if is_mini_switch else 0
        with self._get_session() as session:
            client = session.get(DiscoveredClient, mac_formatted)
            if client:
                client.is_mini_switch = val
                if is_mini_switch:
                    client.device_type = "phone"
                    client.passthrough_port = passthrough_port

    def delete_discovered_client(self, mac: str):
        """Deletes a discovered client from the database."""
        mac_formatted = str(mac).replace("-", ":").upper()
        if len(mac_formatted) == 12 and ":" not in mac_formatted:
            mac_formatted = ":".join(mac_formatted[i:i+2] for i in range(0, 12, 2))
        with self._get_session() as session:
            client = session.get(DiscoveredClient, mac_formatted)
            if client:
                session.delete(client)

    # ---------------------------------------------------------
    # Active Client Monitoring & Mobility Tracking
    # ---------------------------------------------------------

    def record_client_ip_observation(
        self, mac: str, ip: str, hostname: str = "", timestamp: Optional[float] = None
    ):
        """Records or updates an IP address association for a client MAC."""
        if not mac or not ip:
            return
        mac_fmt = str(mac).replace("-", ":").upper()
        if len(mac_fmt) == 12 and ":" not in mac_fmt:
            mac_fmt = ":".join(mac_fmt[i:i+2] for i in range(0, 12, 2))
        now = timestamp or time.time()
        with self._get_session() as session:
            existing = session.execute(
                select(ClientIpHistory).where(
                    ClientIpHistory.mac == mac_fmt,
                    ClientIpHistory.ip == ip
                )
            ).scalars().first()

            if existing:
                existing.last_seen = now
                existing.is_active = 1
                if hostname and not existing.hostname:
                    existing.hostname = hostname
            else:
                session.execute(
                    update(ClientIpHistory)
                    .where(ClientIpHistory.mac == mac_fmt, ClientIpHistory.is_active == 1)
                    .values(is_active=0)
                )
                session.add(ClientIpHistory(
                    mac=mac_fmt,
                    ip=ip,
                    hostname=hostname,
                    first_seen=now,
                    last_seen=now,
                    is_active=1
                ))

    def record_client_connection_event(
        self,
        mac: str,
        event_type: str,
        switch_ip: str,
        switch_name: str = "",
        port: str = "",
        vlan: str = "1",
        ssid: str = "",
        signal_dbm: Optional[int] = None,
        from_switch_ip: Optional[str] = None,
        from_port: Optional[str] = None,
        connected_at: Optional[float] = None,
        disconnected_at: Optional[float] = None,
    ):
        """Logs a client mobility event (connected, disconnected, roamed)."""
        mac_fmt = str(mac).replace("-", ":").upper()
        if len(mac_fmt) == 12 and ":" not in mac_fmt:
            mac_fmt = ":".join(mac_fmt[i:i+2] for i in range(0, 12, 2))
        now = connected_at or time.time()
        if event_type == "roamed" and from_switch_ip == switch_ip and from_port == port:
            return None
        record = ClientConnectionHistory(
            mac=mac_fmt,
            event_type=event_type,
            switch_ip=switch_ip,
            switch_name=switch_name,
            port=port,
            vlan=vlan,
            ssid=ssid,
            signal_dbm=signal_dbm,
            from_switch_ip=from_switch_ip,
            from_port=from_port,
            connected_at=now,
            disconnected_at=disconnected_at,
        )
        with self._get_session() as session:
            session.add(record)
        return record

    def record_client_metric_sample(
        self,
        mac: str,
        delta_tx: int,
        delta_rx: int,
        speed_tx_bps: int,
        speed_rx_bps: int,
        timestamp: Optional[float] = None,
    ):
        """Records an hourly aggregated bandwidth rollup for a client."""
        mac_fmt = str(mac).replace("-", ":").upper()
        if len(mac_fmt) == 12 and ":" not in mac_fmt:
            mac_fmt = ":".join(mac_fmt[i:i+2] for i in range(0, 12, 2))
        now = timestamp or time.time()
        hour_ts = int(now // 3600) * 3600

        with self._get_session() as session:
            existing = session.execute(
                select(ClientMetricTrend).where(
                    ClientMetricTrend.mac == mac_fmt,
                    ClientMetricTrend.hour_timestamp == hour_ts
                )
            ).scalars().first()

            if existing:
                existing.tx_bytes += delta_tx
                existing.rx_bytes += delta_rx
                if speed_tx_bps > existing.max_tx_bps:
                    existing.max_tx_bps = speed_tx_bps
                if speed_rx_bps > existing.max_rx_bps:
                    existing.max_rx_bps = speed_rx_bps
                count = existing.sample_count
                existing.avg_tx_bps = (existing.avg_tx_bps * count + speed_tx_bps) // (count + 1)
                existing.avg_rx_bps = (existing.avg_rx_bps * count + speed_rx_bps) // (count + 1)
                existing.sample_count = count + 1
            else:
                session.add(ClientMetricTrend(
                    mac=mac_fmt,
                    hour_timestamp=hour_ts,
                    tx_bytes=delta_tx,
                    rx_bytes=delta_rx,
                    max_tx_bps=speed_tx_bps,
                    max_rx_bps=speed_rx_bps,
                    avg_tx_bps=speed_tx_bps,
                    avg_rx_bps=speed_rx_bps,
                    sample_count=1
                ))

    def record_client_metric_samples_batch(self, samples: List[Dict[str, Any]]):
        """Records multiple client metric samples in a single transaction."""
        if not samples:
            return

        # Pre-aggregate by (mac_fmt, hour_ts) to ensure no duplicate keys in batch
        aggregated: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for s in samples:
            mac_fmt = str(s["mac"]).replace("-", ":").upper()
            if len(mac_fmt) == 12 and ":" not in mac_fmt:
                mac_fmt = ":".join(mac_fmt[i:i+2] for i in range(0, 12, 2))
            now = s.get("timestamp") or time.time()
            hour_ts = int(now // 3600) * 3600
            delta_tx = int(s.get("delta_tx", 0))
            delta_rx = int(s.get("delta_rx", 0))
            speed_tx_bps = int(s.get("speed_tx_bps", 0))
            speed_rx_bps = int(s.get("speed_rx_bps", 0))

            key = (mac_fmt, hour_ts)
            if key in aggregated:
                agg = aggregated[key]
                agg["delta_tx"] += delta_tx
                agg["delta_rx"] += delta_rx
                agg["max_tx_bps"] = max(agg["max_tx_bps"], speed_tx_bps)
                agg["max_rx_bps"] = max(agg["max_rx_bps"], speed_rx_bps)
                count = agg["count"]
                agg["avg_tx_bps"] = (agg["avg_tx_bps"] * count + speed_tx_bps) // (count + 1)
                agg["avg_rx_bps"] = (agg["avg_rx_bps"] * count + speed_rx_bps) // (count + 1)
                agg["count"] += 1
            else:
                aggregated[key] = {
                    "delta_tx": delta_tx,
                    "delta_rx": delta_rx,
                    "max_tx_bps": speed_tx_bps,
                    "max_rx_bps": speed_rx_bps,
                    "avg_tx_bps": speed_tx_bps,
                    "avg_rx_bps": speed_rx_bps,
                    "count": 1,
                }

        with self._get_session() as session:
            for (mac_fmt, hour_ts), agg in aggregated.items():
                existing = session.execute(
                    select(ClientMetricTrend).where(
                        ClientMetricTrend.mac == mac_fmt,
                        ClientMetricTrend.hour_timestamp == hour_ts
                    )
                ).scalars().first()

                if existing:
                    existing.tx_bytes += agg["delta_tx"]
                    existing.rx_bytes += agg["delta_rx"]
                    if agg["max_tx_bps"] > existing.max_tx_bps:
                        existing.max_tx_bps = agg["max_tx_bps"]
                    if agg["max_rx_bps"] > existing.max_rx_bps:
                        existing.max_rx_bps = agg["max_rx_bps"]
                    count = existing.sample_count
                    existing.avg_tx_bps = (existing.avg_tx_bps * count + agg["avg_tx_bps"]) // (count + 1)
                    existing.avg_rx_bps = (existing.avg_rx_bps * count + agg["avg_rx_bps"]) // (count + 1)
                    existing.sample_count = count + agg["count"]
                else:
                    session.add(ClientMetricTrend(
                        mac=mac_fmt,
                        hour_timestamp=hour_ts,
                        tx_bytes=agg["delta_tx"],
                        rx_bytes=agg["delta_rx"],
                        max_tx_bps=agg["max_tx_bps"],
                        max_rx_bps=agg["max_rx_bps"],
                        avg_tx_bps=agg["avg_tx_bps"],
                        avg_rx_bps=agg["avg_rx_bps"],
                        sample_count=agg["count"]
                    ))


    def get_client_ip_history(self, mac: str) -> List[Dict[str, Any]]:
        """Returns all historical IP addresses for a given client MAC."""
        mac_fmt = str(mac).replace("-", ":").upper()
        if len(mac_fmt) == 12 and ":" not in mac_fmt:
            mac_fmt = ":".join(mac_fmt[i:i+2] for i in range(0, 12, 2))
        with self._get_session() as session:
            rows = session.execute(
                select(ClientIpHistory)
                .where(ClientIpHistory.mac == mac_fmt)
                .order_by(ClientIpHistory.last_seen.desc())
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "mac": r.mac,
                    "ip": r.ip,
                    "hostname": r.hostname or "",
                    "first_seen": r.first_seen,
                    "last_seen": r.last_seen,
                    "is_active": bool(r.is_active),
                }
                for r in rows
            ]

    def get_client_connection_history(self, mac: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns the connection and roaming event timeline for a given client MAC."""
        mac_fmt = str(mac).replace("-", ":").upper()
        if len(mac_fmt) == 12 and ":" not in mac_fmt:
            mac_fmt = ":".join(mac_fmt[i:i+2] for i in range(0, 12, 2))
        now = time.time()
        with self._get_session() as session:
            rows = session.execute(
                select(ClientConnectionHistory)
                .where(ClientConnectionHistory.mac == mac_fmt)
                .order_by(ClientConnectionHistory.connected_at.desc())
                .limit(limit)
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "mac": r.mac,
                    "event_type": r.event_type,
                    "switch_ip": r.switch_ip,
                    "switch_name": r.switch_name or "",
                    "port": r.port,
                    "vlan": r.vlan or "1",
                    "ssid": r.ssid or "",
                    "signal_dbm": r.signal_dbm,
                    "from_switch_ip": r.from_switch_ip,
                    "from_port": r.from_port,
                    "connected_at": r.connected_at,
                    "disconnected_at": r.disconnected_at,
                    "duration_seconds": (
                        (r.disconnected_at - r.connected_at)
                        if r.disconnected_at
                        else (now - r.connected_at)
                    ),
                }
                for r in rows
            ]

    def get_client_bandwidth_trends(self, mac: str, hours: int = 168) -> List[Dict[str, Any]]:
        """Returns hourly bandwidth rollups for the past N hours for a given client."""
        mac_fmt = str(mac).replace("-", ":").upper()
        if len(mac_fmt) == 12 and ":" not in mac_fmt:
            mac_fmt = ":".join(mac_fmt[i:i+2] for i in range(0, 12, 2))
        since = int(time.time() - hours * 3600)
        with self._get_session() as session:
            rows = session.execute(
                select(ClientMetricTrend)
                .where(
                    ClientMetricTrend.mac == mac_fmt,
                    ClientMetricTrend.hour_timestamp >= since
                )
                .order_by(ClientMetricTrend.hour_timestamp.asc())
            ).scalars().all()
            return [
                {
                    "hour_timestamp": r.hour_timestamp,
                    "tx_bytes": r.tx_bytes,
                    "rx_bytes": r.rx_bytes,
                    "max_tx_bps": r.max_tx_bps,
                    "max_rx_bps": r.max_rx_bps,
                    "avg_tx_bps": r.avg_tx_bps,
                    "avg_rx_bps": r.avg_rx_bps,
                    "sample_count": r.sample_count,
                }
                for r in rows
            ]

    def cleanup_spurious_roamed_events(self, window_seconds: float = 60.0) -> int:
        """Removes spurious flapping roaming events where multiple switch hops generated
        roamed events for the same client in the same cycle or within window_seconds.
        Also cleans up consecutive duplicate location events for the same MAC.
        """
        deleted_count = 0
        with self._get_session() as session:
            try:
                # 1. Clean rapid flapping/duplicate roams within window_seconds
                stmt1 = text("""
                    DELETE FROM client_connection_history
                    WHERE event_type = 'roamed'
                      AND EXISTS (
                          SELECT 1 FROM client_connection_history c2
                          WHERE c2.mac = client_connection_history.mac
                            AND c2.id != client_connection_history.id
                            AND abs(c2.connected_at - client_connection_history.connected_at) < :window
                            AND (c2.id < client_connection_history.id OR c2.connected_at < client_connection_history.connected_at)
                      )
                """)
                res1 = session.execute(stmt1, {"window": window_seconds})
                deleted_count += (res1.rowcount or 0)

                # 2. Clean consecutive duplicate locations for the same MAC
                stmt2 = text("""
                    DELETE FROM client_connection_history
                    WHERE event_type = 'roamed' AND id IN (
                        SELECT c1.id
                        FROM client_connection_history c1
                        JOIN client_connection_history c2 ON c2.mac = c1.mac AND c2.id = (
                            SELECT id FROM client_connection_history c3
                            WHERE c3.mac = c1.mac AND c3.connected_at < c1.connected_at
                            ORDER BY c3.connected_at DESC LIMIT 1
                        )
                        WHERE c1.switch_ip = c2.switch_ip AND c1.port = c2.port
                    )
                """)
                res2 = session.execute(stmt2)
                deleted_count += (res2.rowcount or 0)

                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} spurious roamed connection events from database.")
            except Exception as e:
                logger.error(f"Error during spurious roamed events cleanup: {e}")
        return deleted_count

    def prune_old_client_history(self, retention_seconds: int = 90 * 86400) -> int:
        """Prunes raw connection events older than retention period."""
        self.cleanup_spurious_roamed_events()
        cutoff = time.time() - retention_seconds
        with self._get_session() as session:
            result = session.execute(
                delete(ClientConnectionHistory).where(ClientConnectionHistory.connected_at < cutoff)
            )
            return result.rowcount or 0

