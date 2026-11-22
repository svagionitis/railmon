"""
Terminal output formatters and text table renderers for railmon CLI commands.
Zero external dependencies; pure Python standard library.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
import unicodedata
import zoneinfo

from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
)

try:
    ATHENS_TZ: timezone = zoneinfo.ZoneInfo("Europe/Athens")
except Exception:
    ATHENS_TZ = timezone(timedelta(hours=2))

_GREEK_TO_LATIN_MAP: Dict[str, str] = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
    "ά": "a", "έ": "e", "ή": "i", "ί": "i", "ό": "o", "ύ": "y", "ώ": "o",
    "ϊ": "i", "ϋ": "y", "ΐ": "i", "ΰ": "y",
    "Α": "A", "Β": "V", "Γ": "G", "Δ": "D", "Ε": "E", "Ζ": "Z", "Η": "I",
    "Θ": "Th", "Ι": "I", "Κ": "K", "Λ": "L", "Μ": "M", "Ν": "N", "Ξ": "X",
    "Ο": "O", "Π": "P", "Ρ": "R", "Σ": "S", "Τ": "T", "Υ": "Y", "Φ": "F",
    "Χ": "Ch", "Ψ": "Ps", "Ω": "O",
    "Ά": "A", "Έ": "E", "Ή": "I", "Ί": "I", "Ό": "O", "Ύ": "Y", "Ώ": "O",
}

_STATION_INDEX_CACHE: Optional[Dict[str, Any]] = None


def _get_station_index() -> Dict[str, Any]:
    """Retrieve or build the station index cache for cross-referencing names."""
    global _STATION_INDEX_CACHE
    if _STATION_INDEX_CACHE is None:
        try:
            from tui.mock_engine import build_station_index, load_stations

            stations = list(load_stations().values())
            _STATION_INDEX_CACHE = build_station_index(stations)
        except Exception:
            _STATION_INDEX_CACHE = {}
    return _STATION_INDEX_CACHE


def transliterate_greek_to_latin(text: str) -> str:
    """Convert Greek text to clean 7-bit ASCII Latin characters."""
    if not text:
        return ""
    result: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        if i + 1 < n:
            pair = text[i : i + 2].lower()
            if pair in ("ου", "ού"):
                result.append("Ou" if text[i].isupper() else "ou")
                i += 2
                continue
        ch = text[i]
        result.append(_GREEK_TO_LATIN_MAP.get(ch, ch))
        i += 1
    decomposed = unicodedata.normalize("NFD", "".join(result))
    return "".join(c for c in decomposed if ord(c) < 128)


def format_timetable_time(raw_time: Any, empty_dash: str = "--:--") -> str:
    """Parse raw timestamp or clock string and format into 'HH:MM' in Europe/Athens time.

    Supports:
    - ISO 8601 strings (e.g. '2026-11-21T21:37:00.000Z', '2026-11-21T21:37:00+00:00')
    - Simple clock strings (e.g. '14:30', '14:30:00')
    - Unix epoch timestamps (int / float)

    Args:
        raw_time: Input time representation or None.
        empty_dash: Fallback string for missing/null values.

    Returns:
        Formatted 'HH:MM' string in Europe/Athens local time.
    """
    if not raw_time or str(raw_time).lower() in ("none", "null", ""):
        return empty_dash

    s = str(raw_time).strip()

    # 1. If already 'HH:MM'
    if len(s) == 5 and s[2] == ":" and s[:2].isdigit() and s[3:].isdigit():
        return s

    # 2. If 'HH:MM:SS'
    if len(s) == 8 and s[2] == ":" and s[5] == ":" and s[:2].isdigit():
        return s[:5]

    # 3. Parse ISO 8601 string
    try:
        clean_s = s.replace("Z", "+00:00")
        if "T" in clean_s or " " in clean_s:
            dt = datetime.fromisoformat(clean_s)
            if dt.tzinfo is not None:
                dt_athens = dt.astimezone(ATHENS_TZ)
            else:
                dt_athens = dt.replace(tzinfo=timezone.utc).astimezone(ATHENS_TZ)
            return dt_athens.strftime("%H:%M")
    except (ValueError, TypeError, OSError):
        pass

    # 4. Fallback for strings containing 'T' if fromisoformat failed
    if "T" in s:
        part = s.split("T")[-1].replace("Z", "").strip()
        if len(part) >= 5 and part[2] == ":" and part[:2].isdigit() and part[3:5].isdigit():
            return part[:5]

    return empty_dash


def resolve_timetable_station_name(
    stop: Any,
    is_ascii: bool = False,
    empty_dash: str = "—",
    single_name: bool = False,
) -> str:
    """Extract and format the calling station name for a timetable stop record.

    Cross-references raw stop payload keys ('name', 'stationName', 'stationNameGreek', etc.)
    against known stations in stations.json to provide accurate English and Greek names,
    with automatic Latin transliteration for ASCII purity.

    Args:
        stop: Stop dictionary or TimetableStopDTO instance.
        is_ascii: True if output must be strictly 7-bit ASCII.
        empty_dash: Placeholder for missing names.
        single_name: True if only a single English/Latin name should be returned.

    Returns:
        Formatted station name string.
    """
    en: Optional[str] = None
    el: Optional[str] = None
    name: Optional[str] = None

    if isinstance(stop, dict):
        en = stop.get("stationNameEnglish") or stop.get("station_name_en")
        el = stop.get("stationNameGreek") or stop.get("station_name_el") or stop.get("name")
        name = stop.get("name") or stop.get("stationName") or stop.get("station_name")
        stn_obj = stop.get("station")
        if isinstance(stn_obj, dict):
            en = en or stn_obj.get("nameEnglish") or stn_obj.get("name_en")
            el = el or stn_obj.get("nameGreek") or stn_obj.get("name_el") or stn_obj.get("name")
        stn_id_obj = stop.get("stationId")
        if isinstance(stn_id_obj, dict):
            names_obj = stn_id_obj.get("names", {})
            if isinstance(names_obj, dict):
                en = en or names_obj.get("en")
                el = el or names_obj.get("el") or names_obj.get("default")
            name = name or stn_id_obj.get("name")
    else:
        en = getattr(stop, "station_name_en", None)
        el = getattr(stop, "station_name_el", None)
        name = getattr(stop, "station_name", None) or getattr(stop, "name", None)

    # Cross-reference against stations.json index if English name is missing
    if not en and (el or name):
        from tui.mock_engine import normalize_station_key

        stn_idx = _get_station_index()
        for candidate in (el, name):
            if candidate and isinstance(candidate, str):
                matched = stn_idx.get(normalize_station_key(candidate))
                if matched:
                    en = matched.name_en
                    if not el:
                        el = matched.name_el
                    break

    # If still not found and in ASCII mode or single_name mode, transliterate
    if not en and (el or name):
        en = transliterate_greek_to_latin(str(el or name or ""))

    if is_ascii or single_name:
        resolved = en or transliterate_greek_to_latin(str(el or name or "")) or empty_dash
        return transliterate_greek_to_latin(resolved) if is_ascii else resolved

    if en and el and en != el:
        return f"{en} / {el}"

    return en or el or name or empty_dash


def format_file_size(size_bytes: int) -> str:
    """Format byte size into human-readable representation."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_trips_table(
    trips: List[TripSummary],
    date: Optional[str] = None,
    corridor: Optional[str] = None,
    db_path: str = "railway_history.db",
) -> str:
    """Format a list of TripSummary records into a clean CLI table."""
    date_str = date or "All Dates"
    corridor_str = corridor or "All"
    header_title = f"Recorded Trips for {date_str} (Corridor: {corridor_str}):"

    if not trips:
        return f"{header_title}\nNo matching trips found in {db_path}."

    lines = [
        header_title,
        "-" * 100,
        f"{'Train':<8} {'Date':<11} {'Corridor':<22} {'Points':>7} {'Max Delay':>10} {'Avg Delay':>10} {'Max Speed':>11} {'First Seen':<10} {'Last Seen':<10}",
        "-" * 100,
    ]

    for t in trips:
        corr = (t.corridor or "—")[:20]
        first_time = t.first_seen.split("T")[-1].replace("Z", "")[:8] if "T" in t.first_seen else t.first_seen[-8:]
        last_time = t.last_seen.split("T")[-1].replace("Z", "")[:8] if "T" in t.last_seen else t.last_seen[-8:]
        max_del = f"+{t.max_delay_min}m" if t.max_delay_min > 0 else f"{t.max_delay_min}m"
        avg_del = f"+{t.avg_delay_min:.1f}m" if t.avg_delay_min > 0 else f"{t.avg_delay_min:.1f}m"
        max_spd = f"{t.max_speed_kmh:.1f} km/h"

        lines.append(
            f"{t.train_number:<8} {t.service_date:<11} {corr:<22} {t.total_points:>7} {max_del:>10} {avg_del:>10} {max_spd:>11} {first_time:<10} {last_time:<10}"
        )

    lines.append("-" * 100)
    lines.append(f"Total matching trips: {len(trips)} | Database: {db_path}")
    return "\n".join(lines)


def format_train_history_table(
    rows: List[TelemetryHistoryRow],
    train_number: str,
    total_recorded: Optional[int] = None,
) -> str:
    """Format a train's telemetry breadcrumbs into a readable timeline."""
    header_title = f"Telemetry History for Train {train_number} (Showing: {len(rows)}):"

    if not rows:
        return f"{header_title}\nNo historical telemetry points found."

    lines = [
        header_title,
        "-" * 115,
        f"{'Recorded At (UTC)':<20} {'Lat':<10} {'Lng':<10} {'Speed':<12} {'Heading':<9} {'Delay':<8} {'Status':<11} {'Chainage':<12} {'Next Station':<20}",
        "-" * 115,
    ]

    for r in rows:
        ts = r.recorded_at.replace("T", " ").replace("Z", "")[:19]
        speed_str = f"{r.speed:.1f} km/h"
        hdg_str = f"{r.course:.1f}°" if r.course is not None else "—"
        del_str = f"+{r.delay_min}m" if r.delay_min > 0 else f"{r.delay_min}m"
        stat_str = (r.status or "—")[:10]
        chainage_str = f"{r.chainage_m:,} m" if r.chainage_m is not None else "—"
        station_str = (r.next_station or "—")[:19]

        lines.append(
            f"{ts:<20} {r.lat:<10.4f} {r.lng:<10.4f} {speed_str:<12} {hdg_str:<9} {del_str:<8} {stat_str:<11} {chainage_str:<12} {station_str:<20}"
        )

    lines.append("-" * 115)
    if total_recorded is not None and total_recorded > len(rows):
        lines.append(f"Showing {len(rows)} of {total_recorded:,} recorded points.")
    else:
        lines.append(f"Total recorded points displayed: {len(rows)}")
    return "\n".join(lines)


def format_punctuality_table(
    summaries: List[CorridorPunctualitySummary],
    threshold_min: int = 5,
) -> str:
    """Format corridor punctuality metrics and compliance rates into a table."""
    header_title = f"Corridor Punctuality Report (Threshold: <= {threshold_min} min on-time):"

    if not summaries:
        return f"{header_title}\nNo corridor punctuality data available."

    lines = [
        header_title,
        "-" * 110,
        f"{'Corridor':<28} {'Trips':>7} {'Obs':>9} {'On-Time %':>10} {'Avg Delay':>11} {'Max Delay':>11} {'Delayed':>9} {'Severe (>=30m)':>15}",
        "-" * 110,
    ]

    total_trips = sum(s.total_trips for s in summaries)
    total_obs = sum(s.total_observations for s in summaries)
    weighted_punc = (
        sum(s.punctuality_rate_pct * s.total_trips for s in summaries) / total_trips
        if total_trips > 0
        else 0.0
    )

    for s in summaries:
        corr = s.corridor[:26]
        punc = f"{s.punctuality_rate_pct:.1f}%"
        avg_d = f"+{s.avg_delay_min:.1f}m" if s.avg_delay_min > 0 else f"{s.avg_delay_min:.1f}m"
        max_d = f"+{s.max_delay_min}m" if s.max_delay_min > 0 else f"{s.max_delay_min}m"

        lines.append(
            f"{corr:<28} {s.total_trips:>7} {s.total_observations:>9,d} {punc:>10} {avg_d:>11} {max_d:>11} {s.delayed_count:>9} {s.severe_delay_count:>15}"
        )

    lines.append("-" * 110)
    lines.append(
        f"Network Average Punctuality: {weighted_punc:.2f}% | Evaluated Trips: {total_trips} | Observations: {total_obs:,d}"
    )
    return "\n".join(lines)


def format_dwell_table(
    dwells: List[StationDwellRecord],
    train_number: str,
) -> str:
    """Format station stops, dwell intervals, and arrival/departure delays."""
    header_title = f"Station Dwell Profile for Train {train_number}:"

    if not dwells:
        return f"{header_title}\nNo station stops or dwell events detected."

    lines = [
        header_title,
        "-" * 105,
        f"{'Station Name':<28} {'Arrival (UTC)':<20} {'Departure (UTC)':<20} {'Dwell Time':>12} {'Arr Delay':>10} {'Dep Delay':>10}",
        "-" * 105,
    ]

    for d in dwells:
        st_name = d.station_name[:26]
        arr_time = d.arrival_time.replace("T", " ").replace("Z", "")[:19]
        dep_time = d.departure_time.replace("T", " ").replace("Z", "")[:19]

        m, s = divmod(d.dwell_seconds, 60)
        dwell_str = f"{m}m {s:02d}s" if m > 0 else f"{s}s"

        arr_del = f"+{d.arrival_delay_min}m" if d.arrival_delay_min > 0 else f"{d.arrival_delay_min}m"
        dep_del = f"+{d.departure_delay_min}m" if d.departure_delay_min > 0 else f"{d.departure_delay_min}m"

        lines.append(
            f"{st_name:<28} {arr_time:<20} {dep_time:<20} {dwell_str:>12} {arr_del:>10} {dep_del:>10}"
        )

    lines.append("-" * 105)
    lines.append(f"Total station stops: {len(dwells)}")
    return "\n".join(lines)


def format_stats_table(stats: DatabaseStats, db_path: str) -> str:
    """Format database storage diagnostics, row volumes, and WAL status."""
    time_span = "N/A"
    if stats.earliest_record and stats.latest_record:
        try:
            from datetime import datetime

            t0 = datetime.fromisoformat(stats.earliest_record.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(stats.latest_record.replace("Z", "+00:00"))
            delta = t1 - t0
            days = delta.total_seconds() / 86400
            time_span = f"{days:.2f} days ({delta})"
        except Exception:
            time_span = f"{stats.earliest_record} to {stats.latest_record}"

    earliest = stats.earliest_record.replace("T", " ") if stats.earliest_record else "None"
    latest = stats.latest_record.replace("T", " ") if stats.latest_record else "None"

    lines = [
        "SQLite Telemetry Storage Summary:",
        "-" * 55,
        f"  Database File:        {db_path}",
        f"  Journal Mode:         {'WAL (Write-Ahead Logging)' if stats.wal_mode_enabled else 'DELETE / Rollback'}",
        f"  File Size on Disk:    {format_file_size(stats.file_size_bytes)}",
        f"  Total Telemetry Rows: {stats.total_telemetry_rows:,d}",
        f"  Total Unique Trains:  {stats.total_trains:,d}",
        f"  Total Recorded Trips: {stats.total_trips:,d}",
        f"  Audit Sessions:       {stats.total_sessions:,d}",
        f"  Earliest Record:      {earliest}",
        f"  Latest Record:        {latest}",
        f"  Data Time Span:       {time_span}",
        "-" * 55,
    ]
    return "\n".join(lines)


def format_sessions_table(sessions: List[RecordingSessionDB]) -> str:
    """Format audit recording sessions history table."""
    if not sessions:
        return "No audit recording sessions found in database."

    lines = [
        "Audit Recording Sessions:",
        "-" * 95,
        f"{'Session ID':<12} {'Started (UTC)':<20} {'Ended (UTC)':<20} {'Mode':<8} {'Records Written':>16} {'Client Ver':<12}",
        "-" * 95,
    ]

    for s in sessions:
        sess_id = str(s.session_id) if s.session_id is not None else "—"
        start = s.started_at.replace("T", " ").replace("Z", "")[:19]
        end = s.ended_at.replace("T", " ").replace("Z", "")[:19] if s.ended_at else "Active / Interrupted"

        lines.append(
            f"{sess_id:<12} {start:<20} {end:<20} {s.mode:<8} {s.total_records_written:>16,d} {s.client_version:<12}"
        )

    lines.append("-" * 95)
    lines.append(f"Total sessions: {len(sessions)}")
    return "\n".join(lines)


def _format_board_title_name(station: Any, is_ascii: bool) -> str:
    """Format the station header display name."""
    sid = getattr(station, "id", "")
    name_el = getattr(station, "name_el", "")
    name_en = getattr(station, "name_en", "")

    if not hasattr(station, "name_en"):
        return str(station)

    if is_ascii or not name_el or name_el == name_en:
        return f"{name_en} [{sid}]" if sid else name_en
    return f"{name_el} / {name_en} [{sid}]" if sid else f"{name_el} / {name_en}"


def _extract_board_target_name(
    rec: Dict[str, Any],
    is_arrivals: bool,
    is_ascii: bool,
    empty_dash: str,
) -> str:
    """Extract origin or destination display name from a board record."""
    target_dict = rec.get("origin") if is_arrivals else rec.get("destination")
    if isinstance(target_dict, dict):
        if is_ascii:
            return (
                target_dict.get("nameEnglish")
                or target_dict.get("nameGreek")
                or target_dict.get("id")
                or empty_dash
            )
        t_el = target_dict.get("nameGreek")
        t_en = target_dict.get("nameEnglish")
        if t_el and t_en and t_el != t_en:
            return f"{t_el} / {t_en}"
        return t_el or t_en or target_dict.get("id") or empty_dash

    if isinstance(target_dict, str):
        return target_dict

    return str(
        rec.get("target_station")
        or rec.get("destination_name")
        or rec.get("origin_name")
        or empty_dash
    )


def _extract_board_delay_and_status(rec: Dict[str, Any]) -> Tuple[str, str]:
    """Extract formatted delay and status indicators from a board record."""
    raw_delay = rec.get("delayMinutes") if rec.get("delayMinutes") is not None else rec.get("delay")
    try:
        delay_min = int(raw_delay) if raw_delay is not None else 0
    except (ValueError, TypeError):
        delay_min = 0

    delay_str = f"+{delay_min}m" if delay_min > 0 else f"{delay_min}m"

    raw_status = rec.get("status")
    if raw_status:
        status_str = str(raw_status).replace("_", " ").upper()
    elif delay_min > 0:
        status_str = "DELAYED"
    else:
        status_str = "ON TIME"

    return delay_str, status_str


def format_station_board_table(
    station: Any,
    records: List[Dict[str, Any]],
    is_arrivals: bool = False,
    is_ascii: bool = False,
    data_source: str = "railway.gov.gr",
    timestamp_str: Optional[str] = None,
) -> str:
    """Format station departure or arrival board records into a clean CLI terminal table.

    Args:
        station: StationNode instance or station name string.
        records: List of departure or arrival dictionaries.
        is_arrivals: True if rendering arrival board, False for departure board.
        is_ascii: True if 7-bit ASCII characters should be enforced.
        data_source: Source label for footer metadata (e.g. 'railway.gov.gr').
        timestamp_str: Optional timestamp string for header (e.g. '14:30:00').

    Returns:
        Clean multi-line terminal string table.
    """
    title_name = _format_board_title_name(station, is_ascii)
    board_mode = "ARRIVAL" if is_arrivals else "DEPARTURE"
    board_title = f"STATION {board_mode} BOARD"
    target_col = "ORIGIN" if is_arrivals else "DESTINATION"
    divider_char = "="
    sub_divider_char = "-"
    empty_dash = "-" if is_ascii else "—"

    ts_part = f" ({timestamp_str})" if timestamp_str else ""
    header_title = f"{board_title}: {title_name}{ts_part}"

    width = 100
    lines = [
        divider_char * width,
        header_title,
        divider_char * width,
    ]

    if not records:
        plural_mode = "arrivals" if is_arrivals else "departures"
        lines.append(f"No upcoming {plural_mode} scheduled.")
        lines.append(divider_char * width)
        return "\n".join(lines)

    lines.append(
        f"{'TIME':<7} {'TRAIN':<7} {'TYPE':<12} {target_col:<34} {'PLAT':^5} {'STATUS':<12} {'DELAY':>8}"
    )
    lines.append(sub_divider_char * width)

    for rec in records:
        train_no = str(rec.get("trainNumber") or rec.get("train_number") or rec.get("id") or "N/A")
        service_type = str(rec.get("serviceType") or rec.get("service_type") or "Passenger")

        raw_time = str(
            rec.get("scheduledTime")
            or rec.get("scheduled_time")
            or rec.get("departureTime")
            or rec.get("arrivalTime")
            or rec.get("time")
            or "--:--"
        )
        time_val = raw_time.split("T")[-1].replace("Z", "")[:5] if "T" in raw_time else raw_time[:5]
        target_name = _extract_board_target_name(rec, is_arrivals, is_ascii, empty_dash)

        plat_raw = rec.get("platform")
        platform = str(plat_raw) if plat_raw is not None and str(plat_raw).strip() else empty_dash

        delay_str, status_str = _extract_board_delay_and_status(rec)

        lines.append(
            f"{time_val:<7} {train_no[:6]:<7} {service_type[:11]:<12} {target_name[:33]:<34} {platform[:4]:^5} {status_str[:11]:<12} {delay_str:>8}"
        )

    lines.append(divider_char * width)
    plural_label = "arrivals" if is_arrivals else "departures"
    lines.append(f"Total {plural_label}: {len(records)} | Data source: {data_source}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Train Timetable & Intermediate Stop Inspector Formatters (F-6.2)
# -----------------------------------------------------------------------------


def _format_schedule_header_title(timetable: Any, is_ascii: bool) -> str:
    """Format the header title displaying train number and origin/destination."""
    train_no = (
        getattr(timetable, "train_number", None)
        or (timetable.get("trainNumber") if isinstance(timetable, dict) else None)
        or (timetable.get("train_number") if isinstance(timetable, dict) else None)
        or "N/A"
    )

    def _resolve_endpoint_name(endpoint: Any) -> str:
        if isinstance(endpoint, dict):
            el = (
                endpoint.get("nameGreek")
                or endpoint.get("name_el")
                or endpoint.get("name")
            )
            en = endpoint.get("nameEnglish") or endpoint.get("name_en")
            if not en and el and isinstance(el, str):
                from tui.mock_engine import normalize_station_key

                stn_idx = _get_station_index()
                matched = stn_idx.get(normalize_station_key(el))
                if matched:
                    en = matched.name_en
                else:
                    en = transliterate_greek_to_latin(el)
            if is_ascii or not el or el == en:
                resolved = en or transliterate_greek_to_latin(str(el or "")) or endpoint.get("id") or "—"
                return transliterate_greek_to_latin(str(resolved)) if is_ascii else str(resolved)
            return f"{en} / {el}" if en else el
        if isinstance(endpoint, str) and endpoint.strip():
            s = endpoint.strip()
            return transliterate_greek_to_latin(s) if is_ascii else s
        return "—"

    if isinstance(timetable, dict):
        orig_str = _resolve_endpoint_name(timetable.get("origin"))
        dest_str = _resolve_endpoint_name(timetable.get("destination"))
    else:
        orig_str = getattr(timetable, "origin_name", "—")
        dest_str = getattr(timetable, "destination_name", "—")

    arrow = "->" if is_ascii else "→"
    return f"TRAIN TIMETABLE & ITINERARY: TRAIN {train_no} ({orig_str} {arrow} {dest_str})"


def _extract_stop_station_name(stop: Any, is_ascii: bool, empty_dash: str) -> str:
    """Extract and format the calling station name for a timetable stop."""
    return resolve_timetable_station_name(stop, is_ascii=is_ascii, empty_dash=empty_dash)


def _extract_stop_time_field(raw_time: Any, empty_dash: str = "--:--") -> str:
    """Parse time string into clean HH:MM representation."""
    return format_timetable_time(raw_time, empty_dash=empty_dash)


def _extract_stop_indicator(stop: Any, is_ascii: bool) -> str:
    """Determine stop progression indicator glyph: [X] (passed), ►/> (current), or upcoming."""
    is_passed = getattr(stop, "is_passed", False) if hasattr(stop, "is_passed") else False
    is_current = getattr(stop, "is_current", False) if hasattr(stop, "is_current") else False
    status = ""

    if isinstance(stop, dict):
        is_passed = stop.get("is_passed", False) or stop.get("status") == "DEPARTED"
        is_current = stop.get("is_current", False) or stop.get("status") == "APPROACH"
        status = str(stop.get("status", "")).upper()
    elif hasattr(stop, "status"):
        status = str(getattr(stop, "status", "")).upper()
        if status == "DEPARTED":
            is_passed = True
        elif status == "APPROACH":
            is_current = True

    if is_passed:
        return "[X]"
    if is_current:
        return " > " if is_ascii else " ► "
    return "   "


def _extract_stop_status_and_delay(stop: Any, is_passed: bool, is_current: bool) -> Tuple[str, str]:
    """Extract status description and delay badge for a timetable stop."""
    raw_delay = stop.get("delayMinutes") if isinstance(stop, dict) else getattr(stop, "delay_minutes", None)
    if raw_delay is None and isinstance(stop, dict):
        raw_delay = stop.get("delay_minutes") or stop.get("delay")

    try:
        delay_min = int(raw_delay) if raw_delay is not None else 0
    except (ValueError, TypeError):
        delay_min = 0

    if isinstance(stop, dict):
        raw_status = stop.get("status")
    else:
        raw_status = getattr(stop, "status", None)

    if raw_status:
        status_str = str(raw_status).replace("_", " ").upper()
    elif is_passed:
        status_str = "DEPARTED"
    elif is_current:
        status_str = "APPROACH"
    else:
        status_str = "SCHEDULED"

    if is_passed or is_current:
        delay_str = f"+{delay_min}m" if delay_min > 0 else f"{delay_min}m"
    elif delay_min > 0:
        delay_str = f"+{delay_min}m"
    else:
        delay_str = "---"

    return status_str, delay_str


def format_train_schedule_table(
    timetable: Any,
    is_ascii: bool = False,
    data_source: str = "railway.gov.gr",
) -> str:
    """Format full train timetable and intermediate calling stops into a clean CLI table.

    Args:
        timetable: TrainTimetableDTO or dictionary containing timetable metadata and stops list.
        is_ascii: True if 7-bit ASCII characters should be enforced.
        data_source: Attribution label for table footer.

    Returns:
        Formatted multi-line text table string.
    """
    divider_char = "="
    sub_divider_char = "-"
    empty_dash = "-" if is_ascii else "—"
    time_dash = "--:--"
    width = 100

    header_title = _format_schedule_header_title(timetable, is_ascii)
    lines = [
        divider_char * width,
        header_title,
        divider_char * width,
    ]

    stops: List[Any] = []
    if isinstance(timetable, dict):
        raw_stops = timetable.get("stops")
        if isinstance(raw_stops, list):
            stops = raw_stops
    elif hasattr(timetable, "stops"):
        stops = list(getattr(timetable, "stops", []))

    if not stops:
        lines.append("No intermediate calling points or timetable records found.")
        lines.append(divider_char * width)
        return "\n".join(lines)

    lines.append(
        f"{'ST':<4} {'STATION':<28} {'SCHED ARR':^9} {'ACTUAL ARR':^10} {'SCHED DEP':^9} {'ACTUAL DEP':^10} {'PLAT':^5} {'STATUS':<10} {'DELAY':>7}"
    )
    lines.append(sub_divider_char * width)

    passed_count = 0
    current_station = empty_dash
    overall_delay = 0

    for stop in stops:
        indicator = _extract_stop_indicator(stop, is_ascii)
        is_passed = "[X]" in indicator
        is_current = (">" in indicator) or ("►" in indicator)

        if is_passed:
            passed_count += 1

        station_name = _extract_stop_station_name(stop, is_ascii, empty_dash)
        if is_current:
            current_station = station_name

        sched_arr_raw = stop.get("scheduledArrival") if isinstance(stop, dict) else getattr(stop, "scheduled_arrival", None)
        act_arr_raw = stop.get("actualArrival") if isinstance(stop, dict) else getattr(stop, "actual_arrival", None)
        sched_dep_raw = stop.get("scheduledDeparture") if isinstance(stop, dict) else getattr(stop, "scheduled_departure", None)
        act_dep_raw = stop.get("actualDeparture") if isinstance(stop, dict) else getattr(stop, "actual_departure", None)
        plat_raw = stop.get("platform") if isinstance(stop, dict) else getattr(stop, "platform", None)

        sched_arr = _extract_stop_time_field(sched_arr_raw, time_dash)
        act_arr = _extract_stop_time_field(act_arr_raw, time_dash)
        sched_dep = _extract_stop_time_field(sched_dep_raw, time_dash)
        act_dep = _extract_stop_time_field(act_dep_raw, time_dash)
        platform = str(plat_raw) if plat_raw is not None and str(plat_raw).strip() else empty_dash

        status_str, delay_str = _extract_stop_status_and_delay(stop, is_passed, is_current)
        if (is_passed or is_current) and delay_str != "---":
            try:
                delay_val = int(delay_str.replace("+", "").replace("m", ""))
                overall_delay = max(overall_delay, delay_val)
            except ValueError:
                pass

        lines.append(
            f"{indicator:<4} {station_name[:28]:<28} {sched_arr:^9} {act_arr:^10} {sched_dep:^9} {act_dep:^10} {platform[:4]:^5} {status_str[:10]:<10} {delay_str:>7}"
        )

    lines.append(divider_char * width)
    delay_repr = f"+{overall_delay}m" if overall_delay > 0 else f"{overall_delay}m"
    lines.append(
        f"Total stops: {len(stops)} | Passed: {passed_count} | Next stop: {current_station[:24]} | Delay: {delay_repr} | Source: {data_source}"
    )
    return "\n".join(lines)


def format_infrastructure_table(
    features: Sequence[Any],
    ascii_mode: bool = False,
    title: str = "HELLENIC RAILWAYS - INFRASTRUCTURE GIS ASSETS",
) -> str:
    """Format railway infrastructure GIS assets (tunnels, bridges, signals) into an ASCII/UTF-8 table.

    Args:
        features: Sequence of InfrastructureFeature objects.
        ascii_mode: Whether to force 7-bit ASCII characters.
        title: Table header title.

    Returns:
        Formatted multi-line table string.
    """
    if not features:
        return "No infrastructure assets matching filter criteria."

    width = 96
    border_char = "=" if ascii_mode else "═"
    divider_char = "-" if ascii_mode else "─"

    lines: List[str] = [
        border_char * width,
        f" {title} ",
        border_char * width,
        f"{'ID':<18} {'ASSET NAME':<28} {'TYPE':<8} {'CORRIDOR':<22} {'LENGTH / ASPECT':<15}",
        divider_char * width,
    ]

    tunnel_count = 0
    bridge_count = 0
    signal_count = 0

    for feat in features:
        fid = getattr(feat, "id", "")
        name = getattr(feat, "name", "")
        ftype = getattr(feat, "feature_type", None)
        ftype_str = ftype.value if hasattr(ftype, "value") else str(ftype)
        corridor = getattr(feat, "corridor_id", "")
        length_m = getattr(feat, "length_m", None)
        aspect = getattr(feat, "signal_aspect", None)

        if ftype_str == "tunnel":
            tunnel_count += 1
            extra = f"{length_m / 1000.0:4.1f} km" if length_m else "N/A"
        elif ftype_str == "bridge":
            bridge_count += 1
            extra = f"{int(round(length_m))} m" if length_m else "N/A"
        elif ftype_str == "signal":
            signal_count += 1
            extra = aspect.value.upper() if hasattr(aspect, "value") else str(aspect or "JUNCTION")
        else:
            extra = "N/A"

        lines.append(
            f"{fid[:17]:<18} {name[:27]:<28} {ftype_str.upper()[:7]:<8} {corridor[:21]:<22} {extra:<15}"
        )

    lines.append(divider_char * width)
    lines.append(
        f"Total Assets: {len(features)} | Tunnels: {tunnel_count} | Bridges: {bridge_count} | Signals: {signal_count}"
    )
    lines.append(border_char * width)
    return "\n".join(lines)


def format_speed_limits_table(
    rules: Dict[str, Any],
    tsrs: Sequence[Any] = (),
    corridor_filter: Optional[str] = None,
    ascii_mode: bool = False,
    title: str = "HELLENIC RAILWAYS - CORRIDOR SPEED LIMITS & RESTRICTIONS",
) -> str:
    """Format corridor speed limits, micro-segments, and TSRs into an aligned table.

    Args:
        rules: Dict of corridor ID to CorridorSpeedRule.
        tsrs: Sequence of SlowOrder records.
        corridor_filter: Optional corridor ID or substring filter.
        ascii_mode: Whether to force 7-bit ASCII characters.
        title: Table header title.

    Returns:
        Formatted multi-line table string.
    """
    width = 96
    border_char = "=" if ascii_mode else "═"
    divider_char = "-" if ascii_mode else "─"

    lines: List[str] = [
        border_char * width,
        f" {title} ",
        border_char * width,
        f"{'CORRIDOR':<26} {'SEGMENT / ZONE':<34} {'SPEED LIMIT':<14} {'TYPE':<12}",
        divider_char * width,
    ]

    corr_count = 0
    seg_count = 0
    tsr_count = 0

    norm_filter = corridor_filter.strip().lower() if corridor_filter else None

    # Base corridors and segments
    for cid, rule in sorted(rules.items(), key=lambda x: x[0]):
        if norm_filter and norm_filter not in cid.lower() and norm_filter not in getattr(rule, "name", "").lower():
            continue
        corr_count += 1
        r_name = getattr(rule, "name", cid)
        r_spd = getattr(rule, "default_speed_kmh", 120.0)
        lines.append(
            f"{cid[:25]:<26} {('Base Line Speed: ' + r_name)[:33]:<34} {r_spd:5.1f} km/h   {'BASE':<12}"
        )
        for seg in getattr(rule, "segments", ()):
            seg_count += 1
            s_name = getattr(seg, "name", "")
            s_spd = getattr(seg, "max_speed_kmh", r_spd)
            lines.append(
                f"{cid[:25]:<26} {s_name[:33]:<34} {s_spd:5.1f} km/h   {'SEGMENT':<12}"
            )

    # TSRs
    for tsr in tsrs:
        t_corr = getattr(tsr, "corridor_id", "")
        t_name = getattr(tsr, "name", "")
        t_spd = getattr(tsr, "max_speed_kmh", 0.0)
        if norm_filter and norm_filter not in t_corr.lower() and norm_filter not in t_name.lower():
            continue
        tsr_count += 1
        lines.append(
            f"{t_corr[:25]:<26} {('[TSR] ' + t_name)[:33]:<34} {t_spd:5.1f} km/h   {'SLOW-ORDER':<12}"
        )

    lines.append(divider_char * width)
    lines.append(
        f"Active Corridors: {corr_count} | Segments: {seg_count} | Active Slow Orders (TSR): {tsr_count}"
    )
    lines.append(border_char * width)
    return "\n".join(lines)


def format_slow_orders_table(
    tsrs: Sequence[Any],
    corridor_filter: Optional[str] = None,
    ascii_mode: bool = False,
    title: str = "HELLENIC RAILWAYS - ACTIVE TEMPORARY SPEED RESTRICTIONS (SLOW ORDERS)",
) -> str:
    """Format active Slow Orders (TSRs / Βραδυπορίες) into an aligned table.

    Args:
        tsrs: Sequence of SlowOrder records.
        corridor_filter: Optional corridor ID or substring filter.
        ascii_mode: Whether to force 7-bit ASCII characters.
        title: Table header title.

    Returns:
        Formatted multi-line table string.
    """
    width = 96
    border_char = "=" if ascii_mode else "═"
    divider_char = "-" if ascii_mode else "─"

    norm_filter = corridor_filter.strip().lower() if corridor_filter else None
    filtered_tsrs = [
        t for t in tsrs
        if not norm_filter or (
            norm_filter in getattr(t, "corridor_id", "").lower()
            or norm_filter in getattr(t, "name", "").lower()
        )
    ]

    if not filtered_tsrs:
        return "No active slow orders matching filter criteria."

    lines: List[str] = [
        border_char * width,
        f" {title} ",
        border_char * width,
        f"{'TSR ID':<22} {'RESTRICTION NAME':<30} {'MAX SPEED':<12} {'CORRIDOR':<24}",
        divider_char * width,
    ]

    for tsr in filtered_tsrs:
        tid = getattr(tsr, "id", "")
        name = getattr(tsr, "name", "")
        spd = getattr(tsr, "max_speed_kmh", 0.0)
        corr = getattr(tsr, "corridor_id", "")
        lines.append(
            f"{tid[:21]:<22} {name[:29]:<30} {spd:5.1f} km/h  {corr[:23]:<24}"
        )
        reason = getattr(tsr, "reason", "")
        if reason:
            lines.append(f"  Reason: {reason}")

    lines.append(divider_char * width)
    lines.append(f"Total Active Slow Orders: {len(filtered_tsrs)}")
    lines.append(border_char * width)
    return "\n".join(lines)


def format_sensor_health_table(
    assessments: Sequence[Tuple[Any, Any]],
    alerts_only: bool = False,
    corridor_filter: Optional[str] = None,
    ascii_mode: bool = False,
    title: str = "HELLENIC RAILWAYS - FLEET TELEMETRY & SENSOR HEALTH (F-5.2)",
) -> str:
    """Format fleet telemetry and sensor health records into an aligned table.

    Args:
        assessments: Sequence of tuples of (train_position_or_record, SensorHealthAssessment).
        alerts_only: If True, only include trains with alerts (FROZEN, DROPPED, STALE).
        corridor_filter: Optional corridor ID or substring filter.
        ascii_mode: Whether to force 7-bit ASCII characters.
        title: Table header title.

    Returns:
        Formatted multi-line table string.
    """
    width = 98
    border_char = "=" if ascii_mode else "═"
    divider_char = "-" if ascii_mode else "─"

    norm_filter = corridor_filter.strip().lower() if corridor_filter else None

    filtered = []
    for train, health in assessments:
        pos = getattr(train, "position", train)
        corridor = getattr(pos, "corridor", "") or ""
        train_no = str(getattr(pos, "train_number", "") or getattr(pos, "id", "") or "")

        if norm_filter and norm_filter not in corridor.lower() and norm_filter not in train_no.lower():
            continue

        st_val = getattr(health.state, "value", str(health.state))
        if alerts_only and not getattr(health, "is_alert", False) and st_val not in ("FROZEN", "DROPPED", "STALE"):
            continue

        filtered.append((pos, health))

    if not filtered:
        return "No train telemetry sensor records matching filter criteria."

    lines: List[str] = [
        border_char * width,
        f" {title} ",
        border_char * width,
        f"{'TRAIN':<8} {'TYPE':<6} {'SPEED':<10} {'STATUS':<6} {'STATE':<16} {'AGE':<10} {'DETAILS':<36}",
        divider_char * width,
    ]

    for pos, health in filtered:
        train_no = str(getattr(pos, "train_number", None) or getattr(pos, "id", "") or "N/A")[:7]
        train_type = str(getattr(pos, "train_type", None) or "IC")[:5]
        spd = float(getattr(pos, "speed", 0.0) or 0.0)
        spd_str = f"{spd:5.1f} km/h"
        st_code = getattr(health, "short_code", "RUN")
        state_name = getattr(health.state, "value", str(health.state))[:15]
        age_str = f"{int(getattr(health, 'elapsed_sec', 0.0))}s ago"
        details = str(getattr(health, "description", ""))[:35]

        lines.append(
            f"{train_no:<8} {train_type:<6} {spd_str:<10} {st_code:<6} {state_name:<16} {age_str:<10} {details:<36}"
        )

    lines.append(divider_char * width)
    alert_count = sum(
        1 for _, h in filtered
        if getattr(h, "is_alert", False) or getattr(h.state, "value", str(h.state)) in ("FROZEN", "DROPPED")
    )
    stale_count = sum(
        1 for _, h in filtered
        if getattr(h.state, "value", str(h.state)) == "STALE"
    )
    lines.append(f"Total Trains: {len(filtered)} | Critical Alerts: {alert_count} | Stale Warnings: {stale_count}")
    lines.append(border_char * width)
    return "\n".join(lines)

