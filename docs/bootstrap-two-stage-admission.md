# 两阶段内部准入补充设计

状态：整改独立复审deleg_1ec67b59方案PASS、编码设计PASS；四项前次阻断均解除，无必须整改项。复审记录：/home/hermeswebui/.hermes/cache/delegation/live/deleg_1ec67b59/task-0.log。此结论仅批准设计，不代表实现或运行验收通过。原r3及Storage Contract V2的批准保持有效。本补充仅解决候选准备与限制后签发的循环，不解除运行准入。

## 现存冲突

storage-contract-v2-addendum要求配额、审计门及不可逆限制在BootstrapContext签发之前。当前command先acquire_bootstrap_context，fixed_lifecycle.create再创建候选目录、预留audit.bin、安装quota和write_guard，形成循环。不能删除最后拒绝或伪造候选边界。

## 方案

command改为调用单一execute_operation(policy_id, operation, candidate_id=None, approval_id=None)，不向命令调用方暴露准备对象。内部私有_Preparation只拥有固定锁、策略/存储句柄、目录FD、角色和共享deadline；不是BootstrapContext，不支持build、publish、成功结果或通用写入方法。仅execute_operation内的固定准备步骤可使用它。准备阶段依据现有可信身份/DAC及系统级硬限制运行；候选级写限制尚未安装这一事实不得伪装。

create顺序：验证policy/角色/真实runner/发行身份/Storage V2及观察→以系统随机ID建立现有固定审计预留与候选目录→记录已准备身份→通过真实quota服务分配并复核→安装候选固定写边界→限制后只读策略/卷/审计/身份/配额复核→签发BootstrapContext→既有BUILDING、建库、VERIFIED流程。

publish/recover必须携带严格candidate_id；publish另携approval_id。先定位并持久性确认完整V2审计，按持久化状态分流。RESERVED/BUILDING只允许recover进入AUDIT_TERMINATE模式，不要求候选目录、quota或审批已经存在；安装仅该audit.bin追加能力的边界后，限制后完整复扫与原状态相等，才允许追加既有失败事件，不授予build/publish能力。FAILED/QUARANTINED仅返回既有真实状态，不追加。其余发布推进分支才要求完整候选、quota及审批；recover的审批ID仅取已持久化记录，缺失则BLOCKED。缺失、损坏、无完整首记录的审计一律保留现场并BLOCKED，不补造状态。approver仍走独立审批入口。

## 编码接口与资源所有权

新增display_bootstrap_admission.execute_operation为命令唯一入口；内部_prepare(policy_id, role, deadline)仅由此调用，_Preparation禁止序列化、跨PID、重复消费；构造函数不公开，close幂等，所有权由ExitStack管理。成功签发时通过一次性transfer把锁、目录和storage handle交给BootstrapContext；失败关闭全部自有FD。上下文不持socket/pidfd，这些必须在ACK/EOF清理后、写限制前关闭。

将fixed_lifecycle.create拆成prepare_create与run_create：准备函数只创建现有协议所需候选/审计和quota，不建库、不产生VERIFIED；run_create接受已限制的上下文、候选ID和已准备目录，不再次创建或安装边界。publish/recover同样分离准备期扫描及持久性确认与限制后的状态追加；准备期允许对已绑定audit.bin及其审计目录执行fsync，明确这不是纯读取，也不允许追加、截断、修复或创建。fsync失败即BLOCKED；限制安装后再次fsync及完整复扫，与准备快照不符即拒绝签发。审批的读取校验在准备期执行，正式写入前仍复核固定审批记录。

BootstrapContext._issue仅接受已完成限制后验证的一次性内部receipt；receipt绑定PID、role、candidate_id、部署/资源/卷SHA、目录身份、audit身份、quota证据、限制结果和原绝对deadline。receipt不是磁盘/JSON授权令牌；拒绝外部dict冒充、跨候选/角色/PID复用及二次消费。_Preparation本身不进入生命周期公开接口。

所有阶段使用入口monotonic开始计算的同一deadline，不因准备/签发重新计时。失败准备不删除目录、数据库、审计或quota记录；只在现有已合法打开且允许写的审计范围记录既定失败事件。无法落审计则返回真实BLOCKED，不伪造序列、成功或隔离持久化。重试必须显式恢复既有candidate_id，不重新解释残留为新建成功。

## 发行身份

既有_creator_commit仅git rev-parse HEAD，不证明执行源码等于提交。本补充要求准备阶段从实际worker发行目录取得提交并校验执行项目文件与该提交、受保护目录及无链接规则一致，拒绝脏/未跟踪可导入源码和替代Git环境；与acceptance.code_commit比较。实际发行目录无可验证提交对象时失败关闭，不取报告值作为自身证据。具体接口与限制后复核见下节；禁止另调普通git HEAD代替已验证结果。

## 整改：发行校验handle

`hold_worker_release(*, deadline)`不接受请求路径或commit。可信根从实际已加载的worker入口及admission模块__file__逐级无链接定位，两者必须属于同一root-owned、无非root写/ACL的发行根；校验sys.argv[0]对应固定scripts/bootstrap_worker_entry.py，拒绝其他入口。固定release/api与release/scripts中的所有文件均须被提交覆盖，包含__init__.py、入口及资源文件；拒绝软链接、子模块、嵌套仓库、__pycache__、未跟踪文件和额外可导入扩展。sys.path仅允许该发行根及隔离Python的受信系统标准库路径，禁止第二个项目导入根。

handle使用固定/usr/bin/git、显式--git-dir=<root>/.git及--work-tree=<root>，env仅PATH=/usr/bin:/bin、LC_ALL=C、GIT_CONFIG_NOSYSTEM=1、GIT_CONFIG_GLOBAL=/dev/null、GIT_NO_REPLACE_OBJECTS=1、GIT_OPTIONAL_LOCKS=0，不继承HOME/GIT_*。.git必须为同根受保护真实目录，拒绝commondir、alternates、grafts、replace refs、外置worktree及本地config中的include、filter、fsmonitor或外部命令配置。所有子进程输出有界并共用deadline；不执行hooks、checkout、clean或文本转换。

在限制前读取HEAD提交对象及递归tree（ls-tree -rz --full-tree），只接纳普通blob的100644/100755模式；逐个cat-file读取原始blob，不使用工作区diff或过滤器，比较所覆盖文件的原始字节与执行权限。受保护FD读取前后及命名路径比较dev/ino/uid/gid/mode/nlink/size/mtime_ns/ctime_ns；提交OID必须来自验证成功的提交对象并与acceptance.code_commit一致。执行文件覆盖集合和受保护目录身份一并保存。提交/树/blob对象按Git对象头和实际仓库hash算法重新计算OID验证，算法仅sha1/sha256，拒绝未知算法。

handle.commit为不可变验证结果；`handle.revalidate()`在限制后不启动Git/子进程，仅以无链接只读方式复读受保护执行文件、覆盖目录成员及已固定Git元数据文件，比较身份和原始SHA，且重新校验固定发行根路径。Git提交对象在限制前已验证并绑定执行字节，限制后不以新HEAD替换结果；HEAD/ref/config变化拒绝。自有目录/文件FD由ExitStack持有，成功转移至正式上下文，否则关闭；只读复核在真实限制环境不可达即BLOCKED。_creator_commit删除独立HEAD查询，改为从正式上下文release_handle.commit取值。

## 整改：私有准备函数、receipt与转移

以下函数只在display_bootstrap_admission内部被execute_operation调用，全部首参严格type为_Preparation且验证本进程登记、PID、未关闭、未消费及deadline；不使用Context duck typing，不导出通用读写方法：

- `_quota_prepare(p, candidate_id, candidate_fd, *, allocate)`：ID必须等于p固定ID、FD身份等于准备快照；allocate仅create。内部直接调用抽离的quota传输函数，固定输入为p的部署/资源/runner绑定、角色、目录身份、max_bytes及deadline，不接受外部策略。传输函数只返回已核验响应，不赋予上下文权限。
- `_scan_audit(p, audit_fd, *, confirm_durable)`：只使用已验证配置和audit身份构造AuditFile，校验完整V2头、链、actor及容量；prepare必须confirm_durable=True，允许fsync但不追加。不调用需要Context的open_registry适配器。
- `_install_boundary(p, *, mode, candidate_fd, audit_fd)`：mode精确CREATE/PUBLISH/AUDIT_TERMINATE，由状态分流固定而非CLI选择。CREATE允许候选及固定audit写，PUBLISH允许既有发布操作及固定audit写，AUDIT_TERMINATE仅允许既有audit.bin写，不允许候选/发布目录写或创建。调用抽离的底层Landlock/seccomp安装器，不调用Context方法；任何部分安装失败标记p不可复用并终止操作。
- `_verify_prepared(p, boundary)`：直接复核角色、固定策略/storage/release handles、目录身份、实际限制与audit边界；CREATE/PUBLISH另复核quota，AUDIT_TERMINATE不要求候选存在；所有模式确认audit持久性并完整复扫。验证函数不追加状态、不创建文件。结果包含固定mode和已验证证据。
- `_seal(p, verified)`：仅由上述验证成功路径调用；创建无公共构造器的Receipt对象，在模块私有登记表以对象身份登记p和冻结证据，禁止JSON/dict替代。绑定PID、mode、role、candidate_id、SHA、deadline及全部FD身份；AUDIT_TERMINATE明确没有quota证据，不伪造PASS。
- `_consume_and_transfer(receipt)`：当前单线程进程内先以对象身份pop登记（先消费后动作），复核PID/deadline和所有权，再通过p的ExitStack.pop_all转移到局部owner；构建上下文成功才把owner交给上下文。转移前失败由p.close清理；转移后失败仅由局部owner.close清理；转移成功后p清空所有FD引用且标记consumed，不能再清理已转移资源。消费任一步失败receipt永久作废，不回填登记。

BootstrapContext._issue仅接受已登记Receipt，内部调用消费流程；旧位置参数签发与旧acquire_bootstrap_context公开入口继续拒绝。正常上下文绑定CREATE或PUBLISH操作和唯一候选；AUDIT_TERMINATE使用独立私有审计终结对象而非BootstrapContext，只提供固定追加既有失败事件一次的内部操作，执行前再次核验模式和状态，不能传入任意事件或调用build/publish。审计终结后只返回既有BLOCKED结果及真实失败序列，不宣称候选已修复。

## 风险、验证、回退

- 准备对象扩权：只在唯一编排函数内可达，生命周期入口拒绝它；测试所有构建/发布入口在无正式receipt时拒绝。
- 半准备失败：每个mkdir、audit预留、quota分配、限制安装及签发点注入错误；保留现场且无成功输出，不清理用户数据。
- 跨候选/角色及重复消费：receipt矩阵反例全部拒绝，PID改变和deadline超时拒绝。
- ACK后observer退出：沿用r3，不在EOF后要求活体；进入限制前确认socket/pidfd关闭。
- 限制后读取被拒绝：真实非root角色在.10隔离目录/端口实测；任何必要复核不能执行即BLOCKED，不回退缓存。
- 恢复覆盖：RESERVED、BUILDING、VERIFIED、发布前后故障按现有持久化状态机验证，不新增隐式迁移或自动删除。

全部编码完成后仅.10统一测试、联合验收、全量回归、独立代码终审，再提交推送读回；此文不授权安装正式服务、生产切换、SQL或删除数据。回退为继续关闭准入并保留所有既有媒体，不恢复旧schema写通道。
