from types import SimpleNamespace
from unittest.mock import patch

from gamestream.network import add_lan_candidates, candidate_summary, directly_connected_peer

SDP = 'v=0\r\na=candidate:abc 1 UDP 2122260223 hidden.local 51000 typ host\r\na=end-of-candidates\r\n'


def test_mdns_lan_fallback_preserves_original_and_end_marker():
    with patch('gamestream.network.directly_connected_peer', return_value='10.0.0.20'):
        result, count = add_lan_candidates(SDP, '10.0.0.20')
    assert count == 1
    assert 'hidden.local 51000 typ host' in result
    assert '10.0.0.20 51000 typ host' in result
    assert result.endswith('a=end-of-candidates\r\n')
    assert candidate_summary(result) == {'udp/host/mdns': 1, 'udp/host': 1}


def test_non_lan_peers_do_not_get_guessed_candidates():
    with patch('gamestream.network.directly_connected_peer', return_value=None):
        assert add_lan_candidates(SDP, '8.8.8.8') == (SDP, 0)


def test_only_socket_peer_on_connected_subnet_is_used():
    adapter = SimpleNamespace(ips=[SimpleNamespace(ip='10.0.0.7', network_prefix=24)])
    with patch('ifaddr.get_adapters', return_value=[adapter]):
        assert directly_connected_peer('10.0.0.20') == '10.0.0.20'
        assert directly_connected_peer('10.1.0.20') is None
        assert directly_connected_peer('10.0.0.7') is None
        assert directly_connected_peer('127.0.0.1') is None
        assert directly_connected_peer('8.8.8.8') is None


def test_literal_candidates_are_not_rewritten():
    literal = SDP.replace('hidden.local', '10.0.0.25')
    with patch('gamestream.network.directly_connected_peer', return_value='10.0.0.20'):
        assert add_lan_candidates(literal, '10.0.0.20') == (literal, 0)
