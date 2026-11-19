import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,              -- readings | swaps
    filename      TEXT,
    status        TEXT NOT NULL,              -- applied | rejected
    message       TEXT,
    rows_received INTEGER NOT NULL DEFAULT 0,
    rows_applied  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    point     TEXT NOT NULL,
    ts        TEXT NOT NULL,                  -- 规范化的 UTC 时刻，定宽字符串，字典序即时间序
    value     TEXT NOT NULL,                  -- 十进制累计读数（kWh），以文本保存避免浮点误差
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    PRIMARY KEY (point, ts)
);

CREATE TABLE IF NOT EXISTS swaps (
    point     TEXT NOT NULL,
    ts        TEXT NOT NULL,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    PRIMARY KEY (point, ts)
);

-- 15 分钟尖峰缓存：按 (计量点, UTC 日) 保存；上传事务内按受影响日期增量刷新
CREATE TABLE IF NOT EXISTS daily_peaks (
    point       TEXT NOT NULL,
    day         TEXT NOT NULL,                 -- YYYY-MM-DD（UTC）
    status      TEXT NOT NULL,                 -- ok | none
    peak_kw     TEXT,                          -- 最高 15 分钟平均功率（Decimal 文本）
    reason      TEXT,                          -- status = none 时说明原因
    ties        TEXT NOT NULL DEFAULT '[]',    -- 并列时段 JSON 数组
    computed_at TEXT NOT NULL,
    PRIMARY KEY (point, day)
);
"""


def get_db():
    """每个请求一个连接；autocommit 模式，事务由代码显式控制。"""
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # autocommit；BEGIN/COMMIT 显式管理
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        g.db = conn
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    conn = sqlite3.connect(app.config["DATABASE"])
    # WAL：读写互不阻塞，读事务始终看到一致的快照
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
