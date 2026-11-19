import sqlite3
from datetime import timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import logic
from .db import get_db

bp = Blueprint("main", __name__)

KIND_NAMES = {"readings": "读数", "swaps": "换表"}


def data_version(conn):
    """数据版本号 = 最近一个生效批次 id。同一事务内读取，保证版本与内容一致。"""
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS v FROM uploads WHERE status = 'applied'"
    ).fetchone()["v"]


def list_points(conn):
    return [
        r["point"]
        for r in conn.execute(
            "SELECT point FROM ("
            "  SELECT point FROM readings UNION SELECT point FROM swaps"
            ") ORDER BY point"
        )
    ]


def _existing_reading_times(conn, parsed):
    """本次上传涉及计量点的既有读数时刻（写入前读取，用于定位受影响日期）。"""
    points = sorted({point for point, _ in parsed})
    return logic.existing_reading_times(conn, points)


def peak_report(conn, point, day):
    """单日峰值报告，返回 (result, version)。

    已算出且有效的缓存行直接复用，不重读原始读数/换表明细；缺失日期现算并
    写入缓存。写缓存需要写锁，故计算在 BEGIN IMMEDIATE 内完成，与上传串行。
    """
    # 解析日期与读缓存放在同一个读快照里，避免与上传交错
    conn.execute("BEGIN")
    try:
        day = logic.resolve_peak_day(conn, point, day)
        cached, have = logic.cached_peaks_days(conn, point, [day])
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    if day in have:
        return cached[day], version

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 复核缓存后计算：读到的读数/换表与写入的缓存属于同一版本；
        # 若并发请求已算出，则直接复用
        try:
            results, _ = logic.query_peaks(conn, point, [day])
        except Exception as e:  # noqa: BLE001
            raise logic.PeakRecalcError(f"15 分钟尖峰计算失败：{e}") from e
        version = data_version(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return results[day], version


def record_rejected(conn, kind, filename, message, rows_received):
    """被拒批次单独落一条审计记录（不影响既有数据）；审计失败不掩盖原始错误。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, filename, "rejected", message, rows_received, logic.now_ts()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")


@bp.route("/")
def index():
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：页面看到的是同一版快照
    try:
        points = list_points(conn)
        uploads = conn.execute(
            "SELECT * FROM uploads ORDER BY id DESC LIMIT 50"
        ).fetchall()
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "index.html",
        points=points,
        uploads=uploads,
        version=version,
        kind_names=KIND_NAMES,
    )


@bp.route("/upload/<kind>", methods=["POST"])
def upload(kind):
    if kind not in KIND_NAMES:
        abort(404)
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.index"))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("上传被拒绝：文件编码须为 UTF-8", "error")
        return redirect(url_for("main.index"))

    conn = get_db()
    # 1) 解析与格式校验（不进事务）
    try:
        if kind == "readings":
            parsed, raw_count = logic.parse_readings_csv(text)
        else:
            parsed, raw_count = logic.parse_swaps_csv(text)
    except logic.CsvError as e:
        record_rejected(conn, kind, f.filename, str(e), 0)
        flash(f"上传被拒绝：{e}", "error")
        return redirect(url_for("main.index"))

    # 2) 单个写事务：原始记录、受影响峰值缓存与上传记录一起生效；
    #    失败整体回滚，保持旧数据（旧峰值也不会被改坏）
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 写入前先定位受影响日期：用同一事务内的旧读数/换表快照
        existing_times = _existing_reading_times(conn, parsed)
        cur = conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, f.filename, "applied", "", raw_count, logic.now_ts()),
        )
        upload_id = cur.lastrowid
        if kind == "readings":
            applied, skipped, applied_keys = logic.apply_readings(
                conn, upload_id, parsed
            )
            affected = logic.affected_days_for_readings(existing_times, applied_keys)
        else:
            applied, skipped, applied_keys = logic.apply_swaps(
                conn, upload_id, parsed
            )
            affected = logic.affected_days_for_swaps(existing_times, applied_keys)
        msg = f"接收 {raw_count} 行，新增 {applied} 条，重复跳过 {skipped} 条"

        # 只刷新受影响且已有缓存的日期；未受影响的缓存日期原样复用
        try:
            refreshed = logic.refresh_peaks_for_upload(conn, affected)
        except Exception as e:  # noqa: BLE001
            raise logic.PeakRecalcError(
                f"受影响日期的 15 分钟尖峰重算失败：{e}"
            ) from e

        n = sum(len(v) for v in refreshed.values())
        recalc_note = f"；已刷新 {n} 个日期的尖峰缓存" if n else ""
        conn.execute(
            "UPDATE uploads SET rows_applied = ?, message = ? WHERE id = ?",
            (applied, msg + recalc_note, upload_id),
        )
        conn.execute("COMMIT")
        flash(f"{KIND_NAMES[kind]}上传成功：{msg}{recalc_note}", "success")
    except logic.BatchRejected as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, kind, f.filename, str(e), raw_count)
        flash(f"上传被拒绝：{e}", "error")
    except logic.PeakRecalcError as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, kind, f.filename, str(e), raw_count)
        flash(f"上传失败，已保留原数据与原有峰值结果：{e}", "error")
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
    return redirect(url_for("main.index"))


def _resolve_range(conn, point, start, end):
    """校验/补全日期范围，返回 (start_day, end_day)。缺省时取该点数据最近 31 天。"""
    if start and end:
        d0, d1 = logic.parse_day(start), logic.parse_day(end)
    else:
        row = conn.execute(
            "SELECT MIN(substr(ts,1,10)) AS lo, MAX(substr(ts,1,10)) AS hi"
            " FROM readings WHERE point = ?",
            (point,),
        ).fetchone()
        if row is None or row["lo"] is None:
            raise ValueError("该计量点暂无读数，请指定日期范围")
        d1 = logic.parse_day(end) if end else logic.parse_day(row["hi"])
        d0 = logic.parse_day(start) if start else logic.parse_day(row["lo"])
        if (d1 - d0).days > 30:
            d0 = d1 - timedelta(days=30)
    if d1 < d0:
        raise ValueError("结束日期早于开始日期")
    if (d1 - d0).days > logic.MAX_RANGE_DAYS:
        raise ValueError(f"日期范围过大（最多 {logic.MAX_RANGE_DAYS} 天）")
    return d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")


@bp.route("/usage")
def usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：同一版完整结果
    try:
        points = list_points(conn)
        version = data_version(conn)
        days = gaps = total = None
        error = None
        if point:
            try:
                start, end = _resolve_range(conn, point, start, end)
                days, gaps, total = logic.usage_range(conn, point, start, end)
            except ValueError as e:
                error = str(e)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "usage.html",
        points=points,
        point=point,
        start=start,
        end=end,
        days=days,
        gaps=gaps,
        total=total,
        error=error,
        version=version,
    )


@bp.route("/api/usage")
def api_usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if not (point and start and end):
        return jsonify(error="需要 point、start、end 参数（日期为 YYYY-MM-DD）"), 400
    conn = get_db()
    conn.execute("BEGIN")
    try:
        try:
            start, end = _resolve_range(conn, point, start, end)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        days, gaps, total = logic.usage_range(conn, point, start, end)
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return jsonify(
        point=point,
        start=start,
        end=end,
        version=version,
        total_kwh=logic.fmt_num(total),
        days=[
            {"day": d["day"], "kwh": logic.fmt_num(d["kwh"]), "gap": d["gap"]}
            for d in days
        ],
        gaps=[{"start": g["start"], "end": g["end"]} for g in gaps],
    )


def _peak_json(result, version):
    return jsonify(
        point=result["point"],
        day=result["day"],
        version=version,
        status=result["status"],
        peak_kw=result["peak_kw"],
        ties=result["ties"],
        reason=result["reason"],
    )


@bp.route("/peak")
def peak():
    point = request.args.get("point", "").strip()
    day = request.args.get("day", "").strip()
    conn = get_db()
    result = version = error = None
    if point:
        try:
            result, version = peak_report(conn, point, day)
            day = result["day"]
            result = {**result, "point": point}
        except ValueError as e:
            error = str(e)
        except logic.PeakRecalcError as e:
            error = str(e)
        except sqlite3.OperationalError as e:
            error = f"系统繁忙，请重试（{e}）"
    conn.execute("BEGIN")
    try:
        points = list_points(conn)
        if version is None:
            version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "peak.html",
        points=points,
        point=point,
        day=day,
        result=result,
        error=error,
        version=version,
    )


@bp.route("/api/peak")
def api_peak():
    point = request.args.get("point", "").strip()
    day = request.args.get("day", "").strip()
    if not point:
        return jsonify(error="需要 point 参数（日期 day=YYYY-MM-DD，缺省取最后一个区间覆盖日）"), 400
    conn = get_db()
    try:
        result, version = peak_report(conn, point, day)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except logic.PeakRecalcError as e:
        return jsonify(error=str(e)), 500
    except sqlite3.OperationalError as e:
        return jsonify(error=f"系统繁忙，请重试（{e}）"), 503
    return _peak_json({**result, "point": point}, version)

