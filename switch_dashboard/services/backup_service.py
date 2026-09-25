import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

from switch_dashboard.config import BACKUP_DIR, load_config
from switch_dashboard.protocols.registry import ProtocolRegistry
from switch_dashboard.protocols.base import Capability
from switch_dashboard.security.crypto import decrypt_bytes, encrypt_bytes

logger = logging.getLogger("switch_dashboard.services.backup")


class BackupService:
    def __init__(self, backup_dir: Optional[str] = None):
        self.backup_dir = backup_dir or BACKUP_DIR
        os.makedirs(self.backup_dir, exist_ok=True)
        self._migrate_legacy_backups()

    def _migrate_legacy_backups(self):
        for filename in os.listdir(self.backup_dir):
            if not filename.startswith("switch_cfg_") or not filename.endswith(".bin"):
                continue
            source = os.path.join(self.backup_dir, filename)
            encrypted_name = f"{filename}.enc"
            destination = os.path.join(self.backup_dir, encrypted_name)
            if os.path.exists(destination):
                continue
            with open(source, "rb") as f:
                plaintext = f.read()
            encrypted = encrypt_bytes(plaintext, "backup:v1")
            temp_path = f"{destination}.tmp"
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            if decrypt_bytes(encrypted, "backup:v1") != plaintext:
                os.remove(temp_path)
                raise RuntimeError(f"Could not verify migrated backup {filename}")
            os.replace(temp_path, destination)
            os.remove(source)

    def backup_switch(self, ip: str) -> Dict[str, Any]:
        cfg = load_config()
        sw = next((s for s in cfg.get("switches", []) if s.get("ip") == ip), None)
        if not sw:
            raise ValueError("Switch not found")

        if sw.get("model", "").lower() == "internet":
            raise ValueError("Virtual Internet node cannot be backed up")

        protocol = ProtocolRegistry.create(sw)
        if not protocol.has_capability(Capability.BACKUP):
            raise NotImplementedError(f"Protocol '{protocol.name}' does not support configuration backup.")

        binary_data = protocol.download_backup()
        if not binary_data:
            raise RuntimeError("No backup data retrieved from switch")

        ip_dashed = ip.replace(".", "-")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"switch_cfg_{ip_dashed}_{timestamp}.bin.enc"
        filepath = os.path.join(self.backup_dir, filename)

        encrypted_data = encrypt_bytes(binary_data, "backup:v1")
        fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(encrypted_data)

        logger.info(f"Successfully saved backup: {filename}")
        return {
            "status": "ok",
            "filename": filename,
            "size": len(encrypted_data),
            "timestamp": timestamp,
        }

    def reboot_switch(self, ip: str) -> bool:
        cfg = load_config()
        sw = next((s for s in cfg.get("switches", []) if s.get("ip") == ip), None)
        if not sw:
            raise ValueError("Switch not found")

        if sw.get("model", "").lower() == "internet":
            raise ValueError("Virtual Internet node cannot be rebooted")

        protocol = ProtocolRegistry.create(sw)
        if not protocol.has_capability(Capability.REBOOT):
            raise NotImplementedError(f"Protocol '{protocol.name}' does not support device reboot.")

        res = protocol.reboot_switch()
        return bool(res) if res is not None else True

    def list_backups(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.backup_dir):
            return []

        backups = []
        try:
            for filename in os.listdir(self.backup_dir):
                if filename.startswith("switch_cfg_") and filename.endswith(".bin.enc"):
                    filepath = os.path.join(self.backup_dir, filename)
                    if os.path.isfile(filepath):
                        stat = os.stat(filepath)
                        size = stat.st_size
                        if size >= 1048576:
                            size_str = f"{size / 1048576:.1f} MB"
                        elif size >= 1024:
                            size_str = f"{size / 1024:.1f} KB"
                        else:
                            size_str = f"{size} B"

                        parts = filename[:-8].split("_")
                        if len(parts) >= 5:
                            ip = parts[2].replace("-", ".")
                            date_part = parts[3]
                            time_part = parts[4]
                            if len(date_part) == 8 and len(time_part) == 4:
                                dt_str = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[0:2]}:{time_part[2:4]}"
                            else:
                                dt_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                        else:
                            ip = "Unknown"
                            dt_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")

                        backups.append({
                            "filename": filename,
                            "ip": ip,
                            "size": size,
                            "size_str": size_str,
                            "datetime": dt_str,
                            "mtime": stat.st_mtime,
                        })
            backups.sort(key=lambda x: x["mtime"], reverse=True)
        except Exception as e:
            logger.error(f"Failed to list backups: {e}")
        return backups

    def read_backup(self, filename: str) -> bytes:
        if ".." in filename or "/" in filename or "\\" in filename or not filename.endswith(".bin.enc"):
            raise ValueError("Invalid filename")
        filepath = os.path.join(self.backup_dir, filename)
        if not os.path.isfile(filepath):
            raise FileNotFoundError(filename)
        with open(filepath, "rb") as f:
            encrypted = f.read()
        try:
            return decrypt_bytes(encrypted, "backup:v1")
        except Exception:
            # Compatibility with early encrypted-backup builds that bound AAD to the filename.
            return decrypt_bytes(encrypted, f"backup:{filename}")

    def delete_backup(self, filename: str) -> bool:
        if ".." in filename or "/" in filename or "\\" in filename:
            raise ValueError("Invalid filename")

        filepath = os.path.join(self.backup_dir, filename)
        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            return False

        os.remove(filepath)
        return True


_backup_service: Optional[BackupService] = None


def get_backup_service() -> BackupService:
    global _backup_service
    if _backup_service is None:
        _backup_service = BackupService()
    return _backup_service
