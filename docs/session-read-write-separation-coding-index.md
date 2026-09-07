# 编码设计候选索引与验收状态

候选基线：8bc1fd18e47bfda4514893ca69f252f92f4f1996。状态CANDIDATE_PENDING_INDEPENDENT_REVIEW；并非编码/生产放行。

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
