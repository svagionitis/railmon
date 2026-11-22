"""Train Proximity & Delay Push Alerting Engine (railmon watch).

Provides a real-time watchdog daemon monitoring Greek passenger trains for:
- Departure confirmation
- Dynamic delay increases / recoveries
- Destination station proximity (geodesic distance and ETA)
- Station arrival detection
- Telemetry drop / freeze watchdog

Dispatches alerts across zero-dependency channels:
- Terminal audible bell ('\\a' and Windows MessageBeep)
- Cross-platform desktop notifications (Linux notify-send, macOS osascript, Windows PowerShell toast)
- Outbound JSON webhooks (Telegram Bot API, Discord, Slack, and generic REST HTTP POST)
- In-TUI status banner toasts and visual alerts
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
import urllib.parse
import urllib.request

from tui.geo import GeoPoint, haversine_distance_km
from tui.mock_engine import (
    StationNode,
    load_stations,
    normalize_station_key,
    resolve_station_for_cli,
)

logger = logging.getLogger("railmon.watchdog")


class WatchTriggerType(str, Enum):
    """Event triggers evaluated by the train watchdog engine."""

    DEPARTURE = "departure"
    DELAY_INCREASE = "delay_increase"
    DELAY_REDUCED = "delay_reduced"
    PROXIMITY_DISTANCE = "proximity_distance"
    PROXIMITY_ETA = "proximity_eta"
    ARRIVAL = "arrival"
    TELEMETRY_LOST = "telemetry_lost"
    SENSOR_FROZEN = "sensor_frozen"


class NotificationChannel(str, Enum):
    """Delivery mechanisms for push alert dispatches."""

    BELL = "bell"
    DESKTOP = "desktop"
    WEBHOOK = "webhook"
    TUI_TOAST = "tui_toast"


class WebhookFormat(str, Enum):
    """Payload serialization dialect for outbound HTTP alerts."""

    AUTO = "auto"
    GENERIC = "generic"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"


@dataclass(frozen=True)
class WatchConfig:
    """Immutable operational configuration for a train watch session.

    Attributes:
        train_number: Commercial train identifier (e.g. 'IC 52', '1597').
        destination_station: User-specified destination station query.
        destination_point: Resolved geographic coordinate of destination station.
        destination_name: Resolved canonical destination station display name.
        notify_eta_minutes: ETA threshold in minutes to trigger proximity alert.
        notify_distance_km: Distance threshold in km to trigger proximity alert.
        delay_delta_threshold_min: Delay change threshold in minutes triggering alert.
        poll_interval_sec: Seconds between telemetry evaluation loops.
        telemetry_timeout_sec: Seconds of missing telemetry before alerting drop.
        enable_bell: Enable terminal audio bell alerts.
        enable_desktop: Enable OS native desktop notifications.
        webhook_url: Target URL for push webhooks.
        webhook_format: Serialization dialect for webhook payload.
        webhook_token: Optional authentication token or secret.
        webhook_chat_id: Optional Telegram chat identifier.
        exit_on_arrival: If True, watchdog terminates when destination is reached.
        cooldown_delay_alert_sec: Seconds between successive delay alerts.
    """

    train_number: str
    destination_station: Optional[str] = None
    destination_point: Optional[GeoPoint] = None
    destination_name: Optional[str] = None
    notify_eta_minutes: float = 15.0
    notify_distance_km: float = 15.0
    delay_delta_threshold_min: float = 3.0
    poll_interval_sec: float = 2.0
    telemetry_timeout_sec: float = 180.0
    enable_bell: bool = True
    enable_desktop: bool = True
    webhook_url: Optional[str] = None
    webhook_format: WebhookFormat = WebhookFormat.AUTO
    webhook_token: Optional[str] = None
    webhook_chat_id: Optional[str] = None
    exit_on_arrival: bool = True
    cooldown_delay_alert_sec: float = 300.0


@dataclass(frozen=True)
class WatchAlertEvent:
    """Dispatched alert event payload emitted by the watchdog."""

    timestamp: str
    train_number: str
    trigger_type: WatchTriggerType
    headline: str
    message: str
    current_speed_kmh: float
    current_delay_min: float
    delay_delta_min: float
    distance_to_dest_km: Optional[float] = None
    eta_dest_minutes: Optional[float] = None
    destination_name: Optional[str] = None
    current_lat: Optional[float] = None
    current_lng: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to a JSON-compatible dictionary."""
        return {
            "timestamp": self.timestamp,
            "train_number": self.train_number,
            "trigger_type": self.trigger_type.value,
            "headline": self.headline,
            "message": self.message,
            "current_speed_kmh": round(self.current_speed_kmh, 1),
            "current_delay_min": round(self.current_delay_min, 1),
            "delay_delta_min": round(self.delay_delta_min, 1),
            "distance_to_dest_km": (
                round(self.distance_to_dest_km, 2)
                if self.distance_to_dest_km is not None
                else None
            ),
            "eta_dest_minutes": (
                round(self.eta_dest_minutes, 1)
                if self.eta_dest_minutes is not None
                else None
            ),
            "destination_name": self.destination_name,
            "current_lat": (
                round(self.current_lat, 5) if self.current_lat is not None else None
            ),
            "current_lng": (
                round(self.current_lng, 5) if self.current_lng is not None else None
            ),
        }


@dataclass
class WatchState:
    """Mutable tracking state for a monitored train."""

    train_number: str
    is_active: bool = False
    has_departed: bool = False
    initial_delay: Optional[float] = None
    last_known_delay: float = 0.0
    last_delay_alert_time: float = 0.0
    last_delay_alert_val: float = 0.0
    distance_alert_fired: bool = False
    eta_alert_fired: bool = False
    arrival_alert_fired: bool = False
    telemetry_lost_fired: bool = False
    sensor_frozen_fired: bool = False
    last_moved_point: Optional[GeoPoint] = None
    last_moved_time: float = 0.0
    last_telemetry_time: float = 0.0
    last_known_speed: float = 0.0
    smoothed_speed: float = 0.0
    last_point: Optional[GeoPoint] = None
    initial_point: Optional[GeoPoint] = None
    consecutive_missing_frames: int = 0
    total_alerts_dispatched: int = 0
    route_name: Optional[str] = None
    next_station: Optional[str] = None


def normalize_train_query(query: str) -> str:
    """Normalize train identifier for robust matching (e.g. 'IC 52' -> 'IC52')."""
    return re.sub(r"[\s\-_]", "", str(query)).upper()


def matches_train_number(
    candidate_train_no: Optional[str],
    candidate_id: Optional[str],
    target_query: str,
) -> bool:
    """Check if candidate train matches target query with Greek rail conventions."""
    target_norm = normalize_train_query(target_query)
    if not target_norm:
        return False

    c_num_norm = normalize_train_query(candidate_train_no or "")
    if c_num_norm and c_num_norm == target_norm:
        return True

    c_id_norm = normalize_train_query(candidate_id or "")
    if c_id_norm and c_id_norm == target_norm:
        return True

    # Check for prefix or suffix containment when length >= 3
    if len(target_norm) >= 3:
        if c_num_norm and (c_num_norm in target_norm or target_norm in c_num_norm):
            return True
        if c_id_norm and (c_id_norm in target_norm or target_norm in c_id_norm):
            return True

    # Numeric match (e.g. '52' matches 'IC52' or '1597')
    target_digits = re.sub(r"\D", "", target_norm)
    c_digits = re.sub(r"\D", "", c_num_norm)
    if target_digits and c_digits and target_digits == c_digits:
        return True

    return False


def parse_notify_minutes(val: Union[str, float, int]) -> float:
    """Parse ETA notify threshold string such as '15m', '15 min', or float/int."""
    if isinstance(val, (int, float)):
        return max(0.5, float(val))
    s = str(val).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:m|min|mins)?$", s)
    if m:
        return max(0.5, float(m.group(1)))
    try:
        return max(0.5, float(s))
    except ValueError:
        return 15.0


def resolve_destination_station(
    query: Optional[str],
    stations: Optional[Dict[str, StationNode]] = None,
) -> Tuple[Optional[StationNode], Optional[str]]:
    """Resolve destination station query to canonical StationNode and clean name."""
    if not query or not str(query).strip():
        return None, None
    clean = str(query).strip()
    stn_map = stations if stations is not None else load_stations()
    resolved, _ = resolve_station_for_cli(clean, stn_map)
    if resolved:
        name = resolved.name_el or resolved.name_en or resolved.id
        return resolved, name
    return None, clean


class WatchdogEvaluator:
    """Stateful evaluator processing telemetry and firing watchdog alert triggers."""

    def __init__(
        self,
        config: WatchConfig,
        stations: Optional[Dict[str, StationNode]] = None,
    ) -> None:
        self.stations = stations if stations is not None else load_stations()

        # Resolve destination station if not explicitly supplied with coordinate
        dest_pt = config.destination_point
        dest_nm = config.destination_name
        if config.destination_station and dest_pt is None:
            resolved_stn, canonical_name = resolve_destination_station(
                config.destination_station, self.stations
            )
            if resolved_stn:
                dest_pt = resolved_stn.point
                dest_nm = canonical_name or resolved_stn.name_en

        self.config = WatchConfig(
            train_number=config.train_number,
            destination_station=config.destination_station,
            destination_point=dest_pt,
            destination_name=dest_nm,
            notify_eta_minutes=config.notify_eta_minutes,
            notify_distance_km=config.notify_distance_km,
            delay_delta_threshold_min=config.delay_delta_threshold_min,
            poll_interval_sec=config.poll_interval_sec,
            telemetry_timeout_sec=config.telemetry_timeout_sec,
            enable_bell=config.enable_bell,
            enable_desktop=config.enable_desktop,
            webhook_url=config.webhook_url,
            webhook_format=config.webhook_format,
            webhook_token=config.webhook_token,
            webhook_chat_id=config.webhook_chat_id,
            exit_on_arrival=config.exit_on_arrival,
            cooldown_delay_alert_sec=config.cooldown_delay_alert_sec,
        )

        self.state = WatchState(train_number=self.config.train_number)
        self._lock = threading.Lock()

    def evaluate_position(
        self,
        pos: Any,
        now: Optional[float] = None,
    ) -> List[WatchAlertEvent]:
        """Evaluate a candidate TrainPosition and generate alert events if triggers trip.

        Args:
            pos: TrainPosition instance or compatible object.
            now: Current monotonic/wall time timestamp for deterministic testing.

        Returns:
            List of WatchAlertEvent objects triggered by this update.
        """
        now_ts = time.time() if now is None else now
        now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts))

        # Check train number matching
        t_num = getattr(pos, "train_number", None) or getattr(pos, "id", "")
        t_id = getattr(pos, "id", "")
        if not matches_train_number(t_num, t_id, self.config.train_number):
            return []

        events: List[WatchAlertEvent] = []

        with self._lock:
            st = self.state
            st.is_active = True
            st.consecutive_missing_frames = 0
            st.telemetry_lost_fired = False

            spd = float(getattr(pos, "speed", 0.0) or 0.0)
            dly = float(getattr(pos, "delay", 0.0) or 0.0)
            lat = getattr(pos, "lat", None)
            lng = getattr(pos, "lng", None)
            cur_pt = GeoPoint(lat, lng) if lat is not None and lng is not None else None

            # Track route metadata
            orig = getattr(pos, "origin", None)
            dest = getattr(pos, "destination", None)
            if orig and dest:
                st.route_name = f"{orig} -> {dest}"
            st.next_station = getattr(pos, "next_station", None)

            # Initialize initial delay & position on first seen
            if st.initial_delay is None:
                st.initial_delay = dly
                st.last_known_delay = dly
                st.last_delay_alert_val = dly
            if st.initial_point is None and cur_pt is not None:
                st.initial_point = cur_pt

            # Speed smoothing (exponential moving average)
            if spd > 0.0:
                if st.smoothed_speed > 0.0:
                    st.smoothed_speed = 0.7 * st.smoothed_speed + 0.3 * spd
                else:
                    st.smoothed_speed = spd
            st.last_known_speed = spd

            # ------------------------------------------------------------------
            # Trigger 1: Departure Confirmation
            # ------------------------------------------------------------------
            if not st.has_departed:
                has_moved = False
                disp_km = 0.0
                if st.initial_point is not None and cur_pt is not None:
                    disp_km = haversine_distance_km(st.initial_point, cur_pt)
                    if disp_km >= 0.25:
                        has_moved = True

                if spd >= 5.0 or has_moved:
                    st.has_departed = True
                    dep_headline = f"DEPARTURE: Train {self.config.train_number} Departed"
                    dep_msg = (
                        f"Train {self.config.train_number} has confirmed departure "
                        f"(Speed: {spd:.1f} km/h, Delay: {int(dly):+d}m)."
                    )
                    events.append(
                        WatchAlertEvent(
                            timestamp=now_iso,
                            train_number=self.config.train_number,
                            trigger_type=WatchTriggerType.DEPARTURE,
                            headline=dep_headline,
                            message=dep_msg,
                            current_speed_kmh=spd,
                            current_delay_min=dly,
                            delay_delta_min=0.0,
                            distance_to_dest_km=None,
                            eta_dest_minutes=None,
                            destination_name=self.config.destination_name,
                            current_lat=lat,
                            current_lng=lng,
                        )
                    )

            # ------------------------------------------------------------------
            # Trigger 2: Dynamic Delay Changes (Spike or Recovery)
            # ------------------------------------------------------------------
            delay_delta = dly - st.last_delay_alert_val
            cooldown_elapsed = (now_ts - st.last_delay_alert_time) >= self.config.cooldown_delay_alert_sec

            if delay_delta >= self.config.delay_delta_threshold_min and cooldown_elapsed:
                st.last_delay_alert_val = dly
                st.last_delay_alert_time = now_ts
                headline = f"DELAY INCREASE: Train {self.config.train_number} (+{int(delay_delta)}m)"
                msg = (
                    f"Train {self.config.train_number} delay increased by {int(delay_delta)}m "
                    f"to +{int(dly)}m (Speed: {spd:.1f} km/h)."
                )
                events.append(
                    WatchAlertEvent(
                        timestamp=now_iso,
                        train_number=self.config.train_number,
                        trigger_type=WatchTriggerType.DELAY_INCREASE,
                        headline=headline,
                        message=msg,
                        current_speed_kmh=spd,
                        current_delay_min=dly,
                        delay_delta_min=delay_delta,
                        distance_to_dest_km=None,
                        eta_dest_minutes=None,
                        destination_name=self.config.destination_name,
                        current_lat=lat,
                        current_lng=lng,
                    )
                )

            elif delay_delta <= -self.config.delay_delta_threshold_min and cooldown_elapsed:
                st.last_delay_alert_val = dly
                st.last_delay_alert_time = now_ts
                headline = f"DELAY REDUCED: Train {self.config.train_number} ({int(delay_delta)}m)"
                msg = (
                    f"Train {self.config.train_number} recovered {abs(int(delay_delta))}m of delay "
                    f"(Current delay: {int(dly):+d}m, Speed: {spd:.1f} km/h)."
                )
                events.append(
                    WatchAlertEvent(
                        timestamp=now_iso,
                        train_number=self.config.train_number,
                        trigger_type=WatchTriggerType.DELAY_REDUCED,
                        headline=headline,
                        message=msg,
                        current_speed_kmh=spd,
                        current_delay_min=dly,
                        delay_delta_min=delay_delta,
                        distance_to_dest_km=None,
                        eta_dest_minutes=None,
                        destination_name=self.config.destination_name,
                        current_lat=lat,
                        current_lng=lng,
                    )
                )

            st.last_known_delay = dly

            # ------------------------------------------------------------------
            # Trigger 3 & 4: Destination Proximity (Distance, ETA & Arrival)
            # ------------------------------------------------------------------
            dist_dest_km: Optional[float] = None
            eta_dest_min: Optional[float] = None

            if self.config.destination_point is not None and cur_pt is not None:
                dist_dest_km = haversine_distance_km(cur_pt, self.config.destination_point)

                # Effective speed estimation clamped between 25 and 160 km/h
                v_eff = max(25.0, min(160.0, st.smoothed_speed if st.smoothed_speed >= 10.0 else 45.0))
                eta_dest_min = (dist_dest_km / v_eff) * 60.0

                dest_label = self.config.destination_name or "Destination"

                # Arrival check: within 800m and speed < 25 km/h
                if dist_dest_km <= 0.8 and spd <= 25.0 and not st.arrival_alert_fired:
                    st.arrival_alert_fired = True
                    arr_headline = f"ARRIVAL: Train {self.config.train_number} Arrived at {dest_label}"
                    arr_msg = (
                        f"Train {self.config.train_number} has arrived at {dest_label} "
                        f"(Distance: {dist_dest_km:.2f} km, Speed: {spd:.1f} km/h, Delay: {int(dly):+d}m)."
                    )
                    events.append(
                        WatchAlertEvent(
                            timestamp=now_iso,
                            train_number=self.config.train_number,
                            trigger_type=WatchTriggerType.ARRIVAL,
                            headline=arr_headline,
                            message=arr_msg,
                            current_speed_kmh=spd,
                            current_delay_min=dly,
                            delay_delta_min=0.0,
                            distance_to_dest_km=dist_dest_km,
                            eta_dest_minutes=0.0,
                            destination_name=dest_label,
                            current_lat=lat,
                            current_lng=lng,
                        )
                    )

                # Distance threshold check
                if (
                    dist_dest_km <= self.config.notify_distance_km
                    and not st.distance_alert_fired
                    and not st.arrival_alert_fired
                ):
                    st.distance_alert_fired = True
                    dist_headline = f"PROXIMITY: Train {self.config.train_number} Within {int(self.config.notify_distance_km)} km"
                    dist_msg = (
                        f"Train {self.config.train_number} is now {dist_dest_km:.1f} km away from {dest_label} "
                        f"(ETA ~{int(round(eta_dest_min))}m, Speed: {spd:.1f} km/h, Delay: {int(dly):+d}m)."
                    )
                    events.append(
                        WatchAlertEvent(
                            timestamp=now_iso,
                            train_number=self.config.train_number,
                            trigger_type=WatchTriggerType.PROXIMITY_DISTANCE,
                            headline=dist_headline,
                            message=dist_msg,
                            current_speed_kmh=spd,
                            current_delay_min=dly,
                            delay_delta_min=0.0,
                            distance_to_dest_km=dist_dest_km,
                            eta_dest_minutes=eta_dest_min,
                            destination_name=dest_label,
                            current_lat=lat,
                            current_lng=lng,
                        )
                    )

                # ETA threshold check
                if (
                    eta_dest_min <= self.config.notify_eta_minutes
                    and not st.eta_alert_fired
                    and not st.arrival_alert_fired
                ):
                    st.eta_alert_fired = True
                    eta_headline = f"ETA ALERT: Train {self.config.train_number} Arriving in ~{int(round(eta_dest_min))} min"
                    eta_msg = (
                        f"Train {self.config.train_number} is approximately {int(round(eta_dest_min))} minutes "
                        f"away from {dest_label} ({dist_dest_km:.1f} km, Delay: {int(dly):+d}m)."
                    )
                    events.append(
                        WatchAlertEvent(
                            timestamp=now_iso,
                            train_number=self.config.train_number,
                            trigger_type=WatchTriggerType.PROXIMITY_ETA,
                            headline=eta_headline,
                            message=eta_msg,
                            current_speed_kmh=spd,
                            current_delay_min=dly,
                            delay_delta_min=0.0,
                            distance_to_dest_km=dist_dest_km,
                            eta_dest_minutes=eta_dest_min,
                            destination_name=dest_label,
                            current_lat=lat,
                            current_lng=lng,
                        )
                    )

            # ------------------------------------------------------------------
            # Trigger 5: Sensor Freeze Detection (F-5.2)
            # ------------------------------------------------------------------
            if spd >= 1.0 and cur_pt is not None:
                if st.last_moved_point is None:
                    st.last_moved_point = cur_pt
                    st.last_moved_time = now_ts
                else:
                    disp_m = haversine_distance_km(cur_pt, st.last_moved_point) * 1000.0
                    if disp_m > 10.0:
                        st.last_moved_point = cur_pt
                        st.last_moved_time = now_ts
                        st.sensor_frozen_fired = False
                    elif not st.sensor_frozen_fired and (now_ts - st.last_moved_time) >= 45.0:
                        st.sensor_frozen_fired = True
                        freeze_headline = f"SENSOR FREEZE: Train {self.config.train_number} Coordinates Static"
                        freeze_msg = (
                            f"Train {self.config.train_number} speed is {spd:.0f} km/h but coordinates "
                            f"have remained unchanged for {int(now_ts - st.last_moved_time)}s."
                        )
                        events.append(
                            WatchAlertEvent(
                                timestamp=now_iso,
                                train_number=self.config.train_number,
                                trigger_type=WatchTriggerType.SENSOR_FROZEN,
                                headline=freeze_headline,
                                message=freeze_msg,
                                current_speed_kmh=spd,
                                current_delay_min=dly,
                                delay_delta_min=0.0,
                                distance_to_dest_km=None,
                                eta_dest_minutes=None,
                                destination_name=self.config.destination_name,
                                current_lat=lat,
                                current_lng=lng,
                            )
                        )
            else:
                st.last_moved_point = cur_pt
                st.last_moved_time = now_ts
                st.sensor_frozen_fired = False

            st.last_point = cur_pt
            st.last_telemetry_time = now_ts
            st.total_alerts_dispatched += len(events)

        return events

    def evaluate_timeout(self, now: Optional[float] = None) -> Optional[WatchAlertEvent]:
        """Check if telemetry stream has stalled or train vanished for longer than timeout."""
        now_ts = time.time() if now is None else now
        with self._lock:
            st = self.state
            if not st.is_active or st.telemetry_lost_fired:
                return None
            elapsed = now_ts - st.last_telemetry_time
            if elapsed >= self.config.telemetry_timeout_sec:
                st.telemetry_lost_fired = True
                now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts))
                headline = f"SIGNAL LOST: Train {self.config.train_number} Telemetry Dropped"
                msg = (
                    f"No telemetry updates received for Train {self.config.train_number} "
                    f"for {int(elapsed)} seconds."
                )
                return WatchAlertEvent(
                    timestamp=now_iso,
                    train_number=self.config.train_number,
                    trigger_type=WatchTriggerType.TELEMETRY_LOST,
                    headline=headline,
                    message=msg,
                    current_speed_kmh=st.last_known_speed,
                    current_delay_min=st.last_known_delay,
                    delay_delta_min=0.0,
                    distance_to_dest_km=None,
                    eta_dest_minutes=None,
                    destination_name=self.config.destination_name,
                    current_lat=st.last_point.lat if st.last_point else None,
                    current_lng=st.last_point.lng if st.last_point else None,
                )
        return None


# ==============================================================================
# Multimodal Zero-Dependency Push Notification Dispatchers
# ==============================================================================

def dispatch_terminal_bell() -> None:
    """Emit an audible terminal bell escape character to stdout."""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass

    # Windows console alert beep fallback
    if sys.platform == "win32":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


def dispatch_desktop_notification(title: str, message: str) -> bool:
    """Dispatch a native desktop notification across Windows, macOS, or Linux.

    Zero third-party dependencies: relies on notify-send, osascript, or PowerShell.
    """
    system = sys.platform
    try:
        if system.startswith("linux") or "bsd" in system:
            subprocess.Popen(
                ["notify-send", "-a", "railmon", "-i", "dialog-information", title, message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

        elif system == "darwin":
            clean_title = title.replace('"', '\\"')
            clean_msg = message.replace('"', '\\"')
            script = f'display notification "{clean_msg}" with title "{clean_title}" subtitle "railmon"'
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

        elif system == "win32":
            clean_title = title.replace("'", "''")
            clean_msg = message.replace("'", "''")
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$textNodes = $template.GetElementsByTagName('text'); "
                f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{clean_title}')) > $null; "
                f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{clean_msg}')) > $null; "
                "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('railmon'); "
                "$notification = [Windows.UI.Notifications.ToastNotification]::new($template); "
                "$notifier.Show($notification);"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

    except Exception as exc:
        logger.debug("Desktop notification dispatch failed: %s", exc)
    return False


def _resolve_webhook_format(config: WatchConfig) -> WebhookFormat:
    """Auto-detect webhook payload dialect based on URL pattern if set to AUTO."""
    if config.webhook_format != WebhookFormat.AUTO:
        return config.webhook_format

    url = (config.webhook_url or "").lower()
    if "api.telegram.org" in url:
        return WebhookFormat.TELEGRAM
    if "discord.com" in url or "discordapp.com" in url:
        return WebhookFormat.DISCORD
    if "hooks.slack.com" in url:
        return WebhookFormat.SLACK
    return WebhookFormat.GENERIC


def dispatch_webhook(config: WatchConfig, event: WatchAlertEvent) -> bool:
    """Deliver outbound JSON push notification to configured webhook endpoint."""
    if not config.webhook_url:
        return False

    url = config.webhook_url
    fmt = _resolve_webhook_format(config)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "railmon-watchdog/1.0",
    }
    if config.webhook_token:
        headers["Authorization"] = f"Bearer {config.webhook_token}"

    if fmt == WebhookFormat.TELEGRAM:
        chat_id = config.webhook_chat_id
        # If chat_id not provided in config, try extracting from URL query
        if not chat_id and "?" in url:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            chat_id = qs.get("chat_id", [None])[0]

        speed_str = f"{event.current_speed_kmh:.1f}"
        delay_str = f"{int(event.current_delay_min):+d}"
        text = (
            f"🚆 *railmon watch: {event.headline}*\n\n"
            f"{event.message}\n"
            f"`Speed: {speed_str} km/h | Delay: {delay_str}m`"
        )
        payload = {
            "text": text,
            "parse_mode": "Markdown",
        }
        if chat_id:
            payload["chat_id"] = chat_id

    elif fmt == WebhookFormat.DISCORD:
        is_warn = "DELAY" in event.trigger_type.value.upper() or "LOST" in event.trigger_type.value.upper()
        color = 0xE67E22 if is_warn else 0x2ECC71
        payload = {
            "username": "railmon",
            "embeds": [
                {
                    "title": f"🚆 {event.headline}",
                    "description": event.message,
                    "color": color,
                    "fields": [
                        {"name": "Speed", "value": f"{event.current_speed_kmh:.1f} km/h", "inline": True},
                        {"name": "Delay", "value": f"{int(event.current_delay_min):+d} min", "inline": True},
                    ],
                    "footer": {"text": "Hellenic Railways Live Telemetry"},
                }
            ],
        }

    elif fmt == WebhookFormat.SLACK:
        payload = {
            "text": f"🚆 *{event.headline}*\n{event.message} (Speed: {event.current_speed_kmh:.1f} km/h, Delay: {int(event.current_delay_min):+d}m)",
        }

    else:  # GENERIC
        payload = event.to_dict()

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return bool(200 <= resp.status < 300)
    except Exception as exc:
        logger.warning("Outbound webhook POST to %s failed: %s", url, exc)
        return False


def dispatch_all_channels(
    config: WatchConfig,
    event: WatchAlertEvent,
) -> Dict[NotificationChannel, bool]:
    """Multiplex event delivery across all active alert channels."""
    results: Dict[NotificationChannel, bool] = {}

    if config.enable_bell:
        dispatch_terminal_bell()
        results[NotificationChannel.BELL] = True

    if config.enable_desktop:
        ok = dispatch_desktop_notification(event.headline, event.message)
        results[NotificationChannel.DESKTOP] = ok

    if config.webhook_url:
        ok = dispatch_webhook(config, event)
        results[NotificationChannel.WEBHOOK] = ok

    return results


# ==============================================================================
# Console Presentation & Formatting Utilities
# ==============================================================================

def format_watch_event_line(event: WatchAlertEvent, ascii_mode: bool = False) -> str:
    """Format single alert event for log stream or CLI display."""
    icon = "[!]" if ("DELAY" in event.trigger_type.value.upper() or "LOST" in event.trigger_type.value.upper()) else "[*]"
    if not ascii_mode:
        if event.trigger_type == WatchTriggerType.DEPARTURE:
            icon = "🚀"
        elif event.trigger_type == WatchTriggerType.ARRIVAL:
            icon = "🏁"
        elif "DELAY" in event.trigger_type.value.upper():
            icon = "⚠️"
        elif "PROXIMITY" in event.trigger_type.value.upper():
            icon = "📍"
        elif event.trigger_type == WatchTriggerType.SENSOR_FROZEN:
            icon = "❄️"
        elif event.trigger_type == WatchTriggerType.TELEMETRY_LOST:
            icon = "❌"

    return f"{event.timestamp} {icon} {event.headline} — {event.message}"


def format_watch_ticker(
    config: WatchConfig,
    state: WatchState,
    pos: Optional[Any],
    ascii_mode: bool = False,
) -> str:
    """Generate a compact multi-line ASCII/UTF-8 status panel for interactive terminal display."""
    spd = f"{pos.speed:.1f} km/h" if pos and getattr(pos, "speed", None) is not None else "0.0 km/h"
    dly_val = int(pos.delay) if pos and getattr(pos, "delay", None) is not None else 0
    dly = f"+{dly_val}m" if dly_val > 0 else (f"{dly_val}m" if dly_val < 0 else "0m")

    status_tag = "ACTIVE (En Route)" if state.has_departed else "WAITING DEPARTURE"
    if state.arrival_alert_fired:
        status_tag = "ARRIVED AT DESTINATION"
    elif state.sensor_frozen_fired:
        status_tag = "SENSOR FROZEN"
    elif state.telemetry_lost_fired:
        status_tag = "TELEMETRY LOST"

    dest_str = config.destination_name or config.destination_station or "Not specified"
    route_str = state.route_name or "Hellenic Rail Network"
    next_stn = state.next_station or "—"

    box_h = "-" if ascii_mode else "═"
    box_v = "|" if ascii_mode else "║"

    lines = [
        f"+{box_h * 68}+",
        f"{box_v} RAILMON WATCHDOG: TRAIN {config.train_number:<12} Status: {status_tag:<21} {box_v}",
        f"+{box_h * 68}+",
        f"{box_v} Route:        {route_str:<51} {box_v}",
        f"{box_v} Destination:  {dest_str:<51} {box_v}",
        f"{box_v} Next Station: {next_stn:<51} {box_v}",
        f"{box_v} Speed:        {spd:<15} Delay: {dly:<30} {box_v}",
        f"{box_v} Alerts Fired: {state.total_alerts_dispatched:<15} Bell: {'ON' if config.enable_bell else 'OFF':<9} Desktop: {'ON' if config.enable_desktop else 'OFF':<14} {box_v}",
        f"+{box_h * 68}+",
    ]
    return "\n".join(lines)
