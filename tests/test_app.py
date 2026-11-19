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
