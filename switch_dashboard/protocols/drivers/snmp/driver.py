import time
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple

import voluptuous as vol
from pysnmp.hlapi.v3arch.asyncio import (
    SnmpEngine,
    CommunityData,
    UdpTransportTarget,
    ContextData,
    ObjectType,
    ObjectIdentity,
    get_cmd,
    walk_cmd,
)

from switch_dashboard.protocols.base import BaseProtocol, Capability
from switch_dashboard.protocols.registry import register_protocol
from switch_dashboard.core.ports import normalize_port

logger = logging.getLogger(__name__)

# Standard MIB-2 / Bridge MIB OIDs
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"

# IF-MIB (ifTable & ifXTable)
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
OID_IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"

# Bridge-MIB (dot1dBridge)
OID_DOT1D_BASE_PORT_IF_INDEX = "1.3.6.1.2.1.17.1.4.1.2"
OID_DOT1D_TP_FDB_ADDRESS = "1.3.6.1.2.1.17.4.3.1.1"
OID_DOT1D_TP_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
OID_DOT1D_TP_FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.3"

# Q-BRIDGE-MIB (RFC 4363 dot1qTpFdbTable)
OID_DOT1Q_TP_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"

# IP-MIB (RFC 1213 / RFC 4293 ARP & Neighbor Table)
OID_IP_NET_TO_MEDIA_PHYS_ADDRESS = "1.3.6.1.2.1.4.22.1.2"

# LLDP-MIB (lldpRemTable)
OID_LLDP_REM_CHASSIS_ID = "1.0.8802.1.1.2.1.4.1.1.5"
OID_LLDP_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"
OID_LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"


def format_snmp_speed(speed_bps: int) -> str:
    if speed_bps >= 10_000_000_000:
        return f"{speed_bps // 1_000_000_000}G"
    elif speed_bps >= 1_000_000_000:
        val = speed_bps / 1_000_000_000
        return f"{val:.1f}G".replace(".0", "")
    elif speed_bps >= 1_000_000:
        return f"{speed_bps // 1_000_000}M"
    return "Auto"


def format_snmp_uptime(timeticks: int) -> str:
    total_seconds = timeticks // 100
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    return f"{days} days, {hours:02d}:{mins:02d}:{secs:02d}"


def format_snmp_mac(val: Any) -> str:
    if hasattr(val, "asNumbers"):
        try:
            nums = val.asNumbers()
            if len(nums) == 6:
                return ":".join(f"{b:02X}" for b in nums)
        except Exception:
            pass
    if hasattr(val, "asOctets"):
        try:
            octets = val.asOctets()
            if len(octets) == 6:
                return ":".join(f"{b:02X}" for b in octets)
        except Exception:
            pass
    if isinstance(val, (bytes, bytearray)):
        if len(val) == 6:
            return ":".join(f"{b:02X}" for b in val)
    try:
        b = bytes(val)
        if len(b) == 6:
            return ":".join(f"{x:02X}" for x in b)
    except Exception:
        pass
    val_str = str(val).strip()
    if val_str.startswith("0x"):
        val_str = val_str[2:]
        return ":".join(val_str[i:i+2].upper() for i in range(0, len(val_str), 2))
    if len(val_str) == 17 and (val_str.count(":") == 5 or val_str.count("-") == 5):
        return val_str.replace("-", ":").upper()
    clean_hex = "".join(c for c in val_str if c in "0123456789abcdefABCDEF")
    if len(clean_hex) == 12:
        return ":".join(clean_hex[i:i+2].upper() for i in range(0, 12, 2))
    return val_str


@register_protocol(
    name="snmp",
    display_name="SNMP (v2c / v3)",
    supported_models=["generic_snmp", "snmp", "snmp_router", "snmp_switch", "snmp_ap"]
)
class SNMPProtocol(BaseProtocol):
    """Reference SNMP Protocol Driver with IF-MIB, Bridge-MIB, and LLDP support."""

    name = "snmp"
    display_name = "SNMP (v2c / v3)"
    capabilities = {
        Capability.METRICS,
        Capability.MAC_TABLE,
        Capability.TRANSCEIVERS,
        Capability.NEIGHBORS,
    }
    config_schema = vol.Schema({
        vol.Required("community", default="public", description="SNMP Community String"): str,
        vol.Optional("snmp_port", default=161, description="SNMP UDP Port"): vol.Coerce(int),
        vol.Optional("version", default="2c", description="SNMP Protocol Version"): vol.In(["1", "2c", "3"]),

        # Optional custom OID parameters for non-standard / vendor-specific equipment
        vol.Optional("oid_sys_descr", default=OID_SYS_DESCR, description="System Description OID"): str,
        vol.Optional("oid_sys_uptime", default=OID_SYS_UPTIME, description="System Uptime OID"): str,
        vol.Optional("oid_sys_name", default=OID_SYS_NAME, description="System Name OID"): str,
        vol.Optional("oid_if_descr", default=OID_IF_DESCR, description="Interface Description / Name OID"): str,
        vol.Optional("oid_if_oper_status", default=OID_IF_OPER_STATUS, description="Interface OperStatus OID"): str,
        vol.Optional("oid_if_speed", default=OID_IF_SPEED, description="Interface Speed OID"): str,
        vol.Optional("oid_if_in_octets", default=OID_IF_HC_IN_OCTETS, description="Interface In Octets (Rx) OID"): str,
        vol.Optional("oid_fdb_address", default=OID_DOT1D_TP_FDB_ADDRESS, description="MAC Address Table OID (Bridge-MIB or IP-MIB ARP)"): str,
        vol.Optional("oid_fdb_port", default=OID_DOT1D_TP_FDB_PORT, description="Bridge-MIB FDB Port OID"): str,
        vol.Optional("oid_dot1q_fdb_port", default=OID_DOT1Q_TP_FDB_PORT, description="Q-BRIDGE-MIB VLAN FDB Port OID"): str,
        vol.Optional("oid_lldp_rem_chassis", default=OID_LLDP_REM_CHASSIS_ID, description="LLDP Remote Chassis ID OID"): str,
        vol.Optional("oid_lldp_rem_port", default=OID_LLDP_REM_PORT_ID, description="LLDP Remote Port ID OID"): str,
        vol.Optional("oid_lldp_rem_sysname", default=OID_LLDP_REM_SYS_NAME, description="LLDP Remote System Name OID"): str,

        # Interface / Port visibility filtering
        vol.Optional("ignored_ports", default="", description="Ignored / Hidden Ports (e.g. wg*, vlan*, enc*, lo*)"): str,
        vol.Optional("included_ports", default="", description="Included Ports Allowlist (e.g. igc*, ix*)"): str,
    })

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.community = config.get("community", config.get("snmp_community", "public"))
        self.version = str(config.get("version", config.get("snmp_version", "2c")))
        self.snmp_port = int(config.get("snmp_port", 161))
        self.device_role = config.get("role", "switch")
        self._mock_ports = config.get("mock_ports")
        self._mock_mac_table = config.get("mock_mac_table")
        self._mock_neighbors = config.get("mock_neighbors")

        # Configurable OID parameters with standard fallbacks
        self.oid_sys_descr = config.get("oid_sys_descr") or OID_SYS_DESCR
        self.oid_sys_uptime = config.get("oid_sys_uptime") or OID_SYS_UPTIME
        self.oid_sys_name = config.get("oid_sys_name") or OID_SYS_NAME
        self.oid_if_descr = config.get("oid_if_descr") or OID_IF_DESCR
        self.oid_if_oper_status = config.get("oid_if_oper_status") or OID_IF_OPER_STATUS
        self.oid_if_speed = config.get("oid_if_speed") or OID_IF_SPEED
        self.oid_if_in_octets = config.get("oid_if_in_octets") or OID_IF_HC_IN_OCTETS
        self.oid_if_out_octets = config.get("oid_if_out_octets") or OID_IF_HC_OUT_OCTETS
        self.oid_fdb_address = config.get("oid_fdb_address") or OID_DOT1D_TP_FDB_ADDRESS
        self.oid_fdb_port = config.get("oid_fdb_port") or OID_DOT1D_TP_FDB_PORT
        self.oid_dot1q_fdb_port = config.get("oid_dot1q_fdb_port") or OID_DOT1Q_TP_FDB_PORT
        self.oid_lldp_rem_chassis = config.get("oid_lldp_rem_chassis") or OID_LLDP_REM_CHASSIS_ID
        self.oid_lldp_rem_port = config.get("oid_lldp_rem_port") or OID_LLDP_REM_PORT_ID
        self.oid_lldp_rem_sysname = config.get("oid_lldp_rem_sysname") or OID_LLDP_REM_SYS_NAME

        # Interface visibility filtering
        self.ignored_ports = self._parse_port_patterns(config.get("ignored_ports"))
        self.included_ports = self._parse_port_patterns(config.get("included_ports"))

        # Internal bridge port and interface index cache for rock-solid port stability
        self._bport_to_if_cache: Dict[str, str] = {}
        self._if_to_port_num: Dict[str, str] = {}

    @staticmethod
    def _parse_port_patterns(val: Any) -> List[str]:
        if not val:
            return []
        if isinstance(val, list):
            return [str(p).strip().lower() for p in val if str(p).strip()]
        return [p.strip().lower() for p in str(val).split(",") if p.strip()]

    def is_interface_ignored(self, name: str, if_type: int = 6, for_mac_table: bool = False) -> bool:
        name_lower = str(name).strip().lower()
        if not for_mac_table and if_type in [23, 24, 53, 131, 135, 136, 142, 209, 244, 246, 247, 248]:
            return True

        if self.included_ports:
            matched = False
            for pat in self.included_ports:
                if pat.endswith("*") and name_lower.startswith(pat[:-1]):
                    matched = True
                    break
                elif name_lower == pat:
                    matched = True
                    break
            if not matched:
                return True

        for pat in self.ignored_ports:
            if pat.endswith("*") and name_lower.startswith(pat[:-1]):
                return True
            elif name_lower == pat:
                return True

        # Default internal / VPN / virtual interface prefixes to hide from port UI
        default_skipped_prefixes = [
            "lo", "loopback", "null", "cpu",
            "wg", "tun", "tap", "enc", "pfsync", "pflog", "wt", "gre", "gif", "faith"
        ]
        if not for_mac_table:
            default_skipped_prefixes.extend(["vlan", "pppoe"])

        if name_lower.startswith(tuple(default_skipped_prefixes)):
            return True

        return False

    def _snmp_get(self, oid_str: str, timeout: float = 2.0) -> Tuple[bool, Any]:
        async def _run():
            engine = SnmpEngine()
            transport = await UdpTransportTarget.create((self.ip, self.snmp_port), timeout=timeout, retries=1)
            mp_model = 0 if self.version == "1" else 1
            auth = CommunityData(self.community, mpModel=mp_model)
            logger.debug(f"SNMP GET {self.ip}:{self.snmp_port} OID={oid_str} (comm={self.community}, v={self.version})")
            err_ind, err_stat, err_idx, var_binds = await get_cmd(
                engine, auth, transport, ContextData(),
                ObjectType(ObjectIdentity(oid_str))
            )
            if err_ind:
                logger.debug(f"SNMP GET {self.ip} OID={oid_str} error indication: {err_ind}")
                return False, str(err_ind)
            if err_stat:
                logger.debug(f"SNMP GET {self.ip} OID={oid_str} error status: {err_stat.prettyPrint()}")
                return False, err_stat.prettyPrint()
            if var_binds:
                val = var_binds[0][1]
                logger.debug(f"SNMP GET {self.ip} OID={oid_str} -> {val}")
                return True, val
            logger.debug(f"SNMP GET {self.ip} OID={oid_str} -> empty result")
            return False, "Empty result"

        try:
            return asyncio.run(asyncio.wait_for(_run(), timeout=timeout * 2 + 1.0))
        except asyncio.TimeoutError:
            logger.warning(f"SNMP GET {self.ip} OID={oid_str} timed out")
            return False, "Timeout"
        except Exception as e:
            logger.debug(f"SNMP GET {self.ip} OID={oid_str} exception: {e}")
            return False, str(e)

    def _snmp_walk(self, root_oid: str, timeout: float = 2.0, max_entries: int = 1024) -> Tuple[bool, List[Tuple[str, Any]]]:
        async def _run():
            engine = SnmpEngine()
            transport = await UdpTransportTarget.create((self.ip, self.snmp_port), timeout=timeout, retries=0)
            mp_model = 0 if self.version == "1" else 1
            auth = CommunityData(self.community, mpModel=mp_model)
            results = []
            seen_oids = set()
            logger.debug(f"SNMP WALK {self.ip}:{self.snmp_port} Root OID={root_oid}...")

            async for err_ind, err_stat, err_idx, var_binds in walk_cmd(
                engine, auth, transport, ContextData(),
                ObjectType(ObjectIdentity(root_oid)),
                lexicographicMode=False
            ):
                if err_ind:
                    logger.debug(f"SNMP WALK {self.ip} Root OID={root_oid} error indication: {err_ind}")
                    return False, str(err_ind)
                if err_stat:
                    logger.debug(f"SNMP WALK {self.ip} Root OID={root_oid} error status: {err_stat.prettyPrint()}")
                    return False, err_stat.prettyPrint()

                for vb in var_binds:
                    oid_obj = vb[0]
                    oid_str = str(oid_obj)

                    # Loop detection: detect cyclic firmware returns
                    if oid_str in seen_oids:
                        logger.debug(f"SNMP WALK loop detected for {self.ip}: OID {oid_str} already visited. Stopping walk.")
                        return True, results

                    seen_oids.add(oid_str)
                    results.append((oid_str, vb[1]))

                    if len(results) >= max_entries:
                        logger.debug(f"SNMP WALK reached safety limit ({max_entries} entries) for {self.ip} on OID {root_oid}.")
                        return True, results

            logger.debug(f"SNMP WALK {self.ip} Root OID={root_oid} completed: {len(results)} entries.")
            return True, results

        try:
            overall_timeout = max(5.0, timeout * 3.0)
            return asyncio.run(asyncio.wait_for(_run(), timeout=overall_timeout))
        except asyncio.TimeoutError:
            logger.warning(f"SNMP WALK {self.ip} Root OID={root_oid} overall timeout exceeded")
            return False, "Walk timeout"
        except Exception as e:
            logger.debug(f"SNMP WALK {self.ip} Root OID={root_oid} exception: {e}")
            return False, str(e)

    def test_connection(self) -> Tuple[bool, str]:
        """Tests device SNMP reachability by querying sysDescr."""
        logger.info(f"Testing SNMP connection to {self.ip}:{self.snmp_port} (version={self.version}, community={self.community})...")
        ok, res = self._snmp_get(self.oid_sys_descr, timeout=2.0)
        if ok:
            clean_desc = str(res).replace("\r", " ").replace("\n", " ").strip()
            logger.info(f"SNMP connection verified for {self.ip}: {clean_desc[:80]}")
            return True, f"SNMP Connected: {clean_desc[:80]}"
        logger.warning(f"SNMP connection failed for {self.ip}: {res}")
        return False, f"SNMP Connection Failed: {res}"

    def scrape(self) -> Dict[str, Any]:
        """Polls switch/router/AP information and port telemetry via SNMP MIBs."""
        now = time.time()
        logger.debug(f"Running full SNMP telemetry scrape for switch {self.name_tag} ({self.ip})...")

        # If mock/emulated port data is configured, return it directly
        if self._mock_ports is not None:
            formatted_mock = []
            for p in self._mock_ports:
                port_dict = dict(p)
                link_status = str(port_dict.get("link", "")).lower()
                port_dict.setdefault("status", "up" if link_status in ["up", "link up"] else "down")
                port_dict.setdefault("link", "Link Up" if port_dict["status"] == "up" else "Link Down")
                port_dict["port"] = normalize_port(port_dict.get("port", "1"))
                port_dict.setdefault("speed", "1G")
                port_dict.setdefault("duplex", "Full" if port_dict["status"] == "up" else "")
                formatted_mock.append(port_dict)

            return {
                "name": self.name_tag,
                "ip": self.ip,
                "model": self.model,
                "firmware": "SNMP Agent",
                "uptime": "10 days, 04:12:00",
                "status": "online",
                "mac": self.config.get("mac", ""),
                "role": self.device_role,
                "ports": formatted_mock,
                "timestamp": now,
            }

        # 1. Fetch system info
        ok_sys, sys_desc = self._snmp_get(self.oid_sys_descr, timeout=2.0)
        if not ok_sys:
            # Switch offline or unreachable
            logger.warning(f"SNMP device {self.ip} unreachable or query failed: {sys_desc}")
            ports = []
            for i in range(1, self.port_count + 1):
                ports.append({
                    "port": str(i),
                    "status": "down",
                    "link": "Link Down",
                    "speed": "1G",
                    "duplex": "",
                    "flow_control": "",
                    "tx_bytes": 0,
                    "rx_bytes": 0,
                    "tx_packets": 0,
                    "rx_packets": 0,
                    "sfp_present": False,
                })
            return {
                "name": self.name_tag,
                "ip": self.ip,
                "model": self.model,
                "firmware": "SNMP Agent",
                "uptime": "Offline",
                "status": "offline",
                "error": f"SNMP query failed: {sys_desc}",
                "mac": self.config.get("mac", ""),
                "role": self.device_role,
                "ports": ports,
                "timestamp": now,
            }

        _, sys_name = self._snmp_get(self.oid_sys_name, timeout=1.5)
        _, sys_uptime_raw = self._snmp_get(self.oid_sys_uptime, timeout=1.5)
        uptime_str = "Unknown"
        try:
            uptime_str = format_snmp_uptime(int(sys_uptime_raw))
        except Exception:
            pass

        clean_sys_desc = str(sys_desc).replace("\r", " ").replace("\n", " ").strip()
        logger.debug(f"SNMP system info for {self.ip}: sysName='{sys_name}', sysDescr='{clean_sys_desc[:60]}', uptime='{uptime_str}'")

        # 2. Walk interface tables
        logger.debug(f"Querying SNMP interface tables for {self.ip}...")
        _, if_descrs = self._snmp_walk(self.oid_if_descr, timeout=2.0)
        _, if_types = self._snmp_walk(OID_IF_TYPE, timeout=2.0)
        _, if_oper_stats = self._snmp_walk(self.oid_if_oper_status, timeout=2.0)
        _, if_speeds = self._snmp_walk(self.oid_if_speed, timeout=2.0)
        _, if_high_speeds = self._snmp_walk(OID_IF_HIGH_SPEED, timeout=2.0)
        _, if_in_octets = self._snmp_walk(self.oid_if_in_octets, timeout=2.0)
        if not if_in_octets and self.oid_if_in_octets == OID_IF_HC_IN_OCTETS:
            logger.debug(f"HC in-octets empty for {self.ip}; attempting 32-bit ifInOctets ({OID_IF_IN_OCTETS})...")
            _, if_in_octets = self._snmp_walk(OID_IF_IN_OCTETS, timeout=2.0)
        _, if_out_octets = self._snmp_walk(self.oid_if_out_octets, timeout=2.0)
        if not if_out_octets and self.oid_if_out_octets == OID_IF_HC_OUT_OCTETS:
            logger.debug(f"HC out-octets empty for {self.ip}; attempting 32-bit ifOutOctets ({OID_IF_OUT_OCTETS})...")
            _, if_out_octets = self._snmp_walk(OID_IF_OUT_OCTETS, timeout=2.0)

        def _to_map(walk_res):
            m = {}
            if isinstance(walk_res, list):
                for item in walk_res:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        idx = str(item[0]).split(".")[-1]
                        m[idx] = item[1]
            return m

        desc_map = _to_map(if_descrs)
        type_map = _to_map(if_types)
        oper_map = _to_map(if_oper_stats)
        speed_map = _to_map(if_speeds)
        hs_map = _to_map(if_high_speeds)
        in_map = _to_map(if_in_octets)
        out_map = _to_map(if_out_octets)
        logger.debug(f"SNMP interface metrics retrieved for {self.ip}: {len(desc_map)} descriptions, {len(oper_map)} status, {len(speed_map)} speed, {len(in_map)} in-octets, {len(out_map)} out-octets")

        ports = []
        if desc_map:
            port_num = 1
            for idx, desc in desc_map.items():
                desc_str = str(desc).strip()
                try:
                    if_type = int(type_map.get(idx, 6))
                except Exception:
                    if_type = 6

                # Skip ignored interfaces (loopback, internal, VPN, user-configured)
                if self.is_interface_ignored(desc_str, if_type):
                    continue

                oper = oper_map.get(idx, 2)
                try:
                    is_up = (int(oper) == 1)
                except Exception:
                    is_up = False

                # Skip unused link aggregation groups (LAGs / Port Channels / Trunk bundles)
                is_physical_nic = desc_str.lower().startswith((
                    "ix", "igc", "em", "bge", "re", "eth", "en", "ge-", "xe-", "te-",
                    "tengigabit", "gigabit", "ethernet", "fastethernet"
                ))
                is_lag = not is_physical_nic and (
                    (if_type == 161) or desc_str.lower().startswith(("lag", "port-channel", "bundle", "po", "bond", "trunk"))
                )
                if is_lag and not is_up:
                    logger.debug(f"Skipping unused link aggregation interface {desc_str} (idx={idx}) on {self.ip}")
                    continue

                # If port_count is configured, do not exceed the configured physical port limit
                if self.port_count > 0 and len(ports) >= self.port_count:
                    logger.debug(f"Reached configured port count ({self.port_count}) for {self.ip}; skipping remaining interface {desc_str}")
                    break

                # Resolve speed (prefer ifHighSpeed in Mbps, then fallback to ifSpeed in bps)
                raw_speed = 0
                if idx in hs_map:
                    try:
                        hs = int(hs_map[idx])
                        if hs > 0:
                            raw_speed = hs * 1_000_000
                    except Exception:
                        pass
                if raw_speed == 0:
                    try:
                        raw_speed = int(speed_map.get(idx, 1_000_000_000))
                    except Exception:
                        raw_speed = 1_000_000_000

                try:
                    rx_b = int(in_map.get(idx, 0))
                except Exception:
                    rx_b = 0

                try:
                    tx_b = int(out_map.get(idx, 0))
                except Exception:
                    tx_b = 0

                speed_display = format_snmp_speed(raw_speed) if is_up else ""
                is_sfp = ("sfp" in desc_str.lower()) or ("tengigabit" in desc_str.lower()) or (raw_speed >= 10_000_000_000)

                ports.append({
                    "port": str(port_num),
                    "name": desc_str,
                    "interface": desc_str,
                    "status": "up" if is_up else "down",
                    "link": "Link Up" if is_up else "Link Down",
                    "speed": speed_display,
                    "duplex": "Full" if is_up else "",
                    "flow_control": "",
                    "tx_bytes": tx_b,
                    "rx_bytes": rx_b,
                    "tx_packets": 0,
                    "rx_packets": 0,
                    "sfp_present": is_sfp,
                })
                self._if_to_port_num[str(idx)] = str(port_num)
                port_num += 1
        else:
            for i in range(1, self.port_count + 1):
                ports.append({
                    "port": str(i),
                    "status": "down",
                    "link": "Link Down",
                    "speed": "1G",
                    "duplex": "Full",
                    "flow_control": "",
                    "tx_bytes": 0,
                    "rx_bytes": 0,
                    "tx_packets": 0,
                    "rx_packets": 0,
                    "sfp_present": False,
                })

        up_count = sum(1 for p in ports if p["status"] == "up")
        logger.debug(f"SNMP telemetry scrape complete for {self.ip}: {len(ports)} ports ({up_count} active/up)")

        # Resolve device base MAC address automatically if not configured
        device_mac = self.config.get("mac", "")
        if not device_mac:
            # 1. Try dot1dBaseBridgeAddress (1.3.6.1.2.1.17.1.1.0)
            ok_bmac, bmac_val = self._snmp_get("1.3.6.1.2.1.17.1.1.0", timeout=1.0)
            if ok_bmac and bmac_val:
                device_mac = format_snmp_mac(bmac_val)
            # 2. Try router IP interface MAC via ipAdEntIfIndex.<ip>
            if not device_mac or len(device_mac) != 17:
                ok_ipad, if_idx_val = self._snmp_get(f"1.3.6.1.2.1.4.20.1.2.{self.ip}", timeout=1.0)
                if ok_ipad and if_idx_val:
                    ok_pmac, pmac_val = self._snmp_get(f"1.3.6.1.2.1.2.2.1.6.{if_idx_val}", timeout=1.0)
                    if ok_pmac and pmac_val:
                        device_mac = format_snmp_mac(pmac_val)
            # 3. Fallback to ifPhysAddress.1
            if not device_mac or len(device_mac) != 17:
                ok_pmac, pmac_val = self._snmp_get("1.3.6.1.2.1.2.2.1.6.1", timeout=1.0)
                if ok_pmac and pmac_val:
                    device_mac = format_snmp_mac(pmac_val)

        return {
            "name": str(sys_name) if sys_name else self.name_tag,
            "ip": self.ip,
            "model": self.model or "SNMP Switch",
            "firmware": str(sys_desc)[:50] if sys_desc else "SNMP Agent",
            "uptime": uptime_str,
            "status": "online",
            "mac": device_mac,
            "role": self.device_role,
            "ports": ports,
            "timestamp": now,
        }

    def scrape_mac_table(self) -> List[Dict[str, Any]]:
        """Retrieves learned MAC forwarding table entries via Q-BRIDGE-MIB, dot1dTpFdbTable, or IP-MIB ARP table."""
        if self._mock_mac_table is not None:
            return self._mock_mac_table

        # If configured specifically for ARP table or if Bridge-MIB fails/returns empty
        is_arp_configured = str(self.oid_fdb_address).startswith("1.3.6.1.2.1.4.22")
        mac_table = []
        seen_keys = set()

        if not is_arp_configured:
            # First, fetch dot1dBasePortIfIndex to map bridge ports to ifIndex
            ok_bport, bport_entries = self._snmp_walk(OID_DOT1D_BASE_PORT_IF_INDEX, timeout=2.0)
            if ok_bport and bport_entries:
                for oid, if_idx in bport_entries:
                    b_port = str(oid).split(".")[-1]
                    self._bport_to_if_cache[str(b_port)] = str(if_idx)

            bport_to_if = self._bport_to_if_cache

            def _canonicalize_port(raw_port: Any) -> str:
                b_str = str(raw_port)
                # Map bridge port -> ifIndex (if available)
                if_idx = bport_to_if.get(b_str, b_str)
                # Map ifIndex -> switch port display number (if available)
                return self._if_to_port_num.get(if_idx, if_idx)

            # 1. Attempt Q-BRIDGE-MIB (dot1qTpFdbPort) for 802.1Q VLAN switches
            logger.debug(f"Scraping SNMP MAC forwarding table for {self.ip} via Q-BRIDGE-MIB...")
            ok_q, q_entries = self._snmp_walk(self.oid_dot1q_fdb_port, timeout=2.5)
            if ok_q and q_entries:
                for oid, port_val in q_entries:
                    parts = str(oid).split(".")
                    if len(parts) >= 8:
                        vlan = parts[-7]
                        mac_parts = parts[-6:]
                        try:
                            clean_mac = ":".join(f"{int(x):02X}" for x in mac_parts)
                        except Exception:
                            clean_mac = ""
                        if not clean_mac or clean_mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                            continue
                        mapped_port = _canonicalize_port(port_val)
                        entry_key = (str(mapped_port), clean_mac)
                        if entry_key not in seen_keys:
                            seen_keys.add(entry_key)
                            mac_table.append({
                                "port": str(mapped_port),
                                "mac": clean_mac,
                                "vlan": str(vlan),
                            })

            # 2. Check standard Bridge-MIB (dot1dTpFdbAddress)
            logger.debug(f"Scraping SNMP MAC forwarding table for {self.ip} via Bridge-MIB...")
            ok_addr, fdb_addrs = self._snmp_walk(self.oid_fdb_address, timeout=2.0)
            ok_port, fdb_ports = self._snmp_walk(self.oid_fdb_port, timeout=2.0)
            if ok_addr and fdb_addrs:
                port_map = {}
                for oid, port_val in fdb_ports:
                    suffix = ".".join(oid.split(".")[-6:])
                    port_map[suffix] = port_val

                for oid, mac_val in fdb_addrs:
                    suffix_parts = oid.split(".")[-6:]
                    suffix = ".".join(suffix_parts)

                    clean_mac = format_snmp_mac(mac_val)
                    # If formatted MAC is not a valid 17-char colon-delimited MAC, parse directly from the OID suffix
                    if not clean_mac or len(clean_mac) != 17 or clean_mac.count(":") != 5:
                        if len(suffix_parts) == 6:
                            try:
                                clean_mac = ":".join(f"{int(x):02X}" for x in suffix_parts)
                            except Exception:
                                pass

                    if clean_mac and clean_mac not in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                        b_port = str(port_map.get(suffix, "1"))
                        mapped_port = _canonicalize_port(b_port)
                        entry_key = (str(mapped_port), clean_mac)
                        if entry_key not in seen_keys:
                            seen_keys.add(entry_key)
                            mac_table.append({
                                "port": str(mapped_port),
                                "mac": clean_mac,
                                "vlan": "1",
                            })

            if mac_table:
                logger.debug(f"Found {len(mac_table)} SNMP MAC forwarding table entries on {self.ip}")
                return mac_table

        # Fallback to ARP / Neighbor table (e.g. for routers/firewalls like OPNsense, pfSense)
        logger.debug(f"Querying SNMP ARP table for {self.ip} via IP-MIB (ipNetToMediaPhysAddress)...")
        arp_oid = self.oid_fdb_address if is_arp_configured else OID_IP_NET_TO_MEDIA_PHYS_ADDRESS
        ok_arp, arp_entries = self._snmp_walk(arp_oid, timeout=2.5)
        if not ok_arp or not arp_entries:
            logger.debug(f"No ARP table entries found on {self.ip}")
            return []

        # Fetch interface name mapping (ifDescr)
        _, if_descrs = self._snmp_walk(self.oid_if_descr, timeout=2.0)
        if_map = {oid.split(".")[-1]: str(val).strip() for oid, val in if_descrs}

        arp_mac_table = []
        for oid, mac_val in arp_entries:
            parts = oid.split(".")
            # OID structure: 1.3.6.1.2.1.4.22.1.2.<ifIndex>.<ip0>.<ip1>.<ip2>.<ip3>
            if len(parts) >= 15:
                if_idx = parts[10]
                ip_str = ".".join(parts[11:])
            else:
                if_idx = parts[-5] if len(parts) >= 6 else "1"
                ip_str = ""

            clean_mac = format_snmp_mac(mac_val)
            if not clean_mac or len(clean_mac) != 17 or clean_mac.count(":") != 5:
                continue
            if clean_mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                continue

            if_name = if_map.get(if_idx, str(if_idx))
            if self.is_interface_ignored(if_name, for_mac_table=True):
                continue
            vlan = if_name if "vlan" in if_name.lower() else "1"

            arp_mac_table.append({
                "port": if_name,
                "mac": clean_mac,
                "ip": ip_str,
                "vlan": vlan,
            })
        logger.debug(f"Found {len(arp_mac_table)} ARP table entries on {self.ip}")
        return arp_mac_table

    def scrape_neighbors(self) -> List[Dict[str, Any]]:
        """Retrieves LLDP/CDP neighbor discovery table entries."""
        if self._mock_neighbors is not None:
            return self._mock_neighbors

        logger.debug(f"Scraping SNMP LLDP neighbors for {self.ip} via LLDP-MIB...")
        ok_name, rem_names = self._snmp_walk(self.oid_lldp_rem_sysname, timeout=2.0)
        ok_port, rem_ports = self._snmp_walk(self.oid_lldp_rem_port, timeout=2.0)
        ok_chassis, rem_chassis = self._snmp_walk(self.oid_lldp_rem_chassis, timeout=2.0)

        if not ok_name or not rem_names:
            logger.debug(f"No LLDP neighbor entries found on {self.ip}")
            return []

        port_map = {oid.split(".")[-1]: val for oid, val in rem_ports}
        chassis_map = {oid.split(".")[-1]: val for oid, val in rem_chassis}

        neighbors = []
        for oid, name_val in rem_names:
            idx = oid.split(".")[-1]
            neighbors.append({
                "local_port": str(idx),
                "remote_chassis_id": format_snmp_mac(chassis_map.get(idx, "")),
                "remote_port_id": str(port_map.get(idx, "")),
                "remote_system_name": str(name_val),
                "protocol": "lldp",
            })
        logger.debug(f"Found {len(neighbors)} LLDP neighbor entries on {self.ip}")
        return neighbors
