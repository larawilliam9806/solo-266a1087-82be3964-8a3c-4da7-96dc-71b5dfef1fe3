"""核心业务规则。

计量模型
--------
每个计量点的读数按时刻排序，相邻两条读数构成一个区间 (t1, t2]：

* 若区间 (t1, t2] 内发生过换表（换表时刻 s 满足 t1 < s <= t2，即含后次读数时刻），
  该区间标记为缺口，不推算增量；
* 否则增量 = 后次读数 - 前次读数，必须非负（倒退则整批拒绝），
  增量计入后次读数所属的 UTC 日。

晚到读数插入后自然拆分旧区间；补录换表后原区间变为缺口，已计入的增量自动撤销。
日用量不做冗余物化，查询时在单个读事务内由原始记录现算，
因此“原始记录”和“受影响日结果”永远在同一事务里一起生效。

15 分钟尖峰另按 (计量点, UTC 日) 物化缓存，见文件后半部分。
"""

import bisect
import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from fractions import Fraction

# 定宽规范化 UTC 时刻：字典序 == 时间序
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_POINT_LEN = 200
MAX_RANGE_DAYS = 400

# 尖峰统计：UTC 零点起每 15 分钟一段
SEGMENT_SECONDS = 15 * 60
SEGMENTS_PER_DAY = 24 * 60 // 15
SEGMENT_US = SEGMENT_SECONDS * 1_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class CsvError(Exception):
    """CSV 格式或内容非法。"""


class BatchRejected(Exception):
    """批次与既有数据冲突或导致读数倒退，整批拒绝。"""


class PeakRecalcError(Exception):
    """峰值缓存重算失败。"""


# ---------------------------------------------------------------- 基础工具

def canon_ts(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def now_ts():
    return canon_ts(datetime.now(timezone.utc))


def parse_ts(raw, where=""):
    """解析 ISO 8601 UTC 时刻，返回规范化字符串。必须显式带 UTC 时区。"""
    s = (raw or "").strip()
    if not s:
        raise CsvError(f"{where}时刻为空")
    t = s[:-1] + "+00:00" if s[-1] in ("Z", "z") else s
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise CsvError(
            f"{where}无法解析的时刻 {s!r}（需 ISO 8601，如 2026-01-02T03:04:05Z）"
        )
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise CsvError(f"{where}时刻 {s!r} 必须显式使用 UTC（后缀 Z 或 +00:00）")
    return canon_ts(dt)


def parse_value(raw, where=""):
    """解析非负十进制千瓦时。"""
    s = (raw or "").strip()
    if not s:
        raise CsvError(f"{where}读数为空")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CsvError(f"{where}非法的十进制读数 {s!r}")
    if not d.is_finite():
        raise CsvError(f"{where}读数须为有限十进制数：{s!r}")
    if d < 0:
        raise CsvError(f"{where}读数不能为负：{s!r}")
    return d


def value_str(d):
    """Decimal 的规范文本形式（去尾零、不用科学计数法）。"""
    return format(d.normalize(), "f")


def parse_day(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"非法日期 {s!r}（需 YYYY-MM-DD）")


def fmt_num(d):
    return value_str(d if isinstance(d, Decimal) else Decimal(d))


def fmt_ts(ts):
    """把规范化时刻显示为 '2026-01-02 03:04:05 UTC'（按需保留小数秒）。"""
    body = ts[:-1].replace("T", " ")
    if "." in body:
        head, frac = body.split(".")
        frac = frac.rstrip("0")
        body = head if not frac else f"{head}.{frac}"
    return body + " UTC"


# ---------------------------------------------------------------- CSV 解析

def _iter_rows(text, required):
    try:
        reader = csv.DictReader(io.StringIO(text))
    except csv.Error as e:
        raise CsvError(f"CSV 解析失败：{e}")
    if reader.fieldnames is None:
        raise CsvError("CSV 为空或缺少表头")
    keymap = {}
    for name in reader.fieldnames:
        keymap.setdefault((name or "").strip().lower(), name)
    missing = [c for c in required if c not in keymap]
    if missing:
        raise CsvError(
            f"CSV 缺少必需列：{', '.join(missing)}（需要表头：{', '.join(required)}）"
        )
    for lineno, row in enumerate(reader, start=2):
        if row is None:
            continue
        cells = {k: (row.get(keymap[k]) or "") for k in required}
        if all(not c.strip() for c in cells.values()):
            continue  # 跳过空行
        yield lineno, cells


def parse_readings_csv(text):
    """返回 ({(point, ts): Decimal}, 原始数据行数)。同点同刻不同读数 -> CsvError。"""
    out = {}
    raw_count = 0
    for lineno, cells in _iter_rows(text, ["point", "ts", "value"]):
        raw_count += 1
        where = f"第 {lineno} 行："
        point = cells["point"].strip()
        if not point:
            raise CsvError(f"{where}计量点为空")
        if len(point) > MAX_POINT_LEN:
            raise CsvError(f"{where}计量点名称过长（>{MAX_POINT_LEN} 字符）")
        ts = parse_ts(cells["ts"], where)
        val = parse_value(cells["value"], where)
        key = (point, ts)
        if key in out:
            if out[key] != val:
                raise CsvError(
                    f"{where}同一计量点同一时刻出现不同读数"
                    f"（{value_str(out[key])} 与 {value_str(val)}），整批拒绝"
                )
            continue  # 同值重复行：幂等去重
        out[key] = val
    return out, raw_count


def parse_swaps_csv(text):
    """返回 ({(point, ts)}, 原始数据行数）。"""
    out = set()
    raw_count = 0
    for lineno, cells in _iter_rows(text, ["point", "ts"]):
        raw_count += 1
        where = f"第 {lineno} 行："
        point = cells["point"].strip()
        if not point:
            raise CsvError(f"{where}计量点为空")
        if len(point) > MAX_POINT_LEN:
            raise CsvError(f"{where}计量点名称过长（>{MAX_POINT_LEN} 字符）")
        ts = parse_ts(cells["ts"], where)
        key = (point, ts)
        if key in out:
            continue  # 重复行：幂等去重
        out.add(key)
    return out, raw_count


# ---------------------------------------------------------------- 区间与用量

def point_state(conn, point):
    """读取一个计量点的全部读数/换表，计算相邻读数区间。

    返回 (readings, swaps, intervals)；interval 形如：
    {'start', 'end', 'v1', 'v2', 'gap', 'delta'}，gap 区间 delta 为 None。
    """
    readings = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? ORDER BY ts", (point,)
    ).fetchall()
    swaps = [
        r["ts"]
        for r in conn.execute(
            "SELECT ts FROM swaps WHERE point = ? ORDER BY ts", (point,)
        ).fetchall()
    ]
    intervals = []
    prev = None
    for row in readings:
        ts, val = row["ts"], Decimal(row["value"])
        if prev is not None:
            t1, v1 = prev
            i = bisect.bisect_right(swaps, t1)
            gap = i < len(swaps) and swaps[i] <= ts  # 换表落在 (t1, t2]，含后次时刻
            intervals.append(
                {
                    "start": t1,
                    "end": ts,
                    "v1": v1,
                    "v2": val,
                    "gap": gap,
                    "delta": None if gap else val - v1,
                }
            )
        prev = (ts, val)
    return readings, swaps, intervals


def find_negative_deltas(conn, point):
    """该计量点所有非缺口区间中读数倒退的区间描述（空列表表示正常）。"""
    _, _, intervals = point_state(conn, point)
    errors = []
    for it in intervals:
        if not it["gap"] and it["delta"] < 0:
            errors.append(
                f"计量点 {point}：{fmt_ts(it['start'])} 读数 {value_str(it['v1'])} "
                f"大于 {fmt_ts(it['end'])} 读数 {value_str(it['v2'])}"
                f"（倒退 {value_str(-it['delta'])} kWh）"
            )
    return errors


def usage_range(conn, point, start_day, end_day):
    """计算 [start_day, end_day]（YYYY-MM-DD，含两端）逐日用量与缺口区间。

    返回 (days, gaps, total)：
      days  — [{'day', 'kwh', 'gap'}]，范围内每一天一行，gap 表示当日被缺口覆盖；
      gaps  — 与范围相交的缺口区间 [{'start', 'end'}]；
      total — 范围内用量合计（Decimal）。
    """
    _, _, intervals = point_state(conn, point)
    lo = start_day + "T00:00:00.000000Z"
    hi = (parse_day(end_day) + timedelta(days=1)).strftime(
        "%Y-%m-%dT00:00:00.000000Z"
    )
    daily = defaultdict(Decimal)
    gaps = []
    for it in intervals:
        if it["gap"]:
            if it["end"] >= lo and it["start"] < hi:  # 与所选范围相交
                gaps.append({"start": it["start"], "end": it["end"]})
        else:
            day = it["end"][:10]  # 增量计入后次读数所属 UTC 日
            if start_day <= day <= end_day:
                daily[day] += it["delta"]

    def gap_on(day):
        d_lo = day + "T00:00:00.000000Z"
        d_hi = (parse_day(day) + timedelta(days=1)).strftime(
            "%Y-%m-%dT00:00:00.000000Z"
        )
        return any(g["end"] >= d_lo and g["start"] < d_hi for g in gaps)

    days = []
    d = parse_day(start_day)
    last = parse_day(end_day)
    while d <= last:
        key = d.strftime("%Y-%m-%d")
        days.append({"day": key, "kwh": daily.get(key, Decimal(0)), "gap": gap_on(key)})
        d += timedelta(days=1)
    total = sum((x["kwh"] for x in days), Decimal(0))
    return days, gaps, total


# ---------------------------------------------------------------- 15 分钟尖峰
#
# 每个 UTC 日从零点起划分 96 个 15 分钟时段 [s, s+900s)。时段内电量按相邻
# 读数区间与时段的重合时长比例分摊；只有整个时段都被“无换表”的读数区间
# 覆盖时才参与比较。结果按 (计量点, UTC 日期) 写入 daily_peaks 缓存。
#
# 缓存一致性：
# * 上传在 BEGIN IMMEDIATE 写事务内先按旧读数/换表定位受影响日期，应用新数据
#   后在同一事务内重算这些日期，未受影响的缓存行原样保留；
# * 所有写缓存的计算都在 BEGIN IMMEDIATE 事务内完成，SQLite 单写者语义保证
#   旧计算不可能在新上传之后写入过期峰值。

DAY_US = 24 * 60 * 60 * 1_000_000
# 峰值计算全程用 Fraction 保证精确；输出为十进制文本时保留到 12 位小数
_DECIMAL_PREC = 60
_KW_PLACES = 12


def ts_to_us(ts):
    """规范化 UTC 时刻 -> 自 1970-01-01 起的微秒数。"""
    return (datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc) - EPOCH) // (
        timedelta(microseconds=1)
    )


def us_to_ts(us):
    return canon_ts(EPOCH + timedelta(microseconds=us))


def _day_key(day_index):
    return (EPOCH + timedelta(days=day_index)).date().isoformat()


def days_overlapping_us(t1, t2):
    """区间 (t1, t2] 涉及的全部 UTC 日期（t1 < t2）。

    口径为半开时段 [s, s+900s)：取与区间有正重合时长的时段所属日期。
    """
    if t2 <= t1:
        return []
    return [
        _day_key(k)
        for k in range(t1 // DAY_US, (t2 - 1) // DAY_US + 1)
    ]


def _fraction_of(value):
    return value if isinstance(value, Fraction) else Fraction(value)


def _fraction_str(fr):
    """Fraction -> 十进制文本（不引入浮点误差）。

    峰值为精确有理数；十进制有限时原样输出，否则四舍五入到 12 位小数。
    """
    if fr == 0:
        return "0"
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PREC
        d = Decimal(fr.numerator) / Decimal(fr.denominator)
        if Fraction(d) != fr:
            limit = Decimal(1).scaleb(-_KW_PLACES)
            if d.as_tuple().exponent < -_KW_PLACES:
                d = d.quantize(limit, rounding=ROUND_HALF_UP)
    return value_str(d)


def _compute_point_peaks(conn, point, days):
    """按当前读到的同一版读数/换表，计算若干 UTC 日的 15 分钟尖峰。"""
    _, _, raw_intervals = point_state(conn, point)
    intervals = []
    for it in raw_intervals:
        intervals.append(
            (
                ts_to_us(it["start"]),
                ts_to_us(it["end"]),
                None if it["gap"] else _fraction_of(it["delta"]),
            )
        )
    ends = [x[1] for x in intervals]

    results = {}
    for day in days:
        d = parse_day(day)
        day_start = (
            datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - EPOCH
        ) // timedelta(microseconds=1)
        day_end = day_start + DAY_US

        overlaps_day = [
            (s, e, delta) for s, e, delta in intervals
            if s < day_end and e > day_start
        ]
        non_gap_overlap = any(delta is not None for _, _, delta in overlaps_day)

        peaks = []  # (start_us, Fraction kW)
        fail_gap = fail_partial = False
        for i in range(SEGMENTS_PER_DAY):
            seg_start = day_start + i * SEGMENT_US
            seg_end = seg_start + SEGMENT_US
            j = bisect.bisect_right(ends, seg_start)  # 第一个 end > seg_start
            covered = 0
            energy = Fraction(0)
            seg_gap = False
            while j < len(intervals) and intervals[j][0] < seg_end:
                a, b, delta = intervals[j]
                overlap = min(b, seg_end) - max(a, seg_start)
                if overlap > 0:
                    if delta is None:
                        seg_gap = True
                    else:
                        covered += overlap
                        energy += delta * overlap / (b - a)
                j += 1
            if covered == SEGMENT_US and not seg_gap:
                peaks.append((seg_start, energy * 4))  # kWh / (15/60 h)
            elif seg_gap:
                fail_gap = True
            else:
                fail_partial = True

        if peaks:
            peak = max(kw for _, kw in peaks)
            peak_str = _fraction_str(peak)
            # 以展示值判定并列：同一显示功率的时段全部列出，避免“峰值显示 4
            # 但并列里只有一个 4”的观感问题
            ties = [
                {
                    "start": us_to_ts(start),
                    "end": us_to_ts(start + SEGMENT_US),
                    "kw": kw_str,
                }
                for start, kw in peaks
                for kw_str in (_fraction_str(kw),)
                if kw_str == peak_str
            ]
            results[day] = {
                "day": day,
                "status": "ok",
                "peak_kw": peak_str,
                "ties": ties,
                "reason": None,
            }
            continue

        if not overlaps_day:
            reason = "该日没有任何读数区间覆盖（需要跨该日的相邻读数）"
        elif not non_gap_overlap:
            reason = "该日读数区间全部落在换表缺口内，不推算电量"
        else:
            parts = []
            if fail_partial:
                parts.append("读数区间未完整覆盖任一时段的全部 15 分钟")
            if fail_gap:
                parts.append("部分时段落在换表缺口内")
            reason = "该日没有完整的 15 分钟时段：" + "；".join(parts)
        results[day] = {
            "day": day,
            "status": "none",
            "peak_kw": None,
            "ties": [],
            "reason": reason,
        }
    return results


def cached_days(conn, point, days):
    """返回 days 中已存在峰值缓存的子集。"""
    days = list(days)
    if not days:
        return set()
    marks = ",".join("?" for _ in days)
    rows = conn.execute(
        f"SELECT day FROM daily_peaks WHERE point = ? AND day IN ({marks})",
        [point, *days],
    ).fetchall()
    return {r["day"] for r in rows}


def cached_peaks_days(conn, point, days):
    """返回 ({day: result}, 已缓存的日期集合)；有效结果直接复用。"""
    days = list(days)
    if not days:
        return {}, set()
    marks = ",".join("?" for _ in days)
    rows = conn.execute(
        f"SELECT day, status, peak_kw, reason, ties FROM daily_peaks"
        f" WHERE point = ? AND day IN ({marks})",
        [point, *days],
    ).fetchall()
    out = {}
    for row in rows:
        out[row["day"]] = {
            "day": row["day"],
            "status": row["status"],
            "peak_kw": row["peak_kw"],
            "ties": json.loads(row["ties"]) if row["ties"] else [],
            "reason": row["reason"],
        }
    return out, set(out)


def recompute_peaks(conn, point, days):
    """重算并覆盖写入指定日期的峰值缓存。调用方负责事务。

    先算后写：计算抛错时不会改动任何缓存行；上传事务整体回滚时同样保持旧行。
    """
    days = sorted(set(days))
    if not days:
        return {}
    results = _compute_point_peaks(conn, point, days)
    now = now_ts()
    for day in days:
        r = results[day]
        conn.execute(
            "INSERT INTO daily_peaks(point, day, status, peak_kw, reason, ties,"
            " computed_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(point, day) DO UPDATE SET status=excluded.status,"
            " peak_kw=excluded.peak_kw, reason=excluded.reason, ties=excluded.ties,"
            " computed_at=excluded.computed_at",
            (
                point,
                day,
                r["status"],
                r["peak_kw"],
                r["reason"],
                json.dumps(r["ties"], ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )
    return results


def reading_times_us(conn, point):
    return [
        ts_to_us(r["ts"])
        for r in conn.execute(
            "SELECT ts FROM readings WHERE point = ? ORDER BY ts", (point,)
        ).fetchall()
    ]


def existing_reading_times(conn, points):
    """取指定计量点的既有读数时刻（微秒），用于定位受影响日期。"""
    return {point: reading_times_us(conn, point) for point in points}


def refresh_peaks_for_upload(conn, affected):
    """上传成功后刷新受影响日期的峰值缓存，返回 {point: 已刷新日期集合}。

    affected 形如 {point: {day, ...}}。只重算“已经算过”的日期：从未查询过的
    日期没有缓存行，等首次查询时再按新数据计算即可。未受影响的已算日期原样
    复用，也避免为大量从未查看过的日期做无谓计算。

    必须在写事务内调用（与原始记录同版本提交）：所有写缓存的操作都发生在
    BEGIN IMMEDIATE 下，旧版本的计算不可能在新上传之后写入。
    """
    refreshed = {}
    for point in sorted(affected):
        days = cached_days(conn, point, affected[point])
        if days:
            recompute_peaks(conn, point, days)
            refreshed[point] = days
    return refreshed


def query_peaks(conn, point, days):
    """返回指定日期的峰值结果，并补齐缺失缓存。

    已缓存的日期直接复用，不读取原始读数/换表明细；只有缺失的日期才计算。
    必须在 BEGIN IMMEDIATE 事务内调用：计算与写入同一版本，且与上传串行。
    返回 ({day: result}, 本次新计算的日期集合)。
    """
    days = list(days)
    cached, have = cached_peaks_days(conn, point, days)
    missing = [day for day in days if day not in have]
    if missing:
        cached.update(recompute_peaks(conn, point, missing))
    return cached, set(missing)


def resolve_peak_day(conn, point, day):
    """校验/补全查询日期。缺省取最近一个有读数区间覆盖的 UTC 日。"""
    if day:
        return parse_day(day).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT ts FROM readings WHERE point = ? ORDER BY ts DESC LIMIT 2", (point,)
    ).fetchall()
    if not rows:
        raise ValueError("该计量点暂无读数，请指定 UTC 日期")
    if len(rows) == 1:
        # 只有一条读数，没有任何区间；仍按该读数所属日给出“无完整时段”
        return rows[0]["ts"][:10]
    # 最后一个区间是 (倒数第二条, 最后一条]；其覆盖的最后一个时段所在日
    return days_overlapping_us(ts_to_us(rows[1]["ts"]),
                               ts_to_us(rows[0]["ts"]))[-1]


def affected_days_for_readings(existing_us_by_point, applied_keys):
    """补传读数影响的 UTC 日期集合（按计量点分组）。

    applied_keys 为本次真正写入的 (point, ts)（幂等跳过的不算）。
    新旧读数合并后，只有至少一端是新读数的相邻区间会改变；把这些区间
    涉及的全部日期标为受影响，跨日区间自然覆盖中间日期。
    """
    new_by_point = defaultdict(list)
    for point, ts in applied_keys:
        new_by_point[point].append(ts_to_us(ts))

    days = defaultdict(set)
    for point, new_us in new_by_point.items():
        new_set = set(new_us)
        combined = sorted(set(existing_us_by_point.get(point, [])) | new_set)
        for left, right in zip(combined, combined[1:]):
            if left in new_set or right in new_set:
                days[point].update(days_overlapping_us(left, right))
    return dict(days)


def affected_days_for_swaps(existing_us_by_point, applied_swaps):
    """补录换表影响的 UTC 日期集合（按计量点分组）。

    换表只改变其所在读数区间；区间口径 (t1, t2]，换表 s 影响满足
    t1 < s <= t2 的区间。新换表才需要重算，幂等跳过的不算。
    """
    days = defaultdict(set)
    for point, ts in applied_swaps:
        s = ts_to_us(ts)
        times = existing_us_by_point.get(point, [])
        i = bisect.bisect_left(times, s)
        b = times[i] if i < len(times) else None   # 后次读数（含同刻）
        a = times[i - 1] if i > 0 else None        # 前次读数（严格早于 s）
        if a is not None and b is not None:
            days[point].update(days_overlapping_us(a, b))
    return dict(days)


# ---------------------------------------------------------------- 批量导入
# 以下函数在调用方开启的事务内执行；抛出 BatchRejected 时由调用方回滚。

def apply_readings(conn, upload_id, mapping):
    """写入读数。返回 (新增条数, 幂等跳过条数, 新增的 (point, ts) 列表)。

    - 同点同刻同值：幂等跳过，不重复计数；
    - 同点同刻不同值：BatchRejected，整批回滚；
    - 写入后任一受影响计量点出现读数倒退：BatchRejected，整批回滚。
    """
    applied = skipped = 0
    affected = set()
    applied_keys = []
    for (point, ts), val in mapping.items():
        row = conn.execute(
            "SELECT value FROM readings WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if row is not None:
            if Decimal(row["value"]) == val:
                skipped += 1
                continue
            raise BatchRejected(
                f"计量点 {point} 在 {fmt_ts(ts)} 已存在不同读数"
                f"（现有 {row['value']}，本次 {value_str(val)}），整批拒绝"
            )
        conn.execute(
            "INSERT INTO readings(point, ts, value, upload_id) VALUES (?,?,?,?)",
            (point, ts, value_str(val), upload_id),
        )
        applied += 1
        affected.add(point)
        applied_keys.append((point, ts))

    errors = []
    for p in sorted(affected):
        errors.extend(find_negative_deltas(conn, p))
    if errors:
        raise BatchRejected("读数倒退，整批拒绝：" + "；".join(errors[:5]))
    return applied, skipped, applied_keys


def apply_swaps(conn, upload_id, keys):
    """写入换表记录。返回 (新增条数, 幂等跳过条数, 新增的 (point, ts) 列表)。

    换表只会把区间标记为缺口（撤销已计入增量），不会产生倒退，无需额外校验。
    """
    applied = skipped = 0
    applied_keys = []
    for point, ts in sorted(keys):
        row = conn.execute(
            "SELECT 1 FROM swaps WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if row is not None:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO swaps(point, ts, upload_id) VALUES (?,?,?)",
            (point, ts, upload_id),
        )
        applied += 1
        applied_keys.append((point, ts))
    return applied, skipped, applied_keys
