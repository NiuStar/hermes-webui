# 会话读写分离测试计划

状态：测试设计，未执行功能测试。对应implementation.md M0–M5。本文不能用此前54项诊断测试作为新功能通过证据。

## 1. 测试矩阵

本表T01–T20保留；R1–R6屏障及新增风险验证见 `session-read-write-separation-test-addendum.md`。两份共同构成测试计划，所有新功能用例均为NOT_RUN。风险与用例双向映射见 `session-read-write-separation-risk-matrix.md`。

| ID | 场景及操作 | 必须观察的结果 | 拟建测试文件 |
|---|---|---|---|
| T01 | 空/单/多profile建库，重复schema初始化 | 独立路径、版本一致、跨scope FK拒绝 | tests/test_display_store.py |
| T02 | 相同event_key重复、不同payload冲突、并行分配序号 | 同payload一次落库，冲突明确失败，session_seq唯一有序 | tests/test_display_store.py |
| T03 | PREPARED前/后、旧源保存后、seal前分别进程崩溃 | 已确认事件不丢，UNCERTAIN不发布，不重复运行工具 | tests/test_display_recovery.py |
| T04 | terminal和job事务前后崩溃、启动重复补偿 | 终态有可恢复任务或明确BLOCKED，任务幂等 | tests/test_display_recovery.py |
| T05 | worker租约失效、旧worker迟到、新worker接管 | fence/CAS拒绝旧发布，候选不覆盖新修订 | tests/test_display_worker.py |
| T06 | 归档上一轮同时开始下一轮 | 新事件完整留在水位后，不被覆盖或重复显示 | tests/test_display_worker.py |
| T07 | GET事务前中后发布、旧游标翻页 | 单响应同版本；旧epoch明确reload，不混用偏移 | tests/test_display_paging.py |
| T08 | SSE断线重连、GET/SSE交错、重复事件 | 按session_seq/event_key追平，anchor无重复/遗漏 | tests/test_display_live_events.py |
| T09 | done元数据、ephemeral/answer、工具只有预览或无ID | 不假装完整；兼容旧恢复，降级状态可见 | tests/test_display_live_events.py |
| T10 | 普通完成/失败/取消/替换/压缩中断 | 有效部分可恢复，终态明确，不残留可附着假运行 | tests/test_display_recovery.py |
| T11 | 编辑/再生成/截断/分支/父链压缩与构建交错 | 先失效，epoch递增，旧消息不复活 | tests/test_display_mutations.py |
| T12 | gateway/CLI/恢复/导入未受控外部写方 | 新读资格拒绝，走原来源语义，不错报最新 | tests/test_display_mutations.py |
| T13 | 权限撤销/归档/删除、伪造scope/游标/路径 | 权限即时生效；归档按既有合同可访问，删除不复活；跨profile拒绝 | tests/test_display_security.py |
| T14 | 图片正文与截图占位、同文不同身份、多工具、-1工具索引 | 与既有批准合并语义完全一致，不能文本去重 | tests/test_display_paging.py |
| T15 | Todo非空→空、最新Todo在页外、时间戳混乱 | 最新显式空生效，后台维护不更改用户活动时间 | tests/test_display_paging.py |
| T16 | 大单行、长活动积压、磁盘不足、SQLite锁超时 | 有界/背压或明确旧路径，不截断冒充完整，不自动删除 | tests/test_display_budget.py |
| T17 | 新库不可用期间旧写入口 | 不绕过失效围栏导致新读陈旧；错误/切换可审计 | tests/test_display_mutations.py |
| T18 | 新活动事件尚未同步旧恢复源时尝试回滚 | 阻止丢弃新恢复组件；兼容证明后才能完整回滚 | tests/test_display_recovery.py |
| T19 | 模型配置缺失/变化/同名不同provider或endpoint/远端阻塞 | 正文无同步探测，unknown/stale明确，推理校验保持 | tests/test_session_display_metadata_nonblocking.py |
| T20 | 新旧客户端协议、完整历史导出、深链接恢复 | 旧客户端不接新游标语义，导出不漏活动正文，来源只读边界保留 | tests/test_display_live_events.py |

## 2. TDD执行方式

以上文件为拟新增，不是现有测试。每项先实现失败测试，记录RED具体原因，再做最小实现，运行GREEN及邻接回归。不得仅断言源码字符串或用mock替代事务/真实SQLite行为。并发用两个连接/进程及屏障精确定位，不用随机sleep证明无竞态。崩溃测试只在本地隔离临时状态进程执行，禁止对生产杀进程造故障。

拟执行：
```sh
./scripts/test.sh tests/test_display_store.py tests/test_display_worker.py tests/test_display_recovery.py -vv --maxfail=1
./scripts/test.sh tests/test_display_paging.py tests/test_display_live_events.py tests/test_display_mutations.py tests/test_display_security.py tests/test_display_budget.py tests/test_session_display_metadata_nonblocking.py -vv --maxfail=1
./scripts/test.sh tests/test_run_journal_compact_done.py tests/test_run_journal_process_encoding.py tests/test_sidecar_compact_serialization.py -q
./scripts/test.sh
npm run lint:runtime
```
完整回归耗时长时记录真实未完成，不用目标测试数填补。项目原生JS无打包步骤；JS修改需实际浏览器运行检查，不虚构npm build。浏览器本地fixture按TESTING.md运行，生产UI验证在既有入口，不新建测试部署。

## 3. 基线与真实数据验证

以旧读路径为展示oracle，不按原始数据库行数猜展示数。三个已测样本a110e5f43e66、29e681019f35、d6d43b6448a9仅为首批，不代表所有存量覆盖。生产读取需授权、串行且只保存哈希/计数/元数据；不提交正文、凭据。

每个拟启用会话依次遍历全部分页，验证全局offset、场景扩展、角色顺序、工具关联、截断、anchor、完整正文规范化SHA与旧路径一致。重叠场景按坐标验证不能简单拼接算重复。外部变化时固定源版本后重新比对；无法固定来源不能启用。

验证稳定历史GET不调用全量merge或大小扫描；活动GET只读已索引事件范围，增量has_more时客户端必须继续拉取。检查数据库查询计划走scope/generation/offset及session_seq索引，记录读取行数与载荷字节。

## 4. 性能与资源门禁（均为目标）

每样本、每开关至少20次串行热请求，记录原始单次数据、p50/p95/max、请求错误，不删慢样本；明确p95采用nearest-rank算法。小会话正文p95≤200ms、长会话尾页p95≤800ms、metadata p95≤100ms。冷请求只能在批准且无活动运行的重启后测，目标≤2秒；不足冷样本必须注明未认证。浏览器首屏单列网络、解析、DOM渲染，不能用本机HTTP代替。

开关off/shadow/stable_read/active_read分别测；积压与并发下一轮另测。采集worker和服务RSS、磁盘写入、WAL、空闲磁盘、队列长度及最老任务年龄。派生存储全量扩容预算未量化则回填FAIL；不因测试通过就绕过资源门禁。

## 5. 发布与证据

本轮不执行发布。未来按批准切片M0–M5逐级推进；代码通过不等于生产授权。保留兼容decoder、旧数据、旧日志和备份。验收记录提交/image/入口、启用scope与源修订、样本数、所有错误、测试日志路径、哈希对比、未测项及回滚读回结果。禁止清理失败候选数据。

验收四状态：PASS/FAIL/NOT_RUN/BLOCKED；文档检查PASS不等于功能PASS。进入功能编码前详细设计独立评审；开启真实历史读取前全部相关一致性测试PASS；active_read另需完整事件与客户端重连门禁PASS。
