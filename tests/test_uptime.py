from __future__ import annotations

from datetime import datetime, timedelta, timezone

LOCAL_DAY = "2026-09-24"
OFFSET = 480  # +08:00


def tick_iso(minute: int, *, day: str = LOCAL_DAY, offset: int = OFFSET) -> str:
    """台站本地日历日第 minute 分钟、带偏移的 ISO 时刻。"""
    local = datetime.strptime(day, "%Y-%m-%d") + timedelta(minutes=minute)
    tz = timezone(timedelta(minutes=offset))
    return local.replace(tzinfo=tz).isoformat(timespec="seconds")


def full_day_samples(*, skip=(), offset: int = OFFSET, day: str = LOCAL_DAY):
    skip_set = set(skip)
    return [{"observed_at": tick_iso(i, day=day, offset=offset)} for i in range(1440) if i not in skip_set]


def range_skip(start_minute: int, end_minute: int) -> set[int]:
    """跳过本地 [start,end) 分钟刻度。"""
    return set(range(start_minute, end_minute))


def ingest(client, station, channel, samples, period=60):
    response = client.post(
        "/api/seismic/uptime/samples",
        json={"station_code": station, "channel": channel, "period_seconds": period, "samples": samples},
    )
    assert response.status_code == 202, response.text
    return response.json()


def report(client, station, channel, *, date=LOCAL_DAY, offset=OFFSET, as_of=None, expected=200):
    params = {"date": date, "offset_minutes": offset}
    if as_of:
        params["as_of"] = as_of
    response = client.get(f"/api/seismic/uptime/stations/{station}/channels/{channel}/daily-report", params=params)
    assert response.status_code == expected, response.text
    return response.json()


def create_window(client, payload, expected=201):
    response = client.post("/api/seismic/uptime/maintenance-windows?actor=tester", json=payload)
    assert response.status_code == expected, response.text
    return response.json()


# --------------------------------------------------------------------- 观测


def test_samples_stored_as_utc_with_original_offset(client):
    result = ingest(client, "SC01", "HNZ", [
        {"observed_at": "2026-09-24T08:00:00+08:00"},
        {"observed_at": "2026-09-24T08:01:00+08:00"},
    ])
    assert result["inserted"] == 2
    assert result["first_observed_utc"] == "2026-09-24T00:00:00+00:00"
    assert result["last_observed_utc"] == "2026-09-24T00:01:00+00:00"


def test_duplicate_samples_same_instant_different_offsets(client):
    first = ingest(client, "SC02", "HNZ", [{"observed_at": "2026-09-24T08:00:00+08:00"}])
    assert first["inserted"] == 1
    second = ingest(client, "SC02", "HNZ", [
        {"observed_at": "2026-09-24T00:00:00+00:00"},   # 同一 UTC 时刻
        {"observed_at": "2026-09-24T08:00:00+08:00"},   # 完全重复
    ])
    assert second["inserted"] == 0
    assert second["duplicates"] == 2


def test_observation_without_offset_rejected(client):
    response = client.post("/api/seismic/uptime/samples", json={
        "station_code": "SC03", "channel": "HNZ",
        "samples": [{"observed_at": "2026-09-24T08:00:00"}],
    })
    assert response.status_code == 422


# ------------------------------------------------------ 短暂断链 vs 长离线


def test_brief_disconnect_vs_extended_outage(client):
    missing = range_skip(600, 603) | range_skip(720, 740)  # 3 分钟 + 20 分钟
    ingest(client, "SC10", "HNZ", full_day_samples(skip=missing))
    data = report(client, "SC10", "HNZ")
    assert data["offset"] == "+08:00"
    assert data["day_start_utc"] == "2026-09-23T16:00:00+00:00"
    assert data["day_end_utc"] == "2026-09-24T16:00:00+00:00"
    assert data["missing_seconds"] == 1380
    assert data["observed_seconds"] == 86400 - 1380
    assert data["excluded_seconds"] == 0
    assert data["availability_rate"] == round((86400 - 1380) / 86400, 6)
    reasons = {gap["reason_code"] for gap in data["gaps"]}
    assert reasons == {"brief_disconnect", "extended_outage"}
    assert all(gap["compensation_source"] is None for gap in data["gaps"])
    summary = {(item["status"], item["reason_code"]): item for item in data["reason_summary"]}
    assert summary[("missing", "brief_disconnect")]["seconds"] == 180
    assert summary[("missing", "extended_outage")]["seconds"] == 1200


# ----------------------------------------------------------- 跨午夜维护窗口


def test_cross_midnight_approved_window_is_excluded(client):
    missing = range_skip(0, 30)  # 本地 00:00-00:30 缺测
    ingest(client, "SC11", "HNZ", full_day_samples(skip=missing))
    create_window(client, {
        "station_code": "SC11", "channel": "HNZ",
        "start_at": "2026-09-23T23:30:00+08:00",
        "end_at": "2026-09-24T00:30:00+08:00",
        "reason_code": "planned_maintenance",
        "reason_detail": "跨午夜更换采集器",
        "compensation": "excluded",
        "status": "approved",
        "ticket": "OPS-42",
    })
    data = report(client, "SC11", "HNZ")
    assert data["excluded_seconds"] == 1800
    assert data["missing_seconds"] == 0
    assert data["denominator_seconds"] == 86400 - 1800
    assert data["availability_rate"] == 1.0
    gap = data["gaps"][0]
    assert gap["status"] == "excluded"
    assert gap["start_utc"] == "2026-09-23T16:00:00+00:00"
    assert gap["end_utc"] == "2026-09-23T16:30:00+00:00"
    assert gap["compensation_source"]["compensation"] == "excluded"
    assert gap["compensation_source"]["ticket"] == "OPS-42"
    window = data["windows"][0]
    assert window["submitted_offset"] == "+08:00"
    assert window["start_utc"] == "2026-09-23T16:00:00+00:00"
    assert window["end_utc"] == "2026-09-23T16:30:00+00:00"


def test_draft_window_has_no_effect_until_approved(client):
    missing = range_skip(100, 120)
    ingest(client, "SC12", "HNZ", full_day_samples(skip=missing))
    window = create_window(client, {
        "station_code": "SC12", "channel": "HNZ",
        "start_at": tick_iso(100), "end_at": tick_iso(120),
        "reason_code": "instrument_fault",
    })
    assert window["status"] == "draft"
    before = report(client, "SC12", "HNZ")
    assert before["missing_seconds"] == 1200

    approval = client.post("/api/seismic/uptime/maintenance-windows/%d/approve?actor=chief" % window["id"], json={"approved_by": "王主任"})
    assert approval.status_code == 200
    assert approval.json()["status"] == "approved"
    assert approval.json()["approved_by"] == "王主任"
    # 审批通过后才能登记补偿标记
    patched = client.patch("/api/seismic/uptime/maintenance-windows/%d?actor=chief" % window["id"], json={"compensation": "excluded"})
    assert patched.status_code == 200, patched.text
    after = report(client, "SC12", "HNZ")
    assert after["excluded_seconds"] == 1200
    assert after["missing_seconds"] == 0
    assert after["window_versions"][0]["version"] == 3


def test_compensation_requires_approval(client):
    response = client.post("/api/seismic/uptime/maintenance-windows?actor=tester", json={
        "station_code": "SC13", "channel": "HNZ",
        "start_at": tick_iso(0), "end_at": tick_iso(10),
        "reason_code": "planned_maintenance", "compensation": "backfill",
    })
    assert response.status_code == 422


def test_backfill_compensated_gap_keeps_full_rate(client):
    missing = range_skip(200, 210)
    ingest(client, "SC14", "HNZ", full_day_samples(skip=missing))
    window = create_window(client, {
        "station_code": "SC14", "channel": "HNZ",
        "start_at": tick_iso(200), "end_at": tick_iso(210),
        "reason_code": "telecom_outage", "compensation": "backfill",
        "status": "approved",
    })
    data = report(client, "SC14", "HNZ")
    assert data["compensated_seconds"] == 600
    assert data["missing_seconds"] == 0
    assert data["availability_rate"] == 1.0
    gap = data["gaps"][0]
    assert gap["status"] == "compensated"
    assert gap["reason_code"] == "telecom_outage"
    assert gap["compensation_source"]["window_id"] == window["id"]
    assert gap["compensation_source"]["version"] == 1


# ------------------------------------------------------------- 窗口确定性


def test_overlapping_same_scope_windows_rejected_but_adjacent_allowed(client):
    payload = {
        "station_code": "SC20", "channel": "HNZ",
        "start_at": tick_iso(300), "end_at": tick_iso(360),
        "reason_code": "planned_maintenance",
    }
    create_window(client, payload)
    overlap = client.post("/api/seismic/uptime/maintenance-windows?actor=tester", json={
        "station_code": "SC20", "channel": "HNZ",
        "start_at": tick_iso(330), "end_at": tick_iso(400),
        "reason_code": "planned_maintenance",
    })
    assert overlap.status_code == 409
    assert overlap.json()["error"]["code"] == "conflict"

    adjacent = client.post("/api/seismic/uptime/maintenance-windows?actor=tester", json={
        "station_code": "SC20", "channel": "HNZ",
        "start_at": tick_iso(360), "end_at": tick_iso(400),
        "reason_code": "planned_maintenance",
    })
    assert adjacent.status_code == 201, adjacent.text


def test_station_level_window_covers_channel_gap(client):
    missing = range_skip(500, 505)
    ingest(client, "SC21", "HNZ", full_day_samples(skip=missing))
    create_window(client, {
        "station_code": "SC21", "channel": "*",
        "start_at": tick_iso(480), "end_at": tick_iso(540),
        "reason_code": "power_outage", "compensation": "excluded", "status": "approved",
    })
    data = report(client, "SC21", "HNZ")
    # 排除时长按审批窗口完整时长（1 小时）从分母扣除；缺测段仅 300 秒。
    assert data["excluded_seconds"] == 3600
    assert data["denominator_seconds"] == 86400 - 3600
    assert data["missing_seconds"] == 0
    assert data["windows"][0]["scope"] == "station"
    gap = next(g for g in data["gaps"] if g["status"] == "excluded")
    assert gap["duration_seconds"] == 300
    assert gap["compensation_source"]["compensation"] == "excluded"


def test_client_token_makes_creation_idempotent(client):
    payload = {
        "station_code": "SC22", "channel": "HNZ",
        "start_at": tick_iso(0), "end_at": tick_iso(10),
        "reason_code": "other", "client_token": "ticket-777",
    }
    first = create_window(client, payload)
    second = create_window(client, payload)
    assert first["id"] == second["id"]


# ---------------------------------------------------- 版本化与历史报表重算


def test_window_versions_and_historical_recompute(client):
    station = "SC30"
    missing = range_skip(90, 110)
    ingest(client, station, "HNZ", full_day_samples(day="2026-09-10", skip=missing))

    before_window = report(client, station, "HNZ", date="2026-09-10", as_of="2026-09-09T00:00:00+00:00")
    assert before_window["missing_seconds"] == 1200
    assert before_window["availability_rate"] < 1.0

    window = create_window(client, {
        "station_code": station, "channel": "HNZ",
        "start_at": tick_iso(90, day="2026-09-10"), "end_at": tick_iso(110, day="2026-09-10"),
        "reason_code": "planned_maintenance", "compensation": "excluded", "status": "approved",
    })
    # 审批后再修改补偿方式，产生第二个版本
    patched = client.patch("/api/seismic/uptime/maintenance-windows/%d?actor=tester" % window["id"], json={
        "reason_detail": "实际为通信割接", "compensation": "backfill",
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["version"] == 2

    versions = client.get("/api/seismic/uptime/maintenance-windows/%d/versions" % window["id"]).json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert [v["action"] for v in versions] == ["create", "update"]

    current = report(client, station, "HNZ", date="2026-09-10")
    assert current["compensated_seconds"] == 1200
    assert current["availability_rate"] == 1.0
    assert current["window_versions"][0]["version"] == 2

    # 按历史时刻重算：窗口尚不存在，缺测依旧，原始观测未被改写
    historical = report(client, station, "HNZ", date="2026-09-10", as_of="2026-09-09T00:00:00+00:00")
    assert historical["missing_seconds"] == 1200
    assert historical["windows"] == []

    # 同一 as_of 重算落到同一不可变运行记录
    again = report(client, station, "HNZ", date="2026-09-10", as_of="2026-09-09T00:00:00+00:00")
    assert again["report_run_id"] == historical["report_run_id"]
    stored = client.get("/api/seismic/uptime/report-runs/%d" % historical["report_run_id"])
    assert stored.status_code == 200
    assert stored.json()["missing_seconds"] == 1200


def test_cancel_window_restores_missing_classification(client):
    missing = range_skip(300, 304)
    ingest(client, "SC31", "HNZ", full_day_samples(skip=missing))
    window = create_window(client, {
        "station_code": "SC31", "channel": "HNZ",
        "start_at": tick_iso(300), "end_at": tick_iso(304),
        "reason_code": "planned_maintenance", "compensation": "excluded", "status": "approved",
    })
    approved = report(client, "SC31", "HNZ")
    assert approved["missing_seconds"] == 0

    cancelled = client.post("/api/seismic/uptime/maintenance-windows/%d/cancel?actor=tester" % window["id"])
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["version"] == 2

    after = report(client, "SC31", "HNZ")
    assert after["missing_seconds"] == 240
    assert after["excluded_seconds"] == 0


# ------------------------------------------------------------- 更多边界场景


def test_channel_window_takes_attribution_priority_over_station_window(client):
    missing = range_skip(600, 610)
    ingest(client, "SC40", "HNZ", full_day_samples(skip=missing))
    create_window(client, {
        "station_code": "SC40", "channel": "*",
        "start_at": tick_iso(540), "end_at": tick_iso(660),
        "reason_code": "power_outage", "compensation": "excluded", "status": "approved",
    })
    channel_window = create_window(client, {
        "station_code": "SC40", "channel": "HNZ",
        "start_at": tick_iso(600), "end_at": tick_iso(610),
        "reason_code": "instrument_fault", "compensation": "excluded", "status": "approved",
    })
    data = report(client, "SC40", "HNZ")
    gap = next(g for g in data["gaps"] if g["status"] == "excluded")
    assert gap["compensation_source"]["window_id"] == channel_window["id"]
    assert gap["reason_code"] == "instrument_fault"
    # 台站级窗口在通道列表中同样可见
    listed = client.get("/api/seismic/uptime/stations/SC40/maintenance-windows", params={"channel": "HNZ"}).json()["windows"]
    assert {w["channel"] for w in listed} == {"*", "HNZ"}


def test_reschedule_into_overlap_rejected(client):
    create_window(client, {
        "station_code": "SC41", "channel": "HNZ",
        "start_at": tick_iso(0), "end_at": tick_iso(60), "reason_code": "other",
    })
    second = create_window(client, {
        "station_code": "SC41", "channel": "HNZ",
        "start_at": tick_iso(120), "end_at": tick_iso(180), "reason_code": "other",
    })
    response = client.patch("/api/seismic/uptime/maintenance-windows/%d?actor=tester" % second["id"], json={
        "start_at": tick_iso(30), "end_at": tick_iso(90),
    })
    assert response.status_code == 409


def test_cancelled_window_still_visible_at_earlier_as_of(client):
    missing = range_skip(90, 110)
    ingest(client, "SC42", "HNZ", full_day_samples(day="2026-09-10", skip=missing))
    window = create_window(client, {
        "station_code": "SC42", "channel": "HNZ",
        "start_at": tick_iso(90, day="2026-09-10"), "end_at": tick_iso(110, day="2026-09-10"),
        "reason_code": "planned_maintenance", "compensation": "excluded", "status": "approved",
    })
    approved_at = client.get("/api/seismic/uptime/maintenance-windows/%d" % window["id"]).json()["created_at"]
    assert client.post("/api/seismic/uptime/maintenance-windows/%d/cancel?actor=tester" % window["id"]).status_code == 200

    now_report = report(client, "SC42", "HNZ", date="2026-09-10")
    assert now_report["missing_seconds"] == 1200
    historical = report(client, "SC42", "HNZ", date="2026-09-10", as_of=approved_at)
    assert historical["excluded_seconds"] == 1200
    assert historical["missing_seconds"] == 0


def test_same_gap_split_differently_by_report_offset(client):
    # 本地(+08) 07:30-08:30 缺测，跨过 UTC 午夜。
    missing = range_skip(450, 510)
    samples = full_day_samples(skip=missing)
    # UTC 日报覆盖 00:00–24:00 UTC，相当于本地次日 00:00–08:00 仍需有观测。
    samples += [{"observed_at": tick_iso(i, day="2026-09-25")} for i in range(480)]
    ingest(client, "SC43", "HNZ", samples)
    local_report = report(client, "SC43", "HNZ", offset=480)
    assert local_report["missing_seconds"] == 3600
    assert local_report["day_start_utc"] == "2026-09-23T16:00:00+00:00"

    utc_report = report(client, "SC43", "HNZ", offset=0)
    assert utc_report["day_start_utc"] == "2026-09-24T00:00:00+00:00"
    assert utc_report["day_end_utc"] == "2026-09-25T00:00:00+00:00"
    # UTC 日内只包含本地 08:00-08:30 这段（30 分钟），其余落在前一日。
    assert utc_report["missing_seconds"] == 1800
    gap = utc_report["gaps"][0]
    assert gap["start_utc"] == "2026-09-24T00:00:00+00:00"
    assert gap["end_utc"] == "2026-09-24T00:30:00+00:00"


def test_period_must_divide_day(client):
    response = client.get(
        "/api/seismic/uptime/stations/SC44/channels/HNZ/daily-report",
        params={"date": LOCAL_DAY, "offset_minutes": 480, "period_seconds": 37},
    )
    assert response.status_code == 422


def test_open_ended_window_covers_ongoing_outage(client):
    missing = range_skip(720, 730)
    ingest(client, "SC45", "HNZ", full_day_samples(skip=missing))
    create_window(client, {
        "station_code": "SC45", "channel": "HNZ",
        "start_at": tick_iso(720),
        "reason_code": "planned_maintenance", "compensation": "excluded", "status": "approved",
    })
    data = report(client, "SC45", "HNZ")
    assert data["missing_seconds"] == 0
    assert data["availability_rate"] == 1.0
    window = data["windows"][0]
    assert window["open_ended"] is True
    assert window["end_utc"] == data["day_end_utc"]
    assert data["excluded_seconds"] == 12 * 3600


def test_outage_before_window_start_is_still_counted(client):
    # 缺测 09:30-10:30，窗口只批了 10:00-11:00。
    missing = range_skip(570, 630)
    ingest(client, "SC46", "HNZ", full_day_samples(skip=missing))
    create_window(client, {
        "station_code": "SC46", "channel": "HNZ",
        "start_at": tick_iso(600), "end_at": tick_iso(660),
        "reason_code": "planned_maintenance", "compensation": "excluded", "status": "approved",
    })
    data = report(client, "SC46", "HNZ")
    assert data["missing_seconds"] == 1800
    excluded_gap = next(g for g in data["gaps"] if g["status"] == "excluded")
    missing_gap = next(g for g in data["gaps"] if g["status"] == "missing")
    assert excluded_gap["duration_seconds"] == 1800
    assert missing_gap["duration_seconds"] == 1800
    assert missing_gap["reason_code"] == "extended_outage"


def test_unknown_channel_full_day_is_offline(client):
    data = report(client, "SC99", "HNZ")
    assert data["missing_seconds"] == 86400
    assert data["availability_rate"] == 0.0
    assert all(g["reason_code"] == "extended_outage" for g in data["gaps"])


def test_time_accounting_identity_across_mixed_classifications(client):
    # 缺测三段：00:00-00:10 短暂断链、02:00-03:00 排除窗口、04:00-04:10 补偿窗口
    missing = range_skip(0, 10) | range_skip(120, 180) | range_skip(240, 250)
    ingest(client, "SC50", "HNZ", full_day_samples(skip=missing))
    create_window(client, {
        "station_code": "SC50", "channel": "HNZ",
        "start_at": tick_iso(120), "end_at": tick_iso(180),
        "reason_code": "planned_maintenance", "compensation": "excluded", "status": "approved",
    })
    create_window(client, {
        "station_code": "SC50", "channel": "HNZ",
        "start_at": tick_iso(240), "end_at": tick_iso(250),
        "reason_code": "telecom_outage", "compensation": "interpolated", "status": "approved",
    })
    data = report(client, "SC50", "HNZ")
    assert data["missing_seconds"] == 600
    assert data["excluded_seconds"] == 3600
    assert data["compensated_seconds"] == 600
    # 互斥四分类之和恒等于整日
    assert (
        data["observed_seconds"] + data["excluded_seconds"]
        + data["compensated_seconds"] + data["missing_seconds"]
    ) == 86400
    assert data["denominator_seconds"] == 86400 - 3600
    assert data["availability_rate"] == round(
        (data["observed_seconds"] + data["compensated_seconds"]) / data["denominator_seconds"], 6
    )


def test_window_over_period_with_actual_samples_only_excludes_window(client):
    # 整日有观测，但窗口覆盖了 06:00-07:00（窗口内同样有观测）。
    ingest(client, "SC51", "HNZ", full_day_samples())
    create_window(client, {
        "station_code": "SC51", "channel": "HNZ",
        "start_at": tick_iso(360), "end_at": tick_iso(420),
        "reason_code": "planned_maintenance", "compensation": "excluded", "status": "approved",
    })
    data = report(client, "SC51", "HNZ")
    assert data["missing_seconds"] == 0
    assert data["gaps"] == []
    assert data["excluded_seconds"] == 3600
    assert data["observed_seconds"] == 86400 - 3600
    assert data["availability_rate"] == 1.0
