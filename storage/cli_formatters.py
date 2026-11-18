"""
Terminal output formatters and text table renderers for railmon CLI commands.
Zero external dependencies; pure Python standard library.
"""

from typing import Any, Dict, List, Optional

from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
)


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
