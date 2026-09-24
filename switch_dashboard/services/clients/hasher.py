import hashlib
import json
from typing import Dict


def compute_footprint_hash(footprint: Dict[str, str]) -> str:
    """Computes a deterministic SHA-256 hash of a client's {node_id: port} sighting dictionary.

    The dictionary is sorted by node identifier to ensure order-independence regardless
    of dictionary key insertion order.
    """
    if not footprint:
        return ""
    serialized = json.dumps(sorted(footprint.items()), separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
