import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

try:
    import paramiko
except ImportError:
    paramiko = None

from switch_dashboard.protocols.base import BaseProtocol, Capability
from switch_dashboard.protocols.registry import register_protocol

logger = logging.getLogger(__name__)


class SSHCommandError(RuntimeError):
    """Raised when a remote command exits unsuccessfully."""


@register_protocol(
    name="ssh",
    display_name="Generic SSH (JSON commands)",
    supported_models=["generic_ssh", "ssh"],
)
class SSHProtocol(BaseProtocol):
    """Selectable JSON-command driver and reusable SSH transport."""

    name = "ssh"
    display_name = "Generic SSH (JSON commands)"
    capabilities = {
        Capability.METRICS,
        Capability.MAC_TABLE,
        Capability.NEIGHBORS,
    }
    config_schema = vol.Schema({
        vol.Required("username", description="SSH Username"): str,
        vol.Optional("password", default="", description="SSH Password"): str,
        vol.Optional("key_filename", default="", description="Path to SSH Private Key"): str,
        vol.Optional("ssh_port", default=22, description="SSH Port"): vol.Coerce(int),
        vol.Optional("ssh_timeout", default=10, description="SSH and Command Timeout (seconds)"): vol.Coerce(int),
        vol.Optional("strict_host_key", default=True, description="Require a trusted SSH host key"): bool,
        vol.Required("scrape_command", description="Command returning dashboard telemetry as JSON"): str,
        vol.Optional("mac_table_command", default="", description="Command returning a JSON MAC table array"): str,
        vol.Optional("neighbors_command", default="", description="Command returning a JSON LLDP neighbor array"): str,
    })

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.key_filename = config.get("key_filename", "")
        self.ssh_port = int(config.get("ssh_port", config.get("port", 22)))
        self.ssh_timeout = int(config.get("ssh_timeout", 10))
        self.strict_host_key = config.get("strict_host_key", True)
        self.scrape_command = config.get("scrape_command", "")
        self.mac_table_command = config.get("mac_table_command", "")
        self.neighbors_command = config.get("neighbors_command", "")
        self._last_payload: Dict[str, Any] = {}

    def _create_client(self):
        if paramiko is None:
            raise RuntimeError("Paramiko is required for SSH protocol drivers")

        client = paramiko.SSHClient()
        if self.strict_host_key:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # Do not load known_hosts in non-strict mode. AutoAddPolicy only
            # accepts unknown hosts; loaded stale entries still cause a
            # BadHostKeyException before that policy is consulted.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return client

    def _connect(self):
        client = self._create_client()
        connect_args = {
            "hostname": self.ip,
            "port": self.ssh_port,
            "username": self.username,
            "timeout": self.ssh_timeout,
            "banner_timeout": self.ssh_timeout,
            "auth_timeout": self.ssh_timeout,
            "look_for_keys": not bool(self.password or self.key_filename),
            "allow_agent": not bool(self.password or self.key_filename),
        }
        if self.password:
            connect_args["password"] = self.password
        if self.key_filename:
            connect_args["key_filename"] = self.key_filename

        try:
            client.connect(**connect_args)
            return client
        except paramiko.BadHostKeyException as exc:
            client.close()
            host = f"[{self.ip}]:{self.ssh_port}" if self.ssh_port != 22 else self.ip
            raise RuntimeError(
                f"SSH host key mismatch for {host}. Verify the device identity, then remove "
                f"the stale key with: ssh-keygen -R '{host}'"
            ) from exc
        except Exception:
            client.close()
            raise

    def execute_command(self, command: str, stdin_data: Optional[str] = None) -> str:
        """Execute one command and return stdout, raising on a non-zero exit."""
        if not command or not command.strip():
            raise ValueError("SSH command cannot be empty")

        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=self.ssh_timeout)
            if stdin_data is not None:
                stdin.write(stdin_data)
                stdin.flush()
            stdin.close()

            output = stdout.read().decode("utf-8", errors="replace")
            error = stderr.read().decode("utf-8", errors="replace").strip()
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                detail = error or output.strip() or "no error output"
                raise SSHCommandError(f"Command exited with status {exit_status}: {detail}")
            return output
        finally:
            client.close()

    @staticmethod
    def _parse_json(output: str, expected_type: type, command_name: str):
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{command_name} did not return valid JSON: {exc.msg}") from exc
        if not isinstance(value, expected_type):
            raise ValueError(f"{command_name} must return a JSON {expected_type.__name__}")
        return value

    def test_connection(self) -> Tuple[bool, str]:
        try:
            output = self.execute_command("true")
            return True, "SSH connection successful" if not output else output.strip()
        except Exception as exc:
            return False, f"SSH connection failed: {exc}"

    def scrape(self) -> Dict[str, Any]:
        try:
            payload = self._parse_json(
                self.execute_command(self.scrape_command), dict, "scrape_command"
            )
            ports = payload.get("ports", [])
            if not isinstance(ports, list):
                raise ValueError("scrape_command field 'ports' must be a JSON array")

            payload.setdefault("name", self.name_tag)
            payload.setdefault("ip", self.ip)
            payload.setdefault("model", self.model)
            payload.setdefault("firmware", "")
            payload.setdefault("uptime", "")
            payload.setdefault("mac", self.config.get("mac", ""))
            payload.setdefault("status", "online")
            payload.setdefault("timestamp", time.time())
            payload["ports"] = [self._normalize_port(port) for port in ports]
            self._last_payload = payload
            return payload
        except Exception as exc:
            logger.warning("Generic SSH scrape failed for %s: %s", self.ip, exc)
            return self._offline_result(str(exc))

    def scrape_mac_table(self) -> List[Dict[str, Any]]:
        if self.mac_table_command:
            try:
                return self._parse_json(
                    self.execute_command(self.mac_table_command), list, "mac_table_command"
                )
            except Exception as exc:
                logger.warning("Generic SSH MAC table command failed for %s: %s", self.ip, exc)
                return []
        table = self._last_payload.get("mac_table", [])
        return table if isinstance(table, list) else []

    def scrape_neighbors(self) -> List[Dict[str, Any]]:
        if self.neighbors_command:
            try:
                return self._parse_json(
                    self.execute_command(self.neighbors_command), list, "neighbors_command"
                )
            except Exception as exc:
                logger.warning("Generic SSH neighbors command failed for %s: %s", self.ip, exc)
                return []
        neighbors = self._last_payload.get("neighbors", [])
        return neighbors if isinstance(neighbors, list) else []

    @staticmethod
    def _normalize_port(port: Any) -> Dict[str, Any]:
        if not isinstance(port, dict):
            raise ValueError("Each port returned by scrape_command must be a JSON object")
        normalized = dict(port)
        normalized["port"] = str(normalized.get("port", ""))
        status = str(normalized.get("status", "down")).lower()
        normalized["status"] = "up" if status == "up" else "down"
        normalized.setdefault("link", "Link Up" if normalized["status"] == "up" else "Link Down")
        normalized.setdefault("speed", "")
        normalized.setdefault("duplex", "Full" if normalized["status"] == "up" else "")
        normalized.setdefault("flow_control", "")
        for field in ("tx_bytes", "rx_bytes", "tx_packets", "rx_packets"):
            normalized.setdefault(field, 0)
        return normalized

    def _offline_result(self, error: str) -> Dict[str, Any]:
        ports = []
        for port_number in range(1, self.port_count + 1):
            ports.append(self._normalize_port({"port": port_number, "status": "down"}))
        return {
            "name": self.name_tag,
            "ip": self.ip,
            "model": self.model,
            "firmware": "",
            "uptime": "",
            "mac": self.config.get("mac", ""),
            "status": "offline",
            "error": error,
            "ports": ports,
            "timestamp": time.time(),
        }
