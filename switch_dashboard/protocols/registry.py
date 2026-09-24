import os
import importlib
import logging
from typing import Dict, Type, Any, Optional, List, Callable, Tuple
import voluptuous as vol

from switch_dashboard.protocols.base import BaseProtocol

logger = logging.getLogger("switch_dashboard.protocols.registry")


class ProtocolRegistry:
    _registry: Dict[str, Type[BaseProtocol]] = {}
    _model_mapping: Dict[str, str] = {}
    _metadata: Dict[str, Dict[str, Any]] = {}
    _discovered: bool = False

    @classmethod
    def register(
        cls,
        name: str,
        protocol_cls: Type[BaseProtocol],
        display_name: Optional[str] = None,
        supported_models: Optional[List[str]] = None,
    ):
        cls._registry[name] = protocol_cls
        display = display_name or getattr(protocol_cls, "display_name", name)
        capabilities = [c.value for c in getattr(protocol_cls, "capabilities", set())]
        cls._metadata[name] = {
            "name": name,
            "display_name": display,
            "capabilities": capabilities,
            "models": supported_models or [],
            "config_schema": protocol_cls.get_ui_schema() if hasattr(protocol_cls, "get_ui_schema") else [],
        }
        if supported_models:
            for model in supported_models:
                cls._model_mapping[model.lower()] = name
        logger.debug(f"Registered protocol '{name}' ({display})")

    @classmethod
    def validate_config(cls, name_or_model: str, config: Dict[str, Any]) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Validates configuration against the driver's Voluptuous schema if defined.
        
        Allows extra general device fields while strictly validating driver parameters.
        Returns (is_valid, error_message, coerced_config).
        """
        proto_cls = cls.get_protocol_class(name_or_model)
        if not proto_cls:
            return True, None, config
        schema = proto_cls.get_config_schema() if hasattr(proto_cls, "get_config_schema") else None
        if not schema:
            return True, None, config
        try:
            extended_schema = schema.extend({}, extra=vol.ALLOW_EXTRA)
            valid_cfg = extended_schema(config)
            return True, None, valid_cfg
        except Exception as e:
            return False, str(e), config

    @classmethod
    def get_protocol_class(cls, name_or_model: str) -> Optional[Type[BaseProtocol]]:
        cls.discover_drivers()
        # Direct protocol name match
        if name_or_model in cls._registry:
            return cls._registry[name_or_model]
        # Lowercase match
        lower_name = name_or_model.lower()
        if lower_name in cls._registry:
            return cls._registry[lower_name]
        # Model mapping match
        if lower_name in cls._model_mapping:
            proto_name = cls._model_mapping[lower_name]
            return cls._registry.get(proto_name)
        return None

    @classmethod
    def create(cls, switch_config: Dict[str, Any]) -> BaseProtocol:
        cls.discover_drivers()
        # 1. Check explicit protocol attribute
        proto_name = switch_config.get("protocol")
        if proto_name and proto_name in cls._registry:
            return cls._registry[proto_name](switch_config)

        # 2. Check model mapping
        model = str(switch_config.get("model", "")).lower()
        if model in ["openvswitch", "ovs"]:
            target = "ovs"
        elif model == "fritzbox":
            target = "fritzbox"
        elif model in ["snmp", "generic_snmp"]:
            target = "snmp"
        elif model.startswith(("unifi", "usw", "uap", "usg", "udm", "ucg", "uxg")):
            target = "unifi"
        elif model in ("proxmox", "proxmox_ve", "pve"):
            target = "proxmox"
        else:
            target = cls._model_mapping.get(model, "http_hc")

        proto_cls = cls._registry.get(target) or cls._registry.get("http_hc")
        if proto_cls is None:
            raise ValueError(f"No protocol driver available for switch {switch_config.get('ip')} (model={model})")
        return proto_cls(switch_config)

    @classmethod
    def list_protocols(cls) -> List[Dict[str, Any]]:
        cls.discover_drivers()
        return list(cls._metadata.values())

    @classmethod
    def discover_drivers(cls):
        """Auto-discovers and imports all built-in and plugin drivers in the drivers/ package."""
        if cls._discovered:
            return
        cls._discovered = True

        drivers_dir = os.path.join(os.path.dirname(__file__), "drivers")
        if not os.path.exists(drivers_dir):
            return

        for entry in os.listdir(drivers_dir):
            entry_path = os.path.join(drivers_dir, entry)
            if os.path.isdir(entry_path) and not entry.startswith("_"):
                # Try importing switch_dashboard.protocols.drivers.<entry>.driver
                module_name = f"switch_dashboard.protocols.drivers.{entry}.driver"
                try:
                    importlib.import_module(module_name)
                except Exception as e:
                    logger.debug(f"Driver discovery note for {module_name}: {e}")


def register_protocol(
    name: str,
    display_name: Optional[str] = None,
    supported_models: Optional[List[str]] = None,
) -> Callable[[Type[BaseProtocol]], Type[BaseProtocol]]:
    """Decorator to register a protocol driver class dynamically."""
    def decorator(cls: Type[BaseProtocol]) -> Type[BaseProtocol]:
        ProtocolRegistry.register(
            name=name,
            protocol_cls=cls,
            display_name=display_name,
            supported_models=supported_models,
        )
        return cls
    return decorator
