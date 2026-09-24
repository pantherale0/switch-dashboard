import json
import logging
import re
import time
from typing import Any, Dict, List, Tuple

import voluptuous as vol

from switch_dashboard.protocols.base import Capability
from switch_dashboard.protocols.drivers.ssh import SSHProtocol
from switch_dashboard.protocols.registry import register_protocol

logger = logging.getLogger(__name__)

AUTO_MAC_TABLE_COMMAND = (
    "(command -v swctrl >/dev/null 2>&1 && swctrl mac show 2>/dev/null) || "
    "(command -v ubntbox >/dev/null 2>&1 && ubntbox swctrl mac show 2>/dev/null) || "
    "(command -v bridge >/dev/null 2>&1 && bridge fdb show 2>/dev/null) || "
    "(command -v brctl >/dev/null 2>&1 && brctl showmacs br0 2>/dev/null) || true"
)


@register_protocol(
    name="unifi",
    display_name="Ubiquiti UniFi (Standalone SSH)",
    supported_models=["unifi", "usw", "uap", "usg", "udm", "ucg", "uxg"],
)
class UnifiProtocol(SSHProtocol):
    """Direct SSH driver for standalone UniFi switches, gateways, and APs."""

    name = "unifi"
    display_name = "Ubiquiti UniFi (Standalone SSH)"
    capabilities = {
        Capability.METRICS,
        Capability.MAC_TABLE,
        Capability.NEIGHBORS,
    }
    config_schema = vol.Schema({
        vol.Required("username", default="ubnt", description="UniFi Device SSH Username"): str,
        vol.Optional("password", default="", description="UniFi Device SSH Password"): str,
        vol.Optional("key_filename", default="", description="Path to SSH Private Key"): str,
        vol.Optional("ssh_port", default=22, description="SSH Port"): vol.Coerce(int),
        vol.Optional("ssh_timeout", default=10, description="SSH and Command Timeout (seconds)"): vol.Coerce(int),
        vol.Optional("strict_host_key", default=True, description="Require a trusted SSH host key"): bool,
        vol.Optional("status_command", default="mca-dump", description="Command returning UniFi mca-dump JSON"): str,
        vol.Optional("mac_table_command", default="auto", description="MAC table command, 'auto', or blank to disable"): str,
    })

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.status_command = config.get("status_command", "mca-dump")
        self.mac_table_command = config.get("mac_table_command", "auto")
        self._cache_seconds = 5.0
        self._cached_data: Dict[str, Any] = {}
        self._cache_time = 0.0

    def test_connection(self) -> Tuple[bool, str]:
        try:
            raw = self.execute_command(self.status_command)
            dump = self._parse_dump(raw)
            model = dump.get("model") or dump.get("device_model") or "UniFi device"
            version = dump.get("version") or dump.get("device_version") or "unknown firmware"
            return True, f"Connected to {model} ({version})"
        except Exception as exc:
            return False, f"UniFi SSH connection failed: {exc}"

    def scrape(self) -> Dict[str, Any]:
        return self._get_data()

    def scrape_mac_table(self) -> List[Dict[str, Any]]:
        data = self._get_data()
        return data.get("mac_table", []) if data.get("status") == "online" else []

    def scrape_neighbors(self) -> List[Dict[str, Any]]:
        data = self._get_data()
        return data.get("neighbors", []) if data.get("status") == "online" else []

    def _get_data(self) -> Dict[str, Any]:
        now = time.time()
        if self._cached_data and now - self._cache_time < self._cache_seconds:
            return self._cached_data

        try:
            dump = self._parse_dump(self.execute_command(self.status_command))
            data = self._parse_device_data(dump)
            if not data["mac_table"] and self._should_query_mac_table(dump):
                try:
                    command = self.mac_table_command
                    if command in ("auto", "swctrl mac show"):
                        command = AUTO_MAC_TABLE_COMMAND
                    data["mac_table"] = self._parse_mac_table_text(
                        self.execute_command(command)
                    )
                except Exception as exc:
                    logger.debug("UniFi MAC table command failed for %s: %s", self.ip, exc)
            self._cached_data = data
        except Exception as exc:
            logger.warning("UniFi scrape failed for %s: %s", self.ip, exc)
            self._cached_data = self._offline_result(str(exc))

        self._cache_time = now
        return self._cached_data

    def _should_query_mac_table(self, dump: Dict[str, Any]) -> bool:
        command = str(self.mac_table_command or "").strip()
        if not command:
            return False
        if command not in ("auto", "swctrl mac show"):
            return True

        device_type = str(dump.get("type") or dump.get("device_type") or "").lower()
        model = str(
            dump.get("model_display")
            or dump.get("model")
            or dump.get("device_model")
            or self.model
        ).lower()
        is_ap = (
            device_type in {"uap", "ap"}
            or model.startswith("uap")
            or isinstance(dump.get("vap_table"), list)
            or isinstance(dump.get("radio_table"), list)
        )
        is_switch = device_type in {"usw", "switch"} or model.startswith("usw")
        if not is_ap and isinstance(dump.get("port_table"), list):
            is_switch = is_switch or len(dump["port_table"]) > 1

        if any(key in dump for key in ("mac_table", "fdb_table")):
            return False
        # APs expose clients through station_table. An empty table means no
        # associated clients, not that a switch forwarding command is needed.
        if any(key in dump for key in ("station_table", "vap_table")) and not is_switch:
            return False
        return is_switch

    @staticmethod
    def _parse_dump(raw: str) -> Dict[str, Any]:
        raw = raw.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end <= start:
                raise ValueError("mca-dump did not return a JSON object")
            try:
                parsed = json.loads(raw[start:end + 1])
            except json.JSONDecodeError as exc:
                raise ValueError(f"mca-dump returned invalid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("mca-dump must return a JSON object")
        return parsed

    def _parse_device_data(self, dump: Dict[str, Any]) -> Dict[str, Any]:
        ports_source = dump.get("port_table")
        if not isinstance(ports_source, list):
            ports_source = dump.get("if_table", [])
        if not isinstance(ports_source, list):
            ports_source = []

        ports = []
        for index, entry in enumerate(ports_source, start=1):
            if not isinstance(entry, dict) or self._is_internal_interface(entry):
                continue
            ports.append(self._parse_port(entry, index))

        for entry in dump.get("vap_table", []):
            if isinstance(entry, dict):
                ports.append(self._parse_vap_port(entry))

        hardware_model = dump.get("model") or dump.get("device_model") or self.model or "UniFi"
        model = dump.get("model_display") or hardware_model
        uptime = dump.get("uptime", dump.get("system_uptime", ""))
        if isinstance(uptime, (int, float)):
            uptime = self._format_uptime(int(uptime))

        mac_table = self._parse_embedded_mac_table(dump)
        if any(entry.get("port") == "WLAN" for entry in mac_table):
            if not any(port.get("port") == "WLAN" for port in ports):
                ports.append(self._normalize_port({
                    "port": "WLAN",
                    "name": "Wireless Clients",
                    "status": "up",
                }))

        return {
            "name": dump.get("hostname") or dump.get("name") or self.name_tag,
            "ip": self.ip,
            "model": str(model),
            "hardware_model": str(hardware_model),
            "firmware": str(dump.get("version") or dump.get("device_version") or ""),
            "uptime": str(uptime),
            "mac": self._normalize_mac(
                dump.get("mac") or dump.get("device_mac") or self.config.get("mac", "")
            ),
            "role": self._device_role(dump),
            "status": "online",
            "ports": ports,
            "mac_table": mac_table,
            "neighbors": self._parse_neighbors(dump),
            "timestamp": time.time(),
        }

    @staticmethod
    def _is_internal_interface(entry: Dict[str, Any]) -> bool:
        name = str(entry.get("name") or entry.get("ifname") or "").lower()
        if entry.get("port_idx") is not None:
            return False
        return name.startswith(("lo", "br", "vlan", "gre", "tun", "wifi", "ath", "ra", "bond"))

    def _parse_port(self, entry: Dict[str, Any], fallback_index: int) -> Dict[str, Any]:
        port = entry.get("port_idx")
        if port is None:
            port = (
                entry.get("port")
                or entry.get("num_port")
                or entry.get("name")
                or entry.get("ifname")
                or fallback_index
            )
        name = entry.get("name") or entry.get("ifname") or f"Port {port}"
        is_up = self._as_bool(entry.get("up", entry.get("is_up", entry.get("link", False))))
        speed = self._format_speed(entry.get("speed", entry.get("link_speed", 0))) if is_up else ""
        duplex_value = str(entry.get("duplex", "")).lower()
        full_duplex = self._as_bool(entry.get("full_duplex", duplex_value == "full"))

        return self._normalize_port({
            "port": port,
            "name": str(name),
            "interface": str(entry.get("ifname") or name),
            "status": "up" if is_up else "down",
            "speed": speed,
            "duplex": "Full" if is_up and full_duplex else ("Half" if is_up else ""),
            "tx_bytes": self._as_int(entry.get("tx_bytes", entry.get("bytes_tx", 0))),
            "rx_bytes": self._as_int(entry.get("rx_bytes", entry.get("bytes_rx", 0))),
            "tx_packets": self._as_int(entry.get("tx_packets", entry.get("packets_tx", 0))),
            "rx_packets": self._as_int(entry.get("rx_packets", entry.get("packets_rx", 0))),
            "sfp_present": "sfp" in str(name).lower() or "sfp" in str(entry.get("media", "")).lower(),
            "poe_power": entry.get("poe_power", entry.get("poe_power_consumption")),
        })

    def _parse_vap_port(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        interface = str(entry.get("name") or entry.get("id") or "WLAN")
        radio = str(entry.get("radio") or "").lower()
        band = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}.get(radio, radio.upper())
        essid = str(entry.get("essid") or interface)
        display_name = f"{essid} ({band})" if band else essid
        is_up = self._as_bool(entry.get("up", str(entry.get("state", "")).upper() == "RUN"))
        return self._normalize_port({
            "port": interface,
            "name": display_name,
            "interface": interface,
            "status": "up" if is_up else "down",
            "speed": "",
            "duplex": "",
            "tx_bytes": self._as_int(entry.get("tx_bytes")),
            "rx_bytes": self._as_int(entry.get("rx_bytes")),
            "tx_packets": self._as_int(entry.get("tx_packets")),
            "rx_packets": self._as_int(entry.get("rx_packets")),
            "wireless": True,
            "ssid": essid,
            "radio": radio,
            "channel": entry.get("channel"),
            "client_count": self._as_int(entry.get("num_sta")),
            "satisfaction": entry.get("satisfaction"),
        })

    def _device_role(self, dump: Dict[str, Any]) -> str:
        configured = self.config.get("role")
        if configured:
            return str(configured)
        if isinstance(dump.get("vap_table"), list) or isinstance(dump.get("radio_table"), list):
            return "access_point"
        model = str(dump.get("model_display") or dump.get("model") or "").lower()
        if model.startswith("uap"):
            return "access_point"
        if model.startswith(("usg", "udm", "ucg", "uxg")):
            return "router"
        return "switch"

    @classmethod
    def _parse_embedded_mac_table(cls, dump: Dict[str, Any]) -> List[Dict[str, Any]]:
        source = dump.get("mac_table") or dump.get("fdb_table")
        is_station_table = not source
        source = source or dump.get("station_table") or []
        if not source:
            source = []
            for vap in dump.get("vap_table", []):
                if not isinstance(vap, dict):
                    continue
                vap_port = vap.get("name") or "WLAN"
                vap_ssid = vap.get("essid") or vap.get("ssid") or ""
                for station in vap.get("sta_table", []):
                    if isinstance(station, dict):
                        station = dict(station)
                        station["_vap_port"] = vap_port
                        if vap_ssid and not station.get("ssid"):
                            station["ssid"] = vap_ssid
                        source.append(station)
        if not isinstance(source, list):
            return []
        result = []
        for entry in source:
            if not isinstance(entry, dict):
                continue
            mac = cls._normalize_mac(entry.get("mac") or entry.get("mac_address"))
            port = (
                entry.get("port")
                or entry.get("port_idx")
                or entry.get("ifname")
                or entry.get("radio_name")
                or entry.get("_vap_port")
                or ("WLAN" if is_station_table else None)
            )
            if mac and port is not None:
                item = {
                    "mac": mac,
                    "port": str(port),
                    "vlan": str(entry.get("vlan", entry.get("vid", entry.get("vlan_id", 1)))),
                    "type": str(entry.get("type", "dynamic")).lower(),
                }
                for field in (
                    "ip",
                    "hostname",
                    "signal",
                    "rssi",
                    "rx_bytes",
                    "tx_bytes",
                    "rx_rate",
                    "tx_rate",
                    "ssid",
                    "uptime",
                ):
                    if entry.get(field) not in (None, ""):
                        item[field] = entry[field]
                result.append(item)
        return result

    @classmethod
    def _parse_mac_table_text(cls, raw: str) -> List[Dict[str, Any]]:
        result = []
        for line in raw.splitlines():
            mac_match = re.search(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", line)
            if not mac_match:
                continue
            parts = line.split()
            mac = cls._normalize_mac(mac_match.group(0))
            mac_index = next((i for i, part in enumerate(parts) if mac_match.group(0) in part), -1)
            other = parts[:mac_index] + parts[mac_index + 1:]
            ignored = {"dynamic", "static", "learned", "self", "permanent"}
            values = [part for part in other if part.lower() not in ignored]
            if "dev" in parts and parts.index("dev") + 1 < len(parts):
                # Linux `bridge fdb show`: <mac> dev <interface> ...
                port = parts[parts.index("dev") + 1]
                vlan_match = re.search(r"\bvlan\s+(\d+)\b", line, re.I)
                vlan = vlan_match.group(1) if vlan_match else "1"
            elif mac_index == 1 and parts[0].isdigit() and any(
                value.lower() in {"yes", "no"} for value in parts[2:]
            ):
                # BusyBox `brctl showmacs`: <port> <mac> <local?> <age>
                port = parts[0]
                vlan = "1"
            else:
                port = next(
                    (part for part in values if "/" in part or re.match(r"^(?:eth|sfp|port|lan|wan)\S*$", part, re.I)),
                    "",
                )
                numeric = [part for part in values if part.isdigit()]
                vlan = numeric[0] if numeric else "1"
                if not port:
                    remaining = [part for part in values if part != vlan]
                    port = remaining[0] if remaining else ""
            type_value = next((part.lower() for part in other if part.lower() in ignored), "dynamic")
            if mac and port:
                result.append({
                    "mac": mac,
                    "port": str(port),
                    "vlan": str(vlan),
                    "type": type_value,
                })
        return result

    @classmethod
    def _parse_neighbors(cls, dump: Dict[str, Any]) -> List[Dict[str, Any]]:
        source = dump.get("lldp_table") or dump.get("neighbors") or []
        if isinstance(source, dict):
            source = [
                {"local_port": local_port, **entry}
                for local_port, entry in source.items()
                if isinstance(entry, dict)
            ]
        if not isinstance(source, list):
            return []
        result = []
        for entry in source:
            if not isinstance(entry, dict):
                continue
            local_port = (
                entry.get("local_port")
                or entry.get("local_port_idx")
                or entry.get("local_port_name")
                or entry.get("port")
                or entry.get("port_idx")
            )
            if local_port is None:
                continue
            chassis_id = entry.get("remote_chassis_id") or entry.get("chassis_id") or entry.get("chassis")
            remote_port = entry.get("remote_port_id") or entry.get("port_id") or ""
            result.append({
                "local_port": str(local_port),
                "remote_chassis_id": cls._normalize_mac(chassis_id) or str(chassis_id or ""),
                "remote_port_id": cls._normalize_mac(remote_port) or str(remote_port),
                "remote_system_name": str(entry.get("remote_system_name") or entry.get("system_name") or entry.get("name") or ""),
                "protocol": "lldp",
                "local_port_name": str(entry.get("local_port_name") or ""),
                "chassis_id_subtype": str(entry.get("chassis_id_subtype") or ""),
                "power_allocated": entry.get("power_allocated"),
                "power_requested": entry.get("power_requested"),
            })
        return result

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in {"1", "true", "yes", "up", "active", "connected"}

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _format_speed(cls, value: Any) -> str:
        speed = cls._as_int(value)
        if speed >= 1000:
            gigabits = speed / 1000
            return f"{gigabits:g}G"
        return f"{speed}M" if speed > 0 else ""

    @staticmethod
    def _format_uptime(seconds: int) -> str:
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{days} days, {hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _normalize_mac(value: Any) -> str:
        clean = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
        if len(clean) != 12:
            return ""
        return ":".join(clean[index:index + 2] for index in range(0, 12, 2)).upper()
