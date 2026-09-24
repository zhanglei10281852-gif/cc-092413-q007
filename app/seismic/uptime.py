"""台站/通道维护窗口与连续缺测区间计算。

时间存储约定
------------
所有入库时间统一规范化为 UTC（ISO 8601，``+00:00``），上报方携带的
原始偏移量单独保存在 ``*_offset_minutes`` 列中。报表按台站本地“某日”
（请求携带偏移）换算成 UTC 的半开区间 ``[day_start, day_end)``，因此
跨午夜维护窗口与短暂断链都能被确定性切分。

版本约定
--------
维护窗口每次创建、修改、审批、取消都只追加一个版本快照，原始观测
(``seismic_availability_samples``) 永不被改写。报表可携带 ``as_of``
按历史版本重算；每次计算落一条不可变的报表运行记录。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.seismic.intervals import intersect_intervals, merge_intervals, subtract_intervals, total_length
from app.seismic.timeparse import format_offset, parse_aware, parse_aware_with_offset

# 短于该时长的非计划缺标记为“短暂断链”，达到或超过则为“长时间离线”。
BRIEF_GAP_SECONDS = 300
DEFAULT_PERIOD_SECONDS = 60

WINDOW_REASONS = {"planned_maintenance", "telecom_outage", "power_outage", "instrument_fault", "other"}
COMPENSATIONS = {"excluded", "backfill", "interpolated", "none"}
WINDOW_STATUSES = {"draft", "approved", "cancelled"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS seismic_station_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_code TEXT NOT NULL,
    channel TEXT NOT NULL,
    period_seconds INTEGER NOT NULL DEFAULT 60,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(station_code, channel)
);
CREATE TABLE IF NOT EXISTS seismic_availability_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_code TEXT NOT NULL,
    channel TEXT NOT NULL,
    observed_utc TEXT NOT NULL,
    utc_offset_minutes INTEGER NOT NULL DEFAULT 0,
    is_present INTEGER NOT NULL DEFAULT 1 CHECK(is_present IN (0,1)),
    source_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(station_code, channel, observed_utc)
);
CREATE INDEX IF NOT EXISTS idx_samples_span
    ON seismic_availability_samples(station_code, channel, observed_utc);
CREATE TABLE IF NOT EXISTS seismic_maintenance_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_code TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '*',
    start_utc TEXT NOT NULL,
    end_utc TEXT,
    start_offset_minutes INTEGER NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL CHECK(reason_code IN ('planned_maintenance','telecom_outage','power_outage','instrument_fault','other')),
    reason_detail TEXT NOT NULL DEFAULT '',
    compensation TEXT NOT NULL DEFAULT 'none' CHECK(compensation IN ('excluded','backfill','interpolated','none')),
    ticket TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','cancelled')),
    approved_by TEXT NOT NULL DEFAULT '',
    client_token TEXT NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_windows_scope
    ON seismic_maintenance_windows(station_code, channel, start_utc, end_utc);
CREATE TABLE IF NOT EXISTS seismic_maintenance_window_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES seismic_maintenance_windows(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(window_id, version)
);
CREATE TABLE IF NOT EXISTS seismic_report_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_code TEXT NOT NULL,
    channel TEXT NOT NULL,
    local_date TEXT NOT NULL,
    offset_minutes INTEGER NOT NULL,
    period_seconds INTEGER NOT NULL,
    as_of_utc TEXT NOT NULL,
    window_signature TEXT NOT NULL,
    window_versions_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(station_code, channel, local_date, offset_minutes, period_seconds, as_of_utc)
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _audit_stamp(value: datetime | None = None) -> str:
    # 版本/审批时间戳使用微秒精度，避免同一秒内多次修改导致 as_of 重算歧义。
    return (value or _now()).astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_day(local_date: str, offset_minutes: int) -> tuple[datetime, datetime]:
    """台站本地日期 + 偏移 -> UTC 半开区间 [start, end)。"""
    try:
        local_day = datetime.strptime(local_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError("date 必须是 YYYY-MM-DD 格式") from exc
    tz = timezone(timedelta(minutes=offset_minutes))
    start_local = local_day.replace(tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc)
    return start_utc, start_utc + timedelta(days=1)


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


class UptimeService:
    """维护窗口版本管理、观测接收与日报表计算。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 观测

    def ingest_samples(self, station_code: str, channel: str, samples: list[dict[str, Any]], period_seconds: int | None = None) -> dict[str, Any]:
        """接收一批带时区偏移的观测/心跳时间，重复上报按 (台站,通道,UTC时刻) 去重。"""
        station_code = station_code.strip().upper()
        channel = channel.strip().upper()
        if not samples:
            raise ValidationError("samples 不能为空")
        now = _now()
        accepted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in samples:
            observed, reported_offset = parse_aware_with_offset(item["observed_at"])
            observed_utc = _stamp(observed)
            if observed_utc in seen:
                continue
            seen.add(observed_utc)
            accepted.append(
                {
                    "observed_utc": observed_utc,
                    "offset_minutes": reported_offset,
                    "is_present": 1 if item.get("is_present", True) else 0,
                    "source_hash": hashlib.sha256(
                        json.dumps({"t": observed_utc, "p": item.get("is_present", True)}, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
        accepted.sort(key=lambda row: row["observed_utc"])
        period = int(period_seconds or DEFAULT_PERIOD_SECONDS)
        with transaction(immediate=True) as connection:
            registry = connection.execute(
                "SELECT id FROM seismic_station_channels WHERE station_code=? AND channel=?",
                (station_code, channel),
            ).fetchone()
            if registry is None:
                connection.execute(
                    "INSERT INTO seismic_station_channels(station_code,channel,period_seconds,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    (station_code, channel, period, accepted[0]["observed_utc"], accepted[-1]["observed_utc"]),
                )
            else:
                connection.execute(
                    "UPDATE seismic_station_channels SET period_seconds=COALESCE(?,period_seconds), last_seen_at=? WHERE station_code=? AND channel=?",
                    (period if period_seconds else None, accepted[-1]["observed_utc"], station_code, channel),
                )
            inserted = 0
            for row in accepted:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO seismic_availability_samples"
                    "(station_code,channel,observed_utc,utc_offset_minutes,is_present,source_hash,received_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (station_code, channel, row["observed_utc"], row["offset_minutes"], row["is_present"], row["source_hash"], _stamp(now)),
                )
                inserted += cursor.rowcount
            return {
                "station_code": station_code,
                "channel": channel,
                "received": len(samples),
                "inserted": inserted,
                # 重复包括批内重复时刻与库内既有时刻，重复上报不产生新数据。
                "duplicates": len(samples) - inserted,
                "first_observed_utc": accepted[0]["observed_utc"],
                "last_observed_utc": accepted[-1]["observed_utc"],
            }

    def list_channels(self, station_code: str) -> list[dict[str, Any]]:
        station_code = station_code.strip().upper()
        rows = self.connection.execute(
            "SELECT station_code,channel,period_seconds,first_seen_at,last_seen_at"
            " FROM seismic_station_channels WHERE station_code=? ORDER BY channel",
            (station_code,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 维护窗口

    def _window_public(self, row: sqlite3.Row, *, include_version: int | None = None) -> dict[str, Any]:
        data = dict(row)
        version = include_version if include_version is not None else data.get("current_version", 1)
        data["version"] = version
        data["start_offset"] = format_offset(data.get("start_offset_minutes", 0))
        return data

    def _snapshot(self, window_id: int, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("维护窗口不存在")
        return dict(row)

    def _record_version(self, connection: sqlite3.Connection, window_id: int, action: str, actor: str, at: datetime) -> int:
        snapshot = self._snapshot(window_id, connection)
        version = snapshot["current_version"]
        connection.execute(
            "INSERT INTO seismic_maintenance_window_versions(window_id,version,action,snapshot_json,actor,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (window_id, version, action, json.dumps(snapshot, ensure_ascii=False), actor, _audit_stamp(at)),
        )
        return version

    def _assert_no_overlap(self, connection: sqlite3.Connection, payload: dict[str, Any], *, exclude_id: int | None = None) -> None:
        """同一作用域（台站+通道，'*' 为台站级）窗口时间重叠被确定性拒绝。

        台站级 ('*') 与通道级窗口允许并存，报表合并时按区间并集处理，
        通道级窗口在原因标注上优先——两层规则都不会产生歧义结果。
        """
        rows = connection.execute(
            "SELECT * FROM seismic_maintenance_windows WHERE station_code=? AND channel=? AND status!='cancelled'",
            (payload["station_code"], payload["channel"]),
        ).fetchall()
        new_start = payload["start_utc"]
        new_end = payload["end_utc"]
        for row in rows:
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            other_end = row["end_utc"]
            # 半开区间 [start, end)；None 表示开放结束（延续至无穷）。
            if new_end is None:
                overlapping = other_end is None or other_end > new_start
            elif other_end is None:
                overlapping = new_end > row["start_utc"]
            else:
                overlapping = new_start < other_end and row["start_utc"] < new_end
            if overlapping:
                raise ConflictError(
                    "维护窗口与同作用域既有窗口重叠",
                    context={"conflicting_window_id": row["id"], "start_utc": row["start_utc"], "end_utc": row["end_utc"]},
                )

    def create_window(self, payload: dict[str, Any], actor: str = "operator") -> dict[str, Any]:
        station_code = payload["station_code"].strip().upper()
        channel = (payload.get("channel") or "*").strip().upper()
        start, start_offset = parse_aware_with_offset(payload["start_at"])
        end = parse_aware(payload["end_at"]) if payload.get("end_at") else None
        if end is not None and end <= start:
            raise ValidationError("end_at 必须晚于 start_at")
        reason_code = payload["reason_code"]
        compensation = payload.get("compensation", "none")
        status = payload.get("status", "draft")
        approved_by = payload.get("approved_by", "") or actor
        if reason_code not in WINDOW_REASONS:
            raise ValidationError(f"reason_code 必须是 {sorted(WINDOW_REASONS)} 之一")
        if compensation not in COMPENSATIONS:
            raise ValidationError(f"compensation 必须是 {sorted(COMPENSATIONS)} 之一")
        if status not in WINDOW_STATUSES or status == "cancelled":
            raise ValidationError("创建时 status 只能是 draft 或 approved")
        if compensation != "none" and status != "approved":
            raise ValidationError("只有经过审批 (approved) 的窗口才能携带补偿标记")

        stored = {
            "station_code": station_code,
            "channel": channel,
            "start_utc": _stamp(start),
            "end_utc": _stamp(end) if end else None,
            "start_offset_minutes": start_offset,
            "reason_code": reason_code,
            "reason_detail": payload.get("reason_detail", ""),
            "compensation": compensation,
            "ticket": payload.get("ticket", ""),
            "status": status,
            "approved_by": approved_by if status == "approved" else "",
            "client_token": payload.get("client_token", ""),
        }
        now = _now()
        with transaction(immediate=True) as connection:
            if stored["client_token"]:
                duplicate = connection.execute(
                    "SELECT * FROM seismic_maintenance_windows WHERE client_token=?",
                    (stored["client_token"],),
                ).fetchone()
                if duplicate is not None:
                    return self._window_public(duplicate)
            self._assert_no_overlap(connection, stored)
            cursor = connection.execute(
                "INSERT INTO seismic_maintenance_windows"
                "(station_code,channel,start_utc,end_utc,start_offset_minutes,reason_code,reason_detail,"
                "compensation,ticket,status,approved_by,client_token,current_version,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (
                    stored["station_code"], stored["channel"], stored["start_utc"], stored["end_utc"],
                    stored["start_offset_minutes"], stored["reason_code"], stored["reason_detail"],
                    stored["compensation"], stored["ticket"], stored["status"], stored["approved_by"],
                    stored["client_token"], actor, _audit_stamp(now), _audit_stamp(now),
                ),
            )
            window_id = cursor.lastrowid
            self._record_version(connection, window_id, "create", actor, now)
            row = connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone()
            return self._window_public(row)

    def get_window(self, window_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("维护窗口不存在")
        return self._window_public(row)

    def list_window_versions(self, window_id: int) -> list[dict[str, Any]]:
        self.get_window(window_id)
        rows = self.connection.execute(
            "SELECT id,window_id,version,action,actor,created_at,snapshot_json"
            " FROM seismic_maintenance_window_versions WHERE window_id=? ORDER BY version",
            (window_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_windows(self, station_code: str, channel: str | None = None, *, as_of: datetime | None = None) -> list[dict[str, Any]]:
        station_code = station_code.strip().upper()
        if as_of is None:
            sql = "SELECT * FROM seismic_maintenance_windows WHERE station_code=?"
            params: tuple[Any, ...] = (station_code,)
            if channel:
                # 指定通道时同时返回台站级 ('*') 窗口，口径与报表一致。
                sql += " AND channel IN (?, '*')"
                params = (station_code, channel.strip().upper())
            sql += " ORDER BY start_utc,id"
            return [self._window_public(row) for row in self.connection.execute(sql, params).fetchall()]
        return self._windows_as_of(station_code, channel, as_of)

    def _windows_as_of(self, station_code: str, channel: str | None, as_of: datetime) -> list[dict[str, Any]]:
        """按 as_of 时刻重建窗口状态：取每个窗口 created_at<=as_of 的最新版本。"""
        stamp = _audit_stamp(as_of)  # 微秒精度，保证与版本时间戳字典序可比
        rows = self.connection.execute(
            "SELECT v.* FROM seismic_maintenance_window_versions v JOIN seismic_maintenance_windows w ON w.id=v.window_id"
            " WHERE w.station_code=? AND v.created_at<=?"
            " AND v.version=(SELECT MAX(version) FROM seismic_maintenance_window_versions WHERE window_id=v.window_id AND created_at<=?)"
            " ORDER BY v.id",
            (station_code, stamp, stamp),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            if channel is not None and snapshot["channel"] not in (channel.strip().upper(), "*"):
                continue
            snapshot["version"] = snapshot["current_version"]
            snapshot["start_offset"] = format_offset(snapshot.get("start_offset_minutes", 0))
            result.append(snapshot)
        return result

    def update_window(self, window_id: int, payload: dict[str, Any], actor: str = "operator") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone()
            if row is None:
                raise NotFoundError("维护窗口不存在")
            current = dict(row)
            if current["status"] == "cancelled":
                raise ConflictError("已取消的窗口不能再修改")
            updated = dict(current)
            for field in ("reason_detail", "ticket"):
                if payload.get(field) is not None:
                    updated[field] = payload[field]
            if payload.get("reason_code") is not None:
                if payload["reason_code"] not in WINDOW_REASONS:
                    raise ValidationError("非法 reason_code")
                updated["reason_code"] = payload["reason_code"]
            if payload.get("compensation") is not None:
                if payload["compensation"] not in COMPENSATIONS:
                    raise ValidationError("非法 compensation")
                updated["compensation"] = payload["compensation"]
            if payload.get("end_at") is not None:
                updated["end_utc"] = _stamp(parse_aware(payload["end_at"]))
            if payload.get("start_at") is not None:
                start, start_offset = parse_aware_with_offset(payload["start_at"])
                updated["start_utc"] = _stamp(start)
                updated["start_offset_minutes"] = start_offset
            if updated["end_utc"] is not None and updated["end_utc"] <= updated["start_utc"]:
                raise ValidationError("end_at 必须晚于 start_at")
            if updated["compensation"] != "none" and updated["status"] != "approved":
                raise ValidationError("只有经过审批 (approved) 的窗口才能携带补偿标记")
            if updated == current:
                return self._window_public(row)  # 空修改不落新版本
            self._assert_no_overlap(connection, updated, exclude_id=window_id)
            connection.execute(
                "UPDATE seismic_maintenance_windows SET start_utc=?,end_utc=?,start_offset_minutes=?,"
                "reason_code=?,reason_detail=?,compensation=?,ticket=?,"
                "current_version=current_version+1,updated_at=? WHERE id=?",
                (
                    updated["start_utc"], updated["end_utc"], updated["start_offset_minutes"],
                    updated["reason_code"], updated["reason_detail"], updated["compensation"],
                    updated["ticket"], _audit_stamp(now), window_id,
                ),
            )
            self._record_version(connection, window_id, "update", actor, now)
            return self._window_public(connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone())

    def approve_window(self, window_id: int, actor: str, approved_by: str | None = None) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone()
            if row is None:
                raise NotFoundError("维护窗口不存在")
            if dict(row)["status"] == "cancelled":
                raise ConflictError("已取消的窗口不能审批")
            if dict(row)["status"] == "approved":
                return self._window_public(row)  # 重复审批幂等返回
            approver = approved_by or actor
            connection.execute(
                "UPDATE seismic_maintenance_windows SET status='approved',approved_by=?,"
                "current_version=current_version+1,updated_at=? WHERE id=?",
                (approver, _audit_stamp(now), window_id),
            )
            self._record_version(connection, window_id, "approve", actor, now)
            return self._window_public(connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone())

    def cancel_window(self, window_id: int, actor: str = "operator") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone()
            if row is None:
                raise NotFoundError("维护窗口不存在")
            if dict(row)["status"] == "cancelled":
                return self._window_public(row)
            connection.execute(
                "UPDATE seismic_maintenance_windows SET status='cancelled',current_version=current_version+1,updated_at=? WHERE id=?",
                (_audit_stamp(now), window_id),
            )
            self._record_version(connection, window_id, "cancel", actor, now)
            return self._window_public(connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (window_id,)).fetchone())

    # -------------------------------------------------------------- 日报表

    def _missing_ticks(
        self,
        station_code: str,
        channel: str,
        day_start: datetime,
        day_end: datetime,
        period_seconds: int,
    ) -> list[int]:
        """整日 [day_start, day_end) 内没有观测命中的刻度；从前一晚延续到
        凌晨的离线也必须计入，因此期望域恒为完整本地日。"""
        rows = self.connection.execute(
            "SELECT observed_utc FROM seismic_availability_samples"
            " WHERE station_code=? AND channel=? AND is_present=1 AND observed_utc>=? AND observed_utc<?"
            " ORDER BY observed_utc",
            (station_code, channel, _stamp(day_start), _stamp(day_end)),
        ).fetchall()
        tick_count = int((day_end - day_start).total_seconds() // period_seconds)
        covered: set[int] = set()
        half = period_seconds / 2
        for row in rows:
            seconds = (parse_aware(row["observed_utc"]) - day_start).total_seconds()
            index = int(round(seconds / period_seconds))
            if 0 <= index < tick_count and abs(index * period_seconds - seconds) <= half:
                covered.add(index)
        return [index for index in range(tick_count) if index not in covered]

    @staticmethod
    def _runs(indices: list[int]) -> list[tuple[int, int]]:
        """把连续缺失刻度合并成 (起始刻度, 结束刻度) 游程，结束刻度为开区间刻度。"""
        if not indices:
            return []
        runs: list[tuple[int, int]] = []
        start = previous = indices[0]
        for index in indices[1:]:
            if index == previous + 1:
                previous = index
                continue
            runs.append((start, previous + 1))
            start = previous = index
        runs.append((start, previous + 1))
        return runs

    def daily_report(
        self,
        station_code: str,
        channel: str,
        local_date: str,
        offset_minutes: int = 0,
        period_seconds: int | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        station_code = station_code.strip().upper()
        channel = channel.strip().upper()
        if period_seconds is None:
            registry = self.connection.execute(
                "SELECT period_seconds FROM seismic_station_channels WHERE station_code=? AND channel=?",
                (station_code, channel),
            ).fetchone()
            period_seconds = registry["period_seconds"] if registry else DEFAULT_PERIOD_SECONDS
        period_seconds = int(period_seconds)
        if period_seconds <= 0:
            raise ValidationError("period_seconds 必须为正整数")
        day_seconds_total = 86400
        if day_seconds_total % period_seconds != 0:
            raise ValidationError("period_seconds 必须整除 86400，保证跨日切分确定")

        day_start, day_end = _parse_day(local_date, offset_minutes)
        as_of = as_of or _now()
        # 期望域恒为完整本地日：凌晨延续的离线必须计入；注册表仅提供采样周期。
        domain = [(day_start, day_end)]
        windows = [
            window
            for window in self._windows_as_of(station_code, channel, as_of)
            if window["status"] == "approved" and (window["end_utc"] is None or window["end_utc"] > _stamp(day_start))
            and window["start_utc"] < _stamp(day_end)
        ]
        # 通道级窗口优先于台站级窗口：原因标注按通道级先取。
        windows.sort(key=lambda window: (window["start_utc"], 0 if window["channel"] == channel else 1, window["id"]))

        missing_indices = self._missing_ticks(station_code, channel, day_start, day_end, period_seconds)
        runs = self._runs(missing_indices)

        def clip_window(window: dict[str, Any]) -> tuple[datetime, datetime]:
            start = max(day_start, parse_aware(window["start_utc"]))
            end = day_end if window["end_utc"] is None else min(day_end, parse_aware(window["end_utc"]))
            return start, end

        # 排除与补偿都按区间并集处理重叠窗口；排除优先于补偿，同一时刻不会被重复计算。
        excluded_spans = merge_intervals([clip_window(w) for w in windows if w["compensation"] == "excluded"])
        compensated_spans = subtract_intervals(
            merge_intervals([clip_window(w) for w in windows if w["compensation"] in {"backfill", "interpolated"}]),
            excluded_spans,
        )

        def attribution(moment: datetime, *, compensation: str) -> dict[str, Any] | None:
            # 原因标注优先通道级窗口，其次回退到台站级窗口；
            # 必须命中真正产生该分类（排除/补偿）的窗口。
            pool = [w for w in windows if w["compensation"] == compensation]
            channel_pool = [w for w in pool if w["channel"] == channel]
            station_pool = [w for w in pool if w["channel"] == "*"]
            return self._window_at(channel_pool, moment) or self._window_at(station_pool, moment)

        gap_records: list[dict[str, Any]] = []
        for run_start, run_end in runs:
            gap = (
                day_start + timedelta(seconds=run_start * period_seconds),
                day_start + timedelta(seconds=run_end * period_seconds),
            )
            remaining = subtract_intervals([gap], excluded_spans)
            for start, end in intersect_intervals([gap], excluded_spans):
                window = attribution(start, compensation="excluded")
                gap_records.append(self._gap_record(start, end, "excluded", window))
            for start, end in intersect_intervals(remaining, compensated_spans):
                window = None
                for code in ("backfill", "interpolated"):
                    window = attribution(start, compensation=code)
                    if window is not None:
                        break
                gap_records.append(self._gap_record(start, end, "compensated", window))
            for start, end in subtract_intervals(remaining, compensated_spans):
                duration = (end - start).total_seconds()
                gap_records.append(
                    self._gap_record(
                        start,
                        end,
                        "missing",
                        None,
                        reason_code="brief_disconnect" if duration < BRIEF_GAP_SECONDS else "extended_outage",
                    )
                )

        gap_records.sort(key=lambda record: (record["start_utc"], record["status"]))
        reason_summary: dict[str, dict[str, Any]] = {}
        for gap in gap_records:
            key = f"{gap['status']}:{gap['reason_code']}"
            entry = reason_summary.setdefault(key, {
                "status": gap["status"],
                "reason_code": gap["reason_code"],
                "count": 0,
                "seconds": 0,
            })
            entry["count"] += 1
            entry["seconds"] += gap["duration_seconds"]
        all_gap_spans = merge_intervals(
            [(day_start + timedelta(seconds=s * period_seconds), day_start + timedelta(seconds=e * period_seconds)) for s, e in runs]
        )
        gap_outside_excluded = subtract_intervals(all_gap_spans, excluded_spans)
        # 补偿只在原本缺测、且未被排除的时段生效。
        compensated_seconds = int(total_length(intersect_intervals(gap_outside_excluded, compensated_spans)))
        excluded_seconds = int(total_length(intersect_intervals(excluded_spans, domain)))
        missing_seconds = int(total_length(subtract_intervals(gap_outside_excluded, compensated_spans)))
        # 整日互斥四分类：排除 E + 补偿 C + 实测 O + 缺测 M = 86400
        observed_seconds = int(total_length(domain)) - excluded_seconds - int(total_length(gap_outside_excluded))
        total_expected = int(total_length(domain))
        denominator = total_expected - excluded_seconds
        effective_seconds = observed_seconds + compensated_seconds
        availability_rate = round(effective_seconds / denominator, 6) if denominator > 0 else None

        window_versions = [
            {"window_id": w["id"], "version": w["current_version"], "compensation": w["compensation"], "status": w["status"]}
            for w in windows
        ]
        signature_src = json.dumps(sorted((item["window_id"], item["version"]) for item in window_versions), ensure_ascii=False)
        signature = hashlib.sha256(signature_src.encode()).hexdigest()
        result = {
            "station_code": station_code,
            "channel": channel,
            "local_date": local_date,
            "offset": format_offset(offset_minutes),
            "offset_minutes": offset_minutes,
            "period_seconds": period_seconds,
            "day_start_utc": _stamp(day_start),
            "day_end_utc": _stamp(day_end),
            "as_of_utc": _audit_stamp(as_of),
            "window_versions": window_versions,
            "expected_seconds": total_expected,
            "denominator_seconds": denominator,
            "observed_seconds": observed_seconds,
            "compensated_seconds": compensated_seconds,
            "excluded_seconds": excluded_seconds,
            "missing_seconds": missing_seconds,
            "availability_rate": availability_rate,
            "gaps": gap_records,
            "reason_summary": sorted(
                reason_summary.values(), key=lambda item: (item["status"], -item["seconds"], item["reason_code"])
            ),
            "windows": [self._window_summary(w, day_start, day_end) for w in windows],
        }
        with transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO seismic_report_runs"
                "(station_code,channel,local_date,offset_minutes,period_seconds,as_of_utc,"
                "window_signature,window_versions_json,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    station_code, channel, local_date, offset_minutes, period_seconds, _audit_stamp(as_of),
                    signature, json.dumps(window_versions, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False), _stamp(_now()),
                ),
            )
            run = connection.execute(
                "SELECT id FROM seismic_report_runs WHERE station_code=? AND channel=? AND local_date=?"
                " AND offset_minutes=? AND period_seconds=? AND as_of_utc=?",
                (station_code, channel, local_date, offset_minutes, period_seconds, _audit_stamp(as_of)),
            ).fetchone()
        result["report_run_id"] = run["id"]
        return result

    @staticmethod
    def _window_at(windows: list[dict[str, Any]], moment: datetime) -> dict[str, Any] | None:
        stamp = _stamp(moment)
        for window in windows:
            end = window["end_utc"]
            if window["start_utc"] <= stamp and (end is None or end > stamp):
                return window
        return None

    def _gap_record(
        self,
        start: datetime,
        end: datetime,
        status: str,
        window: dict[str, Any] | None,
        *,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        duration = int((end - start).total_seconds())
        if window is not None:
            reason_code = window["reason_code"]
            compensation_source = {
                "window_id": window["id"],
                "version": window["current_version"],
                "compensation": window["compensation"],
                "ticket": window["ticket"],
                "approved_by": window["approved_by"],
            }
        else:
            compensation_source = None
        return {
            "start_utc": _stamp(start),
            "end_utc": _stamp(end),
            "duration_seconds": duration,
            "status": status,
            "reason_code": reason_code,
            "reason_detail": window["reason_detail"] if window else ("短暂断链" if reason_code == "brief_disconnect" else "长时间离线，未匹配维护窗口"),
            "compensation_source": compensation_source,
        }

    def _window_summary(self, window: dict[str, Any], day_start: datetime, day_end: datetime) -> dict[str, Any]:
        start = max(day_start, parse_aware(window["start_utc"]))
        end = day_end if window["end_utc"] is None else min(day_end, parse_aware(window["end_utc"]))
        return {
            "window_id": window["id"],
            "version": window["current_version"],
            "scope": "station" if window["channel"] == "*" else "channel",
            "channel": window["channel"],
            "start_utc": _stamp(start),
            "end_utc": _stamp(end),
            "open_ended": window["end_utc"] is None,
            "submitted_offset": format_offset(window.get("start_offset_minutes", 0)),
            "reason_code": window["reason_code"],
            "reason_detail": window["reason_detail"],
            "compensation": window["compensation"],
            "ticket": window["ticket"],
            "approved_by": window["approved_by"],
        }

    def get_report_run(self, run_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM seismic_report_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("报表运行记录不存在")
        result = json.loads(row["result_json"])
        result["report_run_id"] = row["id"]
        result["window_signature"] = row["window_signature"]
        return result
