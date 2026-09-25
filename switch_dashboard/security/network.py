import ipaddress
import os
import socket


def _allowed_networks():
    configured = os.environ.get(
        "MANAGEMENT_NETWORKS",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )
    try:
        return [ipaddress.ip_network(value.strip(), strict=False) for value in configured.split(",") if value.strip()]
    except ValueError as exc:
        raise ValueError("MANAGEMENT_NETWORKS contains an invalid CIDR") from exc


def validate_management_target(host: str) -> str:
    value = host.strip().strip("[]")
    if not value:
        raise ValueError("Target address is required")
    try:
        addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(value, None)}
    except (socket.gaierror, ValueError) as exc:
        raise ValueError("Target address could not be resolved") from exc
    allowed = _allowed_networks()
    for address in addresses:
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise ValueError("Target address is not permitted")
        if not any(address in network for network in allowed):
            raise ValueError("Target address is outside configured management networks")
    return value
