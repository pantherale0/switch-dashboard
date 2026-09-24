"""Tests for unified numeric switch port normalization, formatting, and keying."""

import pytest
from switch_dashboard.core.ports import (
    normalize_port,
    ports_equal,
    format_port_display,
    format_port_short,
    port_key,
)
from switch_dashboard.storage.database import Database
from switch_dashboard.storage.repositories.metric_repo import MetricRepository


class TestNormalizePort:
    """Test suite for normalize_port (numeric identifiers: 1 = Port 1, 2 = Port 2)."""

    def test_integers(self):
        assert normalize_port(1) == "1"
        assert normalize_port(0) == "0"
        assert normalize_port(24) == "24"
        assert normalize_port(52) == "52"

    def test_digit_strings(self):
        assert normalize_port("1") == "1"
        assert normalize_port("24") == "24"
        assert normalize_port("  8  ") == "8"
        assert normalize_port("08") == "8"
        assert normalize_port("007") == "7"

    def test_port_prefix_strings(self):
        assert normalize_port("Port 1") == "1"
        assert normalize_port("port 2") == "2"
        assert normalize_port("PORT 3") == "3"
        assert normalize_port("Port-4") == "4"
        assert normalize_port("port_5") == "5"
        assert normalize_port("Port:6") == "6"
        assert normalize_port("port.7") == "7"
        assert normalize_port("Port08") == "8"
        assert normalize_port("Port 24") == "24"

    def test_compact_p_prefix(self):
        assert normalize_port("P1") == "1"
        assert normalize_port("p2") == "2"
        assert normalize_port("P 3") == "3"
        assert normalize_port("p-4") == "4"
        assert normalize_port("P24") == "24"

    def test_lan_wan_prefixes(self):
        assert normalize_port("LAN 1") == "1"
        assert normalize_port("lan1") == "1"
        assert normalize_port("LAN-2") == "2"
        assert normalize_port("lan_3") == "3"
        assert normalize_port("WAN") == "WAN"
        assert normalize_port("wan") == "WAN"
        assert normalize_port("WAN 1") == "1"
        assert normalize_port("wan-2") == "2"

    def test_sfp_prefixes(self):
        assert normalize_port("SFP 1") == "1"
        assert normalize_port("sfp1") == "1"
        assert normalize_port("SFP+ 1") == "1"
        assert normalize_port("sfp+2") == "2"
        assert normalize_port("SFP+-3") == "3"
        assert normalize_port("9/SFP") == "9"

    def test_empty_and_none(self):
        assert normalize_port(None) == ""
        assert normalize_port("") == ""
        assert normalize_port("   ") == ""

    def test_interface_names(self):
        assert normalize_port("eth0") == "0"
        assert normalize_port("eth1") == "1"
        assert normalize_port("ge-0/0/1") == "ge-0/0/1"
        assert normalize_port("GigabitEthernet0/1") == "GigabitEthernet0/1"


class TestPortsEqual:
    """Test suite for ports_equal."""

    def test_numeric_equivalence(self):
        assert ports_equal(1, "1") is True
        assert ports_equal("1", "Port 1") is True
        assert ports_equal("port 1", "P1") is True
        assert ports_equal("Port-1", "1") is True
        assert ports_equal(24, "Port 24") is True

    def test_named_port_equivalence(self):
        assert ports_equal("LAN 1", "lan1") is True
        assert ports_equal("lan-1", "LAN 1") is True
        assert ports_equal("LAN 1", "1") is True
        assert ports_equal("LAN 1", 1) is True
        assert ports_equal("LAN 2", 2) is True
        assert ports_equal("WAN", "wan") is True
        assert ports_equal("SFP+ 1", "1") is True

    def test_different_ports(self):
        assert ports_equal(1, 2) is False
        assert ports_equal("Port 1", "Port 2") is False
        assert ports_equal("LAN 1", "LAN 2") is False
        assert ports_equal("1", "LAN 2") is False

    def test_empty_handling(self):
        assert ports_equal(None, "") is True
        assert ports_equal(None, None) is True
        assert ports_equal("", "   ") is True
        assert ports_equal(1, None) is False


class TestFormatPortDisplay:
    """Test suite for format_port_display."""

    def test_numeric_ports(self):
        assert format_port_display(1) == "Port 1"
        assert format_port_display("1") == "Port 1"
        assert format_port_display("Port 1") == "Port 1"
        assert format_port_display("p1") == "Port 1"

    def test_named_ports(self):
        assert format_port_display("LAN 1") == "Port 1"
        assert format_port_display("lan1") == "Port 1"
        assert format_port_display("LAN 2") == "Port 2"
        assert format_port_display("WAN") == "WAN"
        assert format_port_display("SFP+ 1") == "Port 1"

    def test_empty_and_custom(self):
        assert format_port_display(None) == ""
        assert format_port_display("") == ""
        assert format_port_display("ge-0/0/1") == "ge-0/0/1"


class TestFormatPortShort:
    """Test suite for format_port_short."""

    def test_numeric_ports(self):
        assert format_port_short(1) == "P1"
        assert format_port_short("1") == "P1"
        assert format_port_short("Port 1") == "P1"
        assert format_port_short("p1") == "P1"

    def test_named_ports(self):
        assert format_port_short("LAN 1") == "P1"
        assert format_port_short("lan1") == "P1"
        assert format_port_short("LAN 2") == "P2"
        assert format_port_short("WAN") == "WAN"
        assert format_port_short("SFP+ 1") == "P1"

    def test_empty(self):
        assert format_port_short(None) == ""
        assert format_port_short("") == ""


class TestPortKey:
    """Test suite for port_key."""

    def test_canonical_keys(self):
        assert port_key("192.168.1.1", 1) == "192.168.1.1:1"
        assert port_key("192.168.1.1", "1") == "192.168.1.1:1"
        assert port_key("192.168.1.1", "Port 1") == "192.168.1.1:1"
        assert port_key("192.168.1.1", "p1") == "192.168.1.1:1"
        assert port_key("192.168.1.1", "LAN 1") == "192.168.1.1:1"
        assert port_key("192.168.1.1", "lan1") == "192.168.1.1:1"
        assert port_key("192.168.1.1", "LAN 2") == "192.168.1.1:2"


class TestMetricRepoPortConsolidation:
    """Test metric repository normalization across diverse port inputs."""

    def test_counter_and_history_normalization(self):
        db = Database(":memory:")
        db.init_db()
        repo = MetricRepository(db)

        # Save counters using "Port 1" in the dictionary key
        counters = {
            "192.168.1.10:Port 1": {
                "tx": 100,
                "rx": 200,
                "cum_tx": 1000,
                "cum_rx": 2000,
                "ts": 12345.0,
            }
        }
        repo.save_counters(counters)

        # Load counters and verify canonical normalized key "192.168.1.10:1"
        loaded = repo.load_counters()
        assert "192.168.1.10:1" in loaded
        assert loaded["192.168.1.10:1"]["cum_tx"] == 1000

        # Append live sample using "p1"
        repo.append_live_sample("192.168.1.10", "p1", ts=100.0, cum_tx=1000, cum_rx=2000)

        # Query live history using "Port 1"
        history = repo.get_live_history("192.168.1.10", "Port 1")
        assert len(history) == 1
        assert history[0]["tx"] == 1000
        assert history[0]["rx"] == 2000

        # Query live history using "LAN 1"
        history_lan = repo.get_live_history("192.168.1.10", "LAN 1")
        assert len(history_lan) == 1
        assert history_lan[0]["tx"] == 1000

        # Append hourly sample using integer 1
        repo.append_hourly_sample("192.168.1.10", 1, ts=200.0, tx_bps=50000, rx_bps=80000)

        # Query hourly history using "Port 1"
        hourly = repo.get_hourly_history("192.168.1.10", "Port 1")
        assert len(hourly) == 1
        assert hourly[0]["tx"] == 50000
        assert hourly[0]["rx"] == 80000
