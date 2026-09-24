"""每日通道有效率报表。

缺测判定基于通道登记的采样间隔在本地自然日内铺设的期望槽位（半开区间）。
连续缺测槽位合并为缺测区间，并按以下确定性顺序归因：

1. 与 ``as_of`` 时刻已审批维护窗口的并集相交的部分 → ``maintenance``，
   补偿来源逐条列出（窗口 uid/版本/审批人）；重叠窗口先并集合并，
   并集段内只要有一个窗口为 ``imputed`` 即按插补计为有效，否则整段剔除。
2. 其余缺测段，时长不超过通道短暂断链阈值 → ``brief_dropout``，插补计为有效。
3. 其余 → ``offline``，是真正的长时间离线，拉低有效率并触发巡检。

跨本地午夜的缺测区间会向相邻日各延展一天查找最近心跳，因此跨日维护与跨日
断链的归因不受日界切割影响。报表按 ``as_of`` 选取当时生效的窗口版本重算，
并冻结为快照；原始心跳与观测永不改写。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.errors import NotFoundError
from app.database import get_connection, transaction
from app.seismic.timeutil import day_bounds, parse_instant, utc_iso, utc_stamp
from app.seismic.windows import MaintenanceWindowService, effective_windows

_SLOT_LOOKBACK_DAYS = 1


def _intersect(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> tuple[datetime, datetime] | None:
    start = max(start_a, start_b)
    end = min(end_a, end_b)
    return (start, end) if start < end else None


def _merge_union(intervals: list[tuple[datetime, datetime, dict[str, Any]]]) -> list[tuple[datetime, datetime, list[dict[str, Any]]]]:
    """把带来源的区间合并为不相交并集；同一并集段携带全部来源。"""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime, list[dict[str, Any]]]] = []
    current_start, current_end, sources = ordered[0][0], ordered[0][1], [ordered[0][2]]
    for start, end, source in ordered[1:]:
        if start <= current_end:
            if end > current_end:
                current_end = end
            if source not in sources:
                sources.append(source)
        else:
            merged.append((current_start, current_end, sources))
            current_start, current_end, sources = start, end, [source]
    merged.append((current_start, current_end, sources))
    return merged


class UptimeReportService:
    def __init__(self, connection=None) -> None:
        self.connection = connection or get_connection()
        self.channels = MaintenanceWindowService(self.connection)

    def _heartbeats(self, station_code: str, channel: str, start: datetime, end: datetime) -> set[datetime]:
        rows = self.connection.execute(
            "SELECT observed_at FROM seismic_channel_heartbeats "
            "WHERE station_code=? AND channel=? AND observed_at>=? AND observed_at<?",
            (station_code, channel, utc_iso(start), utc_iso(end)),
        ).fetchall()
        return {datetime.fromisoformat(row["observed_at"]).astimezone(UTC) for row in rows}

    def daily_report(
        self,
        station_code: str,
        channel: str,
        report_date: str,
        utc_offset: str = "+00:00",
        as_of: str | datetime | None = None,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        station_code = station_code.strip().upper()
        channel = channel.strip().upper()
        config = self.channels.get_channel(station_code, channel)
        interval = int(config["sample_interval_seconds"])
        brief_limit = int(config["brief_dropout_seconds"])
        day_start, day_end, zone = day_bounds(report_date, utc_offset)
        if as_of is None:
            as_of_dt = datetime.now(UTC)
        elif isinstance(as_of, str):
            as_of_dt = parse_instant(as_of, "as_of")
        else:
            as_of_dt = as_of.astimezone(UTC)

        window_rows = effective_windows(self.connection, station_code, channel, as_of_dt)
        window_segments = [
            (
                datetime.fromisoformat(row["start_at"]).astimezone(UTC),
                datetime.fromisoformat(row["end_at"]).astimezone(UTC),
                {
                    "uid": row["uid"],
                    "version": row["version"],
                    "reason": row["reason"],
                    "compensation": row["compensation"],
                    "approver": self._approver(row["uid"], row["version"]),
                },
            )
            for row in window_rows
        ]
        union = _merge_union(window_segments)

        pad = timedelta(days=_SLOT_LOOKBACK_DAYS)
        grid_start = day_start - pad
        grid_end = day_end + pad
        slots_total = int((grid_end - grid_start).total_seconds()) // interval
        present = self._heartbeats(station_code, channel, grid_start, grid_end)
        slots_per_day = int((day_end - day_start).total_seconds()) // interval
        present_in_day = sum(1 for point in present if day_start <= point < day_end)

        # 逐槽位标注：缺测槽位按其起点时刻是否落入已审批窗口并集归类。
        # 窗口边界切在槽位中间时，以槽位起点是否落在 [窗口起, 窗口止) 为准，结果确定。
        # 分组键 (类别, 补偿方式)；有心跳的槽位键为 None，用来打断连续分组。
        groups: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for index in range(slots_total):
            point = grid_start + timedelta(seconds=index * interval)
            if point in present:
                key: tuple[str, ...] | None = None
                sources_here: list[dict[str, Any]] = []
            else:
                batches = [sources for seg_start, seg_end, sources in union if seg_start <= point < seg_end]
                sources_here = [item for batch in batches for item in batch]
                if sources_here:
                    compensation = "imputed" if any(item["compensation"] == "imputed" for item in sources_here) else "excluded"
                    key = ("maintenance", compensation)
                else:
                    key = ("gap", "")
            slot_end = point + timedelta(seconds=interval)
            if key is None:
                current = None
                continue
            if current is not None and current["key"] == key:
                current["end"] = slot_end
                for item in sources_here:
                    if item not in current["sources"]:
                        current["sources"].append(item)
            else:
                current = {"key": key, "start": point, "end": slot_end, "sources": list(sources_here)}
                groups.append(current)

        expected = slots_per_day
        segments: list[dict[str, Any]] = []
        offline_slots = 0
        maintenance_excluded_slots = 0
        maintenance_imputed_slots = 0
        brief_slots = 0

        for group in groups:
            clipped = _intersect(group["start"], group["end"], day_start, day_end)
            if clipped is None:
                continue
            clip_start, clip_end = clipped
            slots = int(round((clip_end - clip_start).total_seconds() / interval))
            crosses = group["start"] < day_start or group["end"] > day_end
            if group["key"][0] == "maintenance":
                compensation = group["key"][1]
                reason = "maintenance"
                if compensation == "imputed":
                    maintenance_imputed_slots += slots
                else:
                    maintenance_excluded_slots += slots
            else:
                touches_grid_edge = group["start"] <= grid_start or group["end"] >= grid_end
                duration = (group["end"] - group["start"]).total_seconds()
                if not touches_grid_edge and brief_limit and duration <= brief_limit:
                    reason, compensation = "brief_dropout", "imputed"
                    brief_slots += slots
                else:
                    reason, compensation = "offline", "none"
                    offline_slots += slots
            segments.append({
                "start_at": utc_iso(clip_start),
                "end_at": utc_iso(clip_end),
                "missing_slots": slots,
                "duration_seconds": int((clip_end - clip_start).total_seconds()),
                "reason": reason,
                "compensation": compensation,
                "crosses_day_boundary": crosses,
                "sources": group["sources"] if reason == "maintenance" else [],
            })

        denominator = expected - maintenance_excluded_slots
        available = present_in_day + maintenance_imputed_slots + brief_slots
        availability_rate = round(available / denominator, 6) if denominator > 0 else 1.0
        heartbeat_digest = hashlib.sha256(
            json.dumps(sorted(utc_iso(point) for point in present), ensure_ascii=False).encode()
        ).hexdigest()
        window_versions = [
            {"uid": row["uid"], "version": row["version"], "start_at": row["start_at"], "end_at": row["end_at"],
             "reason": row["reason"], "compensation": row["compensation"], "approver": self._approver(row["uid"], row["version"])}
            for row in window_rows
        ]
        window_digest = hashlib.sha256(
            json.dumps(window_versions, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        observations_digest = hashlib.sha256(f"{heartbeat_digest}:{window_digest}:{interval}".encode()).hexdigest()

        segments.sort(key=lambda item: (item["start_at"], item["end_at"]))
        report = {
            "station_code": station_code,
            "channel": channel,
            "report_date": report_date,
            "utc_offset": utc_offset,
            "day_start_utc": utc_iso(day_start),
            "day_end_utc": utc_iso(day_end),
            "as_of": utc_stamp(as_of_dt),
            "timezone_note": "day_start_utc/day_end_utc/as_of 均为 UTC；utc_offset 定义本地日界",
            "sample_interval_seconds": interval,
            "brief_dropout_seconds": brief_limit,
            "expected_slots": expected,
            "present_slots": present_in_day,
            "maintenance_excluded_slots": maintenance_excluded_slots,
            "maintenance_imputed_slots": maintenance_imputed_slots,
            "brief_dropout_slots": brief_slots,
            "offline_slots": offline_slots,
            "availability_rate": availability_rate,
            "inspection_required": offline_slots > 0,
            "segments": segments,
            "window_versions": window_versions,
            "observations_digest": observations_digest,
        }
        if persist:
            self._freeze(report)
        return report

    def _approver(self, uid: str, version: int) -> str:
        row = self.connection.execute(
            "SELECT approver FROM seismic_maintenance_approvals WHERE uid=? AND version=? AND decision='approved'",
            (uid, version),
        ).fetchone()
        return row["approver"] if row else ""

    def _freeze(self, report: dict[str, Any]) -> None:
        now = utc_iso(datetime.now(UTC))
        with transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO seismic_uptime_reports(station_code,channel,report_date,utc_offset,as_of,"
                "window_versions_json,observations_digest,availability_rate,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (report["station_code"], report["channel"], report["report_date"], report["utc_offset"],
                 report["as_of"], json.dumps(report["window_versions"], ensure_ascii=False, sort_keys=True),
                 report["observations_digest"], report["availability_rate"],
                 json.dumps(report, ensure_ascii=False), now),
            )

    def frozen_report(
        self,
        station_code: str,
        channel: str,
        report_date: str,
        utc_offset: str,
        as_of: str,
    ) -> dict[str, Any]:
        as_of_text = utc_stamp(parse_instant(as_of, "as_of"))
        row = self.connection.execute(
            "SELECT payload_json FROM seismic_uptime_reports "
            "WHERE station_code=? AND channel=? AND report_date=? AND utc_offset=? AND as_of=?",
            (station_code.strip().upper(), channel.strip().upper(), report_date, utc_offset, as_of_text),
        ).fetchone()
        if row is None:
            raise NotFoundError("该版本的报表快照不存在，请先用相同 as_of 重算")
        return json.loads(row["payload_json"])

    def frozen_versions(self, station_code: str, channel: str, report_date: str, utc_offset: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT as_of,availability_rate,observations_digest,created_at FROM seismic_uptime_reports "
            "WHERE station_code=? AND channel=? AND report_date=? AND utc_offset=? ORDER BY as_of",
            (station_code.strip().upper(), channel.strip().upper(), report_date, utc_offset),
        ).fetchall()
        return [dict(row) for row in rows]
