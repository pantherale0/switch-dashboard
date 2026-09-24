import logging
import re
from typing import Dict

logger = logging.getLogger("switch_dashboard.services.clients.classifier")


def clean_brand_name(vendor: str) -> str:
    """Normalizes raw OUI vendor names into consumer-friendly brand names."""
    if not vendor:
        return "Unknown"
    v = vendor.lower()
    if "apple" in v:
        return "Apple"
    if "amazon" in v:
        return "Amazon"
    if "dell" in v:
        return "Dell"
    if "hewlett" in v or "hp " in v or v == "hp":
        return "HP"
    if "lexmark" in v:
        return "Lexmark"
    if "samsung" in v:
        return "Samsung"
    if "google" in v:
        return "Google"
    if "microsoft" in v:
        return "Microsoft"
    if "sony" in v:
        return "Sony"
    if "lg electronics" in v or "lg " in v:
        return "LG"
    if "raspberry" in v:
        return "Raspberry Pi"
    if "espressif" in v:
        return "Espressif"
    if "sonos" in v:
        return "Sonos"
    if "intel" in v:
        return "Intel"
    if "ubiquiti" in v or "unifi" in v:
        return "UniFi"
    if "cisco" in v:
        return "Cisco"
    if "synology" in v:
        return "Synology"
    if "tp-link" in v or "tplink" in v:
        return "TP-Link"
    if "netgear" in v:
        return "Netgear"
    if "asus" in v:
        return "ASUS"
    if "avm" in v or "fritz" in v:
        return "AVM / Fritz!"
    if "qnap" in v:
        return "QNAP"
    if "huawei" in v:
        return "Huawei"
    if "xiaomi" in v:
        return "Xiaomi"
    if "roku" in v:
        return "Roku"
    if "sonoff" in v or "itead" in v:
        return "Sonoff"
    if "anker" in v or "eufy" in v:
        return "Anker / Eufy"
    if "tuya" in v:
        return "Tuya"
    if "shelly" in v or "alterco" in v or "allterco" in v:
        return "Shelly"
    if "ring" in v:
        return "Ring"
    if "nest" in v:
        return "Nest"
    if "philips" in v or "signify" in v:
        return "Philips Hue"
    # Clean corporate entity suffixes
    clean = re.sub(
        r",?\s*(Inc\.?|Corp\.?|Corporation|Co\.?,?\s*Ltd\.?|Technology|Technologies|GmbH|LLC|S\.A\.)$",
        "",
        vendor,
        flags=re.IGNORECASE,
    ).strip()
    return clean or vendor


class DeviceClassifier:
    """Classifies network devices into Fing-style categories, icons, and brand/model designations."""

    @classmethod
    def classify(
        cls,
        vendor: str = "",
        hostname: str = "",
        custom_name: str = "",
        device_type: str = "",
        mac: str = "",
    ) -> Dict[str, str]:
        res = cls.classify_device(
            vendor=vendor,
            hostname=hostname,
            custom_name=custom_name,
            device_type=device_type,
            mac=mac,
        )
        logger.debug(
            f"Classified client {mac or 'unknown'} (vendor='{vendor}', host='{hostname}'): "
            f"category={res['category']}, brand={res['brand']}, model={res['model']}"
        )
        return res

    @staticmethod
    def classify_device(
        vendor: str = "",
        hostname: str = "",
        custom_name: str = "",
        device_type: str = "",
        mac: str = "",
    ) -> Dict[str, str]:
        """Classifies a client into Fing-style device categories, icons, and brand/model."""
        v_lower = (vendor or "").lower()
        h_lower = (hostname or "").lower()
        c_lower = (custom_name or "").lower()
        dt_lower = (device_type or "").lower()
        combined = f"{v_lower} {h_lower} {c_lower} {dt_lower}"

        brand = clean_brand_name(vendor)
        model = ""

        # Extract model from hostname patterns if present
        model_match = re.search(
            r"\b(cx\d+|prox-\w+|latitude|elitebook[\s\w]*|thinkpad[\s\w]*|robovac[\s\w]*|firebox|fingbox[\s\w]*|unraid|diskstation|galaxy[\s\w]*|pixel[\s\w]*|iphone[\s\w]*|ipad[\s\w]*|surface[\s\w]*)\b",
            combined,
            re.IGNORECASE,
        )
        if model_match:
            model = model_match.group(0).strip().title()

        # 1. Router / Gateway
        if (
            dt_lower in ("router", "gateway", "firewall")
            or "router" in combined
            or "gateway" in combined
            or "fritzbox" in combined
            or "udm" in combined
            or "usg" in combined
            or "opnsense" in combined
            or "pfsense" in combined
        ):
            return {
                "category": "router",
                "category_label": "Router",
                "icon": "router",
                "brand": brand or "Router",
                "model": model or "Gateway",
            }

        # 2. Access Point
        if (
            dt_lower in ("ap", "access_point")
            or "access point" in combined
            or "uap" in combined
            or "wps access point" in combined
            or "extender" in combined
        ):
            return {
                "category": "access_point",
                "category_label": "Access Point",
                "icon": "wifi",
                "brand": brand or "UniFi",
                "model": model or "Access Point",
            }

        # 3. Printer
        if (
            dt_lower == "printer"
            or "printer" in combined
            or "print" in combined
            or "lexmark" in combined
            or "epson" in combined
            or "brother" in combined
            or "canon" in combined
            or "laserjet" in combined
        ):
            return {
                "category": "printer",
                "category_label": "Printer",
                "icon": "printer",
                "brand": brand if brand != "Unknown" else "Printer",
                "model": model or (hostname if "print" not in h_lower else "Network Printer"),
            }

        # 4. Laptop
        if (
            dt_lower == "laptop"
            or "laptop" in combined
            or "notebook" in combined
            or "macbook" in combined
            or "elitebook" in combined
            or "thinkpad" in combined
            or "latitude" in combined
            or "slimbook" in combined
            or "xps" in combined
            or "zenbook" in combined
        ):
            return {
                "category": "laptop",
                "category_label": "Laptop",
                "icon": "laptop",
                "brand": brand if brand != "Unknown" else "Laptop",
                "model": model or "PC / Laptop",
            }

        # 5. Smartphone
        if (
            dt_lower in ("phone", "smartphone")
            or "iphone" in combined
            or "pixel" in combined
            or "galaxy s" in combined
            or "galaxy a" in combined
            or "xperia" in combined
            or "oneplus" in combined
            or "xiaomi" in combined
            or "huawei" in combined
        ):
            return {
                "category": "smartphone",
                "category_label": "Smartphone",
                "icon": "mobile",
                "brand": brand if brand != "Unknown" else "Phone",
                "model": model or "Smartphone",
            }

        # 6. Tablet
        if (
            dt_lower == "tablet"
            or "ipad" in combined
            or "tab" in combined
            or "fire tablet" in combined
            or ("amazon" in combined and "fire" in combined and "tv" not in combined)
        ):
            return {
                "category": "tablet",
                "category_label": "Tablet",
                "icon": "tablet",
                "brand": brand if brand != "Unknown" else "Tablet",
                "model": model or "Tablet",
            }

        # 7. Television / Streaming
        if (
            dt_lower in ("tv", "television")
            or "tv" in combined
            or "television" in combined
            or "roku" in combined
            or "bravia" in combined
            or "appletv" in combined
            or "firetv" in combined
            or "chromecast" in combined
            or "shield" in combined
        ):
            return {
                "category": "television",
                "category_label": "Television",
                "icon": "tv",
                "brand": brand if brand != "Unknown" else "Television",
                "model": model or "Smart TV",
            }

        # 8. Media Player / Audio
        if (
            dt_lower == "media_player"
            or "media player" in combined
            or "sonos" in combined
            or "speaker" in combined
            or "audio" in combined
            or "echo" in combined
            or "homepod" in combined
            or "nest audio" in combined
        ):
            return {
                "category": "media_player",
                "category_label": "Media Player",
                "icon": "speaker",
                "brand": brand if brand != "Unknown" else "Audio",
                "model": model or "Media Player",
            }

        # 9. Smart Home / Cleaners / Thermostat / Doorbell / IoT
        if "cleaner" in combined or "robovac" in combined or "vacuum" in combined or "roomba" in combined:
            return {
                "category": "smart_home",
                "category_label": "Smart Cleaner",
                "icon": "cleaner",
                "brand": brand if brand != "Unknown" else "Smart Cleaner",
                "model": model or "RoboVac Series",
            }
        if "doorbell" in combined or "ring" in combined:
            return {
                "category": "smart_home",
                "category_label": "Doorbell",
                "icon": "doorbell",
                "brand": brand if brand != "Unknown" else "Doorbell",
                "model": model or "Smart Doorbell",
            }
        if "thermostat" in combined or "nest" in combined or "tado" in combined or "ecobee" in combined:
            return {
                "category": "smart_home",
                "category_label": "Thermostat",
                "icon": "thermostat",
                "brand": brand if brand != "Unknown" else "Thermostat",
                "model": model or "Smart Thermostat",
            }
        if (
            "plug" in combined
            or "shelly" in combined
            or "sonoff" in combined
            or "tuya" in combined
            or "espressif" in combined
            or "hue" in combined
            or "smart" in combined
        ):
            return {
                "category": "smart_home",
                "category_label": "Smart Device",
                "icon": "plug",
                "brand": brand if brand != "Unknown" else "Smart Device",
                "model": model or "IoT / Smart Home",
            }

        # 10. Server / NAS
        if (
            dt_lower in ("server", "nas")
            or "unraid" in combined
            or "proxmox" in combined
            or "truenas" in combined
            or "synology" in combined
            or "qnap" in combined
            or "server" in combined
            or "storage" in combined
        ):
            return {
                "category": "server",
                "category_label": "Server",
                "icon": "server",
                "brand": brand if brand != "Unknown" else "Server",
                "model": model or "Server / NAS",
            }

        # 11. Gaming Console
        if (
            "playstation" in combined
            or "xbox" in combined
            or "nintendo" in combined
            or "switch" in h_lower
            or "ps4" in combined
            or "ps5" in combined
        ):
            return {
                "category": "gaming",
                "category_label": "Gaming Console",
                "icon": "gamepad",
                "brand": brand if brand != "Unknown" else "Gaming",
                "model": model or "Console",
            }

        # 12. Camera
        if (
            "camera" in combined
            or "cam" in combined
            or "hikvision" in combined
            or "dahua" in combined
            or "reolink" in combined
        ):
            return {
                "category": "camera",
                "category_label": "Camera",
                "icon": "camera",
                "brand": brand if brand != "Unknown" else "Camera",
                "model": model or "IP Camera",
            }

        # 13. Fingbox
        if "fingbox" in combined or "fing" in combined:
            return {
                "category": "iot",
                "category_label": "Fingbox",
                "icon": "cube",
                "brand": "Fing",
                "model": "Fingbox v1",
            }

        # 14. Desktop / Computer (Windows / Linux / Mac)
        if (
            "win1" in combined
            or "win" in combined
            or "linux" in combined
            or "ubuntu" in combined
            or "fedora" in combined
            or "desktop" in combined
            or "pc" in combined
            or dt_lower == "computer"
        ):
            return {
                "category": "computer",
                "category_label": "Computer",
                "icon": "desktop",
                "brand": brand if brand != "Unknown" else "Computer",
                "model": model or (hostname if len(hostname) < 20 else "Desktop PC"),
            }

        # Default Generic Client
        return {
            "category": "generic",
            "category_label": "Device",
            "icon": "cube",
            "brand": brand,
            "model": model or "Generic Device",
        }
