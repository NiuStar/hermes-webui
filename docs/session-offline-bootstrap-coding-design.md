# 离线初始化编码设计候选 v4

状态：CODING_DESIGN_REVISED / REVIEW_PENDING。针对93b7599f449c5d65078a6b807e5f1edf00d828c3所记录V2-01—V2-04整改，保留CD-01—CD-06闭包。方案接受记录沿用a36ab6cd2bfb1f527e73c0051917482f946dca31，不撤销用户方案确认；本文件替代旧编码提纲，不覆盖历史审查证据。当前仅文档调整，未授权功能编码、UID/权限变更、SQL执行、部署、激活或删除。

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
| OperationResult | §3带status判别的严格联合类型 | 不含数据库连接；拒绝分支允许尚无候选ID，不使用空字符串占位 |

字符串必须非空、严格UTF-8可编码，普通元数据字符串最多4096编码字节；无Unicode归一化、无替换。名称字段只用CandidateId，不接受斜杠、点、URI或绝对路径。每份元数据最大65536字节；数组最多256项，每项遵守字符串限制。

规范JSON：UTF-8无BOM；键按Unicode码点排序，separators=(',',':')，ensure_ascii=False；只准object/list/string/int/bool/null，具体字段按表进一步限制，禁止float/NaN/Infinity。解析使用重复键拒绝钩子，拒绝未知字段、重复键、尾随内容和不合法Unicode。读取后重新规范编码必须与持久字节完全一致，否则NONCANONICAL_RECORD；不默默重写。摘要覆盖规范原始字节。manifest长度及解析上限在反序列化前检查。

## 3. API与结果契约

以下是待实现的签名规范，不是已存在接口：

- `acquire_bootstrap_context(policy_id: CandidateId) -> BootstrapContext`：仅服务端入口，读取受保护部署策略并实时预检；私有构造器、不可序列化、不可跨进程使用、退出context后失效。不能把它当作抵御进程内任意Python执行的密码学能力；专用进程及OS隔离是信任边界。
- `create_candidate(ctx) -> OperationResult`：ctx内生成ID、保留预算、排他创建；不接受已有连接/自定义路径。成功返回VERIFIED候选ID和manifest摘要，不自动批准。
- `load_approval(ctx, approval_id) -> ApprovalRecord`：只从固定批准根无跟随读取，核验权限与内容；调用API本身不生成批准。
- `publish_candidate(ctx, candidate_id, approval_id) -> OperationResult`：持锁读回身份/注册表/批准，精确匹配后发布。
- `recover_candidate(ctx, candidate_id) -> OperationResult`：同锁和同身份验证；按§5判断，不创建新业务库、不删除。

所有API先验证输入，再验证ctx/pid/锁，再检查运行前提，最后执行状态判断。`acquire_bootstrap_context`和`load_approval`失败抛`BootstrapRejected(code)`；前者失败不返回ctx，后者失败不返回批准对象。三项操作API返回以下OperationResult联合类型；无有效ctx时返回BLOCKED而非创建新ctx。操作API内可预见的BootstrapRejected转换为BLOCKED，意外程序错误不伪装成功；KeyboardInterrupt/SystemExit仍按下文传播。

各分支均有format_version=1，禁止跨分支额外字段：
- VERIFIED：status="VERIFIED", code="OK", candidate_id:CandidateId, target_name:CandidateId, registry_seq:positive, manifest_sha:Digest。仅create成功使用。
- PUBLISHED_UNACTIVATED：status同名，code="OK"，candidate_id、target_name、registry_seq、manifest_sha与上同，另有completion_record_sha:Digest，覆盖最新持久完成记录的规范字节。只在最新状态确为完成且所有读回通过时使用。
- BLOCKED：status同名，code为下列拒绝码，candidate_id:CandidateId|null, target_name:CandidateId|null, registry_seq:int|null。尚未生成/验证ID时两ID均null；已验证ID时target_name必须与其相等。registry_seq仅在注册链读回成功时有值，否则null。不含成功摘要。
- QUARANTINED：status同名，code="STATE_CONFLICT"或"IDENTITY_CHANGED"，ID字段同BLOCKED；registry_seq同上，quarantine_persisted:bool，只有隔离记录同步并读回才为true，false同样禁止继续操作。
- UNCERTAIN：status同名，code="DURABILITY_UNCERTAIN"，candidate_id和target_name非null且相等，registry_seq:int|null。仅发布目录rename可能发生之后使用，不携带完成成功证据。

拒绝码：INVALID_INPUT、NONCANONICAL_RECORD、UNSUPPORTED_PLATFORM、ACCESS_BOUNDARY_UNPROVEN、LOCK_BUSY、POLICY_MISSING、RESOURCE_LIMIT、AUDIT_UNAVAILABLE、UNKNOWN_SCHEMA、SIDECAR_REMAINS、IDENTITY_CHANGED、APPROVAL_MISMATCH、TARGET_CONFLICT、STATE_CONFLICT、IO_FAILURE、LEGACY_INITIALIZER_DISABLED。OK只能用于成功分支；FAILED最新状态映射BLOCKED/STATE_CONFLICT，QUARANTINED最新状态映射QUARANTINED/STATE_CONFLICT。废除未定义的evidence_sha字段，manifest_sha与completion_record_sha不得混用。

输入校验失败不得创建目录或打开SQLite。SQLite错误映射UNKNOWN_SCHEMA或IO_FAILURE并保留原异常类型于受保护审计，不输出敏感内容；OSError保留errno。发布目录rename成功或结果不确定之后的同步/记录错误返回UNCERTAIN/DURABILITY_UNCERTAIN，不能返回普通可重试失败。KeyboardInterrupt/SystemExit不吞掉：finally关闭连接/FD释放锁，尽力记录；重启以磁盘状态判定，不能依赖异常处理一定运行。

## 4. CD-03：权限、锁和证据寿命

固定部署策略（受root保护）绑定creator_uid、registry_root、approval_root、candidate_root、publish_root、各自祖先身份及支持的挂载/namespace。禁止UID1024控制的hermes-home或data目录承担该边界。创建/发布角色使用专用UID，审批管理角色使用不同可信管理主体；专用UID不能写批准根。root与宿主管理员可信，不防任意root操作；同专用UID不允许运行其他非受控工作。

目录与注册表由专用UID管理；批准根对其只读。祖先不允许非可信主体写入；ACL、挂载别名、user namespace与组权限必须验证。缺ACL工具不等于放行，编码实现应读取并判定受支持ACL xattr，未知ACL/平台返回ACCESS_BOUNDARY_UNPROVEN；不自动chmod/chown。

全局锁是受保护根中预置常规文件的POSIX flock(LOCK_EX|LOCK_NB)，锁文件不可unlink/替换；打开无跟随并fstat核验。只协调可信创建/发布/恢复进程，不宣称flock阻止任意SQL写方。单主机、单部署根、所有进程共用同一锁；不支持跨主机。LOCK_BUSY立即返回，不持其他锁等待；本切片无业务scope锁。锁顺序唯一：全局锁→目录FD→SQLite连接，释放反序。

BootstrapContext绑定pid、锁FD、各根目录FD、mount/namespace身份及策略摘要；每个入口验证ctx未关闭且pid一致。持锁跨越预算预留、候选创建、SQLite打开到关闭、hash、验证、批准读回、rename及同步/恢复。审批可以在创建退出锁后发生；发布重新获取新ctx并全量核验，不沿用旧证据。每次进入预检及阶段边界重查身份/策略；边界检查只能发现变化，防止中间替换依赖持续OS权限与可信管理者不并发改变部署的前提。无法保证此前提即不运行，不把检查快照当证明。

关闭路径：SQLite rollback/close尽力执行，不执行清理DELETE；随后关闭所有候选/根FD，最后解锁并关闭锁FD。失败文件保留；OS进程退出释放锁不代表候选已安全。

### 4.1 V2-04：部署策略与摘要绑定

新增DeploymentPolicy为持久严格类型，包含format_version=1、policy_id:CandidateId、creator_uid:int、approver_uid:int、roots:object、ancestors:list、lock_identity:FileIdentity、platform_requirements:object、resource_policy_id:CandidateId、resource_policy_sha:Digest、expected_ddl_sha:Digest。creator_uid不得等于approver_uid。

roots的精确键为registry_root/approval_root/candidate_root/publish_root/lock_root，每项为{path:str,identity:DirectoryIdentity}。path仅来自root管理的可信配置，必须绝对路径、无NUL、无`.`/`..`分量，不适用CandidateId名称约束；禁止调用请求提供或覆盖。ancestors元素为{path:str,identity:DirectoryIdentity}，逐根列出到文件系统根的祖先，按path排序且不重复，缺任一祖先拒绝。

platform_requirements精确字段：kernel:str、sqlite_version:str、compile_options:list[str]、vfs:str、filesystem:str、mount_id:int、mount_options:list[str]、namespace_id:str；列表按字典序排序且去重，运行读回必须逐项匹配，无法确认即拒绝。此处是固定部署范围，不支持自动迁移到另一namespace。策略键和通用值规则沿用§2。

全部署唯一锁固定为`/etc/hermes-display-bootstrap/bootstrap.lock`，根目录root所有且creator不可写。root预置常规锁文件，creator仅获打开及flock所需权限，无父目录写权限。不可切换的root管理`anchor.json`严格包含format_version=1、lock_path（上述固定值）、lock_identity:FileIdentity；身份由部署前实证写入。所有策略lock_root及lock_identity必须与此锚点精确匹配，否则拒绝；不得由active或调用者选锁。上下文先读验anchor并打开同一固定锁，再持锁读取active/策略；管理员切换、恢复active也持这把锁直到文件及父目录fsync和读回结束。新旧策略执行者因此竞争同一锁，不能在同步前进入。anchor/锁inode/根身份在运行期间不可替换；锁迁移只可在另获授权、停止全部执行者并排空后离线重新部署，不在本协议支持，发现变化即拒绝。新增test_global_anchor_lock覆盖D0/L0与D1/L1配置拒绝、管理端active替换与目录同步期间执行器LOCK_BUSY、回退同锁、锁inode替换拒绝。风险是换策略绕开互斥；固定不可变锚点防止，未验证不得运行。

当前生效锚点固定为`/etc/hermes-display-bootstrap/active.json`，root管理、creator只读；严格字段format_version=1、policy_id:CandidateId、deployment_policy_sha:Digest。管理员使用临时写/fsync/原子替换/fsync父目录更新；此为独立管理授权操作，不由本切片执行。acquire参数policy_id只能断言与active一致，不能选择历史策略；读取策略规范SHA必须等于active.deployment_policy_sha。每个发布入口和rename前重新读回active，变化返回BLOCKED/APPROVAL_MISMATCH；管理者变更active必须遵守相同全局锁协议，专用执行者持锁期间禁止管理者替换，排除检查到rename窗口。保留旧策略文件不使其可加载；只有管理者明确将active恢复到该ID与摘要且持久确认才算允许回退。active缺失/损坏或权限不可信失败关闭；当前用户提供ID、历史批准均不能建立active资格。新增test_active_policy_anchor覆盖保留D0文件但active=D1时指定D0拒绝、审批匹配也拒绝、管理变更锁竞争及授权恢复D0。

可信管理预置策略根`/etc/hermes-display-bootstrap/policies/`：root所有且不可由creator_uid写入，策略文件`<policy_id>.json`，资源文件`resources/<resource_policy_id>.json`；均无跟随读取且验证祖先权限。路径是拟实施配置，不在本轮创建。任何元数据总大小仍受65536字节限制，超限拒绝，不截断祖先清单。

记D=DeploymentPolicy规范字节SHA，R=ResourcePolicy规范字节SHA，E=PlatformEvidence规范字节SHA，M=Manifest规范字节SHA。DeploymentPolicy.resource_policy_sha必须=R；PlatformEvidence.approved_policy_sha=D；Manifest.resource_policy_sha=R且platform_evidence_sha=E。PlatformEvidence不包含自身摘要；DeploymentPolicy不含D，避免循环自哈希。

发布必须同时满足：当前ctx重新读取的D与创建时证据中的D相等；当前R与DeploymentPolicy及Manifest中的R相等；当前平台与platform_requirements匹配；候选目录名称=Manifest.candidate_id=请求candidate_id=ApprovalRecord.candidate_id=ApprovalRecord.target_name=注册target_name；ApprovalRecord.manifest_sha=M=最新VERIFIED及后续注册记录绑定摘要；ApprovalRecord.policy_sha=D；批准scope精确PUBLISH_EMPTY_UNACTIVATED；approval_id与文件名相等；首次VERIFIED→APPROVED及后续注册绑定按§4.2执行。M须由当前只读文件重新计算，不用调用者缓存值；主库字节/身份仍需再次核验。

创建到发布期间D或R变化，旧候选返回BLOCKED/APPROVAL_MISMATCH，不允许改manifest、重新签批同候选来绕过；只有管理者明确恢复完全相同受信策略并重新验证，或另建新候选才可走新流程。新建是否获准仍受预算和授权限制，旧候选保留。批准记录创建后不可更新/替换；重建批准属于单独管理流程，不由发布API代办。审批主体为可信管理者，发布器不把approver_reference文本本身当身份认证。

风险：摘要字段混用使旧批准跨策略生效。对策：上列等式逐项验证、无自动修复；验证见test_policy_digest_binding；回退：不匹配零rename，保留原候选和审批证据。

### 4.2 v4：目录身份、证据持久化与首次批准

DirectoryIdentity精确字段为dev:int、ino:positive、uid:int、gid:int、mode:int；必须是真实目录，无符号链接，五字段逐项匹配。目录nlink/size/mtime/ctime因正常子项变化而变化，只作审计观测，不写入固定策略或用于相等判断。ACL和挂载检查仍单独执行，不因排除nlink而省略。主库及锁文件继续使用FileIdentity，常规文件nlink=1。合法创建/发布不得重写部署策略摘要；根inode替换、权限弱化或主库硬链接仍拒绝。

创建时PlatformEvidence规范全文持久放在`registry_root/<candidate_id>/evidence/<E>.json`，E为其规范字节SHA；evidence目录在同权限域内，必须创建并同步父目录。写入规则为同目录随机O_EXCL临时文件、写完整字节、fsync、no-replace重命名到E.json、fsync evidence目录、读回解析/hash校验。E证据必须在manifest写入和VERIFIED记录之前持久确认；失败返回AUDIT_UNAVAILABLE且不能VERIFIED。证据不放入候选目录，不改变主库+manifest白名单，计入audit_reserve_bytes，所有残留保留。

发布/恢复按Manifest.platform_evidence_sha定位上述受保护文件，无跟随、限长、严格解析、验hash，再验证其中D与当前策略；缺失/损坏返回BLOCKED/APPROVAL_MISMATCH，禁止根据当前平台重新生成历史E。新的运行观测可作单独审计，但不能代替创建证据。证据文件同名已存在时只允许字节完全相同且权限/身份合格的幂等读回，否则隔离；不得覆盖。

首次publish在最新VERIFIED且注册approval_id=null时，读取请求明确指定的批准文件，验证所有非注册审批绑定等式；通过后追加APPROVED（首次写入approval_id），同步并读回，再执行非空审批绑定检查及PUBLISH_INTENT。最新APPROVED/PUBLISH_INTENT/PUBLISHED_UNACTIVATED必须有非空、与请求及批准文件完全相同的approval_id；即使另一个批准候选/摘要相同也禁止替换。FAILED/QUARANTINED仍优先拒绝。

recover不接收批准ID：VERIFIED尚未绑定时返回BLOCKED/APPROVAL_MISMATCH（等待显式发布批准），不扫描批准目录挑选文件；APPROVED及之后只从有效注册链的不可变绑定读取批准，丢失/不一致拒绝。APPROVED记录追加失败无发布rename；重启若发现该记录完整但此前同步结果未知，先重新同步注册目录并读回验证，再继续；临时记录不晋升为正式记录。RESERVED/BUILDING中断不恢复构建，记FAILED并保留；不把完整候选外观当VERIFIED。

风险/对策/验证/回退：目录链接数误拒通过稳定DirectoryIdentity消除；证据丢失禁止合成；审批首次赋值与重入不可变分离。新增验收为test_directory_identity_lifecycle（合法子目录增减策略不变、inode/权限替换拒绝）、test_evidence_restart（创建进程退出后持久读回及丢失/损坏/同步故障拒绝）、test_initial_approval_binding（首次、同ID重入、换ID拒绝、记录失败零rename、未绑定恢复等待）。全部NOT_RUN；任一条件不成立保持BLOCKED，不删除文件。

### 4.3 独立终审整改：固定DDL及确定失败分类

DeploymentPolicy.expected_ddl_sha固定为`578f80789de98456324d430142631c7b7f984f80f3ffe30fc7c31d0634bdcd9d`，摘要对象是批准`api/_display_schema_ddl.py`内SQL常量的严格UTF-8字节，不含Python包装。构建前必须验证运行常量SHA等于该值；Manifest.ddl_sha及受信策略也必须等于该值。该值不能由当前实现运行时生成后自动填入策略。独立验收基准从既有批准coding-schema文档SQL fences恢复原样SQL并比对字节和目录，禁止用待测实现自身生成唯一预期。变更DDL必须单独设计/审批新版本，不允许自行刷新摘要。

失败分类优先级固定：注册链损坏、身份替换或源/目标互相矛盾属于QUARANTINED/STATE_CONFLICT（明确文件身份变化用IDENTITY_CHANGED），尽力追加隔离，写失败quarantine_persisted=false仍拒绝；最新FAILED/QUARANTINED按终态处理。可信审批文件缺失、内容/schema/hash/策略不匹配但候选及注册身份没有冲突，一律BLOCKED/APPROVAL_MISMATCH，不追加终态记录，允许满足原不可变绑定后重新验证；这不是允许替换已绑定审批。审批目录本身权限或身份不可信则BLOCKED/ACCESS_BOUNDARY_UNPROVEN，不访问其中内容。

BUILDING的DDL/空状态/完整性检查不合格一律追加FAILED并返回BLOCKED/UNKNOWN_SCHEMA；候选身份变化则优先隔离。BUILDING I/O或侧文件未归并一律FAILED，返回BLOCKED/IO_FAILURE或SIDECAR_REMAINS；审计记录无法持久则返回BLOCKED/AUDIT_UNAVAILABLE，保留可观察旧状态，恢复按中断构建失败处理。发布目录rename可能发生后I/O/同步不确定仍唯一UNCERTAIN，不追加FAILED；此时发现实际身份或状态冲突才隔离。追加APPROVED等记录失败不允许rename；不因记录可见但未同步而宣称持久成功。

新增验收test_pinned_ddl：替换SQL常量、策略预期值或Manifest.ddl_sha任一项都拒绝；test_failure_classification：同一输入只产生上述唯一结果，覆盖PUBLISH_INTENT/S无/T有/审批不匹配→BLOCKED且无新终态、修复为原匹配批准后允许恢复。风险是实现自证结构或将暂时审批缺失变成永久隔离；对策为受信固定值与确定分类，未满足保持拒绝，所有实验仍NOT_RUN。

## 5. CD-02：注册表、状态与恢复

介质选择：受保护registry_root下每候选一个目录，不新增业务SQLite表。记录以seq补零20位命名`00000000000000000001.json`，前一条规范字节SHA串链。原子追加：同目录O_EXCL临时文件→完整写/fsync→no-replace重命名为序号文件→fsync注册表目录→读回。临时文件残留不自动删除；记录缺号、重复冲突、hash不匹配即隔离。注册目录首次创建须同步父目录。记录追加失败禁止进入下一破坏性阶段；注册表不足以消除文件系统不确定状态。

| 原状态→新状态 | 唯一执行者/前置 | 持久动作及成功条件 | 失败 |
|---|---|---|---|
| NONE→RESERVED | 创建者，预算/保留数量合格 | 首记录持久化预算归属；目标名固定等于候选ID | 无候选；AUDIT_UNAVAILABLE |
| RESERVED→BUILDING | 同锁，新候选目录不存在 | mkdir（已存在即拒绝）创建目录、O_EXCL创建主库；身份读回；记录持久 | FAILED；不复用 |
| BUILDING→VERIFIED | DDL与完整性/空状态、WAL关闭、冻结复核通过 | manifest及候选目录同步，记录绑定manifest摘要 | 校验不合格FAILED；身份/串链冲突QUARANTINED |
| VERIFIED→APPROVED | 发布者，外部批准匹配 | 批准ID/摘要写注册记录并同步 | 保持VERIFIED/BLOCKED |
| APPROVED→PUBLISH_INTENT | 发布者，目标缺失、候选再次核验 | 意图记录先持久化；固定目标/候选/批准 | 无rename，BLOCKED |
| PUBLISH_INTENT→PUBLISHED_UNACTIVATED | 发布者 | 整目录no-replace rename；同步两父目录；完成记录持久；读回一致 | UNCERTAIN，不激活 |
| 任意未完成→FAILED | 可明确归因且未rename | 持久错误记录；禁止沿用创建/发布 | 保留文件 |
| 任意→QUARANTINED | 身份/状态冲突 | 尽力持久隔离标记；读回失败也禁止继续 | 人工处理 |

先验证注册串链，再取最大连续seq的最新记录判定状态：FAILED/QUARANTINED为不可恢复终态，在任何S/T/A/C组合下分别返回BLOCKED/STATE_CONFLICT或QUARANTINED/STATE_CONFLICT，不再进入发布恢复表；历史完成记录不得覆盖后续隔离。PUBLISHED_UNACTIVATED只可幂等读回，不改主库；如发现身份/状态冲突可追加QUARANTINED，随后永久以该最新终态为准。UNCERTAIN是调用结果；持久权威可能仍是PUBLISH_INTENT，恢复不能要求一定有UNCERTAIN记录。

恢复表入口先检查结构冲突：S/T同时存在、同时缺失，或最新完成状态但S有/T无，一律QUARANTINED/STATE_CONFLICT，优先于审批不匹配；无法可靠stat并非缺失，返回IO_FAILURE（发布结果可能不确定则UNCERTAIN），不猜存在性。剩余仅意图状态S有/T无或S无/T有、完成状态S无/T有进入审批行。恢复表中S=候选源目录，T=发布目标，A=批准记录精确匹配，C=最新有效记录是否为PUBLISHED_UNACTIVATED；历史完成记录不算C。只有最新状态为PUBLISH_INTENT或PUBLISHED_UNACTIVATED才进入下表，其余未完成状态必须按转换表处理，不能借文件组合跳级。所有判断先持新ctx锁并验证注册串链与文件身份；表覆盖发布意图及完成后的全部组合：

| S | T | A | C | 行为 |
|---|---|---|---|---|
| 无结构/身份冲突 | 无结构/身份冲突 | 无/不匹配 | 任意 | 仅在下述结构冲突预检通过后BLOCKED/APPROVAL_MISMATCH，不追加终态；不因C绕过审批 |
| 有 | 无 | 匹配 | 无 | 仅最新PUBLISH_INTENT允许重新验证后no-replace；禁止从较早状态或终态进入本行 |
| 无 | 有 | 匹配 | 无 | 仅PUBLISH_INTENT且目标manifest/身份匹配：重新同步两个父目录，追加完成记录、读回；否则隔离 |
| 无 | 有 | 匹配 | 有 | 最新完成记录ID/摘要与目标一致且后面没有任何有效记录，返回PUBLISHED_UNACTIVATED；不重新创建库 |
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
| contract/test_approval_binding | §4.1每条匹配等式分别破坏、未知审批根、请求自带批准dict | APPROVAL_MISMATCH；无rename，目标不变 |
| platform/test_publish_no_replace | 低层系统调用封装测试：两个自有目录竞争一个名称；另测预存sentinel | 原语只一次rename成功、另一EEXIST；预存sentinel不变。仅验证低层，不是合法不同候选同目标的业务场景 |
| platform/test_same_candidate_publish | 两真实进程发布同候选/同批准；第一持全局锁 | 第二LOCK_BUSY；首完成后重试返回同完成记录摘要，不再rename；不同候选指定同目标在输入/审批层拒绝 |
| recovery/test_completed_then_quarantined | 完成seq=n→隔离seq=n+1→文件重新匹配 | 仍QUARANTINED，零rename、无新成功记录，历史完成记录不能翻转终态 |
| contract/test_result_union | 未生成ID输入错误、LOCK_BUSY、VERIFIED、发布成功、隔离写失败 | 按§3唯一分支序列化；null仅用于允许字段，OK不出现在拒绝分支，摘要不混用 |
| contract/test_policy_digest_binding | 部署/资源/批准/manifest各摘要逐个变化，创建后策略更新 | APPROVAL_MISMATCH，零rename；旧候选不重签、不改manifest |
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

## 10. v3整改核对

V2-01：最新隔离/失败终态优先，历史完成记录不能复活候选；新增针对性恢复反例。V2-02：统一结果判别联合、早期失败null、成功OK及两个独立摘要；获取ctx和读取批准使用明确拒绝异常。V2-03：同候选业务幂等与低层不同目录no-replace分层，目标名约束不放宽。V2-04：定义DeploymentPolicy、可信路径及D/R/E/M匹配等式，策略变化拒绝旧批准。

本轮仅DOC_REVISED_PENDING_REVIEW；上述风险分别由新增测试规格验证，新测试全部NOT_RUN。没有功能实现或生产变更，不能把文档整改称为缺陷实测关闭或独立终审通过。
