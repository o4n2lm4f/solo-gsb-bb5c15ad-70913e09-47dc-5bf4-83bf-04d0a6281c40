# 合同费率双时态追溯 API

基于 **Python + FastAPI + SQLite** 的合同费率追溯服务。录入费率时同时保存
**业务生效区间**（valid time）与**系统记录时间**（system time）两个维度；
修订既有区间只追加新版本、旧版本标记取代，历史永不覆盖。

## 一键启动

```bash
docker compose up --build
```

依赖安装与数据库建表全部自动完成。启动后访问：

- API 入口： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_PATH` | 容器内 `/data/rates.db`（本地直跑默认 `./rates.db`） | SQLite 数据库文件路径，启动时自动建表 |
| `ports` | `8000:8000` | 在 `docker-compose.yml` 中修改宿主机端口 |

数据通过 Compose 命名卷 `rates-data` 持久化，删除容器不丢数据。

## 双时态模型

每条费率版本记录两个时间区间（均左闭右开）：

| 维度 | 字段 | 含义 |
|---|---|---|
| 业务时间 | `valid_from` ~ `valid_to` | 费率在现实中生效的区间，`valid_to` 缺省为无限期 |
| 系统时间 | `recorded_at` ~ `superseded_at` | 该记录被系统认知为真的区间，`superseded_at` 为 NULL 表示当前认知 |

修订 = 插入新版本（`version` 递增）并把区间重叠的旧版本置上 `superseded_at`，
旧行不修改、不删除，任意历史认知时点均可回放。

**部分重叠只替换交集（v1.2 起）**：当新区间 `[valid_from, valid_to)` 只压住
某条当前版本的一部分时，旧版本仅在交集内被取代；交集之外未被覆盖的
**左段 `[old.valid_from, valid_from)`、右段 `[valid_to, old.valid_to)`** 会按
旧版本的**原费率与原 `reason`** 追加为新的“补偿版本”。修订窗口之外的报价因此
保持不变，不会凭空产生 gap；旧版本开放上界（`valid_to` 缺省）时，右补偿片段
同样开放上界。一次修订同时覆盖多条旧版本时，每条各自保留窗口外侧片段。

取代标记、新版本与补偿版本在**同一事务**提交：先创建新费率（取得最小的新
版本号），补偿片段再按 `valid_from` 排序紧随其后，`version` 严格递增。

**可审计血缘（v1.4 起）**：每次修订在同一事务写入血缘边表
`rate_version_edges`，边的方向统一为**新版本 → 旧版本（祖先方向）**：

| 边 `relation` | 起点 | 终点 |
|---|---|---|
| `supersedes` | 本次新费率 | 本次被取代的**每一个**旧版本 |
| `retained_from` | 每个补偿片段 | 该片段费率/区间所来源的旧版本 |

因此一次部分重叠修订会写入 1..N 条 `supersedes` 边（每个被取代版本一条）
以及与补偿片段等量的 `retained_from` 边；没有任何出边的版本（包括边表建立
之前写入的全部历史行）在血缘中视为**根**。边只追加、永不改写，与版本行
一起同事务提交（任一步失败整体回滚）。

## 接口

### 1. 录入 / 修订费率 — `POST /rates`

```bash
curl -X POST http://localhost:8000/rates \
  -H 'Content-Type: application/json' \
  -d '{
        "contract_no": "HT-2026-001",
        "rate": 0.052,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to":   "2027-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T09:00:00Z",
        "reason": "初始费率"
      }'
```

- `valid_to`、`recorded_at`、`reason` 均可选；`recorded_at` 缺省为当前时间。
- 与当前版本区间重叠时自动修订：响应中 `superseded_rate_ids` 列出被取代版本，
  `retained_versions` 列出窗口外保留的补偿版本（原费率、原 `reason`，按
  `valid_from` 排序；完整覆盖或无重叠时为空列表）。
- 部分重叠只替换交集：左、右未覆盖部分追加为补偿版本，修订窗口外报价不变；
  历史 `known_at` 仍可看到未被切分的原版本（见下文“双时态模型”）。
- 同一合同 `recorded_at` 必须单调递增，否则 `422 NON_MONOTONIC_RECORDED_AT`；
  `valid_to <= valid_from` 返回 `422 INVALID_INTERVAL`。

部分重叠修订的响应示例（原费率 0.05 覆盖全年，新费率 0.09 只替换 4–6 月）：

```json
{
  "created": {"rate_id": 2, "rate": 0.09, "version": 2,
              "valid_from": "2026-04-01T00:00:00.000000+00:00",
              "valid_to": "2026-07-01T00:00:00.000000+00:00", "status": "current"},
  "superseded_rate_ids": [1],
  "retained_versions": [
    {"rate_id": 3, "rate": 0.05, "version": 3,
     "valid_from": "2026-01-01T00:00:00.000000+00:00",
     "valid_to": "2026-04-01T00:00:00.000000+00:00", "status": "current", "reason": "原费率"},
    {"rate_id": 4, "rate": 0.05, "version": 4,
     "valid_from": "2026-07-01T00:00:00.000000+00:00",
     "valid_to": "2027-01-01T00:00:00.000000+00:00", "status": "current", "reason": "原费率"}
  ]
}
```

### 2. 报价 — `GET /quote`

```bash
curl "http://localhost:8000/quote?contract_no=HT-2026-001&valid_at=2026-03-01T00:00:00Z&known_at=2026-06-01T00:00:00Z"
```

返回该双时态坐标下唯一可见且有效的费率及版本依据（`basis` 含版本号、
记录时间、生效区间）。`known_at` 缺省为当前时间。

| 情形 | 状态码 | `code` |
|---|---|---|
| 恰好一条命中 | 200 | — |
| 无匹配 | 404 | `NO_MATCH` |
| 多条重叠命中 | 409 | `OVERLAP`（附 `candidates` 冲突明细） |

### 3. 版本时间线 — `GET /contracts/{contract_no}/timeline`

```bash
curl http://localhost:8000/contracts/HT-2026-001/timeline
```

按版本号列出全部版本及其业务/系统时间区间与状态（`current` / `superseded`）；
合同不存在返回 `404 CONTRACT_NOT_FOUND`。

### 4. 覆盖率分析 — `GET /contracts/{contract_no}/coverage`

```bash
curl "http://localhost:8000/contracts/HT-2026-001/coverage?from=2026-01-01T00:00:00Z&to=2027-01-01T00:00:00Z&known_at=2026-06-01T00:00:00Z"
```

先按 `recorded_at` / `superseded_at` 筛出 `known_at` 时点可见的版本，
再以这些版本业务区间的全部边界把查询窗口 `[from, to)` 切分为**连续**片段，
逐段标记：

| `status` | 含义 | 附带字段 |
|---|---|---|
| `covered` | 恰好一个可见版本覆盖该段 | `basis`：唯一版本依据 |
| `gap` | 无任何可见版本覆盖 | — |
| `conflict` | 多个可见版本重叠覆盖（脏数据） | `candidates`：全部候选版本 |

分类与候选完全相同的相邻片段会合并为一段。`known_at` 缺省为当前时间；
`from >= to` 返回 `422 INVALID_INTERVAL`；该认知时点下无任何可见版本时，
返回一个覆盖整个查询窗口的 `gap` 片段（合同不存在亦然，不报 404）。

响应示例（上半年为旧费率的补偿版本、下半年为修订后的新版本，全年连续无 gap）：

```json
{
  "contract_no": "HT-2026-001",
  "from": "2026-01-01T00:00:00.000000+00:00",
  "to": "2027-01-01T00:00:00.000000+00:00",
  "known_at": "2026-06-01T00:00:00.000000+00:00",
  "segments": [
    {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-06-01T00:00:00.000000+00:00",
     "status": "covered", "basis": {"rate_id": 3, "rate": 0.052, "version": 3, "...": "..."},
     "candidates": []},
    {"from": "2026-06-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
     "status": "covered", "basis": {"rate_id": 2, "rate": 0.058, "version": 2, "...": "..."},
     "candidates": []}
  ]
}
```

### 5. 计费预览 — `POST /contracts/{contract_no}/charge-preview`

```bash
curl -X POST http://localhost:8000/contracts/HT-2026-001/charge-preview \
  -H 'Content-Type: application/json' \
  -d '{
        "known_at": "2026-06-01T00:00:00Z",
        "lines": [
          {"from": "2026-01-01T00:00:00Z", "to": "2026-07-01T00:00:00Z", "base_amount": 10000.0},
          {"from": "2026-07-01T00:00:00Z", "to": "2027-01-01T00:00:00Z", "base_amount": 12000.0}
        ]
      }'
```

每条账单行 `[from, to)` 按 `known_at` 时点可见版本的业务边界拆分
（与覆盖率分析同一切分规则），`covered` 段费用 =
`base_amount × (段时长 / 行时长) × 命中费率`；行内汇总为 `line_total`，
全部行再汇总为 `total_amount`。费用与时长占比 `portion` 均保留 6 位小数（预览口径）。
`known_at` 缺省为当前时间。

片段（`segments[]`）状态：

| `status` | 含义 | 附带字段 |
|---|---|---|
| `covered` | 恰好一个可见版本覆盖该段 | `rate_id`、`rate`、`charge` |
| `gap` | 无任何可见版本覆盖 | — |
| `conflict` | 多个可见版本重叠覆盖（脏数据） | `candidates`：全部候选版本（`rate_id` / `rate` / `version`） |

账单行（`lines[]`）状态：`ok`（全部片段 covered）/ `gap` / `conflict` /
`mixed`（同时含 gap 与 conflict 段）。含 gap/conflict 段的行 `line_total` 为 `null`；
此时顶层 `status` 为 `rejected`、`total_amount` 为 `null`（**拒绝总额**），
但未受影响的行仍返回各自的计价结果。合同不存在或该认知时点无可见版本时，
所有行自然得到整行 `gap`，不报 404。

校验规则：

| 情形 | 状态码 | `code` |
|---|---|---|
| 账单行 `from >= to` | 422 | `INVALID_INTERVAL` |
| `base_amount` 为负或非有限数值 | 422 | `INVALID_AMOUNT` |
| `known_at` 晚于当前时间 | 422 | `INVALID_KNOWN_AT` |
| 缺少必填字段 / `lines` 为空 / 时间格式非法 | 422 | `INVALID_REQUEST` |

响应示例（两个版本各覆盖一行，全部可计价）：

```json
{
  "contract_no": "HT-2026-001",
  "known_at": "2026-06-01T00:00:00.000000+00:00",
  "status": "ok",
  "total_amount": 1216.0,
  "lines": [
    {
      "from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-07-01T00:00:00.000000+00:00",
      "base_amount": 10000.0, "status": "ok", "line_total": 520.0,
      "segments": [
        {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-07-01T00:00:00.000000+00:00",
         "status": "covered", "portion": 1.0, "rate_id": 1, "rate": 0.052,
         "charge": 520.0, "candidates": []}
      ]
    },
    {
      "from": "2026-07-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
      "base_amount": 12000.0, "status": "ok", "line_total": 696.0,
      "segments": [
        {"from": "2026-07-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
         "status": "covered", "portion": 1.0, "rate_id": 2, "rate": 0.058,
         "charge": 696.0, "candidates": []}
      ]
    }
  ]
}
```

### 6. 变更对比 — `GET /contracts/{contract_no}/changes`

```bash
curl "http://localhost:8000/contracts/HT-2026-001/changes?from=2026-01-01T00:00:00Z&to=2027-01-01T00:00:00Z&before_known_at=2026-02-01T00:00:00Z&after_known_at=2026-06-01T00:00:00Z"
```

分别按 `before_known_at` / `after_known_at` 两个认知时点筛选可见费率，
以**两侧业务区间边界的并集**把 `[from, to)` 切分为连续片段，逐段返回
`before` / `after` 两侧的覆盖状态（结构与覆盖率分析的片段相同：`covered`
附 `basis` 唯一依据、`conflict` 附 `candidates` 全部候选），并给出变更分类：

| `change` | 含义 |
|---|---|
| `added` | before 为 `gap`、after 唯一覆盖（该认知区间内新可见的费率） |
| `removed` | before 唯一覆盖、after 为 `gap` |
| `unchanged` | 两侧均 `gap`，或均唯一覆盖且费率相同 |
| `rate_changed` | 两侧均唯一覆盖但费率不同 |
| `conflict` | 任一侧存在重叠冲突（脏数据） |

分类与两侧候选完全一致的相邻片段合并为一段；每段附 `duration_us`（持续微秒数），
响应末尾的 `summary` 按分类汇总持续微秒数（五个 `*_us` 字段恒存在，合计等于
窗口总时长）。四个时间参数均必填。

校验规则（均返回 422 及稳定错误码）：

| 情形 | `code` |
|---|---|
| `from >= to` | `INVALID_INTERVAL` |
| `before_known_at >= after_known_at` | `INVALID_KNOWN_AT_ORDER` |
| 缺少必填参数 / 时间格式非法 | `INVALID_REQUEST` |

响应示例（3 月起费率由 0.052 修订为 0.058：前半段同费率补偿版本 → `unchanged`，
后半段费率变化 → `rate_changed`）：

```json
{
  "contract_no": "HT-2026-001",
  "from": "2026-01-01T00:00:00.000000+00:00",
  "to": "2027-01-01T00:00:00.000000+00:00",
  "before_known_at": "2026-02-01T00:00:00.000000+00:00",
  "after_known_at": "2026-06-01T00:00:00.000000+00:00",
  "segments": [
    {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-03-01T00:00:00.000000+00:00",
     "change": "unchanged", "duration_us": 5097600000000,
     "before": {"status": "covered",
                "basis": {"rate_id": 1, "rate": 0.052, "version": 1, "...": "..."},
                "candidates": []},
     "after":  {"status": "covered",
                "basis": {"rate_id": 3, "rate": 0.052, "version": 3, "...": "..."},
                "candidates": []}},
    {"from": "2026-03-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
     "change": "rate_changed", "duration_us": 26438400000000,
     "before": {"status": "covered",
                "basis": {"rate_id": 1, "rate": 0.052, "version": 1, "...": "..."},
                "candidates": []},
     "after":  {"status": "covered",
                "basis": {"rate_id": 2, "rate": 0.058, "version": 2, "...": "..."},
                "candidates": []}}
  ],
  "summary": {"added_us": 0, "removed_us": 0, "unchanged_us": 5097600000000,
              "rate_changed_us": 26438400000000, "conflict_us": 0}
}
```

### 7. 版本血缘 — `GET /rates/{rate_id}/lineage`

```bash
curl http://localhost:8000/rates/2/lineage
```

从目标版本出发，沿 `supersedes` / `retained_from` 边向**全部祖先**展开，
返回去重后的有向无环图（DAG）：

- `nodes`：目标版本及全部可达祖先。每个节点包含版本时间线中版本对象的
  **全部现有字段**（`rate_id` / `contract_no` / `rate` / `version` /
  `valid_from` / `valid_to` / `recorded_at` / `superseded_at` / `status` /
  `reason`），另附 `distance`——到目标版本的最短边数（目标为 0，每越过
  一条血缘边 +1）。节点按 **距离目标从近到远**排序，同距离按 `version`
  升序、再按 `rate_id` 升序，保证顺序稳定。
- `edges`：展开范围内的全部血缘边（自动去重），每条边含
  `from_rate_id` / `to_rate_id` / `relation` / `created_at`（边建立时间
  = 修订的 `recorded_at`）。边按起点节点距离、`from_rate_id`、
  `relation`、`to_rate_id` 排序。

历史行没有任何出边时视为根：其血缘只含自身一个节点、边列表为空。
注意边是新版本指向旧版本的，故从旧版本展开不会包含取代它的新版本。

| 情形 | 状态码 | `code` |
|---|---|---|
| 展开成功（含只有根节点） | 200 | — |
| `rate_id` 不存在 | 404 | `RATE_NOT_FOUND` |
| 血缘中存在环或悬空边 | 409 | `LINEAGE_CORRUPT` |

展开使用显式栈的迭代式遍历（非递归），并做灰/黑环检测与悬空端点校验，
发现损坏立即返回 409，**不会无限递归或挂死**。

响应示例（全年 0.05 的 v1 被 4–6 月的 v2 部分修订，v2 取代 v1，
左补偿 v3 / 右补偿 v4 各保留 v1 的来源血缘；从 v2 展开）：

```json
{
  "target_rate_id": 2,
  "nodes": [
    {"rate_id": 2, "rate": 0.09, "version": 2, "status": "current",
     "distance": 0, "...": "..."},
    {"rate_id": 1, "rate": 0.05, "version": 1, "status": "superseded",
     "distance": 1, "...": "..."}
  ],
  "edges": [
    {"from_rate_id": 2, "to_rate_id": 1, "relation": "supersedes",
     "created_at": "2026-03-01T00:00:00.000000+00:00"}
  ]
}
```

从左补偿片段 `GET /rates/3/lineage` 展开则得到
`3 →(retained_from) 1`；多次窗口修订可经 `retained_from` 链逐级追溯到
最初版本（菱形汇合时同一祖先只出现一次，`distance` 取最短边数）。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试
```

## 项目结构

```
app/
  main.py      FastAPI 路由与请求/响应模型
  logic.py     双时态核心逻辑（追加式录入、时点查询、时间线、覆盖率分段、计费预览、变更对比、版本血缘展开）
  db.py        SQLite 连接与自动建表（rate_versions + rate_version_edges）
  timeutil.py  UTC 时间规范化（等宽 ISO 字符串，可字典序比较）
tests/test_api.py
Dockerfile / docker-compose.yml
```
