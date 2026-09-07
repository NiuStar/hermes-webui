# 编码设计候选索引与验收状态

最终受审设计基线：80139190459b3de1e0b6a0913cecf877394439fc。状态：CODING_DESIGN_REVIEW_ACCEPTED（文档设计层）；初轮及复审提出的两项P1均已独立关闭。实现验证/生产启用门禁仍关闭，编码执行须另获授权。下方各轮待审或BLOCKED状态保留为历史记录，以本节最新结论为准。

## 最终独立终审结论

独立任务deleg_50e82718完整分批读取固定80139190的事务、schema文档及本次diff，认可恢复崩溃重入修订，关闭前轮剩余P1，未发现新的P1阻断。确认A=UNCERTAIN、B=PREPARED/recovery_only=1均有合法入口；重用持久恢复键、重新排空原业务及恢复执行者、持scope排他门；active保持0，普通回调不恢复权限；补偿和terminal幂等，SEALED只读返回；与状态转换、binding及恢复键不可变trigger不冲突。

结合deleg_5b1e5d04对四份初始候选的完整终审，以及deleg_d4d89faf对mapping-v1的认可，本轮编码设计及已发现P1修订的独立终审闭环完成。此为分轮审查闭环，不冒充最终提交全部文档重新全量审查或运行验证。

证据：deleg_50e82718/task-0.log（本会话delegation审计目录）。主线程确认80139190到记录提交71b94ebf仅索引变更，受审设计正文无变化。本次仅更新索引，不改受审契约。

验证边界：SQL、真实多连接、崩溃、功能及性能全部NOT_RUN；生产LEGACY。实际写方闭包/OS排他、旧恢复适配、资源预算仍须执行阶段验证，不因设计接受而自动满足。下一步仅在另获授权后进行M0隔离TDD，不接生产、不回填、不启用新读、不删除数据。

- [真实写方与接入](session-read-write-separation-coding-writers.md)：实际函数、调用者传播、权限和既有锁顺序；未证明生产闭包则LEGACY。
- [候选schema](session-read-write-separation-coding-schema.md)：完整表字段、基础/发布trigger正文、数据库与可信入口责任边界。
- [事务与崩溃恢复](session-read-write-separation-coding-transactions.md)：begin、源写、事件、terminal/seal、领取/CAS、回滚屏障。
- [完整事件与旧恢复映射](session-read-write-separation-coding-events.md)：五类完整fixture、规范hash、reducer、兼容扩展、连续凭证。

## 实施文件及隔离验证顺序（尚未授权执行）

| 顺序 | 拟新增/修改模块 | 交付验证 | 失败回退 |
|---|---|---|---|
| M0a | display_store / schema版本初始化与invariant_check | T01/T26/T28真实多连接、每个非法状态/跨scope/NULL基线反例 | 不接生产，保留测试审计 |
| M0b | display_mutations / 显式ScopeKey与Binding | T21–T23/T30；现有agent_lock/LOCK顺序屏障 | 不启用新资格 |
| M0c | display_events / display_compat | T24/T25/T31；完整事件和旧恢复三源读回 | 缺字段或源不匹配BLOCKED |
| M0d | display_worker / display_reader | T26/T32/T33；候选范围、分页无跳跃、幂等重放 | 只保候选和原事件 |
| 后续独立授权 | models/streaming/session_ops/session_recovery/state_sync及Agent底层适配 | 写方闭包、受控源身份及T22/T34/T41 | 未受控scope保持旧读 |
| 最后独立授权 | routes GET/SSE能力协商、客户端游标、异步模型元数据 | T14/T19/T20/T37/T40；逐scope影子对比及冷热端到端 | 关闭新能力；完整回滚须凭证 |

不添加React或新前端框架；保留现有Python/vanilla JS。模型元数据异步只改变展示等待，不改变推理容量判定。真实生产隔离服务/权限路线变化需要重新批准，不由本文自动引入。

## 资源与回退门

默认新能力关闭；RSS、磁盘、WAL和单对象上限必须通过M0隔离测量给出真实值，当前未获得数值依据的构建拒绝启动。预算输入明确包含现有库、候选、原源、WAL、临时排序和并发数量；无预算不回填。超预算停止新构建/背压，不杀生产、不删除数据库/备份/暂存。风险与测试详见四份候选及既有D01–D26/T01–T42。

## 独立终审

deleg_5b1e5d04已针对固定8bc1fd18候选派发只读独立终审；返回前不能标PASS。索引仅汇总，不改变被审候选契约。实际运行目前只有文档静态核验与fixture hash计算；SQL、并发、崩溃、功能及生产性能均NOT_RUN。

## 编码终审结果及修订记录

独立终审deleg_5b1e5d04完整读取8bc1fd18四份候选，结论不接受为完整可编码设计，存在两项P1：UNCERTAIN/OPEN缺合法终态恢复路径；旧恢复映射缺确定身份、结构、列适配及逐源hash覆盖。该结论为静态设计审查，不是SQL或生产测试失败。

1461f5ac修订：transactions§8/schema§6加入恢复专用持久标志、证据门和UNCERTAIN→PREPARED受控路径，保持普通回调撤权；events§6定义mapping-v1身份分配、扩展结构、Agent源码列清单和三源coverage/hash。两项当前均DOC_REVISED_PENDING_REVIEW，不是已关闭。

独立复审deleg_d4d89faf针对1461f5ac已派发。返回并处理结论前整体CODING_DESIGN_BLOCKED。全部SQL/功能NOT_RUN；未改代码、部署或生产数据。

## 第二轮复审与崩溃重入修订

独立复审deleg_d4d89faf固定1461f5ac：旧恢复映射P1在文档设计层可关闭；恢复P1仍OPEN，原因是恢复短事务提交后退出留下PREPARED/recovery_only=1，缺合法重入。

80139190459b3de1e0b6a0913cecf877394439fc补齐恢复入口A(UNCERTAIN)/B(PREPARED且recovery_only=1)，要求同持久恢复键、重新排空全部旧执行者、持实际源门、重新计算剩余幂等补偿，已SEALED只读返回；新增恢复键不可变trigger及提交后崩溃测试规格。恢复项仍DOC_REVISED_PENDING_REVIEW，独立终审deleg_50e82718针对该固定提交已派发，返回前整体CODING_DESIGN_BLOCKED。没有SQL或功能执行，生产LEGACY。
