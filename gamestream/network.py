"""LAN ICE fallback and compact diagnostics, without logging SDP credentials."""
from __future__ import annotations

import ipaddress
from collections import Counter


def directly_connected_peer(peer: str | None) -> str | None:
    """Trust the socket peer, never Forwarded headers, and only on a local subnet."""
    if not peer:
        return None
    try:
        address = ipaddress.ip_address(peer)
        if address.is_loopback or address.is_unspecified or not address.is_private:
            return None
        import ifaddr
        local = [ip for adapter in ifaddr.get_adapters() for ip in adapter.ips]
        for ip in local:
            value = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
            if address == ipaddress.ip_address(value):
                return None  # Local reverse proxy / local browser, not the remote device.
        for ip in local:
            value = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
            network = ipaddress.ip_network(f'{value}/{ip.network_prefix}', strict=False)
            if address in network:
                return str(address)
    except (ValueError, OSError):
        return None
    return None


def add_lan_candidates(sdp: str, peer: str | None) -> tuple[str, int]:
    """Keep advertised candidates and add a direct LAN fallback for mDNS hosts.

Browsers may hide their LAN address behind a .local name. If multicast DNS is
blocked, aioice otherwise discards that candidate. On the same subnet, HTTPS
already identifies the device; try that address at the browser's offered UDP
port as well. STUN authentication still verifies every resulting candidate pair.
"""
    address = directly_connected_peer(peer)
    if not address:
        return sdp, 0
    result = []
    added = 0
    for line in sdp.splitlines():
        result.append(line)
        parts = line.split()
        if (line.startswith('a=candidate:') and len(parts) >= 8
                and parts[2].lower() == 'udp' and parts[4].lower().endswith('.local')
                and parts[6:8] == ['typ', 'host']):
            parts[0] += 'lan'
            parts[4] = address
            try:
                parts[3] = str(max(1, int(parts[3]) - 1))
            except ValueError:
                continue
            result.append(' '.join(parts))
            added += 1
    return '\r\n'.join(result) + '\r\n', added


def candidate_summary(sdp: str) -> dict:
    counts = Counter()
    for line in sdp.splitlines():
        if not line.startswith('a=candidate:'):
            continue
        parts = line.split()
        if len(parts) >= 8:
            counts[f'{parts[2].lower()}/{parts[7]}' + ('/mdns' if parts[4].endswith('.local') else '')] += 1
    return dict(counts)
