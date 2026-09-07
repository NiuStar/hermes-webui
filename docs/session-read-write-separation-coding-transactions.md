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

write_source(binding,source_kind,write_intent)取得scope门；短事务验证scope当前epoch/revision、run OPEN或TERMINAL_PENDING、mutation PREPARED、token有效、source已登记；TERMINAL_PENDING仅允许将既有已提交对象/终态幂等刷入旧源，禁止产生新内容。退出短事务但保留门，执行旧源写并重新打开读取实际结果。源失败或结果未知时持久记UNCERTAIN，不能凭异常类型断言未写。无法记录UNCERTAIN时停止受控接入，不能继续发送成功确认。

commit_event(binding,event_key,envelope)取得同一scope门，在BEGIN IMMEDIATE验证绑定、schema和对象版本。重复键先查询原事件：同绑定同hash返回原seq，不分配新号、不恢复写权限；不同hash拒绝。新事件要求OPEN/PREPARED，先读取last_seq+1作为候选seq，插入完整payload（events_advance触发器推进last_seq），再更新对象版本索引；不得在插入前手工推进seq。提交后方可对外确认该事件。此确认不等于旧源兼容完成；兼容缺口必须保留事件及新恢复组件。

对象版本在单run内从1连续增长，终态清单中的hash是规范完整对象hash；event payload_hash仅覆盖规范payload；幂等检查另比较完整语义信封（排除服务端seq、传输时间及token）。精确规范编码及字段映射另文定义，未定义完整前不得实现互通。

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

## 8. 终审P1修订：UNCERTAIN恢复专用入口

recover_uncertain(binding,recovery_key)是唯一允许UNCERTAIN→PREPARED及恢复专用PREPARED重入的入口；普通回调、归档worker和超时器不得调用。启动恢复协调器可调用此入口，但不能调用普通写接口绕过恢复模式。入口接受两种持久状态：(A) UNCERTAIN；(B) PREPARED且recovery_only=1。PREPARED且recovery_only=0不属于恢复重入，拒绝。先关闭该run active、阻止新回调并取得scope门，证明原执行者已排空/停止，逐源核对身份、当前revision、已确认事件与实际副作用记录。无法判定外部工具是否执行时，A保持UNCERTAIN；B在持门短事务中PREPARED→UNCERTAIN并保留恢复标志。若该事务失败则停止接入，原持久状态仍只能经本入口重入，不执行工具重试。

核验只能恢复存储写入，不恢复推理/工具执行。入口建立恢复上下文(recovery_key,source_manifest_sha,原binding,待补偿对象清单)，A路径在短事务校验仍为同一UNCERTAIN、epoch/revision/token未变后转PREPARED并记录恢复审计。B路径核对持久recovery_key与请求相同、recovery_only=1、原binding及证据结构完整，保持PREPARED不制造状态跃迁；复核后更新恢复证据并提交。已有recovery_key必须复用，冲突拒绝，不因重启分配新键。run保持OPEN或TERMINAL_PENDING，active_allowed保持0。持scope门跨越后续补偿与终态/seal全过程，其他普通回调被入口级recovery_only标志拒绝；该标志必须持久化到mutations，不能仅内存维护。

OPEN时只允许从已持久事件和逐源读回证据生成缺失对象终态（未知执行结果只能明确unknown并INCOMPLETE，不能伪成功）；提交唯一terminal，run转TERMINAL_PENDING。已有terminal时原键读回，禁止第二terminal。幂等刷入旧源后seal；INCOMPLETE可封存稳定源但不可发布/active/完整回滚，job置BLOCKED。再次I/O未知则回到UNCERTAIN，保留recovery_only；重启只能继续本恢复入口。

原binding不变是为维持events复合FK；排空原执行者及持久recovery_only共同撤销其实际使用权限。write_source/commit_event在recovery_only=1时必须匹配recovery_key且调用模式RECOVERY_STORAGE，仅允许恢复清单内对象；普通token即使相等也拒绝。T21/T23/T25新增OPEN+UNCERTAIN、已有terminal+UNCERTAIN、恢复中再次退出、旧回调穿插反例。失败回退保持UNCERTAIN/LEGACY，不绕trigger、不重跑工具。

### 恢复专用PREPARED的崩溃重入门

启动扫描同时枚举UNCERTAIN及PREPARED/recovery_only=1；扫描仅发现任务，不授予权限。每次重入均重新取得scope排他门，并证明原业务执行者和上次恢复执行者已停止/排空；不能根据租约到期直接夺取源写权限。无法证明则BLOCKED并保持active=0。全程持门，确保只有一个恢复执行者。

从持久事件、终态和实际旧源重新计算剩余补偿，不信任崩溃前内存进度。已有事件使用原event_key幂等读回，已有terminal不再提交；未完成源刷写按已保存对象身份及版本幂等补偿，再按OPEN/TERMINAL_PENDING状态推进。恢复提交后、任意源写后、terminal后或seal响应前退出均可重新进入：前两种重读补偿，terminal后仅刷源与seal；若已SEALED则核对绑定及终态后只读返回原结果，绝不重获写权限。

T21/T25恢复崩溃子例必须在UNCERTAIN→PREPARED提交后且首次源补偿前强制退出，确认重启读到PREPARED/recovery_only=1，能由同恢复键合法重入；再分别于源补偿、terminal提交、seal提交后退出验证幂等。并行旧回调、不同恢复键、新恢复进程未排空旧执行者均拒绝。风险是双恢复者重写旧源；措施是实际排他门和重复排空证明，失败保留状态/审计而非强制解锁。
