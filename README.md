# 博物馆借展运输环境监控系统

面向博物馆借展运输场景的中文 Web 应用：按批次管理运输节点与文物，采集温湿度读数，
按时间轴自动核验并生成风险事件，支持处置闭环与评估版本管理。

**零第三方依赖**：仅使用 Python 3.9+ 标准库（`http.server` + `sqlite3`），
Docker 镜像无需安装任何包即可构建运行。

## 功能特性

- **持久化**：批次、有序运输节点、每件文物的温湿度上下限与约定采样间隔、
  绑定文物与节点的带时间戳读数，全部存入 SQLite。
- **按时间核验**：读数按时间轴排序评估；同一文物同一指标（温度/湿度）的连续越界
  读数**跨节点合并**为一个风险事件；相邻读数间隔超过约定采样间隔时标出**缺测区间**
  并**中断**正在进行的事件。
- **处置闭环**：工作人员必须填写**处置说明**与**证据编号**才能关闭事件。
- **评估版本**：补录或更正读数自动生成新的评估版本；旧版本的读数与关闭结论保持
  可查（只读快照）；**新版事件不自动继承关闭状态**；提供版本间差异展示
  （新增/更正/移除读数，新增/消失/变化事件，关闭状态未继承清单）。
- **跨文物共因风险组**：按版本对风险事件构图——同一指标的两个事件**起止时间闭区间
  相交**且涉及**至少一个相同运输节点**时连边，取连通分量，仅覆盖至少两件文物的
  分量成组（传递相连的成员同样纳入）。组内返回成员事件、文物、起止时间、**全组
  共有节点（可能为空）**与开放/已关闭计数；补录/更正生成新版时重算分组，旧版分组
  保持可查；关闭事件只更新所属版本的计数，不改变成员。
- **离线采集包**：现场设备离线暂存温湿度读数，恢复网络后按包上传。包携带**包编号、
  采集时所见评估版本号（基准版本，尚无版本用 0）、备注**与现有多条读数；当前版本
  已前进时逐键比对基准版与当前版，全部一致才合并为一个新版本，冲突则整包不写入
  并返回冲突键及两版值；**同编号包重试幂等**（相同内容返回首次结果，不同内容拒绝），
  并发上传同样遵守。
- **输入校验**：重复时间戳、无序上下限（下限 > 上限）、非数值读数一律拒绝保存，
  并返回中文原因。
- **页面**：首页与批次页展示批次风险摘要（开放/已关闭事件、按指标统计、缺测区间数、
  涉险文物等）。

## 快速开始

### 方式一：Docker Compose（一键启动）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t museum-monitor .
docker run -d -p 8000:8000 -v museum-data:/data --name museum-monitor museum-monitor
```

### 方式三：本地直接运行（无需安装依赖）

```bash
python3 app.py
```

## 配置

通过环境变量配置（均有默认值）：

| 变量      | 默认值             | 说明                     |
| --------- | ------------------ | ------------------------ |
| `HOST`    | `0.0.0.0`          | 监听地址                 |
| `PORT`    | `8000`             | 监听端口                 |
| `DB_PATH` | `museum.db`（容器内 `/data/museum.db`） | SQLite 数据库文件路径 |

## 地址与端口

- 页面入口：<http://localhost:8000/>
  - `/` 批次列表与风险摘要
  - `/batches/<id>` 批次详情（摘要、节点、文物、读数录入、事件处置、缺测区间、跨文物共因风险组、版本）
  - `/batches/<id>/versions/<n>` 历史版本只读快照（含该版风险组）
  - `/batches/<id>/diff?from=<a>&to=<b>` 版本差异
- API 前缀：`http://localhost:8000/api`
- 健康检查：`GET /api/health`

## 数据模型

| 表          | 说明                                                                 |
| ----------- | -------------------------------------------------------------------- |
| `batches`   | 运输批次                                                             |
| `nodes`     | 运输节点，`seq` 为批次内顺序（唯一）                                  |
| `artifacts` | 文物：温湿度上下限、`sample_interval` 约定采样间隔（秒）              |
| `versions`  | 评估版本：每次提交读数生成一个版本，批次内版本号递增                  |
| `readings`  | 读数：绑定版本、文物、节点、时间戳、温度、湿度；`corrected_from` 标记更正来源 |
| `events`    | 风险事件：指标、起止时间、读数个数、实测范围、限值、涉及节点、最大偏离、状态与关闭结论 |
| `gaps`      | 缺测区间：起止时间、约定间隔、实际间隔                                |
| `packages`  | 已接收的离线采集包：包编号（批次内唯一）、基准版本、内容指纹、生成的版本号与首次结果 |

## 业务规则

1. **事件合并**：同一文物同一指标，按时间轴相邻的越界读数合并为一个事件，
   与所在节点无关（可跨节点），事件记录涉及的全部节点（按节点顺序）。
2. **缺测中断**：相邻读数间隔 `> sample_interval` 时记缺测区间，进行中的事件
   在缺测前的最后一条越界读数处结束；缺测之后若仍越界，生成新事件。
3. **关闭条件**：`note`（处置说明）与 `evidence_no`（证据编号）均必填；
   仅可关闭**当前版本**的事件，历史版本事件与结论只读可查。
4. **版本化**：每次提交读数（追加/补录/更正）都生成新版本并重新评估；
   更正指同一文物同一时间戳但数值不同（旧版本中的旧值保持不变）；
   新版本事件一律为开放状态，不继承旧版关闭结论。
5. **跨文物共因风险组**（按版本）：同一指标（温度/湿度）的两个事件，仅当起止时间
   **闭区间**相交（端点相接也算）且涉及至少一个相同运输节点时连边；取全部连边的
   **连通分量**，只有覆盖至少两件不同文物的分量成组；经传递相连（A-B、B-C 相连而
   A-C 不相连）的成员也须纳入同组。`common_nodes` 是**全体成员**涉及节点的交集，
   传递相连时可能为空；`links` 仅列出真正存在的连边及其共同节点，**不得据此声称
   每对成员都有共同节点**。补录/更正读数生成新版时按新版事件重算分组，旧版分组
   始终可查；关闭事件只更新该版本组内的开放/已关闭计数，不改变成员与连边。
6. **拒绝保存**（返回 400 与中文原因）：
   - 重复时间戳：同一提交内重复，或与当前版本已有读数完全相同；
   - 无序上下限：温度/湿度下限大于上限；
   - 非数值读数：温度/湿度无法解析为数值；
   - 其他：时间戳格式无效、文物/节点不属于该批次、采样间隔非正整数等。
7. **离线采集包**（详见下文「离线采集包上传」）：
   - 合并条件：当前版本已前进时，包内每个「文物+时间戳」在基准版与当前版的
     读数（节点、温度、湿度）必须一致，否则整包不写入并返回 409 与冲突明细；
   - 成功合并只生成一个新评估版本，校验与风险评估规则与在线提交完全一致；
   - 幂等：同批次同编号且基准版本、备注、读数相同的重试返回首次结果
     （不产生新版本）；同编号内容不同拒绝（409）；并发上传遵守同一约束；
   - 无效基准版本、跨批次文物/节点、非法读数均明确报错（400），
     不产生版本也不占用包编号。

## HTTP API

所有 API 均返回 JSON；错误响应为 `{"error": "原因", "details": [...]}`。

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/api/health` | 健康检查 |
| POST | `/api/batches` | 创建批次 |
| GET  | `/api/batches` | 批次列表（含各批次风险摘要） |
| GET  | `/api/batches/<id>` | 批次详情（节点、文物、摘要） |
| POST | `/api/batches/<id>/nodes` | 添加运输节点 |
| POST | `/api/batches/<id>/artifacts` | 登记文物（限值 + 采样间隔） |
| POST | `/api/batches/<id>/readings` | 提交读数，生成新评估版本 |
| POST | `/api/batches/<id>/packages` | 上传离线采集包（冲突检查 + 幂等合并，见下文） |
| GET  | `/api/batches/<id>/packages` | 已成功接收的采集包列表 |
| GET  | `/api/batches/<id>/summary` | 批次风险摘要（当前版本） |
| GET  | `/api/batches/<id>/events?status=open&version=<n>` | 事件列表（默认当前版本） |
| GET  | `/api/batches/<id>/versions` | 版本列表 |
| GET  | `/api/batches/<id>/versions/<n>` | 版本快照（读数、事件、缺测区间、风险组） |
| GET  | `/api/batches/<id>/diff?from=<a>&to=<b>` | 版本差异 |
| GET  | `/api/batches/<id>/risk-groups?version=<n>` | 跨文物共因风险组（默认当前版本；批次无版本时 groups 为空） |
| GET  | `/api/batches/<id>/versions/<n>/risk-groups` | 指定历史版本的共因风险组 |
| POST | `/api/events/<id>/close` | 关闭事件（须处置说明 + 证据编号） |

### 调用示例

```bash
BASE=http://localhost:8000

# 1. 创建批次
curl -s -X POST $BASE/api/batches -H 'Content-Type: application/json' \
  -d '{"name":"2026 春季书画巡展","description":"本馆 → 巡展场馆"}'

# 2. 添加有序节点
curl -s -X POST $BASE/api/batches/1/nodes -H 'Content-Type: application/json' \
  -d '{"seq":1,"name":"库房","location":"本馆"}'
curl -s -X POST $BASE/api/batches/1/nodes -H 'Content-Type: application/json' \
  -d '{"seq":2,"name":"运输车"}'

# 3. 登记文物（温度 18~24℃，湿度 45~60%RH，采样间隔 600 秒）
curl -s -X POST $BASE/api/batches/1/artifacts -H 'Content-Type: application/json' \
  -d '{"name":"青花瓷瓶","temp_min":18,"temp_max":24,"hum_min":45,"hum_max":60,"sample_interval":600}'

# 4. 提交读数（生成评估版本 v1；温度跨节点连续越界将合并为一个事件）
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' -d '{
  "note": "首批数据",
  "readings": [
    {"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:00:00Z","temperature":20,"humidity":50},
    {"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:10:00Z","temperature":26,"humidity":50},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:20:00Z","temperature":27,"humidity":50},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:30:00Z","temperature":21,"humidity":50}
  ]}'

# 5. 关闭事件（缺任一项将被拒绝）
curl -s -X POST $BASE/api/events/1/close -H 'Content-Type: application/json' \
  -d '{"note":"已调整车厢温控并复查","evidence_no":"EV-2026-0001"}'

# 6. 更正读数（同一时间戳、不同数值）→ 生成 v2，新版事件不继承关闭状态
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"note":"更正运输车读数","readings":[{"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:20:00Z","temperature":22,"humidity":50}]}'

# 7. 查看版本差异与批次风险摘要
curl -s "$BASE/api/batches/1/diff?from=1&to=2"
curl -s "$BASE/api/batches/1/summary"

# 8. 校验拒绝示例（均返回 400 与原因）
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"readings":[{"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:00:00Z","temperature":20,"humidity":50}]}'   # 重复时间戳
curl -s -X POST $BASE/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"readings":[{"artifact_id":1,"node_id":1,"ts":"2026-10-01T10:00:00Z","temperature":"偏高","humidity":50}]}' # 非数值
curl -s -X POST $BASE/api/batches/1/artifacts -H 'Content-Type: application/json' \
  -d '{"name":"错误示例","temp_min":30,"temp_max":10,"hum_min":45,"hum_max":60,"sample_interval":600}'              # 无序上下限
```

也可以直接运行端到端演示脚本（服务启动后）：

```bash
./scripts/demo.sh
```

### 跨文物共因风险组（本轮新增）

至少登记两件文物并让二者在**同一运输节点、同一指标上出现时间重叠**的风险事件，
即可被识别为一组：

```bash
BASE=http://localhost:8000

# 节点：1 库房、2 运输车、3 展厅；文物：1 青花瓷瓶、2 绢画（限值 18~24℃）
# 两件文物在「运输车」同期（08:10–08:30）温度越界 → 闭区间相交且共享节点

# 查询当前版本的共因风险组
curl -s "$BASE/api/batches/1/risk-groups"

# 查询历史版本（补录/更正读数重算分组后，旧版仍可查）
curl -s "$BASE/api/batches/1/risk-groups?version=1"
curl -s "$BASE/api/batches/1/versions/2/risk-groups"
```

返回结构（组内事件、文物、起止时间、全组共有节点、开放/已关闭计数、真实连边）：

```json
{
  "batch_id": 1,
  "version": 1,
  "rule": "同一指标的两个风险事件，起止时间闭区间相交且涉及至少一个相同运输节点时连边；……",
  "groups": [
    {
      "group_id": "G1",
      "metric": "temperature",
      "metric_label": "温度",
      "start_ts": "2026-10-01T08:10:00+00:00",
      "end_ts": "2026-10-01T08:30:00+00:00",
      "artifact_count": 2,
      "event_count": 2,
      "open_events": 2,
      "closed_events": 0,
      "artifacts": [{"id": 1, "name": "青花瓷瓶"}, {"id": 2, "name": "绢画"}],
      "events": [
        {"id": 1, "artifact_id": 1, "artifact_name": "青花瓷瓶", "metric": "temperature",
         "start_ts": "...", "end_ts": "...", "status": "open", "nodes": [{"id": 2, "name": "运输车"}]}
      ],
      "common_nodes": [{"id": 2, "name": "运输车"}],
      "links": [
        {"events": [1, 2], "shared_nodes": [{"id": 2, "name": "运输车"}]}
      ]
    }
  ]
}
```

要点：

- `common_nodes` 为**全体成员事件**涉及节点的交集，成员仅经传递相连时为 `[]`，
  不代表组内每对事件都有共同节点；逐对共同节点请看 `links`。
- 补录或更正读数生成新版本后自动按新版事件重算；`?version=n` 可查任意旧版分组。
- 关闭某事件只改变该版本组内 `open_events/closed_events`，成员、连边与起止不变；
  历史版本事件不可关闭（沿用原有 400 规则）。
- 批次不存在返回 `404 {"error":"批次 n 不存在"}`；版本不存在返回
  `404 {"error":"批次 n 的版本 m 不存在"}`。
- 批次详情页展示**当前版**风险组卡片，版本快照页展示对应历史版分组。

### 离线采集包上传（本轮新增）

现场设备离线时暂存温湿度读数，恢复网络后按**采集包**上传：
`POST /api/batches/<id>/packages`。

请求体（`readings` 与在线提交格式相同）：

```json
{
  "package_id": "PKG-2026-0001",
  "base_version": 1,
  "note": "运输车离线采集回补",
  "readings": [
    {"artifact_id": 1, "node_id": 2, "ts": "2026-10-01T10:00:00Z", "temperature": 21, "humidity": 50}
  ]
}
```

- `package_id`：包编号，**批次内唯一**，用于幂等去重；
- `base_version`：采集时设备所见的评估版本号，**尚无版本用 0**；
- `note`：备注，作为新版本的备注保存；
- `readings`：多条读数，格式与 `POST /api/batches/<id>/readings` 相同。

**合并规则**：

1. 当前版本号等于 `base_version`：直接合并。
2. 当前版本已前进（`base_version` 小于当前版本号）：仅当包内每个
   「文物+时间戳」在**基准版**与**当前版**的读数（节点、温度、湿度）完全一致
   才合并；任一不一致即返回 `409` 与冲突明细（冲突键及两版值，缺失一侧为
   `null`），**整包不写入**：
   ```json
   {
     "error": "采集包与当前评估版本冲突（基准 v1，当前 v3），整包未写入",
     "package_id": "PKG-2026-0001",
     "base_version": 1,
     "current_version": 3,
     "conflicts": [
       {"artifact_id": 1, "ts": "2026-10-01T08:20:00+00:00",
        "base": {"node_id": 2, "temperature": 27.0, "humidity": 50.0},
        "current": {"node_id": 2, "temperature": 22.0, "humidity": 50.0}}
     ]
   }
   ```
3. 成功合并**只生成一个新评估版本**（`201`），校验（跨批次文物/节点、非法
   读数、重复时间戳等）与风险评估（事件合并、缺测中断、共因风险组）规则与
   在线提交完全一致；旧版快照照常可查。
4. **幂等**：同批次同编号且基准版本、备注、读数相同的重试返回**首次结果**
   （`200`，`"deduplicated": true`，不再生成新版本）；同编号但内容不同返回
   `409`；并发上传遵守同一约束（事务串行化 + 批次内包编号唯一约束）。
   内容比较按语义规范化：读数顺序、`20` 与 `20.0`、`Z` 与 `+00:00`
   等写法差异不影响「相同」判定。
5. **报错**（均不产生版本、不占用包编号，修正后可用同编号重传）：
   无效基准版本（不存在/负数/非整数/缺失）、跨批次文物或节点、非法读数
   返回 `400` 与中文原因；批次不存在返回 `404`。

**调用示例**：

```bash
BASE=http://localhost:8000

# 1. 设备在版本 v1 时离线采集，恢复后上传（当前仍是 v1 → 直接合并为 v2）
curl -s -X POST $BASE/api/batches/1/packages -H 'Content-Type: application/json' -d '{
  "package_id": "PKG-2026-0001",
  "base_version": 1,
  "note": "运输车离线采集回补",
  "readings": [
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T10:00:00Z","temperature":21,"humidity":50},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T10:10:00Z","temperature":26,"humidity":50}
  ]}'

# 2. 网络重试：相同内容原样重发 → 200 返回首次结果，不生成新版本
curl -s -X POST $BASE/api/batches/1/packages -H 'Content-Type: application/json' -d '{
  "package_id": "PKG-2026-0001",
  "base_version": 1,
  "note": "运输车离线采集回补",
  "readings": [
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T10:00:00Z","temperature":21,"humidity":50},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T10:10:00Z","temperature":26,"humidity":50}
  ]}'

# 3. 查看已成功接收的采集包
curl -s $BASE/api/batches/1/packages

# 4. 若期间在线更正过读数（当前已到 v3），基准 v1 的包且包内键被改动 → 409 冲突明细
curl -s -X POST $BASE/api/batches/1/packages -H 'Content-Type: application/json' -d '{
  "package_id": "PKG-2026-0002",
  "base_version": 1,
  "readings": [
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:20:00Z","temperature":23,"humidity":50}
  ]}'
```

响应（首次合并成功，`201`）在在线提交响应的基础上增加
`package_id`、`base_version`、`deduplicated` 字段；`GET /api/batches/<id>/packages`
返回已接收包的 `package_id`、`base_version`、`note`、生成的 `version_no` 与接收时间。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 目录结构

```
├── app.py               # 入口：HTTP 服务与路由（页面 + API）
├── museum/
│   ├── db.py            # SQLite 连接与表结构
│   ├── evaluate.py      # 风险事件评估（跨节点合并、缺测中断）
│   ├── riskgroups.py    # 跨文物共因风险组（同指标 + 闭区间相交 + 共享节点连边，连通分量）
│   ├── diff.py          # 版本差异计算
│   ├── service.py       # 业务逻辑与校验
│   ├── web.py           # 极简路由/响应封装
│   └── pages.py         # 中文页面渲染
├── tests/test_app.py    # 评估逻辑单元测试 + API 端到端测试
├── scripts/demo.sh      # curl 端到端演示
├── Dockerfile
├── docker-compose.yml
└── README.md
```
