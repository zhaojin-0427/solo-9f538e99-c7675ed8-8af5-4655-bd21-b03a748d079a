# 包装线封口参数箱样核对 API

一条包装线做封口参数对照试验。切换后设备需要稳定，计划参数必须在**采样开始前 5 分钟**起持续生效到采样结束。本服务用于录入批次、补录/更正切换日志，并给出某批箱样能否纳入分析的结论。

技术栈：FastAPI + SQLite（标准库 `sqlite3`，无 ORM）。启动后打开 `/docs` 即可在线录入和查询。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

数据保存在命名卷 `check-data` 中；重建容器不会丢数据。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

环境变量 `CHECK_DB_PATH` 指定数据库文件，默认 `data/checks.db`。

### 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## 核对规则

| 规则 | 说明 |
| --- | --- |
| 检查窗口 | `[采样开始 − 5 分钟, 采样结束)`，左闭右开 |
| 生效参数 | 某时刻的有效参数 = `switched_at <= 该时刻` 的**最近一条**切换记录 |
| 未知 | 首条切换记录之前，参数视为未知 |
| 合格条件 | 检查窗口**全程**参数已知且等于计划参数 |
| 计划参数比较 | 三个参数分别比较，统一四舍五入到 3 位小数 |
| 切换时刻 | 全局唯一（同时写入 API 校验与数据库唯一约束） |
| 时间格式 | 必须带时区，按 UTC 存储；精确到秒。naive 时间返回 422 |

结果中同时给出：

* `status` / `eligible` / `reason_code`：`ok` / `parameter_mismatch` / `unknown_params`
* `first_problem_at` + `first_problem_kind`：窗口内**最早**的问题时刻（未知或不符取更早者）
* `first_mismatch_at`、`first_unknown_at`：分别给出首个不符、首个未知时刻
* `first_problem_switch_record_no` / `first_problem_switch_switched_at`：该问题对应的切换记录
* `segments`：窗口按切换记录切分的明细，便于人工复核
* `violations`：所有不合格时段

## 切换日志与增量重查

`PUT /switches/{record_no}` 在记录号不存在时补录、已存在时更正。写入后：

1. 比较**新旧两条有效参数时间线**，得到前后参数不同的时段 `changed_intervals`
   （`end = null` 表示一直延伸到无穷）。
2. 只重查检查窗口与这些时段**相交**的批次；不相交的批次保持原结论和原修订号。
3. 每个被重查的批次追加一条修订记录，`transition` 为
   `revoked`（资格撤下）/ `restored`（资格恢复）/ `unchanged`。
4. 切换日志、全部重查结果、批次修订号在**同一事务**内提交。

请求失败（参数校验 422、切换时刻冲突 409、重查中途异常）时整个事务回滚，
日志和核对结果都不改动，批次沿用原结果。

## 工况段与分组查询

做封口参数对照分析时，**同一段连续工况里取出的多个箱样只算一次试验重复**。
服务把切换时间线压缩成「工况段」——设备以同一组参数连续运行的极大半开区间
`[started_at, ended_at)`，`ended_at` 为 `null` 表示该段尚未结束：

* 同一组参数连续上报多次仍算**一段**；
* 参数调成别的、再调回来，前后算**两段**；
* 首条切换记录之前参数未知，不形成工况段。

工况段持久化在 `condition_segments` 表中，随切换补改在**同一事务内局部增量
维护**——只更新与有效参数变更时段相交的段（拆组/并组），范围之外的段原样
保留；补改失败整体回滚，沿用原分组。即使补改的变更时段与所有样本的检查窗口
都不相交（没有批次被重查），工况段也会正确反映拆组或并组。

`GET /analysis/condition-groups?start=...&end=...` 提供分组查询：

1. 输入 UTC 起止时刻，按**采样开始时刻**筛出左闭右开区间 `[start, end)` 内
   当前**合格**的批次（不合格批次不入选，但计入 `total_batch_count`）。
2. 返回区间内各工况段的**实际起止、参数和入选批号**（含没有样本的空段），
   未结束的工况段 `ended_at` 为 `null`。
3. `parameter_stats` 按参数统计**运行段数**（即试验重复数）与合格批次数。

资格与工况段在同一只读连接、同一已提交状态下读取。小范围查询只做索引定位
（`condition_segments.started_at` 唯一索引、`batches.sampling_start` 索引），
不扫描全线历史，也不逐条读取段内的同值切换记录。

旧版本 SQLite 卷（尚无工况段表数据）启动时会在单个事务内按全量切换日志一次性
回填工况段，之后的补改走增量维护；沿用原 SQLite 卷，不需要数据迁移脚本。

## 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/batches` | 录入批号、计划参数、UTC 采样起止 |
| `GET` | `/batches` | 批次列表（分页） |
| `GET` | `/batches/{batch_no}` | 当前结论 + 修订历史 |
| `GET` | `/batches/{batch_no}/check` | 能否纳入分析 + 首个不符/未知时刻 |
| `GET` | `/switches` | 全部切换记录 |
| `GET` | `/switches/{record_no}` | 单条切换记录 |
| `PUT` | `/switches/{record_no}` | 补录 / 更正切换记录并增量重查 |
| `GET` | `/analysis/condition-groups` | 按 UTC 时刻分组查询工况段与合格批次 |
| `GET` | `/health` | 健康检查 |

### 示例

```bash
# 录入批次（此时还没有切换日志 → 参数未知，不合格）
curl -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "batch_no": "LOT-2026-001",
  "plan_parameters": {"seal_temperature_c": 180, "seal_pressure_kpa": 300, "dwell_time_s": 1.5},
  "sampling_start": "2026-11-23T09:00:00Z",
  "sampling_end":   "2026-11-23T09:30:00Z"
}'

# 补录切换日志（08:50，早于窗口起点 08:55 → 恢复资格）
curl -X PUT localhost:8000/switches/SW-0001 -H 'Content-Type: application/json' -d '{
  "switched_at": "2026-11-23T08:50:00Z",
  "parameters": {"seal_temperature_c": 180, "seal_pressure_kpa": 300, "dwell_time_s": 1.5}
}'

# 按批号查结论
curl localhost:8000/batches/LOT-2026-001/check

# 工况分组查询：按采样开始时刻筛 [08:00, 16:00) 内合格批次，按工况段归组
curl -G localhost:8000/analysis/condition-groups \
  --data-urlencode 'start=2026-11-23T08:00:00Z' \
  --data-urlencode 'end=2026-11-23T16:00:00Z'
```

返回示例（节选）：

```json
{
  "query": {"start": "2026-11-23T08:00:00Z", "end": "2026-11-23T16:00:00Z"},
  "segments": [
    {
      "started_at": "2026-11-23T07:00:00Z",
      "ended_at": "2026-11-23T09:00:00Z",
      "parameters": {"seal_temperature_c": 180.0, "seal_pressure_kpa": 300.0, "dwell_time_s": 1.5},
      "batch_nos": ["LOT-2026-001", "LOT-2026-002"]
    },
    {
      "started_at": "2026-11-23T10:00:00Z",
      "ended_at": null,
      "parameters": {"seal_temperature_c": 180.0, "seal_pressure_kpa": 300.0, "dwell_time_s": 1.5},
      "batch_nos": ["LOT-2026-004"]
    }
  ],
  "parameter_stats": [
    {"parameters": {"seal_temperature_c": 180.0, "seal_pressure_kpa": 300.0, "dwell_time_s": 1.5},
     "segment_count": 2, "batch_count": 3}
  ],
  "segment_count": 2,
  "eligible_batch_count": 3,
  "total_batch_count": 4
}
```

## 目录结构

```
app/
  main.py         FastAPI 入口与路由
  schemas.py      请求/响应模型（含时区校验）
  service.py      事务编排、增量重查、工况分组查询、启动回填
  evaluation.py   纯函数核对逻辑（未知/不符/变更时段）
  segments.py     纯函数工况段压缩与局部增量规划（拆组/并组）
  repository.py   SQL 访问（含工况段表与索引定位查询）
  db.py           连接、schema、事务
  params.py       封口参数定义与规范化
  timeutil.py     UTC 时间处理
tests/
  test_evaluation.py       核对逻辑单测
  test_segments.py         工况段拆组/并组增量规划单测
  test_condition_groups.py 分组查询端到端测试
  test_api.py              HTTP 端到端测试
```

## 已知限制

* 单包装线模型：切换日志是全局的，没有「产线」维度。
* 检查窗口结束点严格是采样结束时刻（不含）；恰在该时刻的切换不影响本批。
* 批次录入后不支持修改计划参数或采样时间，只能新建批次。
* 无鉴权、无限流；SQLite 单写者，适合单实例部署（compose 中即单实例）。
