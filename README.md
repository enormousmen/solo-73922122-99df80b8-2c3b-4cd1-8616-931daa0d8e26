# 环保实验室水样核对与质控放行 API

把仪器上录错的样品编号核对回送检水样，避免检测值归到错误的采样点；并在质控员
更正仪器质控记录后，按 z 值规则判定各水样批次应放行还是暂停出报告。

服务使用 FastAPI，两个清单各最多 12 行。除了给出一个最优匹配，它还会分析
**所有**最优方案：只有一条仪器记录在全部最优方案中都对应同一条送检记录时，
才返回可自动归档的确定映射；否则返回候选水样并提示是否需要人工核对。

## 快速启动

需要 Docker 与 Docker Compose 插件：

```bash
docker compose up --build
```

启动后：

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI 交互式 API 文档（含可直接试调的更正、补录示例） |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/api/v1/demo> | 可直接提交的演示请求 |
| <http://localhost:8000/api/v1/demo/expected> | 内置演示数据的核对结果 |
| `GET /api/v1/qc/{仪器}/{检测项目}` | 查询质控记录、结论与修订号 |
| `POST /api/v1/qc/{仪器}/{检测项目}/batches` | 提交新质控批次 |
| `PUT /api/v1/qc/{仪器}/{检测项目}/batches/{序号}/correction` | 更正实测值 |
| `POST /api/v1/qc/{仪器}/{检测项目}/backfill` | 补录历史序号 |

容器以两个 uvicorn 工作进程运行，SQLite 数据保存在命名卷 `qc-data`
（容器内 `/data/qc_control.db`），重启后数据保留。本地开发（非容器）若
`/data` 不可写，会自动改用工作目录下 `.local-data/qc_control.db`，也可用
环境变量 `QC_DB_PATH` 指定任意路径。

端口可通过环境变量调整，例如 `API_PORT=9000 docker compose up --build`。

运行演示：

```bash
curl -s -X POST http://localhost:8000/api/v1/check \
  -H 'Content-Type: application/json' \
  --data @demo/check_request.json
```

也可以在 Swagger UI 中先调用 `GET /api/v1/demo`，再把响应体粘贴到
`POST /api/v1/check` 的请求体中。

## 请求格式

```json
{
  "submissions": [
    {"id": "S01", "sample_no": "WQ1001", "sampling_point": "东岸取水口"},
    {"id": "S02", "sample_no": "WQ1003", "sampling_point": "西岸取水口"},
    {"id": "S03", "sample_no": "WQ5555", "sampling_point": "南岸取水口"},
    {"id": "S04", "sample_no": "WQ1003", "sampling_point": "西岸复采点"}
  ],
  "instruments": [
    {"id": "I01", "sample_no": "WQ1002", "value": 7.42},
    {"id": "I02", "sample_no": "WQ1004", "value": 7.18},
    {"id": "I03", "sample_no": "WQ1000", "value": 7.51},
    {"id": "I04", "sample_no": "WQ5555", "value": 8.03},
    {"id": "I05", "sample_no": "ZZZ999", "value": 6.97},
    {"id": "I06", "sample_no": "WQ1006", "value": 7.66}
  ]
}
```

- 两个清单均不能超过 12 行，可以为空。
- `id`、`sample_no` 只允许英文字母和数字。
- `id` 是行标识，在两类记录中忽略大小写后都必须唯一。
- `sample_no` 允许重复，因为同一水样可能在多个采样点或容器中送检。
- `sampling_point` 不能为空，接受中文等任意文本。
- `value` 必须是有限数字（不允许 NaN/Infinity）。

请求中任何不符合上述约束的内容都会由 FastAPI 返回 `422`。

## 响应格式

```json
{
  "summary": {
    "submission_count": 4,
    "instrument_count": 6,
    "max_matched_pairs": 4,
    "min_total_distance": 3,
    "confirmed_count": 1,
    "review_count": 5,
    "possible_unmatched_count": 5
  },
  "results": [
    {
      "instrument_id": "I01",
      "recorded_sample_no": "WQ1002",
      "value": 7.42,
      "status": "review",
      "confirmed_mapping": null,
      "candidate_mappings": [
        {
          "submission_id": "S01",
          "sample_no": "WQ1001",
          "sampling_point": "东岸取水口",
          "distance": 1
        },
        {
          "submission_id": "S02",
          "sample_no": "WQ1003",
          "sampling_point": "西岸取水口",
          "distance": 1
        },
        {
          "submission_id": "S04",
          "sample_no": "WQ1003",
          "sampling_point": "西岸复采点",
          "distance": 1
        }
      ],
      "possible_unmatched": true
    },
    {
      "instrument_id": "I04",
      "recorded_sample_no": "WQ5555",
      "value": 8.03,
      "status": "confirmed",
      "confirmed_mapping": {
        "submission_id": "S03",
        "sample_no": "WQ5555",
        "sampling_point": "南岸取水口",
        "distance": 0
      },
      "candidate_mappings": [
        {
          "submission_id": "S03",
          "sample_no": "WQ5555",
          "sampling_point": "南岸取水口",
          "distance": 0
        }
      ],
      "possible_unmatched": false
    }
  ]
}
```

每条仪器记录的结论：

- `status=confirmed`：在全部最优方案中，该记录始终对应同一条送检记录。
  `confirmed_mapping` 可直接用于归档。
- `status=review`：存在多个候选送检记录，或存在“未匹配”的最优方案。
  `confirmed_mapping` 为 `null`，需人工核对。
- `candidate_mappings`：该记录在**所有最优方案中出现过**的全部候选送检行。
  即使只有一个候选，只要 `possible_unmatched=true`，也不能自动归档。
- `possible_unmatched`：是否有某个最优方案使该记录没有匹配。

候选与确定映射都以 `submission_id` 精确到送检行，而不是只到样品编号。
因此，同一个 `sample_no` 出现在不同采样点时，不会误把检测值归到另一个
采样点。

## 核对规则

1. **候选边**：样品编号忽略大小写后计算 Levenshtein 距离，距离不超过 2
   才允许配对。编号本身只含英文字母和数字。
2. **一对一**：每条送检记录和每条仪器记录最多使用一次。
3. **两级最优**：先最大化配对数，再在配对数相同的方案中最小化总距离。
   即使存在“只配一对、总距离更小”的方案，也会优先选择配对数更多的方案。
4. **稳定性判断**：对每条候选边，强制使用该边后重新求解；若仍能得到同一个
   全局最优（配对数与总距离都不变），该边就出现在某个最优方案中。
   对每条仪器记录，去掉它后若仍能达到同一个全局最优，则存在它未匹配的
   最优方案。

内部使用最小费用最大流求两级最优解；最大规模仅 12×12，因此可以逐条边做
强制约束验证，而不必枚举可能数量巨大的所有最优匹配。单次请求最多约 157 次
小规模流计算。

## 内置演示数据说明了什么

`demo/check_request.json` 与 `demo/expected_response.json` 中包含：

- `I04` 的 `WQ5555` 精确命中 `S03`，是唯一确定映射。
- `I01`、`I02`、`I03`、`I06` 都可对应 `S01`、`S02`、`S04`；这些行中
  `S02`、`S04` 的样品编号相同但采样点不同。
- 上述四条记录都可能在某些最优方案中未匹配，因此全部为 `review`。
- `I05` 的 `ZZZ999` 与任何编号距离都超过 2，没有候选，但仍可能未匹配。

演示数据的全局最优为 4 对、总距离 3。

## 质控放行 API

质控员更正仪器质控记录后，需要知道哪些水样批次应暂停出报告。质控数据按
**同一仪器 + 同一检测项目**组成独立序列，用 SQLite 持久化，每序列最多保存
100 个按唯一整数序号排列的批次。

### 判定规则

每个批次含：唯一批次 ID、水样结果 ID 列表、质控实测值、靶值、正标准差，
数值均为十进制字符串（如 `"10.82"`，不接受科学计数法、`NaN`、`Infinity`），
内部使用 `Decimal` 精确计算，不使用二进制浮点：

```
z = (实测值 − 靶值) / 标准差
```

按序号升序逐批判断：

1. `|z| > 3` → **暂停**该批（`paused`）；
2. 否则，本批与**前一批** z 同号、且两者都满足 `|z| > 2` → **暂停**该批；
3. 其他情况 → **放行**（`released`）。

边界按严格大于处理：`|z|` 恰好等于 3 或 2 都不触发对应规则；z=0 没有符号，
不参与同号判断。被暂停的批次仍然作为后一批的“前一批”参与判断。补录造成的
序号空洞不影响规则——“前一批”始终是序号升序后紧邻的已存在批次。

### 修订号与并发

- 全库维护一个修订号 `revision`，初始为 0；每次成功写入加 1。
- 所有写请求都必须携带调用方读到的修订号；与库中当前值不符时返回 `409`，
  本次写入完全不生效。
- 记录写入、全部批次结论重算、修订号推进在同一个 SQLite 事务
  （`BEGIN IMMEDIATE`）内完成，任一步失败整体回滚。
- 开启 WAL 与 `busy_timeout`，配合进程内互斥锁；两个工作进程并发写入时，
  写事务在数据库层严格串行，配合修订号校验，并发写入与查询等价于串行执行。
- 数据落盘并挂载在命名卷上，服务/容器重启后记录、结论与修订号均保留。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/qc/{instrument}/{test_item}` | 一次查询返回记录、结论和当前修订号 |
| POST | `.../batches` | 提交新批次，序号自动分配为当前最大序号 + 1 |
| PUT | `.../batches/{seq}/correction` | 更正指定批次的实测值，结论整体重算 |
| POST | `.../backfill` | 向空缺的历史序号补录一批 |

写响应包含：写入后的新修订号、本次写入批次的结论、**状态发生改变的水样结果
ID 及其前后状态**（新增/补录批次自身的水样结果以 `before_status=null` 列出），
以及写入后该序列的全部批次记录与重算结论。

### 调用示例

```bash
# 1. 查询当前修订号（空库为 0）
curl -s http://localhost:8000/api/v1/qc/ICP-01/lead

# 2. 提交新批次：z=(10.82-10.00)/0.20=4.1 → paused
curl -s -X POST http://localhost:8000/api/v1/qc/ICP-01/lead/batches \
  -H 'Content-Type: application/json' --data @demo/qc_add_batch.json

# 3. 更正序号 1 的实测值（revision 先从查询结果取）
curl -s -X PUT http://localhost:8000/api/v1/qc/ICP-01/lead/batches/1/correction \
  -H 'Content-Type: application/json' --data @demo/qc_correction.json

# 4. 补录空缺的历史序号 1
curl -s -X POST http://localhost:8000/api/v1/qc/ICP-01/lead/backfill \
  -H 'Content-Type: application/json' --data @demo/qc_backfill.json
```

在 <http://localhost:8000/docs> 中，三个写接口的请求体都内置了可直接试调的
命名示例（含更正 `10.82 → 10.10`、补录历史序号 1 的完整场景）。

常见错误：修订号过期/序号占用/批次 ID 重复/超过 100 批 → `409`；更正不存在
的序号 → `404`；十进制字符串非法、标准差非正、批次内水样结果 ID 重复 → `422`。

## 本地开发与测试

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

测试：

```bash
pytest -q
```

测试包含随机小规模输入与独立穷举程序的对照，验证最优配对数、最小总距离、
候选集合和“可能未匹配”判定。

## 目录

```text
app/main.py        FastAPI 路由、校验模型、响应组装（水样核对）
app/matcher.py     Levenshtein、最小费用流、最优方案稳定性分析
app/qc_rules.py    十进制 z 值计算与质控结论重算规则
app/qc_store.py    SQLite 持久化、修订号、事务与并发控制
app/qc_api.py      质控放行 API 路由与 /docs 示例
demo/              可直接提交的演示请求与预期响应
tests/             API、算法、规则与持久化测试
Dockerfile         API 镜像（两个 uvicorn 工作进程）
docker-compose.yml 一键启动配置（含 SQLite 命名卷）
```
