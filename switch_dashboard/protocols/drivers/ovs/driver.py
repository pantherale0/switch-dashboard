import os
import re
import json
import time
import logging
import re
from typing import Tuple

import voluptuous as vol
from switch_dashboard.protocols.base import Capability
from switch_dashboard.protocols.drivers.ssh import SSHProtocol
from switch_dashboard.protocols.registry import register_protocol

logger = logging.getLogger(__name__)

@register_protocol(
    name="ovs",
    display_name="Open vSwitch (SSH / Local)",
    supported_models=["openvswitch", "ovs"]
)
class OVSProtocol(SSHProtocol):
    name = "ovs"
    display_name = "Open vSwitch (SSH / Local)"
    capabilities = {
        Capability.METRICS,
        Capability.MAC_TABLE,
    }
    config_schema = vol.Schema({
        vol.Required("username", default="ovs-monitor", description="SSH Username"): str,
        vol.Optional("password", default="", description="SSH Password"): str,
        vol.Optional("bridge", default="vmbr0", description="OVS Bridge Name"): str,
        vol.Optional("ssh_port", default=22, description="SSH Port"): vol.Coerce(int),
        vol.Optional("ssh_timeout", default=15, description="SSH and Command Timeout (seconds)"): vol.Coerce(int),
        vol.Optional("strict_host_key", default=True, description="Require a trusted SSH host key"): bool,
        vol.Optional("key_filename", default="", description="Path to SSH Private Key (optional)"): str,
    })

    def __init__(self, config):
        config = dict(config)
        config.setdefault("strict_host_key", True)
        super().__init__(config)
        self.name = config.get("name", config.get("ip", "OVS"))
        self.ip = config["ip"]
        self.username = config.get("username", "ovs-monitor")
        self.password = config.get("password", "")
        self.bridge = config.get("bridge", "vmbr0")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", self.bridge):
            raise ValueError("Invalid OVS bridge name")
        self.port_count = config.get("port_count", 24)
        self.model = config.get("model", "openvswitch")
        self._cached_data = None
        self._cache_time = 0

    def _get_data(self):
        import time
        if self._cached_data and (time.time() - self._cache_time < 5.0):
            return self._cached_data
        data = self._scrape_ovs()
        self._cached_data = data
        self._cache_time = time.time()
        return data

    def scrape(self):
        return self._get_data()

    def scrape_mac_table(self):
        data = self._get_data()
        return data.get("mac_table", [])

    def scrape_dhcp_snooping(self):
        return {"enabled": False, "ports": {}}

    def scrape_igmp(self):
        return {"enabled": False, "entries": []}

    def scrape_jumbo_frame(self):
        return {"enabled": False, "size": "Disabled"}

    def scrape_transceiver(self):
        return None

    def download_backup(self):
        return b""

    def reboot_switch(self):
        return "Not supported for virtual switches."

    def _scrape_ovs(self):
        import json
        import time

        remote_script = """
import subprocess
import json
import re
import os

def run_cmd(cmd):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return res.stdout, res.stderr
    except Exception as e:
        return "", str(e)

data = {}

# 1. OVS VSCTL show
vsctl_out, _ = run_cmd("sudo ovs-vsctl show")
ovs_ver = re.search(r"ovs_version:\\s*\\\"([^\\\"]+)\\\"", vsctl_out)
data["ovs_version"] = ovs_ver.group(1) if ovs_ver else "3.5.0"

# 2. Find bridges
bridges = re.findall(r"Bridge\\s+(\\S+)", vsctl_out)
data["bridges"] = bridges
bridge = "{bridge}"

# 3. OVS OFCTL show <bridge>
ofctl_out, _ = run_cmd("sudo ovs-ofctl show " + bridge)
data["ofctl"] = ofctl_out

# 4. OVS APPCTL fdb/show <bridge>
fdb_out, _ = run_cmd("sudo ovs-appctl fdb/show " + bridge)
data["fdb"] = fdb_out

# 5. VMID to name mappings
vm_names = {}
pct_out, _ = run_cmd("sudo pct list")
for line in pct_out.splitlines():
    line = line.strip()
    if not line or line.startswith("VMID"):
        continue
    parts = line.split()
    if len(parts) >= 3:
        vm_names[parts[0]] = "LXC " + parts[0] + " (" + parts[-1] + ")"

qm_out, _ = run_cmd("sudo qm list")
for line in qm_out.splitlines():
    line = line.strip()
    if not line or "VMID" in line:
        continue
    parts = line.split()
    if len(parts) >= 3:
        vm_names[parts[0]] = "VM " + parts[0] + " (" + parts[1] + ")"

data["vm_names"] = vm_names

# Get uptime
uptime_str = "unknown"
try:
    with open("/proc/uptime") as f:
        uptime_seconds = float(f.read().split()[0])
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    if days > 0:
        uptime_str = str(days) + "d " + str(hours) + "h " + str(minutes) + "m"
    elif hours > 0:
        uptime_str = str(hours) + "h " + str(minutes) + "m"
    else:
        uptime_str = str(minutes) + "m"
except (OSError, ValueError):
    uptime_str = ""
data["uptime"] = uptime_str

# bridge mac
bridge_mac = ""
try:
    with open("/sys/class/net/" + bridge + "/address") as f:
        bridge_mac = f.read().strip().upper()
except (OSError, ValueError):
    pass
data["mac"] = bridge_mac

# 6. Read interface stats
interfaces = {}
for dev in os.listdir("/sys/class/net/"):
    stat_path = "/sys/class/net/" + dev + "/statistics"
    if os.path.exists(stat_path):
        stats = {}
        for sfile in ["tx_bytes", "rx_bytes", "tx_packets", "rx_packets"]:
            try:
                with open(stat_path + "/" + sfile) as f:
                    stats[sfile] = int(f.read().strip())
            except (OSError, ValueError):
                stats[sfile] = 0
                
        speed = 0
        try:
            with open("/sys/class/net/" + dev + "/speed") as f:
                speed = int(f.read().strip())
        except (OSError, ValueError):
            pass
            
        operstate = "unknown"
        try:
            with open("/sys/class/net/" + dev + "/operstate") as f:
                operstate = f.read().strip()
        except (OSError, ValueError):
            pass
            
        interfaces[dev] = {
            "speed": speed,
            "operstate": operstate,
            "stats": stats
        }
data["interfaces"] = interfaces

print(json.dumps(data))
""".replace("{bridge}", self.bridge)

        try:
            out = self.execute_command("python3", stdin_data=remote_script)
            res_data = json.loads(out.strip())
            return self._parse_scraped_data(res_data)
        except Exception as e:
            logger.error(f"[OVSScraper] Failed to scrape OVS switch {self.name} ({self.ip}): {e}")
            return self._fallback()

    def _parse_scraped_data(self, data):
        import re
        import time

        vm_names = data.get("vm_names", {})
        ofctl = data.get("ofctl", "")
        fdb = data.get("fdb", "")
        interfaces = data.get("interfaces", {})

        port_to_iface = {}
        for line in ofctl.splitlines():
            line = line.strip()
            m = re.match(r"(\d+|LOCAL)\((\S+)\):", line)
            if m:
                port_num = m.group(1)
                iface_name = m.group(2)
                port_to_iface[port_num] = iface_name

        ports = []
        port_to_key = {}
        vm_mac_map = {}

        for port_num, iface in port_to_iface.items():
            m = re.search(r"(?:veth|tap|fwln)(\d+)", iface)
            is_vm = False
            vm_name = None
            friendly_name = iface

            if m:
                vmid = m.group(1)
                is_vm = True
                if vmid in vm_names:
                    full_name = vm_names[vmid]
                    short_m = re.search(r"\(([^)]+)\)", full_name)
                    short_name = short_m.group(1) if short_m else vmid
                    friendly_name = f"LXC {vmid} ({short_name})" if "LXC" in full_name else f"VM {vmid} ({short_name})"
                    vm_name = short_name
                else:
                    friendly_name = f"VM/LXC {vmid}"
                    vm_name = f"VM {vmid}"
            else:
                vm_name = None

            port_to_key[port_num] = (port_num, friendly_name if is_vm else None)

            istat = interfaces.get(iface, {})
            speed_val = istat.get("speed", 0)
            operstate = istat.get("operstate", "up")

            if speed_val >= 10000:
                speed_str = "10G"
            elif speed_val >= 1000:
                if speed_val % 1000 == 0:
                    speed_str = f"{int(speed_val/1000)}G"
                else:
                    speed_str = f"{speed_val/1000:.1f}".rstrip("0").rstrip(".") + "G"
            elif speed_val > 0:
                speed_str = f"{speed_val}M"
            else:
                if iface.startswith("veth") or iface.startswith("tap") or iface.startswith("fwln"):
                    speed_str = "10G"
                else:
                    speed_str = "1G"

            status = "up" if operstate.lower() in ["up", "unknown"] else "down"
            stats = istat.get("stats", {})

            ports.append({
                "port": port_num,
                "status": status,
                "link": "Link Up" if status == "up" else "Link Down",
                "speed": speed_str,
                "duplex": "Full" if status == "up" else "",
                "flow_control": "",
                "tx_packets": stats.get("tx_packets", 0),
                "rx_packets": stats.get("rx_packets", 0),
                "tx_bytes": stats.get("tx_bytes", 0),
                "rx_bytes": stats.get("rx_bytes", 0),
                "vm_name": vm_name,
                "interface": iface
            })

        # Parse MAC table
        mac_table = []
        for line in fdb.splitlines():
            line = line.strip()
            if not line or "VLAN" in line:
                continue
            parts = line.split()
            if len(parts) >= 3:
                port_num = parts[0]
                vlan = parts[1]
                mac = parts[2].upper()

                p_key, vm_label = port_to_key.get(port_num, (port_num, None))

                mac_table.append({
                    "mac": mac,
                    "type": "dynamic" if port_num != "LOCAL" else "static",
                    "port": p_key,
                    "vlan": vlan
                })

                if vm_label:
                    clean_mac = mac.replace(":", "").upper()
                    vm_mac_map[clean_mac] = vm_label

        return {
            "name": self.name,
            "ip": self.ip,
            "model": "Open vSwitch",
            "mac": data.get("mac", ""),
            "uptime": data.get("uptime", ""),
            "firmware": data.get("ovs_version", ""),
            "ports": ports,
            "mac_table": mac_table,
            "vm_mac_map": vm_mac_map,
            "dhcp_snooping": {"enabled": False, "ports": {}},
            "igmp": {"enabled": False, "entries": []},
            "jumbo_frame": {"enabled": False, "size": "Disabled"},
            "timestamp": time.time(),
        }

    def _fallback(self):
        import time
        return {
            "name": self.name,
            "ip": self.ip,
            "model": "Open vSwitch",
            "mac": "",
            "uptime": "",
            "firmware": "",
            "ports": [{"port": str(i), "status": "unknown", "speed": "",
                       "link": "Unknown", "duplex": "", "flow_control": "",
                       "tx_packets": 0, "rx_packets": 0,
                       "tx_bytes": 0, "rx_bytes": 0}
                      for i in range(1, self.port_count + 1)],
            "mac_table": [],
            "vm_mac_map": {},
            "dhcp_snooping": {"enabled": False, "ports": {}},
            "igmp": {"enabled": False, "entries": []},
            "jumbo_frame": {"enabled": False, "size": "Disabled"},
            "timestamp": time.time(),
        }


    def test_connection(self) -> Tuple[bool, str]:
        try:
            d = self.scrape()
            if "error" in d:
                return False, d["error"]
            return True, "Connected"
        except Exception as e:
            return False, str(e)

# Backward-compatibility alias
OVSScraper = OVSProtocol
