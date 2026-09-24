from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.errors import DomainError
from app.seismic.reports import UptimeReportService
from app.seismic.schemas import (
    ChannelUpsert,
    ComputeRequest,
    EventCreate,
    EventPatch,
    HeartbeatBatch,
    MaintenanceWindowCancel,
    MaintenanceWindowCreate,
    MaintenanceWindowDecision,
    MaintenanceWindowRevise,
    ObservationCreate,
)
from app.seismic.service import SeismicService
from app.seismic.windows import MaintenanceWindowService

router = APIRouter(prefix="/api/seismic", tags=["地震科学计算"])


def service() -> SeismicService:
    return SeismicService()


def window_service() -> MaintenanceWindowService:
    return MaintenanceWindowService()


def report_service() -> UptimeReportService:
    return UptimeReportService()


@router.post("/events", status_code=201)
def create_event(payload: EventCreate):
    try:
        return service().create_event(payload.model_dump(), actor=payload.source)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="external_id 已存在") from exc
        raise


@router.get("/events/{event_id}")
def get_event(event_id: int, include_observations: bool = Query(True)):
    value = service().get_event(event_id, include_observations)
    if value is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return value


@router.patch("/events/{event_id}")
def patch_event(event_id: int, payload: EventPatch):
    try:
        return service().patch_event(event_id, payload.model_dump(exclude_unset=True), actor="operator")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/events/{event_id}/observations", status_code=201)
def add_observation(event_id: int, payload: ObservationCreate):
    try:
        return service().add_observation(event_id, payload.model_dump(), actor="station")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/events/{event_id}/computations", status_code=202)
def enqueue(event_id: int, payload: ComputeRequest):
    try:
        return service().enqueue_computation(event_id, payload.model_version, payload.grid_step_km, payload.radius_km, payload.requested_by)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/computations/claim")
def claim(worker_id: str = Query(..., min_length=1)):
    task = service().claim_task(worker_id)
    return {"task": task}


@router.post("/computations/{task_id}/calculate")
def calculate(task_id: int, worker_id: str = Query(..., min_length=1)):
    try:
        return service().calculate_task(task_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="任务不属于该工作者或不存在") from exc


@router.get("/computations/{task_id}")
def get_computation(task_id: int):
    row = service().connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return dict(row)


# ---------------------------------------------------------------- 台站通道与心跳


@router.post("/channels", status_code=201)
def upsert_channel(payload: ChannelUpsert):
    try:
        return window_service().upsert_channel(
            payload.station_code,
            payload.channel,
            sample_interval_seconds=payload.sample_interval_seconds,
            brief_dropout_seconds=payload.brief_dropout_seconds,
            actor="admin",
        )
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/channels")
def list_channels(station_code: str | None = Query(default=None)):
    return {"channels": window_service().list_channels(station_code)}


@router.post("/channels/{station_code}/{channel}/heartbeats", status_code=201)
def ingest_heartbeats(station_code: str, channel: str, payload: HeartbeatBatch):
    try:
        return window_service().ingest_heartbeats(station_code, channel, payload.observed_at)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ---------------------------------------------------------------- 维护窗口

@router.post("/maintenance-windows", status_code=201)
def create_window(payload: MaintenanceWindowCreate):
    try:
        return window_service().create_window(payload.model_dump(exclude_unset=True), actor=payload.created_by)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.patch("/maintenance-windows/{uid}")
def revise_window(uid: str, payload: MaintenanceWindowRevise):
    try:
        data = payload.model_dump(exclude_unset=True)
        actor = data.pop("actor", "system")
        return window_service().revise_window(uid, data, actor=actor)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/maintenance-windows/{uid}/decision", status_code=200)
def decide_window(uid: str, payload: MaintenanceWindowDecision):
    try:
        return window_service().decide_window(uid, payload.version, payload.decision, payload.approver, payload.comment)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/maintenance-windows/{uid}/cancel")
def cancel_window(uid: str, payload: MaintenanceWindowCancel):
    try:
        return window_service().cancel_window(uid, actor=payload.actor, reason=payload.reason)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/maintenance-windows")
def list_windows(
    station_code: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    include_superseded: bool = Query(default=True),
):
    return {"windows": window_service().list_windows(station_code, channel, include_superseded=include_superseded)}


@router.get("/maintenance-windows/{uid}")
def get_window(uid: str):
    try:
        return window_service().get_window(uid)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


# ---------------------------------------------------------------- 有效率报表

@router.get("/stations/{station_code}/channels/{channel}/uptime")
def uptime_report(
    station_code: str,
    channel: str,
    date: str = Query(..., description="本地自然日 YYYY-MM-DD"),
    offset: str = Query("+00:00", description="定义日界的 UTC 偏移，如 +08:00"),
    as_of: str | None = Query(default=None, description="按该时刻（带偏移）可见的窗口版本重算"),
    persist: bool = Query(True),
):
    try:
        return report_service().daily_report(station_code, channel, date, offset, as_of, persist=persist)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/stations/{station_code}/channels/{channel}/uptime/versions")
def uptime_versions(
    station_code: str,
    channel: str,
    date: str = Query(...),
    offset: str = Query("+00:00"),
):
    return {"versions": report_service().frozen_versions(station_code, channel, date, offset)}


@router.get("/stations/{station_code}/channels/{channel}/uptime/snapshot")
def uptime_snapshot(
    station_code: str,
    channel: str,
    date: str = Query(...),
    offset: str = Query("+00:00"),
    as_of: str = Query(...),
):
    try:
        return report_service().frozen_report(station_code, channel, date, offset, as_of)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
