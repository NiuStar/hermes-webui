# 编码设计：真实写方与围栏接入

状态：CANDIDATE_PENDING_INDEPENDENT_REVIEW，功能NOT_RUN；当前部署排他资格未证明，全部scope默认LEGACY。源码定位基线WebUI 43ea70817a102c6e5eedde13ff187cb74abf6ec6（本阶段代码未变）；本机Agent 58472d803a32edd19773bb2ed7426490981a636e。Agent仅作为源码参考，不等于生产Agent版本已核对。

## 1. 已定位入口及设计改动

| 写方 | 实际源码入口 | 围栏接入与传播 |
|---|---|---|
| sidecar保存/备份 | api/models.py Session.save L1472–1660；messages/tool_calls/anchor_activity_scenes及extra序列化L1543–1555 | save新增显式binding参数；取得profile/scope门后检查绑定，持门覆盖旧备份、临时文件、原子replace及读回；返回结构化保存结果，原有提前return不能被当成功 |
| retry/截断/再生成 | api/session_ops.py truncate_session_at_keep L918–933；retry_last L936起；apply_regeneration_plan L142起 | 这是内存修改入口，不是全部落盘；在修改前建立REWRITE mutation，传播到save及Agent状态修改。保留messages/context_messages/truncation_watermark/boundary和shrink generation语义 |
| 恢复备份 | api/session_recovery.py recover_session L401–429 | 恢复可能直接替换文件，不能只改Session.save；启动恢复与HTTP恢复共享受控恢复包装，先撤active、排空、REWRITE再恢复读回 |
| 缺失sidecar补建 | session_recovery.py recover_missing_sidecars_from_state_db L650–703 | 每scope先登记源身份和REWRITE；未知父链或DB写方默认LEGACY；不因补建成功启用 |
| journal追加 | api/run_journal.py append_run_event L630–695 | binding传播至实际文件append/fsync；旧run_id:seq继续旧协议，新session_seq另存展示库。journal写失败阻断compat，不把旧done metadata当完整正文 |
| 元数据同步 | api/state_sync.py sync_session_start L108–132、usage L135–197、title L200–242 | title/usage若影响展示仍登记mutation或独立明确元数据版本；不能笼统排除state_sync。基线首版保守纳围栏 |
| Agent保存 | run_agent.py _persist_session L2120–2158 | 上层streaming注入服务端binding并贯穿每次内部持久化；仅包WebUI外层无效。无法改变内部实际写调用则该run无新读资格 |
| Agent改写/删除 | hermes_state.py replace_messages L12401–12499、delete_session L14416–14473、delete_session_if_empty L14475–14517、delete_sessions L14519–14601 | 正文增删改均在实际DB写事务处检查binding；批量操作按排序scope集合持门；删除仍需要用户另授权，本文不授权 |
| CLI/gateway/cron/独立Agent | 共用Agent持久化库，但可能独立JSONL/原始SQL/外部还原 | 未逐入口查证，不声明闭包；无权绕过专用写入者才可受控。现阶段明确排除启用范围并保持其可达scope及子scope LEGACY |
| handoff/导入/父链/备份还原 | routes存在独立handoff持久化路径；管理脚本可绕过上述API | 必须由写服务统一入口或在操作前全局撤资格；路径未完全审计属于启用阻断，不凭合作锁授资格 |

## 2. 最小接入模块与权限设计

拟新增display_mutations负责binding/门、display_store负责展示库事务、display_compat负责源写与读回、display_events负责完整事件验证、display_worker负责候选构建与发布、display_reader负责固定快照分页。routes/streaming仅传显式ScopeKey和Binding，不直接改展示库。旧读取路径保持独立，任何新资格失败均走原有语义，不拼接未经验证的新旧正文。

第一阶段只实现本地隔离适配和shadow，不更改生产权限结构。要启用scope必须另批专用源写入服务/OS隔离设计及部署：权威目录和SQLite/WAL/SHM仅写服务身份可写，推理工具、CLI、gateway及WebUI请求执行身份不能写；关闭或证明所有历史可写句柄失效；父链全部受控。仅chmod但仍同UID运行任意工具不成立。跨进程scope门由写服务持有，不能用无失效保护的独立锁文件代替权限。

这不是自动授权引入新生产服务：现有路线未满足该资格时继续LEGACY，先完成隔离M0验证。OS隔离/服务拆分若需改变既定部署必须重新获批，不允许以“继续编码”授权发布路线变化。

锁顺序必须兼容现有_agent_lock→LOCK→index顺序：外层统一scope门→agent_lock→短LOCK；任何持LOCK调用save会因其内部再次获取非重入LOCK死锁。把scope门接入实际落盘前还须改造调用栈以避免agent_lock→scope门反向获取；M0记录所有嵌套路径并用确定性屏障验证，不能只新增一把锁。

## 3. 失败关闭与验证

| 风险 | 对策 | 验证 | 回退 |
|---|---|---|---|
| 保存返回None/早退被认为成功 | 新适配器强制重新打开目标并比对，结构化COMMITTED/UNCERTAIN | T21/T25暂停replace与读回 | 不seal，不删源 |
| Agent内部未传token | 底层DB和JSONL写拒无binding；未知写方可达scope不启用 | T22/T23真实内部/外部写尝试 | LEGACY |
| 锁逆序死锁 | 明确全栈顺序，禁止持LOCK调用save；超时不夺锁 | T21/T34屏障 | 停新读/构建，不杀业务 |
| 恢复/父链绕过 | 源集合登记、父依赖撤资格、启动恢复在资格授予之前 | T22/T34/T42 | 拒资格、不自动删备份 |
| 版本参考不是生产证明 | 固定WebUI/Agent版本清单并在启用前复核每个实际写模块 | T30/T41 | 版本漂移立即拒新能力 |

完成口径：已给出已定位入口和可编码接入责任；不是全生产写方闭包证明。独立终审必须区分“隔离验证的设计可用”和“生产受控资格未证明”，不能把CLI/脚本调查缺口隐去后宣称生产可接入。
