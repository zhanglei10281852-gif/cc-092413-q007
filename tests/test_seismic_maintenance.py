from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc
TZ8 = timezone(timedelta(hours=8))


def _slot_heartbeats(day_start_utc: datetime, interval: int, missing: list[tuple[datetime, datetime]], *, days: int = 1, offset=TZ8):
    """生成除 missing 区间外、以指定偏移上报的心跳时间字符串。"""
    skip: set[datetime] = set()
    for start, end in missing:
        point = start
        while point < end:
            skip.add(point)
            point += timedelta(seconds=interval)
    values: list[str] = []
    point = day_start_utc
    end = day_start_utc + timedelta(days=days)
    while point < end:
        if point not in skip:
            values.append(point.astimezone(offset).isoformat(timespec="seconds"))
        point += timedelta(seconds=interval)
    return values


def _ingest(client, station, channel, values, batch=1000):
    for index in range(0, len(values), batch):
        response = client.post(
            f"/api/seismic/channels/{station}/{channel}/heartbeats",
            json={"observed_at": values[index:index + batch]},
        )
        assert response.status_code == 201, response.text
    return values


def test_heartbeats_require_timezone_offset_and_deduplicate(client):
    created = client.post("/api/seismic/channels", json={
        "station_code": "ST01", "channel": "HNZ",
        "sample_interval_seconds": 3600, "brief_dropout_seconds": 7200,
    })
    assert created.status_code == 201, created.text

    bad = client.post("/api/seismic/channels/ST01/HNZ/heartbeats", json={"observed_at": ["2026-09-24T12:00:00"]})
    assert bad.status_code == 422

    payload = {"observed_at": [
        "2026-09-24T20:00:00+08:00",   # 同一时刻
        "2026-09-24T12:00:00Z",       # UTC == 20:00+08:00，应判重
        "2026-09-24T21:00:00+08:00",
    ]}
    first = client.post("/api/seismic/channels/ST01/HNZ/heartbeats", json=payload)
    assert first.status_code == 201, first.text
    body = first.json()
    assert len(body["accepted"]) == 2
    assert len(body["duplicates"]) == 1
    # 存储统一 UTC，响应明确 +00:00
    stored = sorted(item["observed_at"] for item in body["accepted"])
    assert stored == ["2026-09-24T12:00:00+00:00", "2026-09-24T13:00:00+00:00"]
    assert {item["source_offset"] for item in body["accepted"]} == {"+08:00"}

    second = client.post("/api/seismic/channels/ST01/HNZ/heartbeats", json=payload)
    assert second.status_code == 201
    assert len(second.json()["accepted"]) == 0
    assert len(second.json()["duplicates"]) == 3


def test_channel_sample_interval_must_divide_day(client):
    response = client.post("/api/seismic/channels", json={
        "station_code": "ST02", "channel": "HNZ",
        "sample_interval_seconds": 3700, "brief_dropout_seconds": 7400,
    })
    assert response.status_code == 422 or response.status_code == 400


def _setup_day(client, station="BJ01", channel="HNZ", interval=60, brief=300, missing=None):
    client.post("/api/seismic/channels", json={
        "station_code": station, "channel": channel,
        "sample_interval_seconds": interval, "brief_dropout_seconds": brief,
    })
    # 本地 2026-09-24 (+08:00)
    day_start = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
    values = _slot_heartbeats(day_start, interval, missing or [])
    _ingest(client, station, channel, values)
    return day_start


def test_report_distinguishes_brief_offline_and_approved_maintenance(client):
    day_start = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
    brief = (day_start + timedelta(hours=2), day_start + timedelta(hours=2, minutes=4))
    offline = (day_start + timedelta(hours=5), day_start + timedelta(hours=6))
    maint = (day_start + timedelta(hours=10), day_start + timedelta(hours=11))
    _setup_day(client, missing=[brief, offline, maint])

    # 维护窗口先创建但不审批：整段维护时间按离线处理
    create = client.post("/api/seismic/maintenance-windows", json={
        "station_code": "BJ01", "channel": "HNZ",
        "start_at": maint[0].astimezone(TZ8).isoformat(timespec="seconds"),
        "end_at": maint[1].astimezone(TZ8).isoformat(timespec="seconds"),
        "reason": "仪器标定", "compensation": "excluded", "uid": "MW-R-1",
    })
    assert create.status_code == 201
    as_of = "2026-09-26T00:00:00+00:00"

    before = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime?date=2026-09-24&offset=%2B08:00&as_of=2026-09-26T00:00:00%2B00:00&persist=false")
    assert before.status_code == 200, before.text
    before_json = before.json()
    assert before_json["offline_slots"] == 120  # 60(offline) + 60(未审批维护)
    assert before_json["brief_dropout_slots"] == 4
    assert before_json["maintenance_excluded_slots"] == 0
    assert before_json["inspection_required"] is True

    # 审批后维护段被剔除，不再算缺测；真正离线仍触发巡检
    decided = client.post("/api/seismic/maintenance-windows/MW-R-1/decision", json={
        "decision": "approved", "approver": "zhang.si", "comment": "已核实",
    })
    assert decided.status_code == 200
    after = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": as_of, "persist": "false",
    }).json()
    assert after["offline_slots"] == 60
    assert after["maintenance_excluded_slots"] == 60
    assert after["brief_dropout_slots"] == 4
    # 有效率 = (在线1316 + 短暂插补4) / (期望1440 - 维护剔除60) = 1320/1380
    # 真正离线 60 槽位拉低有效率
    assert after["availability_rate"] == pytest.approx(1320 / 1380)
    assert after["inspection_required"] is True
    reasons = {segment["reason"] for segment in after["segments"]}
    assert reasons == {"brief_dropout", "offline", "maintenance"}
    maint_seg = next(segment for segment in after["segments"] if segment["reason"] == "maintenance")
    assert maint_seg["sources"][0]["uid"] == "MW-R-1"
    assert maint_seg["sources"][0]["version"] == 1
    assert maint_seg["sources"][0]["approver"] == "zhang.si"
    assert after["day_start_utc"] == "2026-09-23T16:00:00+00:00"
    assert after["day_end_utc"] == "2026-09-24T16:00:00+00:00"
    assert after["as_of"].endswith("+00:00")


def test_overlapping_windows_union_imputed_wins(client):
    day_start = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
    gap = (day_start + timedelta(hours=3), day_start + timedelta(hours=3, minutes=6))
    _setup_day(client, missing=[gap])
    # 两个已审批窗口在缺测区间上重叠：一个剔除、一个插补
    for uid, start_min, end_min, comp in [
        ("MW-O-1", 178, 188, "excluded"),
        ("MW-O-2", 182, 190, "imputed"),
    ]:
        response = client.post("/api/seismic/maintenance-windows", json={
            "station_code": "BJ01", "channel": "HNZ",
            "start_at": (day_start + timedelta(minutes=start_min)).astimezone(TZ8).isoformat(timespec="seconds"),
            "end_at": (day_start + timedelta(minutes=end_min)).astimezone(TZ8).isoformat(timespec="seconds"),
            "reason": "重叠窗口", "compensation": comp, "uid": uid,
        })
        assert response.status_code == 201
        decision = client.post(f"/api/seismic/maintenance-windows/{uid}/decision",
                               json={"decision": "approved", "approver": "li.gong"})
        assert decision.status_code == 200

    report = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": "2026-09-26T00:00:00+00:00", "persist": "false",
    }).json()
    # 重叠并集段中存在 imputed，整个并集覆盖的缺测槽位都计为插补
    assert report["maintenance_imputed_slots"] == 6
    assert report["maintenance_excluded_slots"] == 0
    assert report["availability_rate"] == pytest.approx(1.0)
    seg = report["segments"][0]
    assert {source["uid"] for source in seg["sources"]} == {"MW-O-1", "MW-O-2"}


def test_cross_midnight_window_split_across_local_days(client):
    day_start = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
    # 本地 09-24 23:30 至 09-25 00:30 的缺测
    cross_gap = (day_start + timedelta(hours=23, minutes=30), day_start + timedelta(days=1, minutes=30))
    client.post("/api/seismic/channels", json={
        "station_code": "BJ01", "channel": "HNZ",
        "sample_interval_seconds": 60, "brief_dropout_seconds": 300,
    })
    values = _slot_heartbeats(day_start, 60, [cross_gap], days=2)
    _ingest(client, "BJ01", "HNZ", values)

    response = client.post("/api/seismic/maintenance-windows", json={
        "station_code": "BJ01", "channel": "HNZ",
        "start_at": "2026-09-24T23:30:00+08:00",
        "end_at": "2026-09-25T00:30:00+08:00",
        "reason": "跨午夜检修", "compensation": "excluded", "uid": "MW-X-1",
    })
    assert response.status_code == 201
    client.post("/api/seismic/maintenance-windows/MW-X-1/decision",
                json={"decision": "approved", "approver": "wang.qin"})

    first_day = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": "2026-09-26T00:00:00+00:00", "persist": "false",
    }).json()
    second_day = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-25", "offset": "+08:00", "as_of": "2026-09-26T00:00:00+00:00", "persist": "false",
    }).json()
    seg_a = next(segment for segment in first_day["segments"] if segment["crosses_day_boundary"])
    seg_b = next(segment for segment in second_day["segments"] if segment["crosses_day_boundary"])
    assert seg_a["missing_slots"] == seg_b["missing_slots"] == 30
    assert seg_a["end_at"] == "2026-09-24T16:00:00+00:00"
    assert seg_b["start_at"] == "2026-09-24T16:00:00+00:00"


def test_window_revision_and_cancel_leave_version_chain(client):
    day_start = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
    maint = (day_start + timedelta(hours=8), day_start + timedelta(hours=9))
    _setup_day(client, missing=[maint])
    client.post("/api/seismic/maintenance-windows", json={
        "station_code": "BJ01", "channel": "HNZ",
        "start_at": maint[0].astimezone(TZ8).isoformat(timespec="seconds"),
        "end_at": maint[1].astimezone(TZ8).isoformat(timespec="seconds"),
        "reason": "标定", "compensation": "excluded", "uid": "MW-V-1",
    })
    client.post("/api/seismic/maintenance-windows/MW-V-1/decision",
                json={"decision": "approved", "approver": "zhang.si"})
    # v1 审批记录（微秒精度）作为历史重算锚点
    chain = client.get("/api/seismic/maintenance-windows/MW-V-1").json()
    approvals = chain["approvals"]
    assert any(item["decision"] == "approved" and item["version"] == 1 for item in approvals)

    # 修订为 imputed 并重新审批
    revision = client.patch("/api/seismic/maintenance-windows/MW-V-1",
                            json={"compensation": "imputed", "reason": "标定-改插补", "actor": "admin"})
    assert revision.status_code == 200
    assert revision.json()["version"] == 2
    assert revision.json()["status"] == "proposed"
    client.post("/api/seismic/maintenance-windows/MW-V-1/decision",
                json={"decision": "approved", "approver": "zhang.si"})

    # 修订前审批未通过的 v2 不能被审批非最新版本
    # 历史报表锚点：用 v1 审批时刻（从审批表读出）
    v1_decided = next(item["decided_at"] for item in approvals if item["version"] == 1)
    old_report = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": v1_decided, "persist": "true",
    }).json()
    assert [(item["uid"], item["version"]) for item in old_report["window_versions"]] == [("MW-V-1", 1)]
    assert old_report["maintenance_excluded_slots"] == 60
    assert old_report["maintenance_imputed_slots"] == 0

    new_report = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": "2026-09-26T00:00:00+00:00", "persist": "true",
    }).json()
    assert [(item["uid"], item["version"]) for item in new_report["window_versions"]] == [("MW-V-1", 2)]
    assert new_report["maintenance_imputed_slots"] == 60

    # 快照应可按版本取回，且两次重算结果一致（不改写原始心跳）
    versions = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime/versions",
                          params={"date": "2026-09-24", "offset": "+08:00"}).json()["versions"]
    assert len(versions) == 2
    snapshot = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime/snapshot", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": v1_decided,
    })
    assert snapshot.status_code == 200
    assert snapshot.json()["maintenance_excluded_slots"] == 60

    # 取消窗口：最新版本为 cancelled，新 as_of 下窗口不再生效
    client.post("/api/seismic/maintenance-windows/MW-V-1/cancel", json={"actor": "admin", "reason": "取消"})
    cancelled_report = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": "2026-09-27T00:00:00+00:00", "persist": "false",
    }).json()
    assert cancelled_report["window_versions"] == []
    assert cancelled_report["offline_slots"] == 60


def test_cannot_approve_superseded_version(client):
    _setup_day(client)
    client.post("/api/seismic/maintenance-windows", json={
        "station_code": "BJ01", "channel": "HNZ",
        "start_at": "2026-09-24T10:00:00+08:00", "end_at": "2026-09-24T11:00:00+08:00",
        "reason": "x", "uid": "MW-S-1",
    })
    client.patch("/api/seismic/maintenance-windows/MW-S-1", json={"reason": "y"})
    response = client.post("/api/seismic/maintenance-windows/MW-S-1/decision",
                           json={"version": 1, "decision": "approved", "approver": "a"})
    assert response.status_code == 409


def test_create_window_idempotent(client):
    _setup_day(client)
    payload = {
        "station_code": "BJ01", "channel": "HNZ",
        "start_at": "2026-09-24T10:00:00+08:00", "end_at": "2026-09-24T11:00:00+08:00",
        "reason": "x", "uid": "MW-I-1", "idempotency_key": "batch-20260924-01",
    }
    first = client.post("/api/seismic/maintenance-windows", json=payload)
    second = client.post("/api/seismic/maintenance-windows", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_station_level_window_applies_to_channel(client):
    day_start = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
    maint = (day_start + timedelta(hours=7), day_start + timedelta(hours=8))
    _setup_day(client, missing=[maint])
    client.post("/api/seismic/maintenance-windows", json={
        "station_code": "BJ01",
        "start_at": maint[0].astimezone(TZ8).isoformat(timespec="seconds"),
        "end_at": maint[1].astimezone(TZ8).isoformat(timespec="seconds"),
        "reason": "全站停电维护", "compensation": "excluded", "uid": "MW-STA-1",
    })
    client.post("/api/seismic/maintenance-windows/MW-STA-1/decision",
                json={"decision": "approved", "approver": "boss"})
    report = client.get("/api/seismic/stations/BJ01/channels/HNZ/uptime", params={
        "date": "2026-09-24", "offset": "+08:00", "as_of": "2026-09-26T00:00:00+00:00", "persist": "false",
    }).json()
    assert report["maintenance_excluded_slots"] == 60
    assert report["segments"][0]["sources"][0]["uid"] == "MW-STA-1"
