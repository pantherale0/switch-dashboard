import time
import logging
from typing import Tuple
from urllib.parse import urlparse
from defusedxml import ElementTree as ET

try:
    import requests
    from requests.auth import HTTPDigestAuth
except ImportError:
    requests = None
    HTTPDigestAuth = None

import voluptuous as vol
from switch_dashboard.protocols.base import BaseProtocol, Capability
from switch_dashboard.protocols.registry import register_protocol

logger = logging.getLogger(__name__)

@register_protocol(
    name="fritzbox",
    display_name="AVM FRITZ!Box (TR-064 SOAP)",
    supported_models=["fritzbox"]
)
class FritzBoxProtocol(BaseProtocol):
    name = "fritzbox"
    display_name = "AVM FRITZ!Box (TR-064 SOAP)"
    capabilities = {
        Capability.METRICS,
        Capability.MAC_TABLE,
    }
    config_schema = vol.Schema({
        vol.Optional("username", default="", description="FRITZ!Box Username (if configured)"): str,
        vol.Required("password", description="FRITZ!Box Admin Password"): str,
        vol.Optional("protocol_port", default=49000, description="TR-064 SOAP Port"): vol.Coerce(int),
    })

    def __init__(self, config):
        super().__init__(config)
        self.name = config.get("name", config.get("ip", "FritzBox"))
        self.ip = config["ip"]
        self.username = config.get("username", "admin")
        self.password = config.get("password", "")
        self.model = config.get("model", "fritzbox")
        self._cached_data = None
        self._cache_time = 0

    def _get_data(self):
        import time
        if self._cached_data and (time.time() - self._cache_time < 5.0):
            return self._cached_data
        data = self._scrape_fritzbox()
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
        return "Not supported."

    def _soap_request(self, service, action, body_content=""):
        import requests
        from requests.auth import HTTPDigestAuth

        service_part = service.split(":")[-2].lower()
        if "wancommoninterfaceconfig" in service_part:
            control_path = "wancommonifconfig1"
        elif "lanethernetinterfaceconfig" in service_part:
            control_path = "lanethernetifcfg"
        else:
            control_path = service_part

        ip = self.ip
        if ":" not in ip:
            ip = f"{ip}:49000"

        url = f"http://{ip}/upnp/control/{control_path}"
        
        headers = {
            "SoapAction": f"{service}#{action}",
            "Content-Type": 'text/xml; charset="utf-8"'
        }
        
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="{service}">
      {body_content}
    </u:{action}>
  </s:Body>
</s:Envelope>"""
        
        auth = HTTPDigestAuth(self.username, self.password) if self.username else None
        r = requests.post(url, data=envelope, headers=headers, auth=auth, timeout=5, allow_redirects=False)
        r.raise_for_status()
        return r.text

    def _scrape_fritzbox(self):
        import time
        import requests
        from requests.auth import HTTPDigestAuth

        try:
            def strip_ns(el):
                for elem in el.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
                return el

            # 1. Device Info
            uptime = ""
            model = "FRITZ!Box"
            firmware = ""
            mac = ""
            try:
                res_info = self._soap_request("urn:dslforum-org:service:DeviceInfo:1", "GetInfo")
                root_info = ET.fromstring(res_info)
                strip_ns(root_info)
                
                el_uptime = root_info.find(".//NewUpTime")
                if el_uptime is not None:
                    uptime_sec = int(el_uptime.text)
                    days = uptime_sec // 86400
                    hours = (uptime_sec % 86400) // 3600
                    minutes = (uptime_sec % 3600) // 60
                    if days > 0:
                        uptime = f"{days}d {hours}h {minutes}m"
                    elif hours > 0:
                        uptime = f"{hours}h {minutes}m"
                    else:
                        uptime = f"{minutes}m"
                        
                el_model = root_info.find(".//NewModelName")
                if el_model is not None:
                    model = el_model.text
                    
                el_fw = root_info.find(".//NewSoftwareVersion")
                if el_fw is not None:
                    firmware = el_fw.text

                el_sn = root_info.find(".//NewSerialNumber")
                if el_sn is not None:
                    mac = el_sn.text
                    if len(mac) == 12 and all(c in '0123456789ABCDEFabcdef' for c in mac):
                        mac = ":".join(mac[i:i+2] for i in range(0, 12, 2)).upper()
            except Exception as e:
                logger.error(f"FritzBoxScraper: Error fetching DeviceInfo: {e}")

            # 2. WAN Status & Statistics
            wan_status = "down"
            wan_link = "Link Down"
            wan_speed = "Auto"
            wan_tx_bytes = 0
            wan_rx_bytes = 0
            wan_tx_packets = 0
            wan_rx_packets = 0

            try:
                res_wan = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetCommonLinkProperties")
                root_wan = ET.fromstring(res_wan)
                strip_ns(root_wan)
                
                el_link = root_wan.find(".//NewPhysicalLinkStatus")
                if el_link is not None:
                    wan_status = "up" if el_link.text.lower() == "up" else "down"
                    wan_link = "Link Up" if wan_status == "up" else "Link Down"
                    
                el_ds_rate = root_wan.find(".//NewLayer1DownstreamMaxBitRate")
                el_us_rate = root_wan.find(".//NewLayer1UpstreamMaxBitRate")
                if el_ds_rate is not None and el_us_rate is not None:
                    ds_bps = int(el_ds_rate.text) * 1000
                    us_bps = int(el_us_rate.text) * 1000
                    
                    def fmt_bps_short(bps):
                        if bps >= 1_000_000_000:
                            return f"{int(bps/1_000_000_000)}G"
                        if bps >= 1_000_000:
                            return f"{int(bps/1_000_000)}M"
                        return f"{int(bps/1_000)}K"
                    
                    wan_speed = f"{fmt_bps_short(ds_bps)}/{fmt_bps_short(us_bps)}"

                res_tx_b = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalBytesSent")
                root_tx_b = ET.fromstring(res_tx_b)
                strip_ns(root_tx_b)
                el_tx_b = root_tx_b.find(".//NewTotalBytesSent")
                if el_tx_b is not None:
                    wan_tx_bytes = int(el_tx_b.text)

                res_rx_b = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalBytesReceived")
                root_rx_b = ET.fromstring(res_rx_b)
                strip_ns(root_rx_b)
                el_rx_b = root_rx_b.find(".//NewTotalBytesReceived")
                if el_rx_b is not None:
                    wan_rx_bytes = int(el_rx_b.text)

                res_tx_p = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalPacketsSent")
                root_tx_p = ET.fromstring(res_tx_p)
                strip_ns(root_tx_p)
                el_tx_p = root_tx_p.find(".//NewTotalPacketsSent")
                if el_tx_p is not None:
                    wan_tx_packets = int(el_tx_p.text)

                res_rx_p = self._soap_request("urn:dslforum-org:service:WANCommonInterfaceConfig:1", "GetTotalPacketsReceived")
                root_rx_p = ET.fromstring(res_rx_p)
                strip_ns(root_rx_p)
                el_rx_p = root_rx_p.find(".//NewTotalPacketsReceived")
                if el_rx_p is not None:
                    wan_rx_packets = int(el_rx_p.text)

            except Exception as e:
                logger.error(f"FritzBoxScraper: Error fetching WAN stats: {e}")

            # 3. Hosts List & Virtual WLAN/LAN Active status
            mac_table = []
            active_wlan_hosts = 0
            max_wlan_speed = 0
            lan_ports_active = {1: False, 2: False, 3: False, 4: False}
            lan_ports_max_speed = {1: 0, 2: 0, 3: 0, 4: 0}

            try:
                res_path = self._soap_request("urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetHostListPath")
                root_path = ET.fromstring(res_path)
                strip_ns(root_path)
                el_path = root_path.find(".//NewX_AVM-DE_HostListPath")
                if el_path is not None and el_path.text:
                    path = el_path.text
                    ip_part = self.ip
                    if ":" not in ip_part:
                        ip_part = f"{ip_part}:49000"
                    if path.startswith("http"):
                        download_url = path
                    else:
                        download_url = f"http://{ip_part}{path}"
                        
                    expected = urlparse(f"//{ip_part}")
                    target = urlparse(download_url)
                    if target.scheme != "http" or target.hostname != expected.hostname or target.port != expected.port:
                        raise ValueError("FRITZ!Box returned a host-list URL for a different origin")
                    auth = HTTPDigestAuth(self.username, self.password) if self.username else None
                    r_list = requests.get(download_url, auth=auth, timeout=5, allow_redirects=False)
                    r_list.raise_for_status()
                    
                    root_list = ET.fromstring(r_list.content)
                    items = root_list.findall("Item")
                    
                    for item in items:
                        el_act = item.find("Active")
                        if el_act is not None and el_act.text == "1":
                            el_mac = item.find("MACAddress")
                            el_ip = item.find("IPAddress")
                            el_name = item.find("HostName")
                            el_type = item.find("InterfaceType")
                            el_port = item.find("X_AVM-DE_Port")
                            el_speed = item.find("X_AVM-DE_Speed")
                            
                            mac_addr = el_mac.text.upper() if el_mac is not None and el_mac.text else ""
                            ip_addr = el_ip.text.strip() if el_ip is not None and el_ip.text else ""
                            host_name = el_name.text.strip() if el_name is not None and el_name.text else ""
                            iftype = el_type.text if el_type is not None and el_type.text else ""
                            port_str = el_port.text if el_port is not None and el_port.text else "0"
                            speed_str = el_speed.text if el_speed is not None and el_speed.text else "0"
                            
                            try:
                                speed_val = int(speed_str)
                            except ValueError:
                                speed_val = 0
                                
                            if not mac_addr:
                                continue
                                
                            v_port = ""
                            if iftype == "802.11":
                                v_port = "WLAN"
                                active_wlan_hosts += 1
                                if speed_val > max_wlan_speed:
                                    max_wlan_speed = speed_val
                            elif iftype == "Ethernet":
                                try:
                                    port_num = int(port_str)
                                except ValueError:
                                    port_num = 0
                                    
                                if port_num in [1, 2, 3, 4]:
                                    v_port = str(port_num)
                                    lan_ports_active[port_num] = True
                                    if speed_val > lan_ports_max_speed[port_num]:
                                        lan_ports_max_speed[port_num] = speed_val
                                else:
                                    v_port = "1"
                                    lan_ports_active[1] = True
                                    if speed_val > lan_ports_max_speed[1]:
                                        lan_ports_max_speed[1] = speed_val
                                        
                            if v_port:
                                mac_table.append({
                                    "mac": mac_addr,
                                    "ip": ip_addr,
                                    "hostname": host_name,
                                    "type": "dynamic",
                                    "port": v_port,
                                    "vlan": "1"
                                })
            except Exception as e:
                logger.error(f"FritzBoxScraper: Error parsing hosts list: {e}")

            # 4. Construct Virtual Ports
            ports = []
            ports.append({
                "port": "WAN",
                "status": wan_status,
                "link": wan_link,
                "speed": wan_speed,
                "duplex": "Full" if wan_status == "up" else "",
                "flow_control": "",
                "tx_packets": wan_tx_packets,
                "rx_packets": wan_rx_packets,
                "tx_bytes": wan_tx_bytes,
                "rx_bytes": wan_rx_bytes
            })
            
            for i in range(1, 5):
                active = lan_ports_active[i]
                speed_val = lan_ports_max_speed[i]
                speed_str = "Auto"
                if active:
                    if speed_val >= 1000:
                        speed_str = "1G"
                    elif speed_val > 0:
                        speed_str = f"{speed_val}M"
                    else:
                        speed_str = "1G"
                ports.append({
                    "port": str(i),
                    "name": f"LAN {i}",
                    "status": "up" if active else "down",
                    "link": "Link Up" if active else "Link Down",
                    "speed": speed_str if active else "",
                    "duplex": "Full" if active else "",
                    "flow_control": "",
                    "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0
                })
                
            wlan_active = active_wlan_hosts > 0
            wlan_speed_str = ""
            if wlan_active:
                if max_wlan_speed >= 1000:
                    wlan_speed_str = f"{max_wlan_speed/1000:.1f}G".replace(".0", "")
                elif max_wlan_speed > 0:
                    wlan_speed_str = f"{max_wlan_speed}M"
                else:
                    wlan_speed_str = "Auto"
            ports.append({
                "port": "WLAN",
                "status": "up" if wlan_active else "down",
                "link": "Link Up" if wlan_active else "Link Down",
                "speed": wlan_speed_str if wlan_active else "",
                "duplex": "Full" if wlan_active else "",
                "flow_control": "",
                "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0
            })

            return {
                "name": self.name,
                "ip": self.ip,
                "model": model,
                "mac": mac,
                "uptime": uptime,
                "firmware": firmware,
                "status": "online",
                "ports": ports,
                "mac_table": mac_table,
                "dhcp_snooping": {"enabled": False, "ports": {}},
                "igmp": {"enabled": False, "entries": []},
                "jumbo_frame": {"enabled": False, "size": "Disabled"},
                "timestamp": time.time(),
            }
        except Exception as e:
            logger.error(f"FritzBoxScraper: Scraping failed for {self.ip}: {e}")
            return self._fallback()

    def _fallback(self):
        import time
        return {
            "name": self.name,
            "ip": self.ip,
            "model": "FRITZ!Box",
            "status": "offline",
            "mac": "",
            "uptime": "",
            "firmware": "",
            "ports": [
                {"port": "WAN", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "1", "name": "LAN 1", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "2", "name": "LAN 2", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "3", "name": "LAN 3", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "4", "name": "LAN 4", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0},
                {"port": "WLAN", "status": "down", "speed": "", "link": "Link Down", "duplex": "", "flow_control": "", "tx_packets": 0, "rx_packets": 0, "tx_bytes": 0, "rx_bytes": 0}
            ],
            "mac_table": [],
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
FritzBoxScraper = FritzBoxProtocol
