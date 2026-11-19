import io
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "t"}
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, kind, csv_text, filename="data.csv"):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    return client.post(
        f"/upload/{kind}", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def usage(client, point, start, end):
    resp = client.get(f"/api/usage?point={point}&start={start}&end={end}")
    assert resp.status_code == 200
    return resp.get_json()


def day_kwh(result, day):
    return next(d["kwh"] for d in result["days"] if d["day"] == day)


def peak(client, point, day):
    resp = client.get(f"/api/peak?point={point}&day={day}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# ---------------------------------------------------------------- 基本规则

def test_basic_daily_usage(client):
    csv_text = (
        "point,ts,value\n"
        "A,2026-01-01T00:00:00Z,100\n"
        "A,2026-01-02T00:00:00Z,150\n"
        "A,2026-01-03T00:00:00Z,230\n"
    )
    resp = upload(client, "readings", csv_text)
    assert resp.status_code == 200
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-01") == "0"      # 首条读数无前一读数，不计增量
    assert day_kwh(r, "2026-01-02") == "50"     # 增量计入后次读数所属 UTC 日
    assert day_kwh(r, "2026-01-03") == "80"
    assert r["total_kwh"] == "130"
    assert r["gaps"] == []


def test_decimal_precision(client):
    csv_text = (
        "point,ts,value\n"
        "A,2026-01-01T00:00:00Z,0.1\n"
        "A,2026-01-02T00:00:00Z,0.3\n"
    )
    upload(client, "readings", csv_text)
    r = usage(client, "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "0.2"    # 十进制精确，不是 0.19999...


def test_idempotent_reupload(client):
    csv_text = (
        "point,ts,value\n"
        "A,2026-01-01T00:00:00Z,100\n"
        "A,2026-01-02T00:00:00Z,150\n"
    )
    upload(client, "readings", csv_text)
    resp = upload(client, "readings", csv_text)  # 原样重传
    assert "重复跳过 2 条" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "50"      # 不重复计数


def test_conflict_same_ts_different_value_rejected(client):
    upload(client, "readings", "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                               "A,2026-01-02T00:00:00Z,150\n")
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-02T00:00:00Z,151\n"
                  "A,2026-01-03T00:00:00Z,200\n")
    body = resp.get_data(as_text=True)
    assert "已存在不同读数" in body and "整批拒绝" in body
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "50"      # 旧数据保持不变
    assert day_kwh(r, "2026-01-03") == "0"       # 同批其它行也未生效


def test_conflict_within_file_rejected(client):
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                  "A,2026-01-01T00:00:00Z,101\n")
    assert "同一计量点同一时刻出现不同读数" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-01")
    assert r["total_kwh"] == "0"


def test_negative_delta_rejected_and_atomic(client):
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                  "A,2026-01-02T00:00:00Z,90\n")   # 倒退
    body = resp.get_data(as_text=True)
    assert "读数倒退" in body and "整批拒绝" in body
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["total_kwh"] == "0"                  # 整批未生效


def test_bad_row_makes_whole_batch_fail(client):
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                  "A,not-a-time,150\n")
    assert "上传被拒绝" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-01")
    assert r["total_kwh"] == "0"


def test_negative_value_and_naive_ts_rejected(client):
    assert "不能为负" in upload(
        client, "readings", "point,ts,value\nA,2026-01-01T00:00:00Z,-1\n"
    ).get_data(as_text=True)
    assert "UTC" in upload(
        client, "readings", "point,ts,value\nA,2026-01-01 08:00:00,1\n"
    ).get_data(as_text=True)
    assert "UTC" in upload(
        client, "readings", "point,ts,value\nA,2026-01-01T08:00:00+08:00,1\n"
    ).get_data(as_text=True)


# ---------------------------------------------------------------- 换表与缺口

def test_swap_creates_gap_and_revokes_delta(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n"
           "A,2026-01-03T00:00:00Z,170\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "50"

    # 补录换表（发生在后次读数时刻）→ 原计入的 50 撤销，区间变缺口
    upload(client, "swaps", "point,ts\nA,2026-01-02T00:00:00Z\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "0"
    assert day_kwh(r, "2026-01-03") == "20"      # 换表后的区间仍正常计量
    assert len(r["gaps"]) == 1
    assert r["gaps"][0]["start"] == "2026-01-01T00:00:00.000000Z"
    assert r["gaps"][0]["end"] == "2026-01-02T00:00:00.000000Z"
    gap_day = next(d for d in r["days"] if d["day"] == "2026-01-02")
    assert gap_day["gap"] is True


def test_swap_between_readings_marks_gap(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,180\n")
    upload(client, "swaps", "point,ts\nA,2026-01-02T12:00:00Z\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert r["total_kwh"] == "0"                  # 换表区间不推算增量
    assert len(r["gaps"]) == 1


def test_swap_at_earlier_reading_does_not_gap_following_interval(client):
    # 换表时刻 == 前次读数时刻：属于上一个区间（含后次时刻），不影响后一区间
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    upload(client, "swaps", "point,ts\nA,2026-01-01T00:00:00Z\n")
    r = usage(client, "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "50"


def test_swap_upload_idempotent(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    upload(client, "swaps", "point,ts\nA,2026-01-02T00:00:00Z\n")
    resp = upload(client, "swaps", "point,ts\nA,2026-01-02T00:00:00Z\n")
    assert "重复跳过 1 条" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert len(r["gaps"]) == 1


# ---------------------------------------------------------------- 晚到读数

def test_late_reading_splits_interval(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-03") == "60"

    # 晚到读数拆分旧区间：60 拆成 30 + 30
    upload(client, "readings", "point,ts,value\nA,2026-01-02T00:00:00Z,130\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "30"
    assert day_kwh(r, "2026-01-03") == "30"
    assert r["total_kwh"] == "60"


def test_late_reading_causing_backwards_rejected(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    resp = upload(client, "readings", "point,ts,value\nA,2026-01-02T00:00:00Z,200\n")
    assert "读数倒退" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-03") == "60"       # 旧数据保持


# ---------------------------------------------------------------- 页面

def test_pages_render(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    assert client.get("/").status_code == 200
    resp = client.get("/usage?point=A&start=2026-01-01&end=2026-01-02")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "50" in body
    # 默认日期范围（不填日期）
    assert client.get("/usage?point=A").status_code == 200


# ---------------------------------------------------------------- 并发一致性

def test_concurrent_uploads_and_queries(app):
    """并发上传 + 查询：每次查询看到的都是某一版的完整结果。"""
    points = [f"P{i}" for i in range(4)]
    days = [f"2026-01-{d:02d}" for d in range(1, 11)]

    def csv_for(point):
        lines = ["point,ts,value"]
        for i, day in enumerate(days):
            lines.append(f"{point},{day}T00:00:00Z,{1000 + i * 10}")
        return "\n".join(lines) + "\n"

    errors = []

    def do_upload(point):
        c = app.test_client()
        try:
            resp = upload(c, "readings", csv_for(point))
            if resp.status_code != 200:
                errors.append(f"upload {point}: {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"upload {point}: {e}")

    def do_query():
        c = app.test_client()
        expected_full = str(sum(10 for _ in range(9)))  # 90
        for _ in range(30):
            for p in points:
                try:
                    r = usage(c, p, "2026-01-01", "2026-01-10")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"query {p}: {e}")
                    return
                # 单批原子性：要么整批未生效(0)，要么整批完整生效(90)
                if r["total_kwh"] not in ("0", expected_full):
                    errors.append(f"query {p}: 看到部分结果 {r['total_kwh']}")
                    return

    threads = ([threading.Thread(target=do_upload, args=(p,)) for p in points]
               + [threading.Thread(target=do_query) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    c = app.test_client()
    for p in points:
        r = usage(c, p, "2026-01-01", "2026-01-10")
        assert r["total_kwh"] == "90"


def test_concurrent_identical_uploads_idempotent(app):
    csv_text = ("point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                "A,2026-01-02T00:00:00Z,150\n")
    errors = []

    def do_upload():
        c = app.test_client()
        try:
            resp = upload(c, "readings", csv_text)
            if resp.status_code != 200:
                errors.append(resp.status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    threads = [threading.Thread(target=do_upload) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    r = usage(app.test_client(), "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "50"  # 并发重传不重复计数


# ---------------------------------------------------------------- 15 分钟尖峰

def readings_csv(point, rows):
    lines = ["point,ts,value"]
    lines += [f"{point},{ts},{value}" for ts, value in rows]
    return "\n".join(lines) + "\n"


def test_peak_basic_and_ties(client):
    # 当日 96 kWh 均匀消耗：每 15 分钟 1 kWh -> 4 kW，96 个时段全部并列
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    r = peak(client, "A", "2026-01-01")
    assert r["status"] == "ok"
    assert r["peak_kw"] == "4"
    assert len(r["ties"]) == 96
    assert r["ties"][0] == {
        "start": "2026-01-01T00:00:00.000000Z",
        "end": "2026-01-01T00:15:00.000000Z",
        "kw": "4",
    }
    # 次条读数所在日没有区间覆盖（不算前一条读数的日用量）
    r2 = peak(client, "A", "2026-01-02")
    assert r2["status"] == "none"
    assert "读数区间覆盖" in r2["reason"]


def test_peak_proportional_overlap(client):
    # 区间 00:00->00:20 共 10 kWh：00:00-00:15 分得 7.5 -> 30 kW；
    # 00:15-00:20 只有 5 分钟覆盖，时段不完整，不参与比较
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-01T00:20:00Z", "10"),
    ]))
    r = peak(client, "A", "2026-01-01")
    assert r["status"] == "ok"
    assert r["peak_kw"] == "30"
    assert len(r["ties"]) == 1
    assert r["ties"][0]["start"] == "2026-01-01T00:00:00.000000Z"


def test_peak_uses_distinct_values_not_daily_average(client):
    # 日总量同为 24 kWh，但尖峰集中在 00:00-00:15（占比 1/2）-> 48 kW；
    # 若只按日用量平均会得到 1 kW，说明尖峰不会被日用量掩盖
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-01T00:15:00Z", "12"),
        ("2026-01-02T00:00:00Z", "24"),
    ]))
    r = peak(client, "A", "2026-01-01")
    # 00:00-00:15: 12 kWh -> 48 kW；其余时段按 12 kWh / 885 min 分摊
    assert r["peak_kw"] == "48"
    assert len(r["ties"]) == 1


def test_peak_segments_align_to_utc_midnight(client):
    # 读数起点不在时段边界：00:07:30 -> 00:37:30，电量 6 kWh（0.2 kWh/分钟）。
    # 00:00-00:15 与 00:30-00:45 都不完整；只有 00:15-00:30 全段覆盖：
    # 3 kWh -> 12 kW，说明时段严格从 UTC 零点起每 15 分钟划分
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:07:30Z", "0"),
        ("2026-01-01T00:37:30Z", "6"),
    ]))
    r = peak(client, "A", "2026-01-01")
    assert r["status"] == "ok"
    assert r["peak_kw"] == "12"
    assert len(r["ties"]) == 1
    assert r["ties"][0]["start"] == "2026-01-01T00:15:00.000000Z"


def test_peak_cross_day_interval_split(client):
    # 22:00 -> 次日 02:00，8 kWh：每天各被覆盖 2 小时 = 8 个完整时段，
    # 每小时 2 kWh -> 每时段 0.5 kWh -> 2 kW，8 个时段并列
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T22:00:00Z", "0"),
        ("2026-01-02T02:00:00Z", "8"),
    ]))
    r1 = peak(client, "A", "2026-01-01")
    r2 = peak(client, "A", "2026-01-02")
    assert r1["peak_kw"] == "2" and len(r1["ties"]) == 8
    assert r2["peak_kw"] == "2" and len(r2["ties"]) == 8


def test_peak_gap_excluded_and_reason(client):
    # 当日只有一条跨日区间，但换表落在区间内 -> 全段缺口
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    upload(client, "swaps", "point,ts\nA,2026-01-01T12:00:00Z\n")
    r = peak(client, "A", "2026-01-01")
    assert r["status"] == "none"
    assert "换表" in r["reason"]


def test_peak_partial_coverage_no_complete_segment(client):
    # 只有 10 分钟覆盖，没有任何完整 15 分钟时段
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-01T00:10:00Z", "5"),
    ]))
    r = peak(client, "A", "2026-01-01")
    assert r["status"] == "none"
    assert "15 分钟" in r["reason"]


def test_peak_reused_without_reading_detail(app, monkeypatch):
    """查询有效结果时直接复用缓存，不再读取原始明细。"""
    from app import logic

    c = app.test_client()
    upload(c, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    first = peak(c, "A", "2026-01-01")
    assert first["status"] == "ok"

    def boom(*args, **kwargs):
        raise AssertionError("命中缓存时不应读取原始读数/换表明细")

    monkeypatch.setattr(logic, "point_state", boom)
    again = peak(c, "A", "2026-01-01")
    assert again["peak_kw"] == first["peak_kw"]
    assert again["ties"] == first["ties"]


def test_late_reading_invalidates_affected_day_only(client, app, monkeypatch):
    from app import logic

    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-04T00:00:00Z", "1728"),   # 每 15 分钟 6 kWh -> 24 kW
        ("2026-01-06T00:00:00Z", "2112"),   # 01-04 ~ 01-06：8 kW
    ]))
    before = {d: peak(client, "A", d) for d in
              ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05")}
    assert before["2026-01-01"]["peak_kw"] == "24"
    assert before["2026-01-05"]["peak_kw"] == "8"

    calls = []
    real = logic.recompute_peaks

    def spy(conn, point, days):
        calls.append((point, sorted(days)))
        return real(conn, point, days)

    monkeypatch.setattr(logic, "recompute_peaks", spy)

    # 在 01-02 中午补一条读数：拆分出 (01-01, 01-02 12:00] 与
    # (01-02 12:00, 01-04] 两个跨日区间；后者与 01-04 无正重合，
    # 受影响日期为 01-01 ~ 01-03
    upload(client, "readings", "point,ts,value\nA,2026-01-02T12:00:00Z,288\n")
    assert calls == [("A", ["2026-01-01", "2026-01-02", "2026-01-03"])]
    # 查询已刷新的日期不再触发重算：288 kWh / 36 h -> 8 kW
    after_01 = peak(client, "A", "2026-01-01")
    assert after_01["peak_kw"] == "8"
    assert calls == [("A", ["2026-01-01", "2026-01-02", "2026-01-03"])]

    # 01-04 虽然紧邻被改动的区间终点，但该区间不与 01-04 重合，
    # 其结果仍来自 01-04 -> 01-06 区间，缓存直接复用
    after_04 = peak(client, "A", "2026-01-04")
    assert after_04["peak_kw"] == before["2026-01-04"]["peak_kw"]
    assert after_04["ties"] == before["2026-01-04"]["ties"]

    # 未受影响的 01-05 继续复用旧结果，查询也不触发重算
    after_05 = peak(client, "A", "2026-01-05")
    assert after_05["peak_kw"] == before["2026-01-05"]["peak_kw"]
    assert after_05["ties"] == before["2026-01-05"]["ties"]
    assert calls == [("A", ["2026-01-01", "2026-01-02", "2026-01-03"])]


def test_swap_fill_invalidates_affected_days(client):
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
        ("2026-01-03T00:00:00Z", "192"),
    ]))
    assert peak(client, "A", "2026-01-01")["peak_kw"] == "4"
    assert peak(client, "A", "2026-01-02")["peak_kw"] == "4"

    # 补录换表：落在 01-01 00:00 -> 01-02 00:00 的区间内，只影响 01-01 这一天
    upload(client, "swaps", "point,ts\nA,2026-01-01T06:00:00Z\n")
    r1 = peak(client, "A", "2026-01-01")
    assert r1["status"] == "none" and "换表" in r1["reason"]
    # 01-02 由 01-02 -> 01-03 的区间覆盖，不受影响，缓存继续复用
    assert peak(client, "A", "2026-01-02")["peak_kw"] == "4"
    # 01-03 是最后一条读数所在日，本就没有区间覆盖
    assert peak(client, "A", "2026-01-03")["status"] == "none"


def test_swap_cross_day_interval_invalidates_both_days(client):
    # 22:00 -> 次日 02:00，补录 23:00 的换表：两天都受影响
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T22:00:00Z", "0"),
        ("2026-01-02T02:00:00Z", "8"),
        ("2026-01-02T06:00:00Z", "16"),
    ]))
    assert peak(client, "A", "2026-01-01")["status"] == "ok"
    assert peak(client, "A", "2026-01-02")["status"] == "ok"
    upload(client, "swaps", "point,ts\nA,2026-01-01T23:00:00Z\n")
    r1 = peak(client, "A", "2026-01-01")
    r2 = peak(client, "A", "2026-01-02")
    assert r1["status"] == "none"
    # 01-02 的 02:00-06:00 区间仍正常，但 02:00 前被缺口覆盖，尖峰来自 02:00+
    assert r2["status"] == "ok"
    assert r2["ties"][0]["start"] >= "2026-01-02T02:00:00"


def test_failed_upload_keeps_original_peak(client):
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    assert peak(client, "A", "2026-01-01")["peak_kw"] == "4"
    # 读数倒退 -> 整批拒绝，原峰值保留
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T12:00:00Z,120\n")
    assert "读数倒退" in resp.get_data(as_text=True)
    assert peak(client, "A", "2026-01-01")["peak_kw"] == "4"


def test_peak_stale_cache_never_visible_after_upload(app):
    """上传成功后再查询，拿到的一定是新数据对应的峰值。"""
    c = app.test_client()
    upload(c, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    assert peak(c, "A", "2026-01-01")["peak_kw"] == "4"
    upload(c, "readings", "point,ts,value\nA,2026-01-01T12:00:00Z,96\n")
    assert peak(c, "A", "2026-01-01")["peak_kw"] == "8"


def test_peak_recalc_failure_surfaces(app, monkeypatch):
    """重算失败须提示，且原数据与旧峰值保持不变。"""
    from app import logic

    c = app.test_client()
    upload(c, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    assert peak(c, "A", "2026-01-01")["peak_kw"] == "4"
    before = peak(c, "A", "2026-01-01")

    def boom(*args, **kwargs):
        raise RuntimeError("模拟重算故障")

    monkeypatch.setattr(logic, "recompute_peaks", boom)
    resp = upload(c, "readings", "point,ts,value\nA,2026-01-01T12:00:00Z,96\n")
    body = resp.get_data(as_text=True)
    assert "重算失败" in body and "保留原数据" in body
    # 事务整体回滚：读数与峰值都是旧的
    r = peak(c, "A", "2026-01-01")
    assert r["peak_kw"] == before["peak_kw"]
    assert r["ties"] == before["ties"]


def test_peak_page_renders(client):
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    resp = client.get("/peak?point=A&day=2026-01-01")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "4" in body and "并列时段" in body
    assert client.get("/peak?point=A").status_code == 200  # 缺省日期
    assert client.get("/api/peak?point=A").status_code == 200


def test_peak_rejects_bad_day_and_unknown_point(client):
    upload(client, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    assert client.get("/api/peak?point=A&day=2026-13-01").status_code == 400
    assert client.get("/api/peak?point=A&day=x").status_code == 400
    assert client.get("/api/peak").status_code == 400
    assert client.get("/api/peak?point=A").status_code == 200


def test_peak_concurrent_queries_consistent(app):
    csv_text = readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
        ("2026-01-03T00:00:00Z", "192"),
    ])
    upload(app.test_client(), "readings", csv_text)
    errors = []

    def do_query():
        c = app.test_client()
        for _ in range(15):
            for day in ("2026-01-01", "2026-01-02"):
                try:
                    r = peak(c, "A", day)
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e))
                    return
                if r["status"] != "ok" or r["peak_kw"] != "4":
                    errors.append(f"{day}: {r}")
                    return

    threads = [threading.Thread(target=do_query) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_idempotent_reupload_does_not_recalc(app, monkeypatch):
    """原样重传（幂等跳过）不算受影响，已算日期继续复用。"""
    from app import logic

    c = app.test_client()
    csv_text = readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ])
    upload(c, "readings", csv_text)
    assert peak(c, "A", "2026-01-01")["peak_kw"] == "4"

    calls = []
    real = logic.recompute_peaks
    monkeypatch.setattr(
        logic, "recompute_peaks",
        lambda conn, point, days: calls.append((point, sorted(days)))
        or real(conn, point, days),
    )
    upload(c, "readings", csv_text)
    assert calls == []
    assert peak(c, "A", "2026-01-01")["peak_kw"] == "4"
    assert calls == []


def test_peak_query_and_upload_concurrent(app):
    """并发查询与上传：查询要么看到上传前的完整结果，要么看到上传后的完整结果。"""
    c = app.test_client()
    upload(c, "readings", readings_csv("A", [
        ("2026-01-01T00:00:00Z", "0"),
        ("2026-01-02T00:00:00Z", "96"),
    ]))
    assert peak(c, "A", "2026-01-01")["peak_kw"] == "4"
    errors = []
    done = threading.Event()

    def do_upload():
        try:
            upload(app.test_client(), "readings",
                   "point,ts,value\nA,2026-01-01T12:00:00Z,96\n")
        except Exception as e:  # noqa: BLE001
            errors.append(f"upload: {e}")
        finally:
            done.set()

    def do_query():
        c2 = app.test_client()
        while not done.is_set():
            try:
                r = peak(c2, "A", "2026-01-01")
            except Exception as e:  # noqa: BLE001
                errors.append(f"query: {e}")
                return
            if r["status"] != "ok" or r["peak_kw"] not in ("4", "8"):
                errors.append(f"unexpected: {r}")
                return

    t_up = threading.Thread(target=do_upload)
    t_qs = [threading.Thread(target=do_query) for _ in range(3)]
    for t in t_qs:
        t.start()
    t_up.start()
    t_up.join()
    for t in t_qs:
        t.join()
    assert not errors, errors
    assert peak(c, "A", "2026-01-01")["peak_kw"] == "8"

