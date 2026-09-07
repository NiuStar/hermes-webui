# 编码设计：事务与锁顺序

状态：CANDIDATE_PENDING_INDEPENDENT_REVIEW。依据2bcad176的R1–R6契约。本文件是编码设计的一部分，不是完整DDL、功能实现或编码许可。SQL与测试均NOT_RUN。独立终审须覆盖写方清单、完整DDL和事件映射后才开展。

## 1. 通用接口和锁顺序

拟新增api/display_mutations.py与api/display_store.py。所有接口显式接收ScopeKey(profile_identity,session_id)，禁止依赖线程默认profile。Binding为(scope_id,epoch,actual_revision,mutation_id,run_id,writer_token)，只在服务端流转；客户端输入不能构造权限。

统一锁顺序：受控源集合的scope门（按scope_id排序）→展示库BEGIN IMMEDIATE→旧源自身事务。禁止持展示库写事务等待scope门，禁止在展示库事务内进行网络请求或长旧源写。实际旧源写采用先短事务检查、后持scope门写源；两阶段之间门不释放。源门跨进程实现与OS排他资格是两件事：合作式锁只协调接入者；没有真实排他证明仍LEGACY。

接口返回结构化结果，不能用bool混淆：COMMITTED、IDEMPOTENT、REJECTED_BINDING、CONFLICT、UNCERTAIN、RETRYABLE、BLOCKED。COMMITTED仅证明该接口指定的持久事务完成，不证明所有旧源已兼容。

## 2. begin_mutation

输入expected_epoch、expected_revision、operation_key、kind(APPEND或REWRITE)、required_sources。先取得scope门，开启短写事务，核对资格及无PREPARED/UNCERTAIN；幂等operation_key必须绑定同一输入hash，冲突拒绝。分配revision=current+1，REWRITE同时递增epoch，APPEND保留epoch；插入PREPARED并更新scope revision。token由可信随机源生成，只保存服务端。

APPEND必须固定同epoch、来源有效的最近SEALED版本及base_covered_seq；没有可用base时不创建active能力，但允许旧路径写入，不把NULL当完整空历史。显式空会话须有经验证的空基线。先前SEALED未发布轮次的连续事件保留，不把其水位清零。

事务提交后才能允许首笔旧源写；提交失败不得写旧源。接口退出释放scope门，但长期PREPARED记录继续串行阻止其他mutation；后续每次写重新取得门并校验绑定。破坏性操作不得强行覆盖开放mutation，须先恢复核验并撤权。

## 3. write_source与commit_event

write_source(binding,source_kind,write_intent)取得scope门；短事务验证scope当前epoch/revision、run OPEN、mutation PREPARED、token有效、source已登记。退出短事务但保留门，执行旧源写并重新打开读取实际结果。源失败或结果未知时持久记UNCERTAIN，不能凭异常类型断言未写。无法记录UNCERTAIN时停止受控接入，不能继续发送成功确认。

commit_event(binding,event_key,envelope)取得同一scope门，在BEGIN IMMEDIATE验证绑定、schema和对象版本。重复键先查询原事件：同绑定同hash返回原seq，不分配新号、不恢复写权限；不同hash拒绝。新事件要求OPEN/PREPARED，在事务中分配seq、插入完整payload和对象版本索引。提交后方可对外确认该事件。此确认不等于旧源兼容完成；兼容缺口必须保留事件及新恢复组件。

对象版本在单run内从1连续增长，终态清单中的hash是规范完整对象hash；event payload_hash包含事件语义字段而排除服务端分配seq及传输时间。精确规范编码及字段映射另文定义，未定义完整前不得实现互通。

## 4. terminal与seal

terminal为唯一终态事件，先核对对象清单、版本和工具终态。提交终态与持久恢复任务/outbox必须同一展示库事务，run进入TERMINAL_PENDING；OPEN以外拒绝新对象更新，但允许原event_key只读幂等查询。

seal(binding)在scope门内重新核对全部必需旧源的实际写入及来源清单，核对当前mutation实际revision，确认无待执行源写。成功将mutation置SEALED、run置SEALED，并登记或激活归档job；若terminal已登记job，不能重复创建逻辑身份。凭证不完整时不得取得完整回滚资格；源稳定可证明而回滚映射不完整必须分开记录，不能将SEALED等同COMPATIBLE。

失败/cancel也有terminal，不重跑工具来补终态。恢复仅重读源/重放已持久事件及幂等补偿，不调用原外部副作用。

## 5. worker领取与发布

领取短事务验证目标SEALED、资格、预算证据，逻辑job以(scope,target_revision)唯一。新建attempt，单调增加fence；固定epoch、revision、mutation、base_generation、covered_seq、source_manifest_sha、owner、lease。租约只决定尝试有效性，不改变源写权限。

构建在写事务之外完成；候选、段及清单不可变，校验失败BLOCKED，临时I/O错误RETRY。同revision重建产生新attempt和generation，不覆盖旧候选。发布前取得scope门并复核实际源；保持门直到事务提交。

发布事务逐项验证：当前attempt/fence/owner及租约；目标mutation SEALED；无开放mutation；scope epoch/revision/eligibility；候选VERIFIED；段完整、源manifest一致、连续事件覆盖且不越过目标终态。最后执行NULL安全指针CAS：

```sql
UPDATE scopes SET published_generation=:candidate
WHERE scope_id=:scope AND epoch=:epoch AND revision=:revision
  AND published_generation IS :base AND eligibility='ELIGIBLE';
-- 必须changes()=1；否则整个事务ROLLBACK。
-- 同一事务更新generation状态和job状态，任何更新不命中均回滚。
```

上述语句不是独立充分发布门，完整DDL须用约束/trigger及仅可信入口权限共同落实前述条件。失败不得发布指针、丢事件或删除旧候选。

## 6. 回滚屏障与崩溃判定

| 窗口 | 恢复动作 | 禁止行为 |
|---|---|---|
| begin提交前退出 | 未提交则无mutation；operation_key查询后重试 | 先写旧源 |
| PREPARED后源写前退出 | 检查全部源身份和修订，证实未写才关闭/补偿 | 按超时自动SEALED |
| 旧源部分成功后退出 | UNCERTAIN，逐源读回，保留新恢复组件 | 重跑工具或猜完整 |
| event提交后未送达 | 原键返回原seq；GET/SSE补查 | 重新分配seq |
| terminal后seal前退出 | 恢复任务核对源与清单，幂等seal | 新对象追加 |
| candidate完成后发布前退出 | 重领新attempt，旧fence不得发布 | 复用失效lease |
| CAS后commit前退出 | SQLite原子回滚，读取原指针 | 根据内存宣称发布 |
| commit后响应前退出 | 读回job及generation返回原结果 | 二次发布新版本 |

完整回滚先停止新接入，登记全部进程启动ID与配置代次并排空源写；关闭新订阅，重新读取全部required_sources及指纹，在无开放mutation、无待源写、全部已确认事件获连续凭证后才卸载新恢复组件。无响应进程未证实退出则BLOCKED；不以单进程开关代替全进程屏障。

## 7. 风险、验证与回退

| 决策 | 风险及处理 | 验证/回退 |
|---|---|---|
| 固定锁顺序 | 多scope父链可能死锁，排序并禁止逆序嵌套，超时不强行夺锁 | T21/T22/T34屏障；失败旧读，不删除数据 |
| 长PREPARED短事务 | run崩溃阻塞活性，持久恢复任务及明确UNCERTAIN | T21/T31；不放宽发布条件 |
| 事件与旧源分阶段 | 已确认事件旧源未同步，保留持久恢复组件与连续凭证 | T25/T27；完整回滚BLOCKED |
| 固定base/连续覆盖 | 新轮覆盖上轮未归档事件遗漏，构建水位连续验证 | T32/T33；拒候选保持旧路径 |
| attempt/fence/CAS | 过期worker误发布，所有状态更新单事务并检查命中数 | T26；拒旧attempt |
| 源门到发布提交 | 持锁期间响应延迟；构建在锁外，仅最终复核在锁内 | T16/T26；超预算停构建不杀服务 |

尚未解决的完整交付门：全量DDL和trigger、字段级事件/恢复映射、实际OS排他及全部写方接入证据。本文不关闭这些门，不能单独提交为“编码设计终审通过”。
