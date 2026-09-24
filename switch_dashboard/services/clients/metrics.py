import logging
from typing import Any, Dict, List, Optional, Tuple
from switch_dashboard.services.clients.models import ClientSighting, LiveSpeed

logger = logging.getLogger("switch_dashboard.services.clients.metrics")


class ClientMetricsCalculator:
    """Calculates real-time bandwidth bitrates, delta counters, and counter-wrap protections."""

    def __init__(self):
        # _prev_counters[mac] = {'tx': int, 'rx': int, 'ts': float}
        self._prev_counters: Dict[str, Dict[str, Any]] = {}
        # _live_speeds[mac] = LiveSpeed
        self._live_speeds: Dict[str, LiveSpeed] = {}

    def get_live_speed(self, mac: str) -> LiveSpeed:
        return self._live_speeds.get(mac, LiveSpeed())

    def get_all_live_speeds(self) -> Dict[str, LiveSpeed]:
        return self._live_speeds

    def compute_bandwidth(
        self,
        mac: str,
        sightings: List[ClientSighting],
        is_child: bool,
        ts: float,
    ) -> Tuple[int, int, Optional[Dict[str, Any]]]:
        """Calculates current tx/rx bitrates and creates a metric trend sample if delta counters are present.

        Returns:
            (speed_tx_bps, speed_rx_bps, Optional[metric_sample_dict])
        """
        # 1. Search for direct byte counters (e.g. from AP station table or host)
        cur_tx = None
        cur_rx = None
        for s in sightings:
            if s.tx_bytes is not None and s.rx_bytes is not None:
                try:
                    cur_tx = int(s.tx_bytes)
                    cur_rx = int(s.rx_bytes)
                    break
                except (ValueError, TypeError):
                    pass

        speed_tx_bps = 0
        speed_rx_bps = 0
        sample: Optional[Dict[str, Any]] = None

        if cur_tx is not None and cur_rx is not None:
            prev = self._prev_counters.get(mac)
            if prev:
                dt = ts - prev["ts"]
                if dt > 0:
                    d_tx = max(0, cur_tx - prev["tx"])
                    d_rx = max(0, cur_rx - prev["rx"])
                    # 5 GB/s counter wrap guard
                    if d_tx < 5_000_000_000 and d_rx < 5_000_000_000:
                        speed_tx_bps = int(d_tx * 8 / dt)
                        speed_rx_bps = int(d_rx * 8 / dt)
                        sample = {
                            "mac": mac,
                            "delta_tx": d_tx,
                            "delta_rx": d_rx,
                            "speed_tx_bps": speed_tx_bps,
                            "speed_rx_bps": speed_rx_bps,
                            "timestamp": ts,
                        }
                    else:
                        logger.debug(
                            f"Ignored abnormal counter delta for {mac}: d_tx={d_tx}, d_rx={d_rx} (wrap or reset)"
                        )
            self._prev_counters[mac] = {"tx": cur_tx, "rx": cur_rx, "ts": ts}
            self._live_speeds[mac] = LiveSpeed(
                speed_tx_bps=speed_tx_bps,
                speed_rx_bps=speed_rx_bps,
                cum_tx=cur_tx,
                cum_rx=cur_rx,
                ts=ts,
            )
            return (speed_tx_bps, speed_rx_bps, sample)

        if is_child:
            # Hypervisor child guest reported rates
            child_sighting = next((s for s in sightings if s.is_child), None)
            if child_sighting:
                speed_tx_bps = int(child_sighting.speed_tx_bps or 0)
                speed_rx_bps = int(child_sighting.speed_rx_bps or 0)
            self._live_speeds[mac] = LiveSpeed(
                speed_tx_bps=speed_tx_bps,
                speed_rx_bps=speed_rx_bps,
                cum_tx=0,
                cum_rx=0,
                ts=ts,
            )
            return (speed_tx_bps, speed_rx_bps, None)

        # Fallback to reported rates from AP (e.g. UniFi rx_rate/tx_rate in kbps)
        for s in sightings:
            if s.rx_rate is not None or s.tx_rate is not None:
                try:
                    speed_tx_bps = int(float(s.tx_rate or 0) * 1000)
                    speed_rx_bps = int(float(s.rx_rate or 0) * 1000)
                    self._live_speeds[mac] = LiveSpeed(
                        speed_tx_bps=speed_tx_bps,
                        speed_rx_bps=speed_rx_bps,
                        cum_tx=0,
                        cum_rx=0,
                        ts=ts,
                    )
                    break
                except (ValueError, TypeError):
                    pass

        return (speed_tx_bps, speed_rx_bps, None)
