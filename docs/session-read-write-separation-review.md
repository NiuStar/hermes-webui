# 会话读写分离：独立评审记录

状态：BLOCKED。被评审版本：cd20b4fe。评审对象：session-read-write-separation-implementation.md及session-read-write-separation-test-plan.md；已确认架构方向不变。独立只读评审任务deleg_b2abf57c已完成，不等于设计通过。

## 初轮未关闭的P1问题（历史结论保留）

| ID | 问题 | 必须补齐的契约与验证 | 状态 |
|---|---|---|---|
| R1 | mutation围栏没有闭合到发布条件 | mutation持久绑定实际revision；同scope重叠写规则；目标SEALED且无PREPARED/UNCERTAIN才能发布/取得读取资格；旧写暂停与乱序seal屏障测试 | OPEN |
| R2 | 来源标签不能证明写方闭包 | WebUI会话也可能被Agent/CLI写入；列出全部实际写方、写前围栏和撤销资格方式；无法证明排他控制则保持旧读；覆盖已启用WebUI会话的外部写入 | OPEN |
| R3 | events/runs未绑定epoch | run/event持久绑定epoch和写token；commit校验当前权限；迟到旧回调隔离且不进入当前GET/SSE；测试epoch切换及旧重连 | OPEN |
| R4 | 完整事件协议及replay/live交接不充分 | 版本化payload必填字段、对象身份、delta基线、确定性reducer、终态完整性；订阅注册/水位/缓冲交接协议；交接窗口提交事件不遗漏 | OPEN |
| R5 | 回滚缺持久兼容确认凭证 | 逐run/事件明确旧恢复源及所需字段；源写成功并读回后推进兼容水位；确认前崩溃保守阻断，不能凭内存判断已同步 | OPEN |
| R6 | schema无法唯一落实重试与CAS | 持久候选输入epoch/revision/base_generation/covered_seq；同修订BLOCKED/SUPERSEDED重建状态表；关键DDL、跨scope FK及版本指针约束；真实SQLite多连接验证 | OPEN |

## 门禁结论

详细设计不能标PASS，不能冻结schema或进入生产接入、回填、新读启用。独立评审认为仅M0隔离临时库离线schema/TDD可作为契约验证范围，但这不是schema批准，也不构成用户编码授权。本轮继续文档修订并复审，不执行功能代码、测试部署或迁移。

修订顺序：R1/R2/R3写权限及有效期 → R6事务/schema → R4活动协议 → R5回滚凭证 → 测试矩阵逐项对应 → 独立复审。每项必须写出确定性规则和反例测试，不能以“实现时处理”关闭。20组既有测试仅为计划，未执行；此前54项诊断测试与本轮功能无关。

R1已有ca3fb45a文档修订；R2–R6及逐项风险/测试扩展本轮补充，均需独立复审，不因新增文字自动关闭。对应规范：session-read-write-separation-protocol-v2.md；风险：session-read-write-separation-risk-matrix.md；测试：session-read-write-separation-test-addendum.md。

本记录保留原始评审证据，尚未宣称任何问题已获独立关闭。原始数据库、sidecar、journal、备份和暂存未改变。


## 本轮修订交付与覆盖核验

R1：implementation§9，T21；R2：protocol-v2 R2，T22；R3：R3，T23；R4：R4，T24；R5：R5，T25；R6：R6，T26。以上状态均为DOC_REVISED_PENDING_REVIEW，而非已关闭。

新增风险矩阵D01–D26，每项含来源、触发/影响、措施、验证和残余/回退；架构全部11个编号章节及实施全部10个编号章节均有明确风险链接。测试主计划T01–T20与扩展T21–T42共同构成42组规格，全部NOT_RUN。

实际运行的仅文档检查：26风险行/42测试行编号唯一；风险引用的测试全部定义，测试扩展风险引用全部存在；无APPEND占位，git diff --check通过。检查不执行SQL，不证明事务、功能或生产性能。

以下历史状态由最新复审结论更新；初轮OPEN及上文DOC_REVISED_PENDING_REVIEW保留为历史证据，不是最新逐项状态。

## 最新联合复审 deleg_1dff8521：R1–R6文档契约可接受

结论：R1与R2–R6在指定范围内联合一致，未发现协议级阻断，可接受为待验证文档契约。最新文档状态为DOC_REVIEW_ACCEPTED；此前JOINT_REVIEW_PENDING已由本结论替代。实现/启用门禁仍BLOCKED，功能NOT_RUN，不代表实现可用、schema冻结、编码授权或生产放行。

读取完整性：implementation§9 L100–117及§10 L118–122完整读取；protocol-v2全文L1–105分段完整覆盖，无遗漏截断。独立只读评审确认：稳定发布SEALED且无PREPARED/UNCERTAIN与独立active能力不冲突；active固定同epoch不可变基线且仅追加自身对象；破坏性写先闭合/恢复旧mutation并撤权再串行增epoch；未归档事件连续覆盖；token检查至实际源写持有scope门，撤权排空在途写，发布维持R1围栏并补充NULL安全CAS、fence及同事务状态提交。

仍未通过的验证门槛（非新增协议矛盾）：实际写方闭包、权限隔离及源写围栏；完整事件、旧恢复映射及逐源回滚凭证；完整DDL/trigger和真实多连接CAS/崩溃验证。未受控scope默认LEGACY，不启用active_read。下一阶段须另获授权，不能由文档认可自动进入编码。

证据：/home/hermeswebui/.hermes/cache/delegation/live/deleg_1dff8521/task-0.log。子任务未修改文件、执行SQL或提交；本记录由主线程更新。

## 历史独立复审 deleg_01bdb892：R2–R6文档可接受

被审修订97887787；后续fa355797仅增加章节风险链接。独立结论：R2–R6文档修订可接受，未发现新的真正阻断；不构成功能或生产放行。最新状态：R2–R6为DOC_REVIEW_ACCEPTED，R1为JOINT_REVIEW_PENDING；整体仍BLOCKED。

评审确认：active固定同epoch的不可变基线、未归档事件连续覆盖；持久events为GET/SSE唯一投递来源且通知丢失可补查；实际写入门覆盖token检查至落盘；回滚逐源读回、连续兼容凭证及全进程排空；CAS包含NULL基线、attempt/fence、同事务发布及同修订重建。

明确限制：第二次读取被截断，未取得implementation§9 R1正文，故该次复审不完整背书R1联合一致性。不能把上述局部接受写成整体设计PASS。补充只读联合复审已派发deleg_1dff8521，要求完整提取R1及protocol-v2再下结论，结果待返回。

证据：/home/hermeswebui/.hermes/cache/delegation/live/deleg_01bdb892/task-0.log。复审全程只读，未修改文件、提交或执行SQL。风险26行和测试42组仅为文档覆盖口径，所有功能测试仍NOT_RUN。

完整DDL/FK/trigger留至M0是可接受的编码细化，但仍须另获授权并完成离线TDD验证；不得据此冻结schema、启动编码或启用生产。实际写方排他证明缺失时scope维持LEGACY，不能以文档覆盖率替代启用资格。
