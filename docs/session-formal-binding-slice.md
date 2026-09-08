# 正式 binding 必要拒绝门（未接线）

契约依据：coding-index 最终批准 80139190459b3de1e0b6a0913cecf877394439fc（非下方历史 candidate 状态）；transactions §§1/3/8 的显式 ScopeKey、完整 Binding、普通恢复撤权；coding-schema §§1/3/6；writers §2 的源门与 OS 排他边界。用户另行授权本次隔离编码，不授权启用。

本轮只新增 `api/display_mutations.py` 的普通新事件 binding 前置拒绝门及专用测试。显式连接由调用者持有，不发现 profile、路径或数据库；ScopeKey 是已授权服务端解析结果，本模块不充当身份认证器。无 runtime imports/routes/GET 改动。

状态空间：0/多个 scope、同 session 不同 profile、绑定六字段逐项错配、空/错误类型、安全整数、OPEN/TERMINAL_PENDING/SEALED、PREPARED/UNCERTAIN、recovery_only、LEGACY/ELIGIBLE、连接事务/配置失效。精确匹配也只能 BLOCKED；不存在 COMMITTED 或授权成功分支。只读取持久关系，不从 bool、journal done、shadow observation 或调用者声明授予资格。

边界：这是必要拒绝门，不是实际写入口、OS 源门、业务 invariant_check 或终态发现。完整 invariant_check（对象/终态/outbox/发布/receipt）、恢复专用入口、三源实际身份读回、事务内调用及锁顺序待后续垂直切片；不能凭本门结果写源/事件。恢复键即使相等也不能借普通入口恢复写权限。无幂等原事件读取接口。检查入口目前只支持 idle/default-row-factory 的 FK/FULL/WAL 连接；活动事务返回 BLOCKED 且不代调用者 rollback/commit。后续真正写事务不能直接复用此 preflight 为授权，必须在持实际源门的事务内重新实现/验证必要条件。SEALED 完整终态夹具及全业务 invariant_check 本轮未实现/未测，不把 OPEN→TERMINAL_PENDING 的拒绝测试当作完整 seal 验证。

## 当前固定版本验收（覆盖下方历史计数，不覆盖历史日志）

本轮测试全部在10.126.126.10的隔离目录/opt/hermes-bootstrap-tests/implementation-01前台执行。专用测试45项，已包含18项非法UTF-8标识符；迁移后的夹具从批准coding-schema文档SQL建立合成库，不调用已退役initialize，不授予生产资格。neighbors-05.xml组合126通过，其中该文件45项，未跳过；后续新增bootstrap测试不改变binding源码。

当前SHA256：api/display_mutations.py=f5d162027f19707e5506a9aa982eac9de29921e6909506362469af8a03333a0c；tests/test_display_mutations.py=d1cc55fda3c29e93fdbd187bb0c68c37ac91d0cff8101c6c06688d2c28adf6fb。

独立前台静态终审会话20260908_120021_6c953c，退出0，passed=true，security_concerns=[]，logic_errors=[]。仅建议更新旧文档的计数及SHA，已在本节补齐。原始JSON：/workspace/history-failure-evidence/implementation-on-10/binding-review-01.json。范围仅必要拒绝门，不能外推bootstrap生命周期、业务写入、激活或生产验收。审查后代码SHA需保持一致。

## 实测证据（以下均为历史）

证据根 `/workspace/history-failure-evidence/formal-binding-slice`，逐轮 command.json/exit.json/junit.xml/pytest.log；`verified.json` 由 XML 实际解析并计算 SHA-256。原样 scripts/test.sh 运行于私有源码副本，既有依赖只读，无依赖安装。Landlock wrapper 原样复用；实测共享 AGENTS.md 写打开与 `/dev/shm` 创建均 DENIED，证据根写允许。没有放宽共享内存权限。

| 轮次 | 实际结果 | exit |
|---|---|---|
| RED 1 | 1 failed：门缺失 | 1 |
| GREEN 1 | 1 passed | 0 |
| RED 2 | 17 failed / 5 passed：六字段/恢复撤权未核对 | 1 |
| GREEN 2 | 22 passed | 0 |
| RED 3 | 5 failed / 22 passed：连接失效未 fail-closed | 1 |
| GREEN final（专用+初始化+display_history+history_capture） | 54 passed，0 skipped/errors | 0 |

专用测试27项；真实文件 SQLite、双连接提交撤权后重读；所有 fixture 只证明数据层拒绝条件，不证明外部源资格。Ruff 与 git diff --check exit 0。全套、进程崩溃、实际锁序/OS生产闭包 NOT_RUN。

代码 SHA-256：
- api/display_mutations.py：`b97b780d347587c2cc4b164b308905cad0db52056b7fb769c6a974f9077afa8b`
- tests/test_display_mutations.py：`e9568eebcef51f323efe1c49b80a356f92c64cfbe211abbbd7f305af6e849d33`

初始化模块、DDL、初始化 tests 的 SHA 与前切片记录完全相同；没有修改它们。基线 HEAD 仍 cd71c15b7f8fc459f19658f8220db43a341f18ee，无提交、无部署。

风险/对策/验证/回退：跨 profile 或旧 token 被误认→完整 JOIN 与严格类型→真实临时 SQLite 反例；检查后源变化→没有授予分支且不得缓存 BLOCKED 为许可→双连接撤权反例；数据层可信误推外部资格→匹配仍 BLOCKED→数据库 ELIGIBLE 仍拒绝；异常连接→BLOCKED 不修复不提交→PRAGMA/事务测试。回退保持无接线与 LEGACY，保留所有数据和证据，不删除、不部署、不提交。
