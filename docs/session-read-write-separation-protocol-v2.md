# 会话读写分离：R2–R6 契约修订 v2

状态：**BLOCKED，待独立复审，未编码、未执行DDL、未迁移、未回填、未启用新读。** 本文补充 implementation.md 的 R1 和 review.md 的 R2–R6；冲突处以本文收紧条件为准，不改变旧协议。文档完成不等于问题关闭，T22–T26 均为待执行测试。

## R2：实际写方闭包与资格撤销（T22）

风险：`source=webui`、启动参数、合作式文件锁、watcher 或无近期变更，均不证明 CLI/Agent 无法写入同一权威存储。

必须按 profile 的实际路径/文件身份登记：WebUI Session.save；streaming 驱动的 Agent 实际 state.db 正文/工具/终态持久化；session_ops 的编辑、再生成、压缩、截断、分支、删除；导入、恢复、备份还原；gateway/CLI/定时任务/独立 Agent；父链源写方。统计同步只有在证明不改变展示字段时才可排除。登记记录含入口、实际落盘模块、源集合、进程身份、适配版本、围栏接入和排他控制凭证；不能以 WebUI 外层调用已围栏推定内部 Agent 保存已接入。

所有会改变展示的写入必须在首次权威写之前取得 R1 持久 mutation/token；内部 Agent 每次实际落盘也验证该 token，全部登记写方完成后才能 SEALED。无法接入的独立 CLI 不得仅靠自愿遵守协议视为受控。新读资格必须有可验证 OS 权限隔离/专用写入服务与接入控制证明，覆盖新进程、已有文件句柄、父链及备份恢复；同 UID 任意程序仍能绕过时不成立，保持 LEGACY。

撤销顺序：先在派生库事务持久化 eligibility=LEGACY、资格版本递增、dirty_reason，并撤销 run 权限；提交成功且全进程读门禁可见后，才允许新增写方/解除隔离/写旧源。不得先写再由 watcher 降级。库不可写则拒绝受控写或执行 R5 全进程屏障；外部已绕过代表资格证明失效，立即 fail closed，不能宣称此前快照仍严格一致。

T22：在已启用 WebUI 会话上分别由内部 Agent 和外部 CLI 写入；暂停写前事务验证无源写；拒绝无 token 落盘；尝试旧句柄/新进程绕过控制；撤资格提交失败不得继续写；父源改变使子资格失效。残余：实际入口和 OS 控制尚未验证，因此默认 LEGACY。回退：旧读，保留所有源；不得以 watch 通过代替排他证明。

## R3：run/event 有效期与 mutation 串行（T23）

风险：旧回调在编辑或重新生成后污染新 epoch，或异步事件绕过串行 mutation。

run 创建事务持久绑定 `(scope_id, epoch, run_id, actual_revision, writer_token, mutation_id)`；token 是服务端不透明权限，不交给客户端。commit_event 必须提供同一绑定和 event_key，在 BEGIN IMMEDIATE 内校验当前 scope epoch/revision、资格、mutation PREPARED、当前 token、run OPEN；校验成功后才分配 session_seq 并插入事件。合法重复键只返回原 seq；不同 payload hash 冲突。seal 后禁止新增事件，已存在合法重试可读取原提交结果但不重获写权限。

同 scope 的 PREPARED/UNCERTAIN mutation 仍只允许一个；一次长 run 可以在其 PREPARED 期间多次提交事件，不意味着每个回调启动新 mutation。稳定历史当前结果仅在 SEALED 且无开放 mutation 时有资格；active 展示是明确的活动快照能力，可在该唯一 PREPARED run 中读取已提交完整事件，不能把 DIRTY 历史冒充当前稳定历史。run 的取消/终态先在该 mutation 内提交；破坏性操作必须等其闭合或将其恢复核验后撤权，随后用新的串行 mutation 增 epoch，不能重叠旧源写。

旧 epoch/token/revision 回调拒绝且**不分配 seq**；仅写独立隔离审计（原绑定、原因、payload hash、接收时间；不进入 events、不更新 run/兼容水位），不得落盘旧源。旧 GET/SSE cursor 返回 409/reload_required。审计保留受权限及隐私策略约束，不作为恢复源。

T23：在回调验证前/后插入 epoch 切换与撤权，验证事务排序、无旧事件/旧源写、seq 不消耗；乱序 seal、取消后迟到工具结果、旧重连、相同键不同正文均拒绝。残余：Agent 回调与真实保存间的权限检查未实现。回退：关闭 active 能力，旧恢复路径仅在 R5 凭证通过后启用。

## R4：完整事件与确定性重放（T24）

首版只支持完整对象替换，不支持text delta。公共必填：schema_version=1、scope_id、epoch、run_id、event_key、session_seq（服务端分配）、anchor_id、kind、object_id、object_version、payload_hash。客户端不能选择scope或序号。hash按固定规范JSON编码计算；未知版本拒绝，不猜字段。

| kind | payload必填 | reducer规则 |
|---|---|---|
| message_upsert | role、content（完整多模态结构）、timestamp、tool_calls完整列表、display_order | 身份为epoch/run/object_id；同version同hash幂等，不同hash冲突；新版本必须恰为上一版本+1 |
| activity_upsert | owner_anchor、activity_kind、state、完整展示内容、display_order | 仅替换该anchor所属对象；不能依据相同文字合并 |
| tool_result | tool_call_id、name、完整arguments、完整result、status、owner_anchor | ID必须已由调用登记且同run；缺ID或预览内容不得标完整 |
| todo_replace | todos完整列表、owner_anchor | 空列表是有效替换；不扫描旧历史找非空值 |
| terminal | outcome、last_content_seq、object_manifest（ID/version/hash集合）、completeness | 所有对象与清单匹配、无缺版本、工具均有明确终态才COMPLETE；失败/取消不要求成功答案，但须完整记录已有有效部分 |

对象内容沿用既有受支持显示schema，未知多模态字段保留并隔离，不静默丢字段。重复事件以event_key验证；事件严格按session_seq归约，稳定排序按已持久display_order及object_id，不按到达时间。终态后禁止新对象更新。message对象不得改动已被base_generation覆盖的历史身份；触及旧对象则撤销active资格并通过非追加mutation重建。

active基线：run创建时固定最近SEALED的base_generation/base_revision/base_covered_seq，必须同epoch且来源有效；历史部分只读该不可变版本，不重新读取正在变化的旧源。唯一PREPARED run仅可追加自己的对象。下一轮使上一轮未发布任务过期时，后续构建必须覆盖尚未归档的连续有效事件，不能丢弃旧水位范围。

GET固定(epoch,generation,covered_seq,upper_seq)，分页resume_seq仅等于实际交付的最后事件，空页保持旧游标，has_more=true时不得跳upper。所有投递都从持久events查询，不直接以进程队列正文作为第二来源。SSE先注册唤醒器，再查持久水位并分页重放；追平后再次查库，之后通知或有界超时都重新查询last_delivered之后的数据。通知只作提示，丢失/重复通知不会丢事件；跨进程无共享通知也由定时补查恢复。网络半帧失败不推进客户端确认，重连幂等重放。每批检查epoch/权限，变化立即reload_required。没有replay→live更换数据源的窗口。

风险：完整对象替换放大写入、定时补查增加负载；对策是限速合并尚未确认的更新、索引分页、背压，已确认事件不得丢弃。T24覆盖注册前后/追平后提交、全部通知丢失、半帧、分页未完、未知schema、对象缺版本及终态清单不符。残余：完整工具及多模态适配未实现；回退旧协议但不得声称独立恢复完整。

## R5：持久兼容凭证与回滚（T25）

新增compat_receipts(scope_id,epoch,run_id,through_seq,source_kind,source_identity,source_revision,canonical_hash,verifier_version,verified_at)，唯一键覆盖scope/epoch/run/through_seq/source_kind。每个run有required_sources集合，不以一份文件存在代替全源完成。

| 恢复源 | 必须读回验证的内容 |
|---|---|
| WebUI sidecar | 完整用户/助手正文、多模态引用、角色顺序、工具ID/参数/结果、anchor活动、Todo、终态、截断与父链字段 |
| Agent state.db | 该运行应进入模型上下文的正文/工具关联、上下文顺序、会话身份与有效状态；不可把展示投影当模型上下文 |
| 兼容journal | 旧解码器实际支持的事件、游标、终态及所依赖的sidecar；done元数据不能单独证明正文恢复 |

不是要求三份字节相同：使用版本化字段映射逐字段比对新事件规范状态与旧恢复路径重建结果；字段不受旧版本支持则该运行不得取得完整回滚资格。凭证只在旧源保存成功、重新打开真实恢复源验证后提交；写成功但确认前崩溃重新核验，不补猜凭证。推进兼容水位必须无空洞且全部required_sources通过；终态归档不能跨越缺口。

源后续修订、还原备份、epoch变化或verifier变化使相关凭证失效；回滚时在写入排空屏障内重新读回全部目标及指纹，不能仅信旧verified_at。全进程屏障记录配置代次与参与进程启动ID：停止新接入、排空真实旧源写、关闭新读/订阅并获得全部存活进程确认；无响应进程未被证明停止前禁止解除保护。派生库不可用则拒绝回滚/受控写，不用单进程开关替代共识。

仅当全部已确认事件被连续凭证覆盖、无PREPARED/UNCERTAIN、无待旧源写，才可停止新恢复组件并切换兼容镜像。否则保留新恢复组件和数据，标BLOCKED，不删除日志、不重跑工具。风险：凭证延迟使回滚受阻；T25覆盖源写成功确认前崩溃、反向失败、源恢复旧备份、兼容水位空洞及进程不确认。残余：旧恢复映射需真实恢复验证，未验证前不得启用active_read。

## R6：持久输入、约束和任务重试（T26）

以下是关键约束DDL说明，不是完整可执行迁移；不得据此称schema已冻结或已测试。现有概念表补字段：runs绑定epoch/actual_revision/mutation_id/writer_token/base_generation/base_revision/base_covered_seq；events绑定同一run元组；jobs逻辑身份不变，新增attempts子表保存每次尝试的输入；generations新增job_id/attempt/fence/base_generation/target_mutation_id/source_manifest_sha。

```sql
-- 在已定义对应列的表上建立；本段不执行。
CREATE UNIQUE INDEX mutation_open_scope ON mutations(scope_id)
 WHERE state IN ('PREPARED','UNCERTAIN');
CREATE UNIQUE INDEX mutation_revision ON mutations(scope_id,actual_revision);
CREATE UNIQUE INDEX run_binding ON runs(scope_id,epoch,run_id,actual_revision,writer_token);
CREATE UNIQUE INDEX job_identity ON jobs(scope_id,target_revision);
CREATE UNIQUE INDEX attempt_identity ON job_attempts(job_id,attempt);
-- events复合FK → runs(scope_id,epoch,run_id,actual_revision,writer_token)
-- mutations.scope_id/runs.scope_id/jobs.scope_id → scopes(scope_id)
-- rows(scope_id,segment_id) → segments(scope_id,segment_id)
-- generation_segments分别以(scope_id,generation)、(scope_id,segment_id)引用同scope父表
-- scene_bounds(scope_id,generation) → generations(scope_id,generation)
-- scopes(scope_id,published_generation) → generations(scope_id,generation)，可空，延迟到事务提交检查
-- generations(scope_id,target_mutation_id) → mutations(scope_id,mutation_id)
-- 所有引用NO ACTION，无级联删除；整数CHECK非负，状态CHECK枚举。
```

job_attempts每条固定(scope_id,epoch,target_revision,target_mutation_id,base_generation,covered_seq,source_manifest_sha,attempt,fence,owner,state)。candidate构建后不能修改这些输入；重建必须新增attempt和generation。跨scope job与generation须复合唯一(job_id,scope_id)及对应FK。对published_generation写入仅由发布事务入口执行；FK证明存在但不能证明VERIFIED，事务还须校验候选/段状态及连续覆盖。发布后rows/segments/版本清单内容禁止UPDATE；写入口校验加数据库拒写trigger，修改新建版本，历史状态变更不得改内容。

| 原状态 | 允许转换 | 条件 |
|---|---|---|
| PENDING/RETRY | LEASED | 到期、源已SEALED、领取事务递增attempt/fence并固化输入 |
| LEASED | RETRY | 暂时I/O失败或租约到期；旧attempt终止，不复用其输入 |
| LEASED | BLOCKED | 证据/预算/完整性不足；记录原因，不无限重试 |
| LEASED | SUPERSEDED | epoch/revision/base改变；不删除事件 |
| BLOCKED | PENDING | 显式修复证据、审计授权；同逻辑job新attempt，fence单调 |
| SUPERSEDED | PENDING | 仅scope仍为同target revision且重新核验；否则新revision新job |
| LEASED | PUBLISHED | 同一事务完成全部CAS；重试已发布job只返回原结果 |

CAS事务BEGIN IMMEDIATE：验证job当前attempt/fence/owner及租约、目标mutation SEALED、无开放mutation、当前scope资格/epoch/revision、候选VERIFIED、完整源清单和所有段；使用`published_generation IS :base_generation`作NULL安全比较。条件UPDATE影响行数必须为1，否则回滚；发布指针、generation状态和job状态同事务提交。构建读取不持有该写事务。外部源核验必须在R2实际写入排他门内保持到发布完成，不能核验后允许旧源被改写。

风险：约束遗漏导致跨scope关联、同修订重试死路或过期发布；T26用真实SQLite多连接（未来离线TDD）验证FK拒绝、NULL基线、重复领取、过期fence、BLOCKED重建及事务各提交点崩溃。残余：完整迁移DDL/trigger尚待M0经授权验证，本轮只确定约束；回退保持新读关闭。

## 写入检查与实际源写之间的门

R2/R3的token检查必须由控制真实落盘的入口执行，并持有scope写入门直到源写结束；撤权/epoch切换先阻止新写、等待在途写排空，再改变代次。不能只在SQLite中检查后释放保护再执行旧源写。合作锁只协调已受控写方，不能排除外部写方；没有实际排他证明的scope始终LEGACY。此限制可能使现有部署暂无可启用scope，是明确门禁，不是部署成功。
