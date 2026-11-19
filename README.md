# 电量日核算（Flask + SQLite）

电网数据员上传晚到的电表累计读数与换表记录，系统按计量点把相邻读数连成区间，
自动算出可核算的逐日用电量，并标记换表造成的缺口区间。

## 业务规则

- **读数 CSV**（表头 `point,ts,value`）：计量点、UTC 时刻（ISO 8601，须带 `Z` 或 `+00:00`）、
  非负十进制千瓦时累计读数。
- **换表 CSV**（表头 `point,ts`）：计量点、UTC 换表时刻。
- **幂等**：同点同刻同值的原样重传自动跳过，不重复计数；
  同点同刻**不同值**（文件内或对比既有数据）→ 整批拒绝。
- **区间计量**：每个计量点按时刻连接相邻读数构成区间 `(t1, t2]`：
  - 区间内发生换表（`t1 < s <= t2`，**含后次读数时刻**）→ 标记缺口，不推算增量；
  - 否则增量 = 后次 − 前次，计入**后次读数所属 UTC 日**；
  - 增量为负（读数倒退）→ 整批拒绝。
- **晚到读数**插入后自动拆分旧区间，增量按新区间重新归属。
- **补录换表**后，原区间变为缺口，此前计入的增量自动撤销。
- **事务性**：一次上传的原始记录与其影响的日结果在同一事务内生效；
  任何校验失败整体回滚，保持旧数据；被拒批次留有审计记录。
- **并发一致性**：SQLite WAL 模式；上传用 `BEGIN IMMEDIATE` 串行化，
  查询在单个读事务内完成，页面只能看到同一版本的完整结果（页面右上角显示数据版本号）。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
```

访问 http://localhost:8000 。数据持久化在命名卷 `meter-data`（容器内 `/data/app.db`）。

### 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python run.py                 # 默认 0.0.0.0:8000（waitress）
pytest tests/                 # 运行测试
```

环境变量：`APP_DATABASE`（SQLite 路径，默认 `./data/app.db`）、`PORT`（默认 8000）、
`SECRET_KEY`、`USE_WAITRESS=0` 可退回 Flask 内置服务器。

## 页面与接口

- `GET /` — 上传入口、查询入口、最近上传批次（含被拒原因）。
- `POST /upload/readings`、`POST /upload/swaps` — 表单上传 CSV（字段名 `file`）。
- `GET /usage?point=..&start=..&end=..` — 逐日用量表 + 缺口区间；日期缺省时取该点数据最近 31 天。
- `GET /api/usage?point=..&start=..&end=..` — 同口径 JSON（含数据版本号），便于核对与集成。

## CSV 示例

```csv
point,ts,value
MP001,2026-01-01T00:00:00Z,1000
MP001,2026-01-03T00:00:00Z,1060
```

```csv
point,ts
MP001,2026-01-03T00:00:00Z
```

## 实现说明

- 读数以文本存十进制（`Decimal` 计算），时刻规范化为定宽 UTC 字符串（字典序即时间序）。
- 日用量不做冗余物化：查询时在单事务内由原始记录现算，
  因此“原始记录”和“受影响日结果”天然同事务生效，晚到读数/补录换表的影响即时可见。
- 代码结构：`app/logic.py`（解析、校验、区间/日用量计算）、`app/routes.py`（页面与 API）、
  `app/db.py`（连接与建表）、`tests/test_app.py`（规则与并发测试）。
