# 离线初始化编码设计候选 v2

状态：CODING_DESIGN_REVISED / REVIEW_PENDING。针对93a8101bb5295966b3d5b7bde50bec63f4bb4137所记录CD-01—CD-06修订。方案接受记录沿用a36ab6cd2bfb1f527e73c0051917482f946dca31，不撤销用户方案确认；本文件替代旧编码提纲，不覆盖历史审查证据。当前仅文档调整，未授权功能编码、UID/权限变更、SQL执行、部署、激活或删除。

## 1. 最小交付与模块

有限闭环：受保护新目录 → 创建空库 → 冻结验证 → 外部批准 → no-replace发布 → 持久读回PUBLISHED_UNACTIVATED。不提供业务连接或激活接口。

- `api/display_bootstrap_policy.py`：类型校验、预算和平台要求；纯配置不是授权能力。
- `api/display_bootstrap_artifact.py`：候选创建、DDL、关闭、冻结验证；引用`api/_display_schema_ddl.py`唯一SQL。
- `api/display_bootstrap_manifest.py`：严格解析、规范字节、摘要与批准记录匹配。
- `api/display_bootstrap_publish.py`：注册表、锁、发布和恢复。
- `api/display_connection.py`：**延期，不在本切片创建**。已激活资格与业务连接工厂须另行编码设计；本切片不能接受path或bool冒充激活资格。
- `api/display_schema.py`：实施时将旧initialize变为无数据库访问的显式拒绝入口，详见§7。

## 2. CD-01：精确值类型与序列化

所有持久对象有`format_version: int = 1`；不识别版本或字段一律拒绝，不做向前兼容猜测。下表字段均必填，除明确标optional外不接受null。整数使用Python精确int，拒绝bool，范围0..9007199254740991；positive要求至少1。记录中不含密钥、任意执行命令或客户端路径。

| 类型 | 字段与限制 | 来源/可变性 |
|---|---|---|
| CandidateId | 32位小写十六进制字符串 | 服务端安全随机生成；全生命周期不变 |
| Digest | 64位小写十六进制SHA256 | 从指定规范字节/文件流计算；不修复输入 |
| FileIdentity | dev:int, ino:positive, uid:int, gid:int, mode:int, nlink:positive | fstat读取；主库nlink必须1；不接受客户端提供值代替读回 |
| PlatformEvidence | format_version, evidence_id:CandidateId, kernel:str, sqlite_version:str, compile_options:list[str], vfs:str, filesystem:str, mount_id:int, mount_options:list[str], namespace_id:str, approved_policy_sha:Digest | 受控预检输出；只作审计，不作为跨运行权限凭证；vfs等不能确认则拒绝 |
| ResourcePolicy | format_version, max_candidate_bytes:positive, min_free_bytes:positive, max_retained_candidates:positive, max_rss_bytes:positive, max_elapsed_seconds:positive, check_interval_ms:positive, audit_reserve_bytes:positive, hard_limit_profile_id:CandidateId | 受保护策略文件；具体数值实施前测量批准，无默认值 |
| Manifest | format_version, candidate_id, ddl_sha:Digest, sqlite_version:str, platform_evidence_sha:Digest, resource_policy_sha:Digest, db_bytes:positive, db_sha:Digest, db_identity:FileIdentity, connection_policy:{foreign_keys:1,synchronous:2}, verification:{integrity_check:"ok",foreign_key_violations:0,empty_state:true}, creator_commit:str | 冻结后生成；creator_commit为完整40或64位小写Git对象ID；manifest不包含自身hash |
| ApprovalRecord | format_version, approval_id:CandidateId, candidate_id, manifest_sha:Digest, target_name:CandidateId, policy_sha:Digest, scope:"PUBLISH_EMPTY_UNACTIVATED", approver_reference:str | 独立受信管理入口创建；调用者不能提交dict自行批准；创建后不可改写 |
| RegistryRecord | format_version, candidate_id, seq:positive, previous_sha:Digest或首条null, state:enum, target_name:CandidateId, manifest_sha:Digest或null, approval_id:CandidateId或null, error_code:enum或null | 当前锁所有者原子追加；可空字段只在尚未产生或无错误时为空 |
| PublishResult | status:enum, code:enum, candidate_id, target_name, registry_seq:int, evidence_sha:Digest或null | 结果不含数据库连接；仅PUBLISHED_UNACTIVATED表示发布持久确认，不表示激活 |

字符串必须非空、严格UTF-8可编码，普通元数据字符串最多4096编码字节；无Unicode归一化、无替换。名称字段只用CandidateId，不接受斜杠、点、URI或绝对路径。每份元数据最大65536字节；数组最多256项，每项遵守字符串限制。

规范JSON：UTF-8无BOM；键按Unicode码点排序，separators=(',',':')，ensure_ascii=False；只准object/list/string/int/bool/null，具体字段按表进一步限制，禁止float/NaN/Infinity。解析使用重复键拒绝钩子，拒绝未知字段、重复键、尾随内容和不合法Unicode。读取后重新规范编码必须与持久字节完全一致，否则NONCANONICAL_RECORD；不默默重写。摘要覆盖规范原始字节。manifest长度及解析上限在反序列化前检查。

## 3. API与结果契约

以下是待实现的签名规范，不是已存在接口：

- `acquire_bootstrap_context(policy_id: CandidateId) -> BootstrapContext`：仅服务端入口，读取受保护部署策略并实时预检；私有构造器、不可序列化、不可跨进程使用、退出context后失效。不能把它当作抵御进程内任意Python执行的密码学能力；专用进程及OS隔离是信任边界。
- `create_candidate(ctx) -> CandidateResult`：ctx内生成ID、保留预算、排他创建；不接受已有连接/自定义路径。成功返回VERIFIED候选ID和manifest摘要，不自动批准。
- `load_approval(ctx, approval_id) -> ApprovalRecord`：只从固定批准根无跟随读取，核验权限与内容；调用API本身不生成批准。
- `publish_candidate(ctx, candidate_id, approval_id) -> PublishResult`：持锁读回身份/注册表/批准，精确匹配后发布。
- `recover_candidate(ctx, candidate_id) -> PublishResult`：同锁和同身份验证；按§5判断，不创建新业务库、不删除。

CandidateResult同PublishResult字段，成功status=VERIFIED。发布返回status仅PUBLISHED_UNACTIVATED、BLOCKED、QUARANTINED、UNCERTAIN；绝无ELIGIBLE/ACTIVE。错误码：INVALID_INPUT、NONCANONICAL_RECORD、UNSUPPORTED_PLATFORM、ACCESS_BOUNDARY_UNPROVEN、LOCK_BUSY、POLICY_MISSING、RESOURCE_LIMIT、AUDIT_UNAVAILABLE、UNKNOWN_SCHEMA、SIDECAR_REMAINS、IDENTITY_CHANGED、APPROVAL_MISMATCH、TARGET_CONFLICT、STATE_CONFLICT、IO_FAILURE、DURABILITY_UNCERTAIN、LEGACY_INITIALIZER_DISABLED。

输入校验失败不得创建目录或打开SQLite。SQLite错误映射UNKNOWN_SCHEMA或IO_FAILURE并保留原异常类型于受保护审计，不输出敏感内容；OSError保留errno。任何rename之后的同步/记录错误返回UNCERTAIN/DURABILITY_UNCERTAIN，不能返回普通可重试失败。KeyboardInterrupt/SystemExit不吞掉：finally关闭连接/FD释放锁，尽力记录；重启以磁盘状态判定，不能依赖异常处理一定运行。

## 4. CD-03：权限、锁和证据寿命

固定部署策略（受root保护）绑定creator_uid、registry_root、approval_root、candidate_root、publish_root、各自祖先身份及支持的挂载/namespace。禁止UID1024控制的hermes-home或data目录承担该边界。创建/发布角色使用专用UID，审批管理角色使用不同可信管理主体；专用UID不能写批准根。root与宿主管理员可信，不防任意root操作；同专用UID不允许运行其他非受控工作。

目录与注册表由专用UID管理；批准根对其只读。祖先不允许非可信主体写入；ACL、挂载别名、user namespace与组权限必须验证。缺ACL工具不等于放行，编码实现应读取并判定受支持ACL xattr，未知ACL/平台返回ACCESS_BOUNDARY_UNPROVEN；不自动chmod/chown。

全局锁是受保护根中预置常规文件的POSIX flock(LOCK_EX|LOCK_NB)，锁文件不可unlink/替换；打开无跟随并fstat核验。只协调可信创建/发布/恢复进程，不宣称flock阻止任意SQL写方。单主机、单部署根、所有进程共用同一锁；不支持跨主机。LOCK_BUSY立即返回，不持其他锁等待；本切片无业务scope锁。锁顺序唯一：全局锁→目录FD→SQLite连接，释放反序。

BootstrapContext绑定pid、锁FD、各根目录FD、mount/namespace身份及策略摘要；每个入口验证ctx未关闭且pid一致。持锁跨越预算预留、候选创建、SQLite打开到关闭、hash、验证、批准读回、rename及同步/恢复。审批可以在创建退出锁后发生；发布重新获取新ctx并全量核验，不沿用旧证据。每次进入预检及阶段边界重查身份/策略；边界检查只能发现变化，防止中间替换依赖持续OS权限与可信管理者不并发改变部署的前提。无法保证此前提即不运行，不把检查快照当证明。

关闭路径：SQLite rollback/close尽力执行，不执行清理DELETE；随后关闭所有候选/根FD，最后解锁并关闭锁FD。失败文件保留；OS进程退出释放锁不代表候选已安全。

## 5. CD-02：注册表、状态与恢复

介质选择：受保护registry_root下每候选一个目录，不新增业务SQLite表。记录以seq补零20位命名`00000000000000000001.json`，前一条规范字节SHA串链。原子追加：同目录O_EXCL临时文件→完整写/fsync→no-replace重命名为序号文件→fsync注册表目录→读回。临时文件残留不自动删除；记录缺号、重复冲突、hash不匹配即隔离。注册目录首次创建须同步父目录。记录追加失败禁止进入下一破坏性阶段；注册表不足以消除文件系统不确定状态。

| 原状态→新状态 | 唯一执行者/前置 | 持久动作及成功条件 | 失败 |
|---|---|---|---|
| NONE→RESERVED | 创建者，预算/保留数量合格 | 首记录持久化预算归属；目标名固定等于候选ID | 无候选；AUDIT_UNAVAILABLE |
| RESERVED→BUILDING | 同锁，新候选目录不存在 | O_EXCL创建目录/主库；身份读回；记录持久 | FAILED；不复用 |
| BUILDING→VERIFIED | DDL与完整性/空状态、WAL关闭、冻结复核通过 | manifest及候选目录同步，记录绑定manifest摘要 | FAILED或QUARANTINED |
| VERIFIED→APPROVED | 发布者，外部批准匹配 | 批准ID/摘要写注册记录并同步 | 保持VERIFIED/BLOCKED |
| APPROVED→PUBLISH_INTENT | 发布者，目标缺失、候选再次核验 | 意图记录先持久化；固定目标/候选/批准 | 无rename，BLOCKED |
| PUBLISH_INTENT→PUBLISHED_UNACTIVATED | 发布者 | 整目录no-replace rename；同步两父目录；完成记录持久；读回一致 | UNCERTAIN，不激活 |
| 任意未完成→FAILED | 可明确归因且未rename | 持久错误记录；禁止沿用创建/发布 | 保留文件 |
| 任意→QUARANTINED | 身份/状态冲突 | 尽力持久隔离标记；读回失败也禁止继续 | 人工处理 |

FAILED/QUARANTINED为终态，不重试同候选构建；PUBLISHED_UNACTIVATED只可幂等读回，不改主库。UNCERTAIN是调用结果；持久权威可能仍是PUBLISH_INTENT，恢复不能要求一定有UNCERTAIN记录。

恢复表中S=候选源目录，T=发布目标，A=批准记录精确匹配，C=完成记录。所有判断先持新ctx锁并验证注册串链与文件身份；表覆盖发布意图及完成后的全部组合：

| S | T | A | C | 行为 |
|---|---|---|---|---|
| 任意 | 任意 | 无/不匹配 | 任意 | BLOCKED或QUARANTINED，不打开SQLite、不rename；不因C绕过审批 |
| 有 | 无 | 匹配 | 无 | 仅最新PUBLISH_INTENT允许重新验证后no-replace；较早状态按正常批准流程；FAILED/隔离禁止 |
| 无 | 有 | 匹配 | 无 | 仅PUBLISH_INTENT且目标manifest/身份匹配：重新同步两个父目录，追加完成记录、读回；否则隔离 |
| 无 | 有 | 匹配 | 有 | 完成记录ID/摘要与目标一致返回PUBLISHED_UNACTIVATED；不重新创建库 |
| 有 | 有 | 匹配 | 任意 | 冲突，QUARANTINED；不猜正确副本，不删除 |
| 无 | 无 | 匹配 | 任意 | 丢失/未决，QUARANTINED；不重建原ID |
| 有 | 无 | 匹配 | 有 | 完成记录与磁盘矛盾，QUARANTINED |

无注册记录但发现源/目标：未知孤儿，只列隔离，不生成批准。新建RESERVED且尚无任何目录不属于发布恢复；可记FAILED释放逻辑活动预算，但保留审计且该ID不得复用。EEXIST只触发上述读回，不覆盖；EXDEV/ENOSYS在rename前BLOCKED，无普通rename降级。失败不能通过重命名其他目标绕过已绑定target_name。

## 6. CD-05：预算执行语义

字节单位B，时间秒/毫秒，RSS为专用工作进程及其受控子进程总驻留内存预算；本切片不启动子进程执行SQLite。candidate_bytes包含候选/发布未激活目录内主库、WAL、SHM、journal、临时文件和manifest的实际分配字节；稀疏文件另比较逻辑字节，任一超限拒绝。注册表/批准/错误日志由独立audit_reserve_bytes预算保障。max_retained_candidates按注册表与根目录并集去重计数，含失败、孤儿、已发布未激活，不因退出释放文件占用。

全局锁内检查并写RESERVED完成逻辑预留：free_bytes必须至少为min_free_bytes + max_candidate_bytes + audit_reserve_bytes。候选已存在数量必须严格小于max_retained_candidates。每个可增长阶段前和不超过check_interval_ms间隔检查；check_interval_ms不得大于max_elapsed_seconds×1000。具体策略经批准输入，不编造运行值。

轮询不提供硬上限：部署须提供经验证的候选空间配额和独立审计预留，以及进程memory/cpu或墙钟限制；hard_limit_profile_id由可信运行器解析到实际限制并核验，普通字符串不授予能力。当前平台无证明则UNSUPPORTED_PLATFORM/RESOURCE_LIMIT且不创建候选。SQLite max_page_count只能作为附加约束，不能覆盖WAL/临时文件。其他磁盘用户仍可能消耗空间，所有write/fsync都必须处理ENOSPC；审计也失败则不继续发布，状态由恢复表判定。

达到限额关闭本候选连接并停止新写，不杀正式服务、不自动删库。活动预留可在确认专用进程退出且持锁时撤销；物理占用和保留计数直到用户明确授权清理且读回完成才减少。清理操作不在本切片API内。

## 7. CD-04：旧入口迁移与切片边界

本次源码检索发现旧模块引用仅`tests/test_display_schema.py`和`tests/test_display_mutations.py`，尚无生产调用者；此为当前检索证据，不作为未来永久事实。实施前必须重新扫描静态/动态导入和连接创建入口，并增加CI防止routes/server接线。

实施目标：`initialize(db)`保留名字但立即抛LegacyInitializerDisabled(code=LEGACY_INITIALIZER_DISABLED)，在访问db属性/执行PRAGMA前拒绝。原WAL竞争用例在旧固定源码证据中永久保留RED；在新测试中用拒绝SQL的连接替身证明旧入口零访问，并用真实未知库证明内容/日志模式不变。旧危险实现在产品模块中不保留隐藏开关或测试特权。

新schema结构测试通过离线候选创建接口获得受控空库，再验证批准DDL；binding夹具可在纯测试模块直接安装参考DDL以测拒绝行为，必须标记为合成协议夹具，不能授予生产资格。同连接重入不再是创建API契约，不把取消该接口测试宣称为原算法已修复；安全策略是废弃危险入口并独立验证新生命周期。

PUBLISHED_UNACTIVATED目录仅用于发布验收，不交给业务连接工厂，不修改GET/worker、runtime imports、compose或现有state.db。后续激活/权限交接/运行库恢复须另设计，不能在本切片顺带实现。

## 8. CD-06：可执行验收规格（均NOT_RUN）

拟建`tests/test_display_bootstrap_contract.py`、`tests/test_display_bootstrap_recovery.py`、`tests/test_display_bootstrap_platform.py`。下表node名各自独立；输入参数矩阵按行明确扩展，不能把环境不满足列为PASS。每项保存command、exit、JUnit、前后文件清单/身份/SHA、注册链、策略/平台摘要；不输出秘密。

| 文件/测试函数 | 夹具与注入 | 预期返回/持久后置 |
|---|---|---|
| contract/test_reject_invalid_record | 每类型字段缺失/多余/bool整数/重复键/float/非法UTF8/超长/非规范JSON | INVALID_INPUT或NONCANONICAL_RECORD；无候选/SQLite打开 |
| contract/test_canonical_digest | 固定规范UTF8字节与已知SHA，用工具生成独立fixture | 相同规范字节同SHA；改变任一字段摘要变；不归一化 |
| contract/test_missing_policy | 逐预算/平台字段缺失，未知hard_limit ID | POLICY_MISSING或UNSUPPORTED_PLATFORM，零创建 |
| platform/test_context_lifetime | 已退出ctx、跨pid、伪造普通dict | 拒绝；无SQLite/rename |
| platform/test_access_boundary | 自有测试根的ACL/祖先/挂载不受控、非可信UID访问、符号/硬链接 | ACCESS_BOUNDARY_UNPROVEN/IDENTITY_CHANGED；保留原文件；权限能力不可测则环境阻断 |
| platform/test_lock_contention | 两个真实进程同锁，首持锁各阶段 | 第二LOCK_BUSY；只有一个RESERVED/创建者；退出锁释放；不unlink锁 |
| contract/test_exact_empty_schema | 新候选、同版本批准DDL参考；逐表插行/多trigger/缺index负对照 | 正常VERIFIED；异常UNKNOWN_SCHEMA并FAILED；目录精确比较 |
| platform/test_wal_freeze | 私有SQLite保留读连接使checkpoint busy、残余WAL/SHM/journal | SIDECAR_REMAINS/BLOCKED；不手删，不能VERIFIED |
| platform/test_immutable_snapshot | 无sidecar冻结候选只读事务；前后hash/身份/文件集合 | 正常不变；替换条件被OS拒绝或IDENTITY_CHANGED；不外推活动库 |
| contract/test_approval_binding | 六字段分别变更、未知审批根、请求自带批准dict | APPROVAL_MISMATCH；无rename，目标不变 |
| platform/test_publish_no_replace | 自有源/目标同FS；两个候选竞争同目标；目标预存sentinel | 一次成功，另一TARGET_CONFLICT；sentinel字节/身份不变；不覆盖 |
| platform/test_unsupported_publish | 封装注入EXDEV/ENOSYS；真实平台另验证 | UNSUPPORTED_PLATFORM；源保留、无目标、无降级rename |
| recovery/test_each_crash_boundary | 自有候选在每次记录fsync、manifest同步、intent同步、rename、两父目录fsync、完成记录前后终止专用测试进程 | 新进程按§5表恢复；不激活、不覆盖；进程故障不标断电PASS |
| recovery/test_state_matrix | §5所有S/T/A/C组合，追加串链缺号/损坏/孤儿 | 精确表中结果；冲突保留；只匹配意图能补完成 |
| recovery/test_audit_write_failure | 注册临时写/fsync/rename/目录sync逐点ENOSPC/EIO | rename前不继续；rename后UNCERTAIN；注册临时保留 |
| contract/test_resource_boundaries | 数量=上限/低一项，free低于/等于要求，候选逻辑/分配字节、RSS、时间超限 | 等界按§6比较；超限停止本候选；保留失败文件与计数 |
| platform/test_hard_limits | 获批自有配额/进程限制负对照，写入超过硬限额 | 系统级拒绝且审计有预留；缺部署能力BLOCKED，不放宽限制 |
| contract/test_legacy_initializer_no_access | 连接替身属性访问即报错；真实未知库前后SHA与mode | LEGACY_INITIALIZER_DISABLED，零DB访问，不改变未知库 |
| contract/test_no_runtime_wiring | 静态引用检查及调用入口审计 | 无routes/server/worker接入，无激活接口；只允许批准测试引用 |

发布各错误码覆盖至少一次测试；不能仅assert异常而忽略事务/FD/锁释放与持久文件状态。平台测试必须使用真实文件SQLite和实际no-replace原语；mock只验证错误分支，不替代目标平台实证。测试目录及失败库均保留，删除需用户明确同意。

## 9. 风险、关闭证据与复审

| 审查项 | 本次对策与验证位置 | 残余风险/回退 |
|---|---|---|
| CD-01 | §2字段与规范字节，§3签名，contract解析/摘要测试 | 未实现解析器，不能声明接口验证PASS |
| CD-02 | §5注册介质/转换/恢复表，recovery故障矩阵 | 断电持久需独立平台验证，未完成不激活 |
| CD-03 | §4OS前提/能力寿命/锁顺序，platform权限测试 | 专用UID和ACL路线未执行；不可证明则BLOCKED |
| CD-04 | §1/7缩到发布未激活、旧接口零访问拒绝 | 连接工厂延期，正式业务仍LEGACY |
| CD-05 | §6量纲/计数/预留/硬限额，资源边界测试 | 实际预算和硬限制能力缺失，禁止构建 |
| CD-06 | §8逐测试夹具/故障/状态及证据 | 所有新用例NOT_RUN，不使用旧105通过替代 |

作者侧修订已覆盖六项缺口，状态统一DOC_REVISED_PENDING_REVIEW，不自行将历史OPEN改成独立关闭。下一步针对本文件新固定SHA复审；编码设计接受之后还须明确实施授权及实际平台前提。无功能代码变更、无新库、无UID操作、无部署。
