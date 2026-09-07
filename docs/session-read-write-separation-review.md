# 会话读写分离：独立评审记录

状态：BLOCKED。被评审版本：cd20b4fe。评审对象：session-read-write-separation-implementation.md及session-read-write-separation-test-plan.md；已确认架构方向不变。独立只读评审任务deleg_b2abf57c已完成，不等于设计通过。

## 未关闭的P1问题

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

本记录仅保存评审结果，尚未宣称任何问题已经修复。原始数据库、sidecar、journal、备份和暂存未改变。
