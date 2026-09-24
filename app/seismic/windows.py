"""台站通道登记、采样心跳与维护窗口的双时态版本管理。

所有时间值在写入前统一归一化为 UTC（ISO 8601，带显式 +00:00 偏移）。
维护窗口采用只追加的版本链：同一 ``uid`` 的每次修订/取消都新增一行版本，
审批记录不可变；历史报表通过 ``as_of`` 选取当时生效的版本重算，
任何版本修订都不会改写已落库的原始观测。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.seismic.timeutil import parse_instant, parse_sourced_instant, utc_iso, utc_stamp


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    """系统列时间戳保留微秒，保证同一秒内多次修订/审批的双时态顺序确定。"""
    return _now().isoformat(timespec="microseconds")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class MaintenanceWindowService:
    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        from app.seismic.service import ensure_schema

        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 台站通道

    def upsert_channel(
        self,
        station_code: str,
        channel: str,
        *,
        sample_interval_seconds: int,
        brief_dropout_seconds: int,
        actor: str = "system",
    ) -> dict[str, Any]:
        station_code = station_code.strip().upper()
        channel = channel.strip().upper()
        if sample_interval_seconds <= 0 or sample_interval_seconds > 86400:
            raise ValidationError("采样间隔必须在 1 到 86400 秒之间")
        if 86400 % sample_interval_seconds:
            raise ValidationError("采样间隔必须能整除 86400，保证每日槽位确定")
        if not 0 <= brief_dropout_seconds <= 86400:
            raise ValidationError("短暂断链阈值必须在 0 到 86400 秒之间")
        if brief_dropout_seconds and brief_dropout_seconds < sample_interval_seconds:
            raise ValidationError("短暂断链阈值不能小于一个采样间隔")
        now = _stamp()
        with transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO seismic_station_channels(station_code,channel,sample_interval_seconds,brief_dropout_seconds,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(station_code,channel) DO UPDATE SET "
                "sample_interval_seconds=excluded.sample_interval_seconds,"
                "brief_dropout_seconds=excluded.brief_dropout_seconds,updated_at=excluded.updated_at",
                (station_code, channel, sample_interval_seconds, brief_dropout_seconds, now, now),
            )
            row = connection.execute(
                "SELECT * FROM seismic_station_channels WHERE station_code=? AND channel=?",
                (station_code, channel),
            ).fetchone()
            return dict(row)

    def get_channel(self, station_code: str, channel: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM seismic_station_channels WHERE station_code=? AND channel=?",
            (station_code.strip().upper(), channel.strip().upper()),
        ).fetchone()
        if row is None:
            raise NotFoundError("台站通道未登记，请先配置采样间隔")
        return dict(row)

    def list_channels(self, station_code: str | None = None) -> list[dict[str, Any]]:
        if station_code:
            rows = self.connection.execute(
                "SELECT * FROM seismic_station_channels WHERE station_code=? ORDER BY station_code,channel",
                (station_code.strip().upper(),),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM seismic_station_channels ORDER BY station_code,channel"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 心跳

    def ingest_heartbeats(self, station_code: str, channel: str, items: list[str]) -> dict[str, Any]:
        station_code = station_code.strip().upper()
        channel = channel.strip().upper()
        self.get_channel(station_code, channel)
        if not items:
            raise ValidationError("至少上报一条心跳")
        accepted: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        now_dt = _now()
        with transaction(immediate=True) as connection:
            for observed_text_raw in items:
                observed_utc, source_offset = parse_sourced_instant(observed_text_raw, "观测时间")
                observed_text = utc_iso(observed_utc)
                digest = hashlib.sha256(
                    f"{station_code}|{channel}|{observed_text}".encode()
                ).hexdigest()
                try:
                    cursor = connection.execute(
                        "INSERT INTO seismic_channel_heartbeats(station_code,channel,observed_at,source_offset,source_hash,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (station_code, channel, observed_text, source_offset, digest, utc_iso(now_dt)),
                    )
                    accepted.append({"id": cursor.lastrowid, "observed_at": observed_text, "source_offset": source_offset})
                except sqlite3.IntegrityError:
                    existing = connection.execute(
                        "SELECT id,observed_at,source_offset FROM seismic_channel_heartbeats "
                        "WHERE station_code=? AND channel=? AND observed_at=?",
                        (station_code, channel, observed_text),
                    ).fetchone()
                    duplicates.append(dict(existing))
        return {"station_code": station_code, "channel": channel, "accepted": accepted, "duplicates": duplicates}

    # ------------------------------------------------------------------ 维护窗口

    def _window_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        start_utc = parse_instant(payload["start_at"], "窗口开始时间")
        end_utc = parse_instant(payload["end_at"], "窗口结束时间")
        if end_utc <= start_utc:
            raise ValidationError("维护窗口结束时间必须晚于开始时间")
        if (end_utc - start_utc).total_seconds() > 14 * 86400:
            raise ValidationError("单个维护窗口不能超过 14 天")
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationError("维护窗口必须填写原因")
        compensation = payload.get("compensation", "excluded")
        if compensation not in {"excluded", "imputed"}:
            raise ValidationError("补偿方式只能是 excluded（剔除）或 imputed（插补计为有效）")
        channel_value = payload.get("channel")
        if channel_value is not None:
            channel_value = channel_value.strip().upper() or None
        return {
            "station_code": payload["station_code"].strip().upper(),
            "channel": channel_value,
            "start_at": utc_iso(start_utc),
            "end_at": utc_iso(end_utc),
            "reason": reason,
            "compensation": compensation,
        }

    def create_window(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        values = self._window_payload(payload)
        uid = (payload.get("uid") or f"MW-{uuid.uuid4().hex[:16].upper()}").strip().upper()
        idempotency_key = (payload.get("idempotency_key") or "").strip()
        now = _stamp()
        with transaction(immediate=True) as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM seismic_maintenance_windows WHERE station_code=? "
                    "AND COALESCE(channel,'')=? AND idempotency_key=? ORDER BY id LIMIT 1",
                    (values["station_code"], values["channel"] or "", idempotency_key),
                ).fetchone()
                if existing:
                    return dict(existing)
            clash = connection.execute(
                "SELECT 1 FROM seismic_maintenance_windows WHERE uid=? AND version=1", (uid,)
            ).fetchone()
            if clash:
                raise ConflictError("窗口标识已存在，请改用修订接口")
            cursor = connection.execute(
                "INSERT INTO seismic_maintenance_windows(uid,version,station_code,channel,start_at,end_at,reason,compensation,"
                "status,parent_version,idempotency_key,created_by,created_at,effective_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, 1, values["station_code"], values["channel"], values["start_at"], values["end_at"],
                 values["reason"], values["compensation"], "proposed", 0, idempotency_key, actor, now, now),
            )
            return dict(connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (cursor.lastrowid,)).fetchone())

    def revise_window(self, uid: str, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        uid = uid.strip().upper()
        with transaction(immediate=True) as connection:
            latest = connection.execute(
                "SELECT * FROM seismic_maintenance_windows WHERE uid=? ORDER BY version DESC LIMIT 1", (uid,)
            ).fetchone()
            if latest is None:
                raise NotFoundError("维护窗口不存在")
            if latest["status"] == "cancelled":
                raise ConflictError("已取消的窗口不能修订，只能新建")
            next_version = latest["version"] + 1
            base = dict(latest)
            base.update({k: v for k, v in payload.items() if k in {"start_at", "end_at", "reason", "compensation", "channel"} and v is not None})
            base["station_code"] = latest["station_code"]
            values = self._window_payload(base)
            now = _stamp()
            connection.execute(
                "UPDATE seismic_maintenance_windows SET superseded_at=? WHERE id=?", (now, latest["id"])
            )
            cursor = connection.execute(
                "INSERT INTO seismic_maintenance_windows(uid,version,station_code,channel,start_at,end_at,reason,compensation,"
                "status,parent_version,idempotency_key,created_by,created_at,effective_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, next_version, values["station_code"], values["channel"], values["start_at"], values["end_at"],
                 values["reason"], values["compensation"], "proposed", latest["version"], "", actor, now, now),
            )
            return dict(connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (cursor.lastrowid,)).fetchone())

    def decide_window(self, uid: str, version: int | None, decision: str, approver: str, comment: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValidationError("审批结论只能是 approved 或 rejected")
        uid = uid.strip().upper()
        now = _stamp()
        with transaction(immediate=True) as connection:
            latest = connection.execute(
                "SELECT * FROM seismic_maintenance_windows WHERE uid=? ORDER BY version DESC LIMIT 1", (uid,)
            ).fetchone()
            if latest is None:
                raise NotFoundError("维护窗口不存在")
            target_version = version or latest["version"]
            if target_version != latest["version"]:
                raise ConflictError("只能审批最新版本的修订")
            if latest["status"] != "proposed":
                raise ConflictError(f"窗口版本当前状态为 {latest['status']}，不能再次审批")
            connection.execute(
                "INSERT INTO seismic_maintenance_approvals(uid,version,decision,approver,comment,decided_at) VALUES(?,?,?,?,?,?)",
                (uid, target_version, decision, approver, comment.strip(), now),
            )
            connection.execute(
                "UPDATE seismic_maintenance_windows SET status=? WHERE uid=? AND version=?",
                (decision, uid, target_version),
            )
            return dict(connection.execute(
                "SELECT * FROM seismic_maintenance_windows WHERE uid=? AND version=?", (uid, target_version)
            ).fetchone())

    def cancel_window(self, uid: str, actor: str = "system", reason: str = "") -> dict[str, Any]:
        uid = uid.strip().upper()
        with transaction(immediate=True) as connection:
            latest = connection.execute(
                "SELECT * FROM seismic_maintenance_windows WHERE uid=? ORDER BY version DESC LIMIT 1", (uid,)
            ).fetchone()
            if latest is None:
                raise NotFoundError("维护窗口不存在")
            if latest["status"] == "cancelled":
                return dict(latest)
            now = _stamp()
            connection.execute(
                "UPDATE seismic_maintenance_windows SET superseded_at=? WHERE id=?", (now, latest["id"])
            )
            cursor = connection.execute(
                "INSERT INTO seismic_maintenance_windows(uid,version,station_code,channel,start_at,end_at,reason,compensation,"
                "status,parent_version,idempotency_key,created_by,created_at,effective_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, latest["version"] + 1, latest["station_code"], latest["channel"], latest["start_at"], latest["end_at"],
                 reason or latest["reason"], latest["compensation"], "cancelled", latest["version"], "", actor, now, now),
            )
            return dict(connection.execute("SELECT * FROM seismic_maintenance_windows WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_windows(self, station_code: str | None = None, channel: str | None = None, *, include_superseded: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT w.*, a.decision AS last_decision, a.approver AS last_approver, a.decided_at AS decided_at " \
              "FROM seismic_maintenance_windows w LEFT JOIN seismic_maintenance_approvals a " \
              "ON a.uid=w.uid AND a.version=w.version WHERE 1=1"
        args: list[Any] = []
        if station_code:
            sql += " AND w.station_code=?"
            args.append(station_code.strip().upper())
        if channel:
            sql += " AND (w.channel IS NULL OR w.channel=?)"
            args.append(channel.strip().upper())
        if not include_superseded:
            sql += " AND w.superseded_at IS NULL"
        sql += " ORDER BY w.uid,w.version"
        return [dict(row) for row in self.connection.execute(sql, args).fetchall()]

    def get_window(self, uid: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM seismic_maintenance_windows WHERE uid=? ORDER BY version", (uid.strip().upper(),)
        ).fetchall()
        if not rows:
            raise NotFoundError("维护窗口不存在")
        versions = [dict(row) for row in rows]
        approvals = [dict(row) for row in self.connection.execute(
            "SELECT * FROM seismic_maintenance_approvals WHERE uid=? ORDER BY version", (uid.strip().upper(),)
        ).fetchall()]
        return {"uid": uid.strip().upper(), "versions": versions, "approvals": approvals, "latest": versions[-1]}


def effective_windows(connection: sqlite3.Connection, station_code: str, channel: str, as_of: datetime) -> list[dict[str, Any]]:
    """返回 as_of 时刻真正生效（已审批）的维护窗口，每条为对应版本链的快照。

    版本选择规则（确定性）：
    - 最新可见版本为 cancelled → 整条窗口不生效；
    - 最新可见版本在 as_of 前已审批 → 取最新版本；
    - 最新版本为 proposed/rejected → 回退到 as_of 前已审批的最近旧版本；
    - 从无审批 → 不生效。台站级窗口（channel 为空）与通道级窗口同时纳入，
      重叠在报表计算时按并集合并。
    """
    as_of_text = utc_stamp(as_of)
    rows = connection.execute(
        "SELECT w.*, a.decided_at AS approval_decided_at FROM seismic_maintenance_windows w "
        "LEFT JOIN seismic_maintenance_approvals a ON a.uid=w.uid AND a.version=w.version AND a.decision='approved' "
        "WHERE w.station_code=? AND (w.channel IS NULL OR w.channel=?) AND w.effective_at<=? "
        "ORDER BY w.uid,w.version",
        (station_code, channel, as_of_text),
    ).fetchall()
    chains: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        chains.setdefault(row["uid"], []).append(row)
    result: list[dict[str, Any]] = []
    for uid, versions in chains.items():
        latest = versions[-1]
        if latest["status"] == "cancelled":
            continue
        chosen: sqlite3.Row | None = None
        if latest["status"] == "approved" and latest["approval_decided_at"] and latest["approval_decided_at"] <= as_of_text:
            chosen = latest
        else:
            for previous in reversed(versions[:-1]):
                if previous["status"] == "approved" and previous["approval_decided_at"] and previous["approval_decided_at"] <= as_of_text:
                    chosen = previous
                    break
        if chosen is not None:
            item = dict(chosen)
            item["uid"] = uid
            result.append(item)
    result.sort(key=lambda item: (item["start_at"], item["uid"], item["version"]))
    return result
