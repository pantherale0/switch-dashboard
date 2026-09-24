"""Event hooks and notification callbacks for dashboard events."""

import logging
from typing import Callable, Dict, List, Any

logger = logging.getLogger("switch_dashboard.events")

_listeners: Dict[str, List[Callable[..., Any]]] = {}


def subscribe(event_name: str, callback: Callable[..., Any]):
    if event_name not in _listeners:
        _listeners[event_name] = []
    _listeners[event_name].append(callback)


def emit(event_name: str, **kwargs):
    for callback in _listeners.get(event_name, []):
        try:
            callback(**kwargs)
        except Exception as e:
            logger.error(f"Error executing event handler for {event_name}: {e}")
