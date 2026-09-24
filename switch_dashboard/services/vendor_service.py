import os
import logging
import threading
import urllib.request
import subprocess
from typing import Dict, Optional

from switch_dashboard.config import (
    OUI36_TXT_PATH,
    OUI_TXT_PATH,
    VENDORS_TXT_PATH,
)

logger = logging.getLogger("switch_dashboard.services.vendor")


class VendorService:
    def __init__(self):
        self._lock = threading.Lock()
        self._ieee_cache: Optional[Dict[str, str]] = None
        self._custom_cache: Optional[Dict[str, str]] = None

    def download_single_oui_file(self, url: str, dest_path: str) -> bool:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        logger.info(f"Downloading OUI list from {url} to {dest_path}...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read()
            with open(dest_path, "wb") as f:
                f.write(content)
            with self._lock:
                self._ieee_cache = None
            logger.info(f"Successfully downloaded and saved {os.path.basename(dest_path)}.")
            return True
        except Exception as e:
            logger.warning(f"Failed to download {os.path.basename(dest_path)} using urllib: {e}. Trying curl...")
            try:
                res = subprocess.run(["curl", "-s", "-o", dest_path, "-A", ua, url], timeout=25)
                if res.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
                    with self._lock:
                        self._ieee_cache = None
                    logger.info(f"Successfully downloaded and saved {os.path.basename(dest_path)} via curl.")
                    return True
            except Exception as ex:
                logger.error(f"Failed to download {os.path.basename(dest_path)} via curl: {ex}")
        return False

    def download_oui_files(self):
        if not os.path.exists(OUI36_TXT_PATH):
            self.download_single_oui_file("https://standards-oui.ieee.org/oui36/oui36.txt", OUI36_TXT_PATH)
        if not os.path.exists(OUI_TXT_PATH):
            self.download_single_oui_file("https://standards-oui.ieee.org/oui/oui.txt", OUI_TXT_PATH)

    def load_ieee_oui(self) -> Dict[str, str]:
        oui_db = {}
        if os.path.exists(OUI36_TXT_PATH):
            try:
                with open(OUI36_TXT_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    current_oui = None
                    for line in f:
                        if "(hex)" in line:
                            parts = line.split("(hex)")
                            current_oui = parts[0].strip().replace("-", "").replace(":", "").replace(" ", "").upper()
                        elif "(base 16)" in line and current_oui:
                            parts = line.split("(base 16)")
                            range_str = parts[0].strip()
                            name = parts[1].strip()
                            if "-" in range_str:
                                range_prefix = range_str.split("-")[0].strip()[:3]
                                oui_db[current_oui + range_prefix] = name
                            else:
                                oui_db[current_oui] = name
            except Exception as e:
                logger.error(f"Error parsing IEEE OUI36 file: {e}")

        if os.path.exists(OUI_TXT_PATH):
            try:
                with open(OUI_TXT_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    current_oui = None
                    for line in f:
                        if "(hex)" in line:
                            parts = line.split("(hex)")
                            current_oui = parts[0].strip().replace("-", "").replace(":", "").replace(" ", "").upper()
                        elif "(base 16)" in line and current_oui:
                            parts = line.split("(base 16)")
                            range_str = parts[0].strip()
                            name = parts[1].strip()
                            if "-" in range_str:
                                range_prefix = range_str.split("-")[0].strip()[:3]
                                oui_db[current_oui + range_prefix] = name
                            else:
                                oui_db[current_oui] = name
            except Exception as e:
                logger.error(f"Error parsing IEEE OUI file: {e}")

        return oui_db

    def get_ieee_vendors(self) -> Dict[str, str]:
        with self._lock:
            if self._ieee_cache is None:
                self._ieee_cache = self.load_ieee_oui()
            return self._ieee_cache

    def load_mac_vendors(self) -> Dict[str, str]:
        vendors = {}
        if os.path.exists(VENDORS_TXT_PATH):
            try:
                with open(VENDORS_TXT_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            prefix, name = parts
                            clean_prefix = prefix.replace(":", "").replace("-", "").replace(" ", "").upper()
                            if len(clean_prefix) in [6, 9]:
                                vendors[clean_prefix] = name.strip()
            except Exception as e:
                logger.error(f"Error loading custom MAC vendors: {e}")
        return vendors

    def get_custom_vendors(self) -> Dict[str, str]:
        with self._lock:
            if self._custom_cache is None:
                self._custom_cache = self.load_mac_vendors()
            return self._custom_cache

    def refresh_custom_vendors(self):
        with self._lock:
            self._custom_cache = self.load_mac_vendors()

    def lookup_vendor(
        self,
        mac_str: str,
        vendors: Optional[Dict[str, str]] = None,
        ieee_vendors: Optional[Dict[str, str]] = None,
    ) -> str:
        if not mac_str:
            return ""
        clean_mac = mac_str.replace(":", "").replace("-", "").replace(" ", "").upper()
        prefix_9 = clean_mac[:9]
        prefix_6 = clean_mac[:6]

        ieee = ieee_vendors if ieee_vendors is not None else self.get_ieee_vendors()
        custom = vendors if vendors is not None else self.get_custom_vendors()

        if len(prefix_9) == 9 and prefix_9 in ieee:
            return ieee[prefix_9]
        if len(prefix_6) == 6 and prefix_6 in ieee:
            return ieee[prefix_6]
        if len(prefix_9) == 9 and prefix_9 in custom:
            return custom[prefix_9]
        if len(prefix_6) == 6 and prefix_6 in custom:
            return custom[prefix_6]
        return ""


_vendor_service: Optional[VendorService] = None


def get_vendor_service() -> VendorService:
    global _vendor_service
    if _vendor_service is None:
        _vendor_service = VendorService()
    return _vendor_service
