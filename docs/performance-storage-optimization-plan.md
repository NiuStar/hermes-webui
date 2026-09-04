# Hermes WebUI 性能与存储优化实施方案

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 在不破坏会话完整性、崩溃恢复、跨端可见性和正在运行任务的前提下，把超长会话的切换/首屏加载从秒级降到稳定的百毫秒级以内，限制恢复日志和错误转储的无上限增长，并建立可审计、可回滚的生产发布流程。

**Architecture:** 保留 `state.db` 和 WebUI sidecar 的现有权威边界；优先优化读取路径和可再生/恢复型数据，不直接迁移权威会话数据。生产发布拆成“代码上线”和“不可逆日志淘汰”两个独立开关；任何派生缓存失效时均回退到现有完整读取/`session_snapshot`，不得返回不完整会话。

**Tech Stack:** Python 3.12、stdlib `ThreadingHTTPServer`、SQLite/WAL/FTS5、JSON sidecar、Docker Compose、vanilla JavaScript、pytest/Ruff。

---

## 1. 结论摘要

推荐按以下顺序实施：

1. **P0：先把安全和可观测性补齐**：建立73,252行结构等价合成负载、请求分位数、RSS/CPU、缓存命中、日志清理审计和生产回滚门禁。
2. **P0：生产只上线代码，不立即清历史日志**：将 `2835d4d5` 之后的候选先以超高保留上限部署24小时，确认会话完整性与性能，再单独启用 `8 runs / 256 MiB`。
3. **P1：解决冷会话首屏仍完整解析sidecar的问题**：对安全场景走metadata-only＋有界尾部读取＋SQLite尾部查询；复杂场景继续回退现有完整合并。
4. **P1：治理 request dump、缓存与日志**：request dump按14天或256 MiB/配置档治理；为转录缓存增加字节预算；第一阶段沿用已在隔离测试环境验证的Docker `json-file 50 MiB×3`，更严格的`local 10 MiB×3`只在兼容性验证后启用。
5. **P2：在独立大磁盘副本上执行官方FTS external-content迁移实验**：当前生产仅剩约11 GiB，禁止原地运行 `hermes sessions optimize-storage`。
6. **P3：暂不统一/替换权威存储**：sidecar/state.db统一、历史冷库拆分、年度分库均不进入本轮；先证明读取层优化已不足以满足目标再立项。

---

## 2. 当前证据与诊断

### 2.1 生产状态（只读实测）

| 项目 | 当前值 | 结论 |
|---|---:|---|
| 生产宿主 | `<production-host>` | 当前仍是正式环境 |
| 实际运行镜像 | `hermes-webui:session-integrity-v3-20260901` | 已回到旧稳定镜像 |
| 活动运行 | `active_runs=2`、`active_streams=2` | 不允许当前切换 |
| 宿主资源 | 8核、约11 GiB RAM、无Swap | 需避免并行大JSON解析放大 |
| 根磁盘 | 50 GiB，总用约37 GiB，余约11 GiB，77% | 已进入容量预警区 |
| 容器资源限制 | CPU/内存/PID均无限制 | 先观测后设置，不可盲目限死工具任务 |
| Docker日志 | `json-file`，无限额 | 可立即增加轮转 |
| `state.db` | 约10.81 GiB（2,833,643×4 KiB页） | 权威数据，不先动 |
| SQLite空闲页 | 4.48 MiB，约0.041% | 普通VACUUM当前几乎无收益 |
| sessions | 4,146 | 规模不大 |
| messages | 约805,000 | 大量历史消息 |
| 最大单会话 | 73,252行 | 冷全量加载是主瓶颈 |
| run journal | 6.063 GiB / 467文件 | 第一可治理存储热点 |
| request dump | 约0.762 GiB / 1,308文件 | 第二可治理存储热点 |

生产一次CPU快照约256%，但当时容器内有活动pytest和两个真实运行，不能归因成WebUI空闲CPU；方案要求分别测“空闲”和“活动任务”基线。

### 2.2 已验证性能

隔离测试环境隔离实例、14,000行合成会话、30次请求：

| 请求 | P50 | P95 |
|---|---:|---:|
| `messages=0`元数据 | 6.7 ms | 8.2 ms |
| 60条分页消息 | 48.2 ms | 67.7 ms |
| 侧边栏 | 4.2 ms | 6.4 ms |

原生产同类大会话元数据加载约4秒；`0a4ccd13`已把metadata-only路径降至毫秒级。该收益已验证，应保留。

### 2.3 SQLite查询结论

现有关键索引已存在：

- `idx_messages_session_id(session_id, id)`
- `idx_messages_session(session_id, timestamp)`
- `idx_messages_session_active(session_id, active, timestamp)`
- `idx_sessions_started(started_at DESC)`

实测最大73,252行会话：

- 全量消息读取：约2.15–2.79秒；
- 最近60行：冷约122 ms，热约1–2 ms；
- 消息计数：约6–12 ms；
- 最近50个会话：约0.2–0.4 ms。

**判断：索引不是当前主因。** 生产已经存在 `idx_messages_session_id(session_id,id)`，并且真实查询计划已使用它；子审查在缺少该索引的独立fixture上测得尾部查询可从346.8 ms降到0.89 ms，这证明该索引必须作为部署前置校验，但不构成生产新增索引的理由。不应再添加重复的 `(session_id,id)` 或 `(started_at)` 索引。主要成本是完整读取、JSON解析、Python对象化和sidecar/state.db合并。

### 2.4 冷会话首屏根因

`GET /api/session?...&msg_limit=N`虽然最终只返回N条可见消息，但当前公共路径仍先调用：

```text
get_session(sid, metadata_only=False)
  -> Session.load(sid)
  -> 读取并解析整个 {sid}.json sidecar
  -> 后续才做窗口截取/合并
```

因此：

- metadata-only已快；
- 同一会话的合并缓存命中已快；
- **首次打开超长、未缓存会话仍会完整解析sidecar**；
- `_FULL_SESSION_RESOLVE_MAX_CONCURRENT=2`只限制放大，不消除单次2秒级工作。

### 2.5 存储根因

#### Run journal

当前6.063 GiB中，长会话每轮`done`事件可能包含约38–45 MiB完整session快照，形成“会话越长，每轮日志越大”的增长。已实现并验证：

- 每会话最多8个已关闭run；
- 每会话已关闭日志最多256 MiB；
- 未终态、不确定、损坏、身份不匹配日志失败关闭并保留；
- 被淘汰游标走权威`session_snapshot`；
- 竞态修复到提交`2835d4d5f2f61e4efe591fcfbcef5bf9424a9e76`。

对当前生产只读投影：

- 467个日志中406个可明确识别为已关闭；
- 61个不确定/未终态文件全部保留；
- 预计从6.063 GiB降至约1.458 GiB；
- 预计回收约4.605 GiB（约75.95%）。

#### Request dump

Agent在API错误/特定预检失败时写入完整但已脱敏的请求诊断文件；当前无独立年龄或容量上限。它不是权威会话存储，可治理，但需在Hermes Agent仓库实施，不能在WebUI里越权删除。

#### FTS

当前数据库同时存在inline-mode：

- 原始`messages.content`；
- `messages_fts_content`正文副本；
- `messages_fts_trigram_content`正文副本；
- trigram内部索引数据超过100万行。

Hermes Agent已提供官方 `hermes sessions optimize-storage`，迁移为external-content布局。但其实现明确属于长时间、磁盘密集型迁移并最终VACUUM。按当前10.81 GiB数据库的2.2–3.2倍保守预留，需要约23.8–34.6 GiB可用空间；生产只有约11 GiB，**当前禁止执行**。

---

## 3. 目标指标与失败关闭门槛

### 3.1 性能SLO

| 场景 | 目标 |
|---|---:|
| metadata-only P95 | ≤50 ms |
| 73,252行结构等价会话首屏60条冷加载P95 | ≤250 ms |
| 首屏60条热加载P95 | ≤100 ms |
| 侧边栏P95 | ≤100 ms |
| 浏览器会话切换到可交互P95 | ≤300 ms |
| 无活动任务10分钟空闲CPU | <单核5%均值 |
| 重复切换10个大 会话20轮 | RSS不得单调增长超过首轮稳定值10% |
| 完整sidecar解析并发 | ≤2，且默认首屏不得进入此路径 |

### 3.2 完整性门槛

任何阶段出现以下任一项立即FAIL并回滚：

- session/message总数意外减少；
- 任一抽样会话首尾角色、message_count、最后助手回复或sidecar SHA不一致；
- active/sellable之类与本项目无关的数据不在范围，但会话来源/profile/read-only/lineage发生漂移；
- `cursor_run_missing`未产生`session_snapshot`回退；
- 500、Traceback、数据库malformed、OOM、容器重启；
- 新代码把不确定日志或未终态日志判为可删除；
- 性能达标但消息缺失、排序变化或tool卡片断链。

### 3.3 容量门槛

- Docker日志：≤150 MiB/容器（50 MiB×3）；
- request dump：≤256 MiB总量，且仅保留符合策略的近期诊断；
- run journal：当前数据应用策略后目标≤1.7 GiB；
- 根磁盘：代码发布前必须≥10 GiB可用，发布后不得低于8 GiB；
- FTS迁移：专用目标盘可用空间≥40 GiB，并另有可验证备份；否则禁止。

---

## 4. 目标架构

### 4.1 权威层保持不变

- `state.db`：Agent权威会话与消息存储；
- WebUI `{sid}.json`：WebUI可续聊、恢复和显示语义的权威sidecar；
- `_index.json`：侧边栏派生投影；
- run journal：短期崩溃恢复/replay证据；
- request dump：错误诊断证据。

本轮不将任一派生数据提升为权威，不删除sidecar，不把单次HTTP 200当作完整性通过。

### 4.2 快速首屏读取层

新增一个**失败可回退的有界首屏路径**：

```text
GET /api/session?messages=1&msg_limit=60
  1. Session.load_metadata_only()
  2. 判定是否满足安全快路条件
  3. 读取signature-bound sidecar尾部/派生尾部页
  4. 用state.db的(session_id,id)索引读取最近候选行
  5. 复用现有显示合并/去重函数
  6. 返回60条可见消息及旧的_msg_before兼容游标
  7. 任一不确定条件 -> 原完整get_session路径
```

快路禁止用于：

- 活动stream/pending turn；
- truncation watermark/boundary；
- 需要跨compression lineage完整前缀；
- sidecar签名变化、格式异常、legacy布局无可靠尾部事实；
- 编辑/rewind/regen/export/完整上下文构建；
- `msg_limit`缺失的显式全量读取。

### 4.3 可观测层

只暴露聚合指标，不暴露session ID、内容或路径：

- metadata/full/tail路径计数；
- full-sidecar parse次数、字节和耗时直方图；
- state.db行数/耗时；
- display merge cache hit/miss；
- session LRU数量/上限；
- run-journal eligible/protected/pruned files与bytes；
- 请求P50/P95/P99；
- 当前RSS/CPU/磁盘告警级别。

### 4.4 生命周期治理层

数据类别分别治理，不做一个“清理全部”按钮：

| 数据 | 默认策略 | 删除权限 | 不可删除 |
|---|---|---|---|
| Run journal | 8个已关闭run或256 MiB/会话 | WebUI同进程、路径锁内 | active/reopened/unknown/malformed/newest closed |
| Request dump | 14天或256 MiB/配置档，最旧优先 | Agent自身 | 最近24小时、全局最新10份、每session最新1份、legal hold、incident bundle、正在写的tmp |
| Docker日志 | 第一阶段`json-file 50 MiB×3`；验证后候选`local 10 MiB×3` | Docker logging driver | 当前打开日志由Docker管理；轮转前导出事件证据 |
| WebUI daemon/bootstrap日志 | 10 MiB×5 | WebUI日志owner | 当前incident capture |
| 自动备份 | pre-update 5份＋pre-migration 5份 | 各自产生者 | 最新可恢复自动备份、全部manual backup、legal hold |
| Quick snapshot | 沿用manual 20份 | Hermes backup owner | 最新完整快照 |
| `state.db` | 不按本轮策略自动删 | Hermes Agent官方命令 | 所有未显式批准的会话 |
| Sidecar | 不自动删 | 现有会话删除契约 | 所有现存会话 |
| `.bak`恢复文件 | 沿用现有恢复契约 | WebUI恢复模块 | 比live更完整或状态不确定者 |

---

## 5. 分阶段实施计划

## Phase 0 — 基线、结构等价负载与审计（P0）

### Task 0.1：建立73,252行无敏感内容的结构等价基准

**Objective:** 覆盖生产最大会话规模，而不复制生产正文。

**Files:**
- Create: `scripts/benchmark_large_session.py`
- Create: `tests/test_large_session_benchmark_fixture.py`
- Reuse: `scripts/test.sh`

**Steps:**

1. 只读统计生产role分布、content长度桶、tool_call比例、active/compacted比例；禁止导出正文。
2. 生成73,252行合成state.db和sidecar，保持字段形状与长度分布。
3. 分别测冷进程与热进程：metadata、首屏60、旧页、全量导出、会话列表。
4. 输出JSON证据：commit、fixture SHA、样本数、P50/P95/P99、RSS峰值、CPU时间、返回消息计数。
5. 固定为后续每个候选的对比基线。

**Verification:**

```bash
./scripts/test.sh tests/test_large_session_benchmark_fixture.py -q
python scripts/benchmark_large_session.py --messages 73252 --samples 30 --json-out /tmp/benchmark.json
```

Expected: fixture无生产正文，结果字段完整，重复运行计数和fixture SHA一致。

### Task 0.2：补充请求/缓存/清理聚合指标

**Files:**
- Modify: `api/request_diagnostics.py`
- Modify: `api/system_health.py`
- Modify: `api/routes.py`
- Modify: `api/models.py`
- Modify: `api/run_journal.py`
- Test: `tests/test_request_diagnostics_cache.py`
- Test: `tests/test_system_health.py`
- Create: `tests/test_session_perf_metrics.py`

**Steps:**

1. 先写红线：指标必须有界、不含session ID/内容/路径。
2. 为首屏路径记录route、rows、bytes、cache_hit、fallback_reason和耗时桶。
3. 为run journal记录计划/实际prune数量与字节，不扫描正文。
4. 指标容器固定大小；过期或高基数值不得进入内存。
5. 暴露到现有安全系统健康payload或结构化日志。
6. 为 `_display_merge_cache`、lineage display cache、`SESSIONS`和agent cache同时记录entry count与估算bytes；高基数session ID不得进入指标标签。
7. 为gateway watcher记录每次poll CPU-ms、是否执行全量parity、读取页/行数和通知延迟。

**Acceptance:** 24小时运行后指标容器大小恒定；没有秘密、正文和标识符。

### Task 0.3：把既有索引变成发布前置校验，而不是运行时迁移

**Files:**
- Create: `scripts/audit_state_db_query_plan.py`
- Create: `tests/test_state_db_query_plan_audit.py`
- Modify: `api/agent_sessions.py`（仅在审计需要共享SQL时）

**Steps:**

1. 只读检查 `idx_messages_session_id(session_id,id)` 是否存在且定义精确匹配。
2. 对最近页、旧页、count、last-active执行 `EXPLAIN QUERY PLAN`。
3. 若索引缺失，标记候选BLOCKED；不得在HTTP请求或WebUI启动路径同步建索引。
4. 索引创建/迁移只能进入Hermes Agent schema migration，并先在隔离副本量化锁、临时空间和写放大。

**Acceptance:** 生产审计必须显示tail/keyset读取使用 `idx_messages_session_id`，不存在 `USE TEMP B-TREE FOR ORDER BY`；否则禁止发布快分页。

---

## Phase 1 — 生产“代码先行、删除后置”发布（P0）

### Task 1.1：构建不可变生产候选

**Objective:** 生产候选必须与GitHub提交、镜像内容、测试证据一一绑定。

**Source baseline:** `2835d4d5f2f61e4efe591fcfbcef5bf9424a9e76`及后续P0补丁。

**Steps:**

1. 完成目标测试、邻近测试、生产门禁、Ruff、compile、diff check。
2. 冻结HEAD、binary diff SHA、文件数，独立P0/P1复审。
3. 提交并推送GitHub，`git ls-remote`读回SHA。
4. 在隔离测试环境从精确提交构建，记录image ID与关键源码SHA。
5. 隔离测试环境执行73,252行基准、重启持久性、run-journal竞态与快照回退。
6. `docker save`输出SHA绑定的镜像包；生产环境只load，不在生产临时编译。

### Task 1.2：在生产环境进行代码发布但暂不触发历史清理

**Deployment file:** `<production-deploy-dir>/docker-compose.yaml`

**Explicit initial settings:**

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "3"
environment:
  HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS: "1000000"
  HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES: "68719476736"
```

超高上限的目的不是最终配置，而是让代码发布和历史淘汰解耦。

`json-file 50m×3`是隔离测试环境已经读回验证的第一阶段配置。Docker `local` driver配合`10m×3`可进一步降低空间与元数据开销，但driver切换会改变日志读取/采集行为，必须先在隔离测试环境验证`docker logs`、事件导出、重启和运维脚本兼容性，不能与首次生产代码发布同时切换。

**Preflight:**

- 连续60秒 `active_runs=0 && active_streams=0`；
- 根盘可用≥10 GiB；
- 旧compose SHA、旧image ID、容器ID、重启数已记录；
- sidecar总数、`state.db` session/message数、抽样会话SHA已记录；
- 新镜像已加载且源码/version SHA一致。

**Post-switch acceptance（15分钟＋24小时）:**

- Docker与应用health均healthy；
- 0重启、0 OOM、0 Traceback；
- session/message计数不下降；
- 抽样长会话首尾、最后助手回复、来源/profile/lineage一致；
- metadata P95≤50 ms；
- 首屏P95不高于旧版1.25倍；
- run-journal文件数/bytes不因新策略突然下降。

**Rollback:** 恢复旧compose精确SHA和旧image ID，重建后读回；回滚不依赖修改后的数据。此阶段不删除历史日志，因此数据回滚边界清晰。

---

## Phase 2 — 激活run-journal有界保留（P0/P1）

### Task 2.1：增加dry-run清理计划与审计manifest

**Files:**
- Modify: `api/run_journal.py`
- Create: `api/run_journal_maintenance.py`
- Create: `tests/test_run_journal_maintenance.py`
- Modify: `docs/rfcs/session-sse-contract-v1.md`

**Manifest字段:** schema_version、generated_at、root signature、每个候选path相对名、size、closed_at、文件签名、eligible/protected reason、预计保留/回收总数；不得写消息正文。

**Rules:**

- plan阶段只读；
- apply必须绑定未过期manifest和相同文件签名；
- apply在WebUI同进程或全服务停止时运行，禁止独立进程与live writer竞争；
- active/reopened/unknown/malformed/identity mismatch全部保护；
- 每批限制文件数和字节数，批间重新检查磁盘、health和active runs；
- 删除后旧游标必须走`session_snapshot`。

### Task 2.2：生产分批启用最终上限

**Final settings:**

```yaml
HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_RUNS: "8"
HERMES_WEBUI_RUN_JOURNAL_MAX_CLOSED_BYTES: "268435456"
```

**Sequence:**

1. 在隔离测试环境复制结构化日志fixture，dry-run与apply一致性验证。
2. 生产环境等待空窗；生成生产dry-run manifest。
3. 人工核对eligible/protected分布；当前基线约406 eligible、61 protected，仅作为对比，不作为硬编码。
4. 每批最多25文件或512 MiB，先处理最大会话。
5. 每批后检查health、抽样会话、snapshot fallback、磁盘和容器重启。
6. 目标run journal≤1.7 GiB，预计释放≥4.0 GiB；实际值写审计报告。

**Rollback boundary:** 被删除的精确SSE事件不可恢复，因此此阶段不是传统字节级回滚。安全性来自：仅删除明确关闭的旧replay文件、保留最新终态、权威sidecar/state.db不变、客户端使用snapshot fallback。若业务要求精确重放历史，则必须先把eligible日志归档到独立盘，不得在同一低空间根盘留副本。

### Task 2.3：让终态journal不再复制完整会话

**Objective:** 从源头消除单个长会话每轮新增38–45 MiB终态日志，使“8 runs / 256 MiB”成为恢复上限而不是常态占用。

**Files:**
- Modify: `api/streaming.py`
- Modify: `api/run_journal.py`
- Modify: `api/routes.py`
- Test: `tests/test_run_journal.py`
- Test: `tests/test_run_journal_routes.py`
- Test: `tests/test_session_lost_response_regression.py`

**Approach:**

1. 浏览器live queue可暂时保持现有`done.session`兼容响应。
2. journal writer对`done`写入compact terminal record：session ID、terminal state、message count/revision、usage和snapshot-required标记，不写完整transcript。
3. replay遇到compact terminal record必须跨越明确`session_snapshot`边界，不能声称完整event replay。
4. `apperror`在权威sidecar持久化失败时仍保留恢复所需payload；不得统一裁剪所有终态类型。
5. 增加单run字节、终态event字节和append/fsync耗时指标。

**Acceptance:** 73,252行会话的普通成功run journal终态记录≤1 MiB；完整sidecar/state.db、最后助手回复和重连结果不变；错误持久化恢复fixture仍通过。

---

## Phase 3 — 冷会话首屏有界读取（P1，最大性能收益）

### Task 3.1：建立安全快路判定器

**Files:**
- Modify: `api/models.py`
- Modify: `api/routes.py`
- Create: `tests/test_session_cold_tail_fast_path.py`

**TDD cases:**

- 普通idle、无truncate、无compression复杂性：允许快路；
- active stream/pending：拒绝快路；
- truncation boundary/watermark：拒绝；
- legacy sidecar无法确认message_count：拒绝；
- sidecar读前后签名变化：拒绝；
- state.db签名变化：拒绝；
- malformed/partial文件：拒绝；
- `messages=0`保持完全不读正文；
- export/regen/新turn context保持完整路径。

### Task 3.2：实现bounded tail读取

**Files:**
- Modify: `api/models.py`
- Modify: `api/routes.py`
- Modify: `api/agent_sessions.py`
- Test: `tests/test_session_tail_payload.py`
- Test: `tests/test_state_db_session_signature.py`
- Test: `tests/test_display_merge_cache_shortcut.py`

**Implementation shape:**

1. metadata-only Session对象提供元数据和签名。
2. 复用/提取现有sidecar尾部有界读取逻辑；不得先`Session.load()`。
3. state.db按 `(session_id,id)` 倒序读取足够候选行；普通未压缩会话优先keyset。
4. 复用现有tool-row/visible-row合并规则，不能另写简化版去重。
5. 输出仍维持 `_messages_offset`、`_messages_truncated`、`_msg_limit_max`兼容字段。
6. 任一签名或语义不确定立即调用现有完整加载。

**Acceptance:**

- 73,252行冷首屏P95≤250 ms；
- 默认首屏测试注入`Session.load()`即失败，证明没有完整解析；
- 返回消息与完整路径截取结果逐字段一致；
- tool card、reasoning、compaction、rewind、lineage fixture全部通过；
- 首屏不把完整Session对象放入LRU；
- RSS峰值比完整路径下降≥70%。

### Task 3.3：分页从OFFSET逐步迁移到稳定cursor

**Files:**
- Modify: `api/routes.py`
- Modify: `static/sessions.js`
- Modify: `api/agent_sessions.py`
- Test: `tests/test_session_endless_scroll.py`
- Create: `tests/test_session_keyset_pagination.py`

**Approach:**

- 新增opaque cursor，内部绑定sidecar/state.db签名和边界message key；
- 普通会话使用`id < before_id ORDER BY id DESC LIMIT N`；
- 旧`msg_before`继续兼容；
- compacted/lineage复杂会话在有可靠派生索引前继续旧路径；
- cursor过期/签名变化时返回明确snapshot/reload信号，不拼接错页。

### Task 3.4：为转录类缓存增加字节预算

**Files:**
- Modify: `api/routes.py`（display/lineage merge cache）
- Modify: `api/models.py`、`api/config.py`（Session LRU）
- Modify: `api/streaming.py`、`api/config.py`（agent cache）
- Create: `tests/test_transcript_cache_byte_budget.py`

**Rules:**

- 保留既有entry count上限，同时增加estimated-byte cap；
- 优先淘汰最旧/最大的、已持久化且可安全重载的entry；
- active、pending、unsaved或无法验证持久性的对象永不淘汰；
- 重复切换10个大 会话20轮时RSS不得单调增长超过稳定基线10%；
- 指标报告cache bytes、largest entry、hit/miss/eviction，不报告session ID。

### Task 3.5：降低gateway watcher空闲CPU

**Files:**
- Modify: `api/gateway_watcher.py`
- Modify: `api/agent_sessions.py`
- Test: `tests/test_issue3506_memory_and_watcher.py`
- Create: `tests/test_gateway_watcher_bounded_parity.py`

**Approach:** 优先使用可靠维护的sessions revision/message_count/last_activity字段做轻量指纹；保留有界周期parity扫描和旧schema fallback。不得为了降CPU静默漏掉same-count rewrite、role-only变化或跨端新消息。

**Acceptance:** 80万消息fixture的常规poll CPU时间下降≥70%，最大通知延迟仍在明确SLO内，parity fixture可发现轻量指纹覆盖不到的变化。

---

## Phase 4 — Agent request dump和日志治理（P1，独立仓库）

### Task 4.1：为request dump增加有界保留

**Repository:** Hermes Agent，基线commit `58472d803a32edd19773bb2ed7426490981a636e`

**Files:**
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `agent/agent_init.py`
- Modify: `hermes_cli/config_defaults.py`
- Test: `tests/run_agent/test_run_agent.py`
- Update: 官方session-storage/diagnostics文档

**Config（建议）:**

```yaml
sessions:
  request_dump_retention_days: 14
  request_dump_max_total_mb: 256
  request_dump_keep_per_session: 1
  request_dump_keep_newest: 10
```

**Deletion contract:**

- 成功原子写入新dump后再best-effort sweep；
- 最近24小时全部保护；
- 全局至少保留最新10份，且每session至少保留最新1份；
- 超过14天或总量256 MiB时最旧优先；
- legal hold和active incident bundle优先于所有年龄/容量规则；
- 文件名解析失败、正在写的tmp、身份不确定全部保留；
- 只扫描`$HERMES_HOME/sessions/request_dump_*.json`；
- dry-run默认生成SHA绑定manifest；apply拒绝inode/size/mtime/path/policy漂移；
- 审计文件mode `0600`并位于删除根之外；只记录聚合和相对身份，不打印正文和错误body。

**Acceptance:** 生产现有约0.762 GiB可逐步收敛至≤256 MiB；会话正文、state.db和gateway JSONL不受影响。

### Task 4.2：Docker日志轮转

生产第一阶段Compose添加已验证配置：

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "3"
```

先在隔离测试环境重建验证，再随Phase 1代码发布带入。读回`HostConfig.LogConfig`，不得只看compose文本。

第二阶段在隔离测试环境单独验证Docker `local` driver、`max-size: 10m`、`max-file: 3`；只有`docker logs`、日志导出、重启和事故采集兼容后才替换第一阶段配置。WebUI daemon/bootstrap文件日志另加10 MiB×5轮转。不要在同一次生产切换中同时改变应用镜像、日志driver和保留策略。

---

## Phase 5 — FTS external-content迁移（P2，当前BLOCKED）

### Blocker

- 当前`state.db`约10.81 GiB；
- 当前根盘余约11 GiB；
- 安全迁移预估需约23.8–34.6 GiB可用；
- 隔离测试环境也只有约13 GiB余量；
- 因此当前两台机器都不允许直接执行。

### Task 5.1：准备独立磁盘副本

**Prerequisite:** 新增/挂载至少40 GiB可用的独立卷，或提供同等隔离工作空间。

**Steps:**

1. 用SQLite backup API创建一致副本，绝不`cp state.db`忽略WAL。
2. 记录源/副本文件SHA、page_count、session/message数、FTS schema、20个样本会话digest。
3. 对副本执行`PRAGMA integrity_check`。
4. 在副本上运行官方：

```bash
hermes sessions optimize-storage --yes
```

5. 记录峰值RSS、临时I/O、耗时、最大磁盘占用和中断恢复。
6. 验证普通/CJK/substring搜索结果集合和排序；验证messages/session计数及抽样digest不变。

### Task 5.2：迁移门禁

只有同时满足才进入生产维护窗：

- `integrity_check=ok`；
- session/message计数精确相等；
- 搜索fixture全通过；
- 数据库体积下降≥25%（若不足，收益不值得承担切换风险）；
- 搜索P95不恶化超过20%；
- 峰值空间低于专用盘70%；
- 中断后可从marker继续；
- 旧数据库完整保留在独立盘直到7天观察结束。

生产执行建议在服务drain并停止写入后进行，最终通过原子文件切换或受控恢复，不在运行中的唯一数据库上直接做不可回滚试验。

---

## Phase 6 — 资源隔离与长期演进（P2/P3）

### Task 6.1：先观测，再设容器硬限制

生产容器会执行真实Agent工具/pytest，不能直接照搬隔离测试环境的1.4 GiB限制。先收集7天：

- idle RSS；
- 1/2/3个并发run的RSS/CPU P95/P99；
- 最大子进程数；
- OOM与任务完成率。

建议公式：

```text
memory limit = min(宿主内存75%, 活动任务P99 RSS × 1.5)
CPU limit = 宿主核数 - 1
PID limit = 实测P99 × 2，且不低于512
```

在当前8核/约12 GiB宿主上，候选上限只能在实测后确定；不能直接把隔离测试环境测试值用于生产。无Swap且磁盘紧张，现阶段不建议从根盘再切大Swap文件。

### Task 6.2：统一会话存储仅做RFC/Spike

参考：`docs/architecture/unified-session-db.md`。

仅当Phase 3后仍不能满足SLO，才评估：

- state.db作为唯一权威；
- WebUI sidecar降级为恢复/导出快照；
- 增量事件或分页表；
- 历史冷库。

进入条件必须包括：跨端source/profile/lineage/rewind/compression/regen完整契约、双写校验、回滚和全量迁移工具。当前不实施。

### Task 6.3：冷会话只归档，不自动永久删除

- 30天无活动会话可提供auto-archive候选，但仅改变可见性；
- active、pinned、legal hold、lineage不完整或存在恢复异常的会话不得进入purge计划；
- 永久删除必须由操作者批准，并先完成WAL-safe导出/备份及恢复抽检；
- manual backup没有自动删除权限；自动备份只清理明确前缀，保留最新可恢复一份。

---

## 6. 推荐实施批次

### 批次A（立即，风险低）

- 73,252行结构等价基准；
- 聚合可观测指标；
- 已验证的Docker `json-file 50 MiB×3`日志轮转；
- state.db索引/查询计划只读审计；
- 隔离测试环境继续soak；
- 生产镜像与回滚脚本冻结；
- 不清数据，不跑FTS迁移。

### 批次B（生产代码发布）

- 等`active_runs=0 && active_streams=0`连续60秒；
- 上线新代码，但用超高run-journal上限；
- 验证15分钟＋观察24小时；
- 任一完整性异常立即回滚。

### 批次C（存储回收）

- dry-run manifest；
- 启用8/256 MiB；
- 分批淘汰明确关闭日志；
- 目标释放≥4.0 GiB；
- request dump策略在Agent独立提交后启用。

### 批次D（冷首屏重构）

- TDD实现bounded-tail fast path；
- 为转录类缓存增加字节预算；
- watcher轻量指纹＋有界parity；
- 隔离测试环境跑73,252行、复杂compaction/lineage、浏览器验收；
- 再走独立不可变发布。

### 批次E（需要新磁盘）

- FTS external-content副本实验；
- 达到迁移门禁后再安排生产维护窗。

---

## 7. 完整验证矩阵

### 自动测试

```bash
./scripts/test.sh tests/test_session_metadata_fast_path.py -q
./scripts/test.sh tests/test_session_tail_payload.py -q
./scripts/test.sh tests/test_display_merge_cache_shortcut.py -q
./scripts/test.sh tests/test_state_db_session_signature.py -q
./scripts/test.sh tests/test_session_endless_scroll.py -q
./scripts/test.sh tests/test_run_journal.py tests/test_run_journal_routes.py -q
./scripts/test.sh tests/test_session_lost_response_regression.py -q
./scripts/test.sh tests/test_reasoning_sidecar_compaction.py -q
./scripts/test.sh tests/test_session_recovery_api.py tests/test_session_recovery_audit.py -q
```

新增测试必须先在旧代码观察失败，再在实现后转绿。

### 静态与构建

```bash
git diff --check
.venv/bin/python -m py_compile api/models.py api/routes.py api/run_journal.py
.venv/bin/ruff check <changed-python-files>
```

完整测试环境必须安装Node；当前缺Node造成的全量基线失败不能伪装成通过。最终候选应由CI或具备Node的隔离环境完成全套。

### 真实运行验收

- 14k和73,252行合成会话；
- 冷进程/热进程各30轮；
- 10个大 会话循环切换20轮；
- 容器重建前后sidecar SHA一致；
- `state.db` session/message计数一致；
- run-journal游标淘汰后snapshot fallback；
- active run期间不得清理其日志；
- 浏览器桌面和移动端：首屏、向上翻页、tool card、reasoning、切换回来、刷新、断线重连。

---

## 8. 风险与取舍

1. **日志淘汰不可字节级回滚。** 所以必须与代码发布分离，并依赖权威snapshot回退。
2. **冷尾快路最容易破坏复杂会话语义。** 仅对可证明安全的普通idle会话启用，复杂状态一律回退。
3. **额外索引会增加写放大。** 现有索引已覆盖核心查询，未有EXPLAIN和基准收益不得新增。
4. **FTS迁移有大I/O和空间放大。** 当前BLOCKED；不能因官方命令“可中断”就绕过磁盘门槛。
5. **容器硬内存限制会杀死工具任务。** 先量化并发Agent子进程，再确定生产上限。
6. **archive不等于释放磁盘。** 归档只改变可见性；真正删除会话需另行显式批准和外部备份，不在本方案自动执行。
7. **sidecar/state.db双权威边界复杂。** 本轮用派生快路和失败回退降低风险，不直接统一存储。
8. **count cap不等于byte cap。** 16个或100个entry若都是超长会话，仍可能保留多份完整transcript；必须同时限制估算字节。
9. **日志driver切换是独立运维变更。** 更严格的`local` driver只有在隔离测试环境证明现有日志工具兼容后才进入生产。

---

## 9. 当前建议决策

建议批准：

- Phase 0；
- Phase 1“代码先行、删除后置”；
- Phase 2 run-journal有界治理；
- Phase 3冷首屏快路；
- Phase 4 request dump和Docker日志治理。

暂不批准：

- 当前生产原地`VACUUM`；
- 当前生产原地`hermes sessions optimize-storage`；
- 自动删除旧会话；
- sidecar/state.db统一迁移；
- 把隔离测试环境资源上限直接复制到生产环境。

**首个执行动作应是Phase 0的73,252行结构等价基准与可观测性，不是继续修改生产数据。**
