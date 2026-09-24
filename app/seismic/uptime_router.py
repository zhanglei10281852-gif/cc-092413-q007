"""台站运行率与维护窗口接口。

所有请求时间必须携带显式时区偏移；存储统一为 UTC，响应中以
``*_utc`` 字段给出 UTC 时间，并以 ``offset`` / ``submitted_offset``
字段明确偏移量。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.errors import ValidationError
from app.seismic.schemas import (
    MaintenanceApproval,
    MaintenanceWindowCreate,
    MaintenanceWindowUpdate,
    SampleBatch,
)
from app.seismic.timeparse import parse_aware
from app.seismic.uptime import UptimeService

router = APIRouter(prefix="/api/seismic/uptime", tags=["台站运行率"])


def service() -> UptimeService:
    return UptimeService()


@router.post("/samples", status_code=202)
def ingest_samples(payload: SampleBatch):
    return service().ingest_samples(
        payload.station_code,
        payload.channel,
        [item.model_dump() for item in payload.samples],
        period_seconds=payload.period_seconds,
    )


@router.get("/stations/{station_code}/channels")
def list_channels(station_code: str):
    return {"station_code": station_code.strip().upper(), "channels": service().list_channels(station_code)}


@router.post("/maintenance-windows", status_code=201)
def create_window(payload: MaintenanceWindowCreate, actor: str = Query(default="operator", min_length=1, max_length=80)):
    return service().create_window(payload.model_dump(), actor=actor)


@router.get("/maintenance-windows/{window_id}")
def get_window(window_id: int):
    return service().get_window(window_id)


@router.get("/maintenance-windows/{window_id}/versions")
def list_window_versions(window_id: int):
    return {"window_id": window_id, "versions": service().list_window_versions(window_id)}


@router.patch("/maintenance-windows/{window_id}")
def update_window(window_id: int, payload: MaintenanceWindowUpdate, actor: str = Query(default="operator", min_length=1, max_length=80)):
    return service().update_window(window_id, payload.model_dump(exclude_unset=True), actor=actor)


@router.post("/maintenance-windows/{window_id}/approve")
def approve_window(window_id: int, payload: MaintenanceApproval, actor: str = Query(default="operator", min_length=1, max_length=80)):
    return service().approve_window(window_id, actor=actor, approved_by=payload.approved_by or None)


@router.post("/maintenance-windows/{window_id}/cancel")
def cancel_window(window_id: int, actor: str = Query(default="operator", min_length=1, max_length=80)):
    return service().cancel_window(window_id, actor=actor)


@router.get("/stations/{station_code}/maintenance-windows")
def list_windows(
    station_code: str,
    channel: str | None = Query(default=None),
    as_of: str | None = Query(default=None, description="按该 UTC 时刻可见的窗口版本重建状态"),
):
    as_of_dt = parse_aware(as_of) if as_of else None
    return {"windows": service().list_windows(station_code, channel, as_of=as_of_dt)}


@router.get("/stations/{station_code}/channels/{channel}/daily-report")
def daily_report(
    station_code: str,
    channel: str,
    date: str = Query(..., description="台站本地日期 YYYY-MM-DD"),
    offset_minutes: int = Query(default=0, ge=-720, le=840, description="台站本地时区偏移（分钟），如 +08:00 为 480"),
    period_seconds: int | None = Query(default=None, gt=0, le=86400),
    as_of: str | None = Query(default=None, description="按历史版本重算：只使用该时刻之前已落版的窗口版本"),
):
    try:
        as_of_dt = parse_aware(as_of) if as_of else None
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return service().daily_report(
        station_code,
        channel,
        date,
        offset_minutes=offset_minutes,
        period_seconds=period_seconds,
        as_of=as_of_dt,
    )


@router.get("/report-runs/{run_id}")
def get_report_run(run_id: int):
    return service().get_report_run(run_id)
