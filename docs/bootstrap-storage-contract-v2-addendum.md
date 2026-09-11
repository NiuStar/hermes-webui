# Storage Contract V2：独立卷ID绑定补充设计

状态：方案设计PASS；编码设计PASS。独立复审deleg_cdf899f9确认两项冲突已消除，完整记录：/home/hermeswebui/.hermes/cache/delegation/live/deleg_cdf899f9/task-0.log。批准仅覆盖设计，不代表实现或运行验收通过。原r3批准保留。

## 问题与范围

r3的Resource V3删除hard_limit_profile_id并引入四角色映射；握手要求独立volume_profile_id/volume_profile_sha，但现有Storage Contract V1仅有volume_profile_sha。不能借用creator或当前runner ID，不能扫描目录选第一项，不能靠调用者JSON提供默认值。

## 精确契约

Storage Contract V2沿用V1全部字段，仅format_version改为严格整数2，新增必填volume_profile_id（原始32位小写hex）。未知字段、bool版本、畸形ID拒绝。完整canonical字节SHA仍由Deployment V3.storage_contract_sha绑定；storage_contract_id保持其原有含义。

可信解析：固定active→Deployment V3→固定storage-contracts/<storage_contract_id>.json→验证完整SHA及Contract V2→固定volumes/<volume_profile_id>.json→验证volume_profile_sha及该文件自身ID。

Volume Profile V1既有内部字段hard_limit_profile_id暂保留字面名称和原值，它在该文件中仅是历史卷记录ID；必须等于Contract V2.volume_profile_id，不得与任何runner ID隐式关联。此方案不改Volume Profile schema、不迁移旧卷记录。

Storage Contract V1继续可用于只读诊断，但V3可信上下文签发仅接受V2。拒绝自动升级、重签、替换旧文件或混写。若需要新策略，创建独立ID并由管理员显式绑定，旧媒体和审计全部保留。

## 编码落点（待本补充终审）

1. storage_contract.validate_contract显式识别V1/V2；V2精确新增字段。
2. storage模块新增固定策略解析辅助函数，返回不可变绑定；前后复读合同及卷记录。
3. acquire/revalidate、storage client/server/worker统一从此绑定取得卷ID及SHA，握手声明只用于比较，不能选择另一卷。
4. verify_fixed_volumes的V3调用显式传profile_id，拒绝缺省。runner_binding仍只处理四角色运行器，不加入卷别名。
5. 客户端观察结果schema仍需独立严格验证observer_self、双视角拓扑和journal，并完成真实存储验收绑定后方可签发。

## 编码设计整改A：固定绑定接口及句柄

新增`hold_storage_binding(policy_id, *, deadline)`上下文管理器，仅接受严格32位小写policy_id，自行校验固定active、anchor、Deployment V3和resource，禁止接收请求卷ID或任意config字典作为选择来源。worker外层持全局锁；observer不重复取锁（避免握手死锁），须持worker pidfd并执行同样前后复核。

返回handle：binding为冻结标量结构，包含deployment_sha、resource_sha、storage_contract_id、storage_contract_sha、volume_profile_id、volume_profile_sha。解析快照不暴露可变引用。合同自身storage_contract_id必须等于deployment引用；合同V2及完整SHA通过后才按合同volume_profile_id读取卷，卷自身hard_limit_profile_id须等于该ID且完整SHA相等。acceptance自身report_id、完整SHA、code_commit/kernel_release/volume_profile_sha及topology_sha均须与合同一致。全部比较通过前不得打开设备或观测journal。

handle用ExitStack持有固定根和部署/resource、storage-contracts、volumes、storage-acceptance目录FD至作用域结束；文件用O_NOFOLLOW/O_NONBLOCK/O_CLOEXEC，检查普通文件、root所有、无组/其他写、nlink=1、记录大小上限。读取前后fstat及父目录下无链接stat一致。记录文件dev/ino/uid/gid/mode/nlink/size/mtime_ns/ctime_ns、原始字节SHA；目录记录dev/ino/uid/gid/mode。

`handle.revalidate()`从固定绝对路径重新逐级安全打开并比较命名目录与持有FD身份，然后从持有FD复读全部记录，比较文件身份、原始字节及SHA，重新验证引用关系。字节相同但inode变化也拒绝。共享绝对deadline；任何失败关闭自有FD，不关闭调用者FD；关闭后的handle不可用。不能声称检测到两次观察间被恶意root替换又还原的文件。

acquire/revalidate/client/worker/server共用此解析器，不再以旧_read充当绑定边界。握手卷声明只用于比较，不能选卷；runner/observer声明分别与固定resource/deployment比较。server全握手和观测期间持handle；client持到最终校验和上下文签发，成功后BootstrapContext接管至close。不得返回半初始化对象。

## 编码设计整改B：固定结果校验与顺序

新增`validate_storage_observation(observation, *, storage_handle, observer_evidence, local_evidence, deadline)`固定校验器，由storage client内部固定调用，删除可信client公开的任意validate_observation回调。协议层测试注入不能用于签发。observer_evidence必须由本次内核凭据、pidfd及system manager复核生成；local_evidence必须由本进程持有的registry/image FD和实时mountinfo生成，不从请求JSON传入。

所有嵌套对象严格字段集，未知/缺失字段拒绝；bool不作为整数，遵守帧大小上限。完整顶层为loop、host、views、observer_self，具体schema在附表定义；比较必须使用handle持有的同一合同、卷、acceptance及本次独立observer证据。

调用顺序：全局锁/active/anchor→角色及runner限制→hold_storage_binding和acceptance校验→持本地根FD并生成实时证据→HELLO→READY→OBSERVE→RESULT摘要/凭据及固定结果校验→ACK前最终pidfd/systemd、策略与证据复核→ACK→共享绝对deadline内的有界EOF清理并关闭socket/pidfd→handle及本地拓扑、角色、runner复核→audit/quota独立门→不可逆限制→限制后既定只读复核→最后deadline及绑定复核→签发并转移handle所有权。限制后若seccomp/DAC阻止必需复核则拒绝签发，不用缓存替代；若与既定限制设计冲突则保持BLOCKED并整改复审。

初次ID/SHA/acceptance不匹配APPROVAL_MISMATCH；持有期间路径/文件/进程变化IDENTITY_CHANGED；schema错误INVALID_INPUT；真实性无法证明ACCESS_BOUNDARY_UNPROVEN；平台不支持UNSUPPORTED_PLATFORM；deadline/预算沿用原错误码。任何异常不得返回可信上下文。

反例需覆盖：同字节不同inode、目录替换、合同自身ID不符、握手改卷、全部嵌套字段缺失/额外/bool整数、target只读、跨绑定journal、最终观测后、本地签发前绑定变化、限制后不能复核。用设备观察计数证明前置ID/SHA错误触发零设备调用；执行仅在.10统一测试阶段。

EOF仅作为清理边界，不承载新证据，不要求EOF后observer仍存活；不得在ACK后重复要求observer活体检查。进入不可逆写限制前必须关闭握手socket和pidfd，其他非普通文件/目录描述符按既有write_guard检查拒绝。无FINAL帧，不增加协议版本或第六帧。

## 观测schema及比较附表

所有整数均严格int（非bool），安全JSON整数上限2^53-1；u32为0..2^32-1。UUID为非零规范小写UUID，不自动修正。以下为精确字段列表。

|对象|字段、类型及比较|
|---|---|
|loop、host|各精确device/fs_uuid/block_bytes/blocks/internal_journal/journal_inode/errors/state/needs_recovery/features/inode_bytes/blocks_per_group/inodes_per_group/journal_uuid/journal_block_bytes/journal_max_blocks/journal_features。device精确major/minor两个u32；两个UUID严格且相等。两个block_bytes严格4096，internal_journal严格true，errors严格0，state为0或1。blocks为正安全整数；journal_inode、blocks_per_group、inodes_per_group、journal_max_blocks为正u32；inode_bytes为128..4096的2次幂。features/journal_features精确compat/incompat/readonly三个u32。needs_recovery严格bool并与features.incompat的0x4位一致。device/fs_uuid分别等于同一合同loop/host期望，loop.blocks*4096不得超过卷audit.image_bytes。沿用decoder不支持特性拒绝规则；解析成功不替代acceptance绑定。|
|views|精确loop/host，各精确target/observer。每个mount精确mount_id/device/root/target/filesystem/options：mount_id正安全整数；device如上；root/target为绝对路径字符串无NUL且UTF-8≤4096字节；filesystem严格ext4；options为非空字符串数组，每项≤256字节、无NUL、无重复且已排序、至多256项。root严格/。target对象必须与客户端实时FD对应mount对象完全相同；loop/host目标mount_id及device分别与合同一致；target必须rw且禁止ro，loop须包含required_mount_options。observer.device/root/target/filesystem须与target相同；observer mount_id允许不同；observer.options允许ro但不放宽target。|
|observer_self|精确user_namespace/cgroup_namespace/mount_namespace/executable_identity。namespace为user/cgroup/mnt:[数字]对应格式，≤128字节；user/cgroup与已验证profile相等。mount namespace本次前后稳定，不要求等于worker。executable_identity按profile的path/sha/identity严格schema验证且完全相等；这是self证据，不替代pidfd/内核凭据/system manager证据。|

local_evidence另须复核loop backing路径、identity、bytes、无xattr及全分配、offset/sizelimit、host直接磁盘及分区起点与同一合同/卷一致，禁止仅比较mount ID。acceptance使用既有validate_acceptance，不变更其schema；全部既定checks必须存在且不允许额外项，每项严格为{status, evidence_sha}对象，status严格为字符串"PASS"，evidence_sha为原始64位小写hex；report自身ID、SHA及topology_sha绑定同一合同。本次实际code_commit、内核版本和worker namespace必须与acceptance/合同相等；发行code_commit取既有受保护发行身份，不从observer JSON取值。未建立实际发行身份时拒绝签发。

## 风险、对策、验证

- 错卷或runner ID混用：独立字段+完整SHA链；测试相同runner、不同卷ID，错误ID在设备读取前拒绝。
- 校验后替换：持受保护目录句柄，前后读回合同及卷记录；替换反例要求IDENTITY_CHANGED/APPROVAL_MISMATCH。
- 旧记录误准入：V3签发强制Contract V2，V1仅诊断；版本矩阵覆盖V1/V2合同、V1卷、V3部署及错误SHA。
- bool/未知字段/大小写修复：严格验证原值，反例覆盖每项，不规范化错误标识。
- 模块通过被当完整验收：仅.10在全部编码完成后统一测试，真实非root角色、Accept=yes服务、配额与限制、发布恢复联合验收，随后全量回归及独立代码终审。

## 回退与授权边界

维持现有可信上下文阻断；不改系统配置、不安装服务、不执行SQL、不迁移或删除数据库/镜像/审计、不切生产。拒绝新合同不触发旧合同写入降级。此文档为方向确认后的设计补充，尚待独立终审，不能据此宣称全部编码完成。
