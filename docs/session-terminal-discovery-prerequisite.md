# 终态发现前置切片：正式 schema 初始化

## Contract Routing / 授权依赖

本轮用户授权隔离编码，不授权部署、GET 切换、真实数据迁移或生产资格。
依据 coding-index M0a 与 coding-schema 全部 SQL、transactions §§3–4/8、events mapping-v1、writers §§2–3。
`history_jobs.py` 是独立 shadow queue，保持原名与语义，不升级冒充正式协议。

核对结果：正式可信终态是完整 `events.kind=terminal`、精确对象清单及持久恢复任务同事务提交；恢复任务不是兼容 receipt，也不是归档 generation。旧 journal summary/prune/done metadata 不足以证明正文完整。正常终态规范值是 `completed`，不能把 shadow 的 `normal` 直接当正式事件。

依赖顺序：正式 schema 初始化 → scope/binding 与源门 → 完整事件/对象归约与三源 mapping-v1 读回 → terminal/outbox 原子事务 → 只发现不授予权限的恢复扫描。现有生产模块尚无这些正式入口；直接从 journal 入 shadow queue 会绕过契约，因此不做。

本轮有限可交付选择用户已明确允许的必要初始化层：专用无运行时接线模块，对显式传入的 SQLite 连接安装原样获批 DDL；拒绝混入已有其他 schema，强制 FK/FULL/WAL，事务提交前核对 schema 与空库一致性。它不提供写事件、凭证、seal、构建、发布或授予 ELIGIBLE 的接口。后续终态/漏单全用例仍为未完成，不能以初始化测试替代。

## 风险 / 对策 / 验证 / 回退

| 风险 | 对策 | 验证 | 回退 |
|---|---|---|---|
| shadow 被冒充正式协议 | 新专用模块，完整获批 DDL 不改 shadow | schema 对照、表/trigger 数核对 | 无运行时 import，保持 LEGACY |
| 初始化覆盖历史/未知库 | 非空未知 schema 拒绝，不 DROP/DELETE | 实库拒绝与内容读回 | 保留所有库及证据 |
| 部分 DDL 提交 | 显式单事务，异常 rollback | SQLite 故障/事务测试 | 不提供半初始化资格 |
| 连接 FK 或同步弱化 | 每次入口强制并读回；活动事务拒绝 | PRAGMA 与事务反例 | 拒绝初始化 |
| schema 同名篡改被当幂等成功 | 完整 sqlite_schema 对照，不只查 user_version | 缺 trigger/异物反例 | 拒绝，不自动修复 |
| 空库检查被误报业务完成 | 明确只初始化前置层 | RED/GREEN 单独列证据 | 终态接线及新读门继续关闭 |

## 尚未完成

没有 runtime terminal receipt、完整对象生产者、三源恢复适配、终态/outbox 提交、漏单扫描、重启补偿、generation builder 或真实源写门。正常/取消/失败业务终态、漏单、跨 scope 恢复、无可信终态拒绝尚不能通过本模块执行；这些需要下一垂直切片而非伪造已获信任的业务 fixture。初始化不是正式协议完整实现。

## P2 复核修订：一项修复，一项阻断（本轮）

证据根：`/workspace/history-failure-evidence/schema-p2-resolution/`。本节覆盖下方旧轮次结论，不改变批准 coding-schema/index。

- **同连接重入：已修复待独立审查。** SQLite 的完整性检查会使 `database_list` 出现内建 `temp`；不能以库数量不是1拒绝。现在要求持久 `main`，只容许其外的 SQLite 内建 `temp`，仍拒绝文件/内存 ATTACH。真实 RED 为1失败9通过，GREEN 为10通过。
- **未知 schema 竞争后的 WAL 副作用：P2 OPEN / 接线阻断。** 新双真实连接测试在首次目录读后、FK PRAGMA 前由第二连接创建未知表并提交。入口拒绝、数据与目录保留、事务释放，但 `journal_mode` 从 delete 持久变 wal；测试保持真实失败，不加 xfail/skip，不宣称已关闭。
- SQLite 3.53.0 实测：对已有 sentinel 表的库，`BEGIN IMMEDIATE` 和 `BEGIN EXCLUSIVE` 内切换 WAL 均报 `cannot change into wal mode from within a transaction`。将目录读简单移入事务不能跨越 WAL 切换；先检查后释放事务仍有竞争窗口。末尾盲目恢复 journal/FK/synchronous 不是安全修复，可能覆盖竞争者设置，也无法覆盖进程崩溃。
- **待批准的限定方案（未实施、不是自动修改协议）：** 初始化阶段必须由调用方证明专用数据库的排他所有权，排空全部其他连接/进程及路径替换者，并持有覆盖首次目录读、WAL 设置、DDL 提交及失败处理的真实访问门。普通 advisory lock 只保护合作调用者，不能证明任意 SQL 写方排除；不能把 `locking_mode=EXCLUSIVE` 设置本身当作已取得该门。并发初始化需在该门外串行排队。还须明确接受：在排他前提下，DDL 故障/崩溃仍可留下 WAL 模式，但 schema 对象事务回滚；若坚持“所有失败完全无持久副作用”，现有显式连接初始化 API 的方案仍不足，需另审离线预置/发布生命周期。不得凭文字条件直接接线生产。

| 风险 | 对策与回退 | 实证 |
|---|---|---|
| temp 放行误纳附加库 | 只容许 main+内建temp；未知附加库失败不改设置 | 两种 ATTACH 反例 |
| WAL 竞争污染未知库 | 保留失败、不恢复竞争者设置；保持未接线 LEGACY | red-race 与 combined 中同一真实断言失败 |
| 原 DDL/约束/事务被弱化 | DDL不变、精确目录对照、authorizer中途失败回滚 | schema 原测试与组合回归 |
| 并发初始化退化 | 四个线程各自真实连接，首次及同连接再次初始化 | boundary-results.json 全部 FK=1/FULL=2/WAL、无活动事务 |
| guard 限制被绕过 | 复用原样脚本/wrapper，私有源码，借用依赖只读；不开放 /dev/shm | 组合中两个既知 SemLock PermissionError 保留 |

组合实跑 schema/binding/display_history/history_capture/history_jobs：87通过、3失败（上述未关闭P2及两个 /dev/shm 隔离失败），exit 1。不是全绿或完整套件通过。每轮 command/exit/XML/log，边界结果与 SHA 在该证据根及 `verified.json`。当前回退为保留证据、不提交/部署、不接 runtime；父独立审查后才能统一决定后续。完整生产权限闭包仍未证明。

## 实测证据（前轮历史）

私有证据根：`/workspace/history-failure-evidence/terminal-schema-slice/`；逐轮 `command.json`、`exit.json`、`junit.xml`、`pytest.log` 及 `events.jsonl` 保留。`verified.json` 是实际解析 XML/exit 得到的汇总及代码 SHA。

使用原样 `scripts/test.sh` 的私有源码副本与既有 `audit_guard.py` Landlock wrapper：仅证据根可写，借用依赖只读，没有安装共享依赖；HOME/HERMES_HOME/state/TMPDIR 全隔离，网络仅测试 fixture 端口。真实 guard probe 确认共享 AGENTS.md 的 O_WRONLY open 被拒绝，而证据根可写。wrapper 为窄化授权，本轮不开放共享 `/dev/shm`。

| 轮次 | 实际结果 | XML |
|---|---|---|
| RED 1 | 1 failed，exit 1，initializer missing 断言 | red-1/junit.xml |
| GREEN 1 | 1 passed，exit 0 | green-1/junit.xml |
| RED 2 | 5 failed / 1 passed，exit 1；未知 schema 未拒绝、非持久库未拒绝、活动事务缺显式拒绝 | red-2/junit.xml |
| GREEN 2 | 6 passed，exit 0 | green-2/junit.xml |
| 邻域含 shadow queue | 57 passed / 2 failed，exit 1 | neighbors/junit.xml |
| 最终初始化+display_history+history_capture | 27 passed，exit 0 | green-final/junit.xml |

SQLite authorizer 中途拒绝真实 CREATE TRIGGER 的回滚用例为追加 characterization（不是新增 RED）：确认已创建表/trigger 全部回滚、连接退出事务，解除故障后成功安装17表66trigger。完整 DDL 与受审文档 SQL 所生成的 sqlite_schema 精确对照。

邻域两失败均在 `multiprocessing.Queue()` 创建共享信号量阶段报 PermissionError，尚未执行 shadow 重启业务；这是受审只写证据根的 guard 不允许 `/dev/shm`，不以略过测试冒充全绿。保留失败，不放宽 guard。本轮没有完整套件通过声明。Ruff 三个 Python 文件与 `git diff --check` 通过。

代码 SHA-256：
- `api/display_schema.py`: `c81447c92a1a2c7bb9a49003fcca8c3fd069d8a1258dfdaa4342bcc1d572ece5`
- `api/_display_schema_ddl.py`: `61744da612921617ffc10446fe580a55b8b8c1047f7cfc093b21b785212a4c97`
- `tests/test_display_schema.py`: `52911b630ccabe10a78fff65f1ecca804efeeb1bc9d57a5093f21c40e0a090f4`

SQL 存于 Python 常量模块，避免现有包配置未包含 api/*.sql 导致安装后缺资产；仅逐字转录获批 SQL，无增删约束。没有 schema 版本迁移 API；初始化以精确目录指纹拒绝未知版本。生产业务 invariant_check 尚未实现，空库 quick/FK check 不替代它。后续正式写入口必须先实现对应 invariant_check。

