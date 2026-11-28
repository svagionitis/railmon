"""Atomic GTFS-RT file writer, SQLite historical exporter, and protobuf inspector."""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gtfs_rt.mapper import GtfsRtMapper
from gtfs_rt.models import FeedKind, FeedMessage, OutputFormat
from storage.db import Database
from storage.repository import HistoricalRepository, validate_export_path
from train_tracker import TrainPosition
from tui.state import StateSnapshot

logger = logging.getLogger("railmon.gtfs_rt.exporter")

MAX_INSPECT_BYTES: int = 16 * 1024 * 1024  # 16 MiB defensive cap

FEED_STEMS: Dict[FeedKind, str] = {
    FeedKind.COMBINED: "feed",
    FeedKind.VEHICLES: "vehicle_positions",
    FeedKind.TRIPS: "trip_updates",
    FeedKind.ALERTS: "alerts",
}


def serialize_feed(msg: FeedMessage, fmt: OutputFormat) -> bytes:
    """Serialize FeedMessage to binary protobuf or UTF-8 ProtoJSON bytes."""
    if fmt == OutputFormat.JSON:
        return json.dumps(msg.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    return msg.to_proto()


def write_atomic_bytes(path: str, payload: bytes) -> str:
    """Validate destination path and write bytes atomically via mkstemp + Windows-safe os.replace."""
    import sys
    import tempfile

    abs_path = validate_export_path(path)
    parent_dir = os.path.dirname(abs_path) or "."
    base_name = os.path.basename(abs_path)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{base_name}.", suffix=".tmp", dir=parent_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                logger.debug("os.fsync unsupported on target filesystem for %s", tmp_path)

        max_attempts = 5 if sys.platform == "win32" else 1
        for attempt in range(max_attempts):
            try:
                os.replace(tmp_path, abs_path)
                break
            except PermissionError:
                if attempt + 1 >= max_attempts:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                logger.debug("Failed removing temporary file %s", tmp_path)
        raise
    return abs_path


def export_feed_file(
    msg: FeedMessage,
    out_path: str,
    fmt: OutputFormat = OutputFormat.PROTOBUF,
) -> str:
    """Serialize a FeedMessage and write it atomically to out_path."""
    payload = serialize_feed(msg, fmt)
    return write_atomic_bytes(out_path, payload)


def _validate_out_dir(out_dir: str) -> str:
    """Validate target directory for split GTFS-RT feed bundle export."""
    if not out_dir or not out_dir.strip():
        raise ValueError("Output directory path cannot be empty")
    raw = out_dir.strip()
    if "\0" in raw:
        raise ValueError("Output directory contains null bytes")
    probe = os.path.join(raw, "feed.pb")
    abs_probe = validate_export_path(probe)
    return os.path.dirname(abs_probe)


def export_feed_dir(
    mapper: GtfsRtMapper,
    out_dir: str,
    fmt: OutputFormat = OutputFormat.PROTOBUF,
    snapshot: Optional[StateSnapshot] = None,
    positions: Optional[Sequence[TrainPosition]] = None,
    schedules: Optional[Dict[str, Dict[str, Any]]] = None,
    now: Optional[float] = None,
) -> List[str]:
    """Export all 4 GTFS-RT feed variants atomically into a validated directory."""
    clean_dir = _validate_out_dir(out_dir)
    if snapshot is None and positions is None:
        raise ValueError(
            "export_feed_dir requires either 'snapshot' or 'positions' to be provided"
        )

    resolved_now = now if now is not None else time.time()
    ext = fmt.value
    written: List[str] = []

    for kind, stem in FEED_STEMS.items():
        if snapshot is not None:
            msg = mapper.build_feed(snapshot, feed_kind=kind, now=resolved_now)
        else:
            msg = mapper.build_from_positions(
                positions=positions or (),
                feed_kind=kind,
                now=resolved_now,
                schedules=schedules,
            )
        target = os.path.join(clean_dir, f"{stem}.{ext}")
        written.append(export_feed_file(msg, target, fmt))
    return written


def load_db_snapshot(
    db_path: str,
    at_time: Optional[str] = None,
    window_sec: int = 120,
) -> Tuple[List[TrainPosition], Dict[str, Dict[str, Any]], Optional[float]]:
    """Load point-in-time positions, schedules, and reference epoch from SQLite WAL storage."""
    if not db_path or not os.path.isfile(db_path):
        raise FileNotFoundError(f"SQLite database file not found: '{db_path}'")

    db = Database(db_path, auto_migrate=False)
    try:
        repo = HistoricalRepository(db)
        positions, schedules, snap_epoch = repo.get_gtfs_snapshot(
            at_time=at_time,
            window_sec=window_sec,
        )
    finally:
        db.close()

    ref_ts = snap_epoch if snap_epoch > 0.0 else None
    return positions, schedules, ref_ts


def export_from_db(
    db_path: str,
    mapper: GtfsRtMapper,
    feed_kind: FeedKind = FeedKind.COMBINED,
    at_time: Optional[str] = None,
    window_sec: int = 120,
) -> FeedMessage:
    """Build a GTFS-RT FeedMessage from a recorded SQLite WAL database snapshot."""
    positions, schedules, ref_ts = load_db_snapshot(
        db_path=db_path,
        at_time=at_time,
        window_sec=window_sec,
    )
    return mapper.build_from_positions(
        positions=positions,
        feed_kind=feed_kind,
        now=ref_ts,
        schedules=schedules,
    )


def inspect_pb_file(path: str) -> Dict[str, Any]:
    """Decode an existing binary GTFS-RT .pb file and return ProtoJSON + summary."""
    if not path or not path.strip():
        raise ValueError("Inspect file path cannot be empty")
    abs_path = os.path.abspath(path.strip())
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"GTFS-RT protobuf file not found: '{abs_path}'")

    with open(abs_path, "rb") as fh:
        raw = fh.read(MAX_INSPECT_BYTES + 1)
    if len(raw) > MAX_INSPECT_BYTES:
        raise ValueError(
            f"GTFS-RT file exceeds maximum allowed inspection size ({MAX_INSPECT_BYTES} bytes)"
        )

    msg = FeedMessage.from_proto(raw)
    vp_count = sum(1 for e in msg.entity if e.vehicle is not None)
    tu_count = sum(1 for e in msg.entity if e.trip_update is not None)
    al_count = sum(1 for e in msg.entity if e.alert is not None)

    return {
        "file": abs_path,
        "byteSize": len(raw),
        "summary": {
            "gtfsRealtimeVersion": msg.header.gtfs_realtime_version,
            "incrementality": msg.header.incrementality.name,
            "timestamp": msg.header.timestamp,
            "entityCount": len(msg.entity),
            "vehiclePositions": vp_count,
            "tripUpdates": tu_count,
            "alerts": al_count,
        },
        "feed": msg.to_dict(),
    }
