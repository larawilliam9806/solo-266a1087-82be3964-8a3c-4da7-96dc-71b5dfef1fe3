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
"""

import bisect
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

# 定宽规范化 UTC 时刻：字典序 == 时间序
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_POINT_LEN = 200
MAX_RANGE_DAYS = 400


class CsvError(Exception):
    """CSV 格式或内容非法。"""


class BatchRejected(Exception):
    """批次与既有数据冲突或导致读数倒退，整批拒绝。"""


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


# ---------------------------------------------------------------- 批量导入
# 以下函数在调用方开启的事务内执行；抛出 BatchRejected 时由调用方回滚。

def apply_readings(conn, upload_id, mapping):
    """写入读数。返回 (新增条数, 幂等跳过条数)。

    - 同点同刻同值：幂等跳过，不重复计数；
    - 同点同刻不同值：BatchRejected，整批回滚；
    - 写入后任一受影响计量点出现读数倒退：BatchRejected，整批回滚。
    """
    applied = skipped = 0
    affected = set()
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

    errors = []
    for p in sorted(affected):
        errors.extend(find_negative_deltas(conn, p))
    if errors:
        raise BatchRejected("读数倒退，整批拒绝：" + "；".join(errors[:5]))
    return applied, skipped


def apply_swaps(conn, upload_id, keys):
    """写入换表记录。返回 (新增条数, 幂等跳过条数)。

    换表只会把区间标记为缺口（撤销已计入增量），不会产生倒退，无需额外校验。
    """
    applied = skipped = 0
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
    return applied, skipped
