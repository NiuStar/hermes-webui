# 会话读写分离：编码详细设计

状态：待独立评审；仅文档，未实施。依据已确认的 session-read-write-separation-design.md。基线为3287fd3b文档及867c2bdc诊断代码，不引入main中的失败候选。

## 1. 现状与选型

已核源码：api/run_journal.py append_run_event使用run_id:seq、追加文件和按策略fsync；_journal_payload_for_sse_event将普通done缩减为元数据（ephemeral和answer例外）。api/streaming.py on_tool兼容不同回调签名，_tool_args_snapshot截短参数；不能假设journal有完整工具结果。api/state_sync.py sync_session_usage为统计同步，不是正文提交协议。api/routes.py既有合并/分页/anchor恢复仍是本轮兼容参照。Session.compact不是模型压缩。

选型：每个明确profile的WebUI状态目录新增独立SQLite `display_projection.db`，WAL、foreign_keys=ON、synchronous=FULL。不修改Agent state.db schema，不引入Redis/新服务。一个进程内后台worker，跨进程由数据库租约仲裁；独立库隔离Agent写锁，方便关闭新路径回退。代价是暂时增加派生存储，不宣称正文去重。

稳定历史使用不可变行段+版本段清单，避免每轮复制完整历史；非追加操作允许重建受影响版本。大单行JSON独立存行，不以固定行数承诺固定内存。完整复杂合并仍可能需要全量对象：只准在后台受预算控制执行，超预算保持旧路径，不宣称已经实现有界流式合并。

## 2. 模块与接口（均为拟新增）

api/display_store.py：连接/schema、读快照、租约、CAS发布；不导入routes。
api/display_projection.py：既有展示语义的纯适配、段构建、窗口及anchor索引。
api/display_worker.py：领取任务、源快照验证、构建、发布及启动补偿。
api/display_events.py：完整展示变更协议；不得把SSE预览伪装完整事件。

接口初版（参数最终以protocol-v2为准，尤其commit_event必须携带epoch/revision/token）：begin_mutation(scope, expected_revision, mutation_id)；commit_event(scope, run_id, event_key, payload)；seal_mutation(mutation_id, source_manifest)；enqueue_projection(scope, revision)；claim_job(owner)；publish(job_id, fence, expected_revision, generation)；read_page(scope, generation, before, limit)。scope含规范化profile根标识、source和session_id，路径由受信profile解析器提供，不接受客户端路径。

## 3. Schema概念清单（非冻结DDL）

下列为初版字段概览，不可据此直接生成迁移；R2–R6字段、接口和约束以 `session-read-write-separation-protocol-v2.md` 为准。各设计的风险和测试见 `session-read-write-separation-risk-matrix.md` 及测试扩展。

所有表含scope_id，profile物理隔离外仍保留source/session_id唯一约束。整数序号非负，所有JSON有schema_version，SQL参数绑定。

- scopes(scope_id PK, source, session_id, epoch, mutation_revision, published_generation NULL, eligibility, dirty_reason, UNIQUE(source,session_id))。
- mutations(scope_id, mutation_id, expected_revision, state, source_manifest_json, PRIMARY KEY(scope_id,mutation_id))；state=PREPARED/SEALED/UNCERTAIN。非追加写前持久化PREPARED。
- events(scope_id, session_seq, run_id, event_key, anchor_id, kind, payload_json, schema_version, PRIMARY KEY(scope_id,session_seq), UNIQUE(scope_id,run_id,event_key))。session_seq在同库事务分配，不拼接比较不同run局部序号。重复键且payload不同必须报冲突，不覆盖。
- runs(scope_id, run_id, terminal_state, sealed_seq, completeness, PRIMARY KEY(scope_id,run_id))。completeness=COMPLETE/LEGACY_INCOMPLETE；终态并不自动等于完整。
- jobs(job_id PK, scope_id, target_revision, state, attempts, lease_owner, lease_until, fence, retry_at, error_code, UNIQUE(scope_id,target_revision))。state=PENDING/LEASED/RETRY/PUBLISHED/BLOCKED/SUPERSEDED；索引(state,retry_at,lease_until)。
- generations(scope_id,generation,source_revision,epoch,covered_seq,state,row_count,last_message_at,todo_json,source_manifest_json, PRIMARY KEY(scope_id,generation))；state=BUILDING/VERIFIED/PUBLISHED。
- segments(scope_id,segment_id,row_count,bytes,sha256,state, PRIMARY KEY(scope_id,segment_id))。
- rows(scope_id,segment_id,row_index,row_json,row_sha,anchor_id, PRIMARY KEY(scope_id,segment_id,row_index))。外键指向segments。
- generation_segments(scope_id,generation,start_offset,segment_id,row_count, PRIMARY KEY(scope_id,generation,start_offset))；索引(scope_id,generation,start_offset)，用于定位全局分页坐标。
- scene_bounds(scope_id,generation,anchor_id,first_offset,last_offset,metadata_json, PRIMARY KEY(scope_id,generation,anchor_id))。保留-1工具索引的既有兼容语义，不直接判损坏。

版本只引用VERIFIED段；构建完成校验段数/行数/哈希/覆盖水位及工具关联后发布。旧版本和段不自动删除。新增schema单独版本化，不改变Agent schema26。

## 4. 跨存储提交与恢复

不能对SQLite、sidecar、Agent库声称跨库原子事务。两阶段围栏：
1. WebUI受控写入先begin_mutation：BEGIN IMMEDIATE，revision递增，eligibility=DIRTY，持久化PREPARED，提交成功才写旧权威存储。
2. 既有Session.save及Agent持久化正常执行；新事件保存完整展示变更，事件、run终态和job登记在派生库同事务提交。实时SSE仅在对应可恢复事件提交后发出；失败不得伪称durable，可继续旧协议但整轮降为LEGACY_INCOMPLETE且不启用增量新读。
3. seal记录确定的源清单；写失败或跨库崩溃保持UNCERTAIN，禁止发布。启动扫描PREPARED及终态无任务记录，按源快照重建或BLOCKED，不能自动重跑工具。
4. 新功能禁用时不调用新库；一旦启用围栏但库不可写，受控破坏性写不能绕过围栏继续，不得产生仍被新读认为有效的旧版本。应返回可重试错误或先全局禁用新读并确认各进程生效。

源清单含侧文件持有句柄的标识/大小/时间/构建时SHA、父链及截断修订、SQLite只读事务快照签名。读前后签名变化则放弃构建。stat本身不证明任意外部原地写绝对可见：未纳入围栏的Agent/CLI/gateway/导入/恢复等写方一律不启用新读资格，保留旧路径。不能用异步watcher充当严格一致性证明。所有写方覆盖与外部变更检测测试通过才扩大资格。

## 5. 活动事件与稳定历史

完整事件保留现有anchor所有权，事件种类和必填字段以protocol-v2的版本化payload表为准。首版只接受完整对象替换，不接受缺少基线证明的delta，不含推理上下文替代物。完整工具ID由原始调用携带，缺失时不得按名称猜配。done只能触发整理，不作为完整正文来源。无法捕获完整变更的运行走旧快照恢复，界面明确降级。

GET在同一个派生库读事务固定epoch/generation/covered_seq及活动upper_seq，只返回covered_seq之后的事件；历史分页只取指定段及scene边界。长活动积压分页返回delta_cursor/has_more，不截掉事件冒充完整。客户端逐页追平后才标记恢复完成。

SSE现有run_id:seq保持兼容；新客户端协商projection_v1，附加session_seq及anchor_id，不能强改旧Last-Event-ID。GET返回本页实际已交付的resume_seq，同时独立返回快照upper_seq；has_more时不能把游标直接推进upper_seq。SSE从持久events补齐，通知只作唤醒，按protocol-v2处理无遗漏交接，以event_key幂等。归档不是删除事件，切换历史generation后重新以covered_seq裁掉已归档增量。过期epoch游标返回明确409/reload_required，不将旧偏移用于新版本。旧客户端继续旧GET/SSE，服务端不能悄悄替换返回语义。

## 6. worker与非追加操作

worker领取在短事务内增加fence并租约；构建不持有写锁。发布事务同时验证lease owner/fence、scope revision/epoch、源清单有效、候选VERIFIED及当前generation等于构建基线；不满足则SUPERSEDED，不能覆盖更新版本。租约到期即不能发布，时钟变动只影响回收时机而不能绕过fence。

追加证明要求旧前缀身份/内容/顺序不变且所有新事件完整；有证明才复用段，仅重建活动尾段。内容键合并导致前缀变更时全版本重建。编辑、重新生成、压缩、分支、恢复、截断均先围栏并增加epoch；父变更使依赖子版本失效。删除/权限缩紧由实时授权拒绝旧版本读取，不能因派生副本存在恢复已删除会话。

## 7. 拟修改入口与分期

M0：新增store schema与测试，不连接生产，功能默认off。
M1：models.Session.save、session_ops变更边界、streaming终态/异常/cancel、session_recovery恢复及routes导入/编辑/压缩：建立mutation与失效；state_sync仅补一致性通知，不让统计更新被当正文变更。gateway_watcher/agent_sessions外部来源先LEGACY，不承诺支持。
M2：worker生成稳定历史shadow；调用既有merge_session_messages_append_only与窗口/anchor语义，不重写去重算法。比较全部拟启用会话分页结果，再开稳定历史读。
M3：streaming原始完整事件捕获、run_journal兼容关联、static/messages.js和static/sessions.js协商游标；active支持默认off，缺完整事件回旧路径。
M4：routes GET路由非阻塞模型容量展示：只取profile/provider/model/endpoint/配置版本匹配的显式或缓存值；unknown/stale明确标记。后台单飞刷新/超时退避，真实推理容量校验不动。
M5：小范围受控验收、逐会话回填；无新测试部署，沿既有生产路线需另获发布授权。

每期测试RED→GREEN→邻接回归→审查→提交推送。不提前停止sidecar/Agent正文写，不删journal，不加入新框架。既有live-to-final、anchor、session-sse合同保持；本设计是新增能力协商而非现有RFC全量实现。

## 8. 资源与回滚门禁

建议初始上限而非实测：worker=1，任务积压1000条，单次100行写批；后台新增RSS软门256MiB、拟议硬门512MiB（同进程尚无已证明硬隔离，不能称已保证）；活动响应目标1MiB但不可切断单事件，超大事件用分页的受权对象读取且旧客户端旧路径。锁等待≤250ms后退避，不在GET执行构建/checkpoint。全量合并若超预算仅拒绝构建，不终止主服务。

实施前必须量化真实源总量、预计派生库+候选版本+WAL+备份的峰值磁盘，记录RSS及I/O。磁盘所需峰值未量化不得启动回填；低于预算安全余量即停止新构建，不删数据腾空间。WAL读事务短批，后台被动checkpoint不得阻塞前台。具体硬资源隔离需本地证明后启用，不能依赖定时采样保证严格硬上限。

回滚先关闭新读及active能力，再停worker，保留数据库与事件。旧journal/sidecar始终保持兼容；回滚镜像必须含既有压缩journal解码器。新事件只有在已同步进旧恢复路径时才允许降级旧版本，否则先保持新读关闭但新恢复组件运行，不能直接遗弃未同步事件。发布前必须证明这个兼容门禁。

## 9. R1修订：写入围栏、源稳定与发布资格

本节收紧§3–6，冲突时以本节为准；状态为修订待复审，不代表功能验证通过。

- mutations新增actual_revision、epoch、writer_token、sealed_at。actual_revision由begin_mutation在同一BEGIN IMMEDIATE事务中分配并持久化，禁止seal时猜测当前revision。mutation_id重试只返回原绑定，参数不同拒绝。
- 同scope最多一个PREPARED或UNCERTAIN mutation；通过部分唯一索引约束，而非仅进程锁。新写遇未闭合mutation必须等待或返回可重试错误，不允许重叠旧源写。UNCERTAIN需先恢复核验，不能按超时自动认定完成。
- 旧权威源写操作必须持有该writer_token。seal仅接受匹配scope、epoch、actual_revision和token的当前mutation；旧token、乱序seal与重复但不同源清单均拒绝。
- SEALED表示全部登记的权威写方已经完成且源清单核验通过，不只是WebUI sidecar保存成功。候选固定输入包括mutation_id、actual_revision、epoch、base_generation、covered_seq、source_manifest_sha。
- 发布事务要求目标mutation为SEALED、scope无PREPARED/UNCERTAIN、scope当前revision/epoch与候选一致、写方资格有效、源核验通过、任务fence和base_generation匹配。任一不满足不更新published_generation。
- 稳定历史新读同样要求上述源稳定资格，并在同一读快照校验版本revision/epoch。DIRTY不得把旧视图标为当前结果；退回旧路径或明确等待。活动新读另需完整事件协议，不能借SEALED规则自动启用。
- 发布与下一次begin_mutation由同库事务排序：先发布后开始新写则立即DIRTY；先开始新写则发布失败。已开始的读仅代表其固定快照，不声称包含随后发生的写入。

### 风险D01：串行围栏可能阻塞正常会话

触发：写方崩溃、seal丢失或UNCERTAIN长期未解决。影响：新写等待、无法启用加速。对策：有界等待、明确错误、持久恢复任务；只核验和恢复持久状态，不重放工具副作用。禁止为可用性跳过围栏。验证：T03/T05/T17及新增T21，在旧源写暂停期间并发构建/发布/新写，断言无新版本发布；注入两个token的乱序seal，断言仅当前合法token可提交。残余风险：未覆盖实际Agent写方时仍不能启用新读。回退：保持旧读；需要绕过新围栏恢复写入时必须先确认所有服务进程关闭新读能力，不能只改一个进程内开关。

## 10. 明确未验证

全读写方围栏尚未实现；稳定源快照跨库证明、完整工具事件捕获、前缀追加证明、资源预算均待TDD验证。性能p95目标与冷首屏未达成。本文没有执行schema、回填、生产写或功能测试。
