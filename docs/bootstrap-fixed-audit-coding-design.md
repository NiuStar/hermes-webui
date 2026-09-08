# 固定审计日志编码设计 v1

状态：CODING_DESIGN_PENDING_REVIEW。用户已明确方案终审通过；本文件据此进入编码设计，不把方案认可当作编码设计终审。前置方案：bootstrap-fixed-audit-design-v2.md，SHA256 5e740cd117aa66131ffaee133191039ddae2790700aa9c315335c2072458f61a。原方案文件保持历史原文。

## 1. 文件格式与预算

文件名固定audit.bin，位于registry_root/<candidate_id>/，目录仅允许该文件；旧JSON/evidence/.audit-reserve存在即拒绝新格式写入。初始化先O_EXCL、0600、无跟随创建；所有文件/目录属于creator且无ACL，单链接。

- 头部4096字节。槽73728字节：领取控制区4096、payload区65536、提交控制区4096。
- 槽数N必须为9..4096的严格整数。文件物理预算B=4096+N*73728，不向下取整。最小667648，最大301993984字节（工具计算）。首版文件系统块大小须为4096，其他拒绝，避免静默忽略分配取整。
- 资源策略显式audit_format=FIXED_LOG_V1、audit_slot_count=N、audit_reserve_bytes=B。旧资源规则只用于只读诊断。
- 每次写在offset/length检查之后执行，所有部分写循环推进；0或负返回按EIO。写前后核验文件大小恒为B、dev/ino/uid/gid/mode/nlink及命名身份；无法确认停止。模块内部可写FD不对外返回。

### 头部

固定前缀为8字节ASCII magic `HBAUD001`，其后uint32小端规范JSON长度L，再后L字节规范JSON，之后零填充至偏移4064；最后32字节为SHA256(前4064字节)。L为1..4052。头部JSON精确字段：format_version=1、candidate_id、file_identity、deployment_sha、resource_sha、slot_count、slot_bytes=73728、payload_bytes=65536。identity使用既有file_identity字段且nlink=1。

先写实整个B字节并fsync文件/目录，再写头部前4064字节并fsync，再写头部SHA并fsync并读回。头部缺失、损坏、不符政策即AUDIT_UNAVAILABLE，不能重初始化。头部提交不是硬件原子写承诺。

### 槽控制区

控制区前136字节按 `<8sIIII16s32s32s32s` 编码：magic、format_version、slot_index、kind、payload_length、candidate_id原始16字节、logical_key原始32字节、payload_sha、header_sha。剩余填零至4064，最后32字节SHA256(前4064字节)。magic领取为`HBCLM001`，提交为`HBCMT001`。提交所有字段必须与领取一致，只有magic及由此产生的控制区SHA不同。

kind：1=registry，2=platform_evidence，3=failure_evidence。payload为严格规范JSON，长度1..65536；其余payload区全零。candidate/logical_key的hex只允许原始小写合法格式解码，不修复。
logical_key：registry使用SHA256(ASCII `registry:` + 20位零填充十进制seq)；两种证据均为规范payload的SHA256，但去重键包含kind，不能把不同kind当同一记录。

槽摘要绑定header_sha，使控制区和payload不能跨文件移植。物理控制摘要与manifest中的逻辑JSON SHA不同；发布结果completion_record_sha仍指逻辑registry JSON。

## 2. 写读状态机

准备：全文件分配+写零+同步→头部提交读回→完整零尾检查→RESERVED→候选配额准备。全部准备可写FD关闭后才能安装Landlock。
追加：扫描全部容器确认完整前缀+全零尾→核验输入及逻辑链→确认首次全零槽→领取区写/fsync/读回→payload写/fsync/读回→提交区写/fsync/读回整槽。不能缓存slot_count绕过重新验证。
任何非零未完整提交槽，或零槽之后出现非零，整容器只读阻断；不跳过、不自动补写、不在同进程重试。异常后writer标记不可再写；退出不自动删除。
恢复看到完整有效提交前缀但无法知道上一进程fsync结果时，先对既有文件fsync并重读，才将其作为可恢复证据。fsync失败返回AUDIT_UNAVAILABLE；已移动目录的操作返回UNCERTAIN，保留具体诊断在非权威命令错误通道，不伪造新的registry状态。
已提交记录语义错误、重复kind/logical_key、重复seq/不连续/错误previous_sha、终态后新增记录均拒绝。失败证据先于失败状态，平台证据先于VERIFIED。失败证据提交成功但下一条状态失败仍属审计不完整，不以证据代替终态。
全零回滚无法由单容器检测，保持已批准故障模型边界，不新增隐式见证机制。

## 3. 模块与API

新增api/display_bootstrap_audit_log.py：
- layout(resource)->Layout：严格预算校验，无I/O。
- prepare(registry_fd, candidate_id, deployment_sha, resource)->AuditIdentity：一次准备、验证、关闭可写FD，仅返回不可变身份。
- open_readonly(registry_fd, expected)->AuditReader：context manager，只读FD；scan逐槽流式读取，不累积整个文件。
- open_writer(registry_fd, expected)->AuditWriter：只能在调用者已完成权限边界后使用；模块检查当前身份、布局、进程所有权；不把调用者布尔值当内核证明。
- reader.records(kind)/reader.get(kind, logical_key)：最多N条的有界迭代/查找。
- writer.append(kind, canonical_payload)->Receipt：返回logical_sha、physical_slot、control_sha，必须完整同步读回；不返回可写FD。
- writer.remaining_slots()/confirm_durable()；close可重复，跨pid只能关闭继承FD不得写入。

改display_bootstrap_publish.read_registry为显式reader参数；提取公共validate_registry_chain供旧只读诊断及新介质共同使用。普通write_record只保留候选manifest与外部审批独立入口，新审计不经旧reserve_fd路径回退。
改lifecycle._write_audit/_persist_evidence/_append/_creation_failure为writer/reader接口；协议层仍校验D/R/E/M。reader首次扫描失败不得尝试追加隔离记录，quarantine_persisted=false。

## 4. 策略版本与审批

DeploymentPolicy V2：保留V1字段但format_version=2，新增storage_contract_id、storage_contract_sha、approval_policy_id、approval_policy_sha。active仍用原ID和整个DeploymentPolicy SHA；未知版本拒绝，不能自动升级。
ResourcePolicy V2：原字段保留，format_version=2，新增audit_format和audit_slot_count；audit_reserve_bytes按第1节精确验证。Manifest V2新增audit_format和audit_header_sha，format_version=2，其他逻辑摘要不变。registry、platform_evidence、approval原有逻辑版本1保留，由新部署版本和manifest绑定新介质。

ApprovalPolicy固定路径 /etc/hermes-display-bootstrap/approval-policies/<id>.json，root只写，creator只读；字段：format_version=1、approval_policy_id、approver_uid、approval_root_identity、max_record_bytes、max_retained_records、min_free_bytes、max_total_allocated_bytes。正整数严格范围；max_record_bytes<=65536。
独立approver入口：持同一固定锁→读active与审批策略SHA→核对真实approver UID/GID/权限→统计批准文件及失败临时占用→检查预算→显式外部批准输入→无覆盖创建临时、写完整、fsync、rename_no_replace、目录sync、读回。所有残留计费不删。预算是独立审批准入/计量，不冒充已预分配creator审计。审批写失败无有效批准、publisher不得移动；它的容量故障不应阻止已有有效批准的只读核验。creator不能调用此入口签发。
审批policy不含部署SHA避免自哈希循环；Deployment绑定审批SHA，审批记录绑定Deployment SHA。计数扫描有上限，超限拒绝而非截断后作判断。

## 5. 准入及句柄所有权

acquire开始即取monotonic起点；打开root并持锁；解析active/deployment/resource/approval/storage策略；验证SHA、UID/根、平台、DDL、硬限制及seccomp、存储。任何失败finally按所有权关闭。构造evidence，最后复读active和所有根；用_issue(..., started_monotonic=原起点)转移一次所有权，局部句柄容器置空，finally不关闭已转移句柄。
签发不执行日志准备/不创建DB，Context仅能进入受控准备。check_owner只管pid/closed，operation_deadline管超期；不能让close依赖已超期预算。

creator：prepare日志并RESERVED→配额→关闭writer→审计O_PATH只读身份→检查全部FD→安装文件级WRITE_FILE与候选目录权限→核验限制→重开writer→BUILDING。
publisher/recover：只读打开并验证容器→安装审计文件写边界与目录移动权限→需要写时重开writer；只读完成确认不要求可用槽。所有角色都保持全局锁到结果完成。
受限后revalidate拆为verify_dac_configuration（身份/mode/ACL及静态可信祖先）与verify_effective_access_before_confinement；后者仅装载前执行。装载后检查同一进程的不可逆限制安装记录及实际内核拒绝探测，不把标志位单独当证明。systemd/cgroup/配额每次实时读，无缓存放行。测试若总线或执行通道被限制则环境BLOCKED，不绕过。

## 6. 空间与状态余量

新建检查候选卷min_free+max_candidate与审计卷min_free+B，inode及保留数分别检查；不要跨卷叠加无关预算。准备后audit只检查实际分配和完整槽，不要求相同额度空闲空间。
剩余槽：VERIFIED发布至少5，APPROVED至少4，INTENT至少3，合法失败收尾至少2，完整发布只读确认0。BUILDING进入前保证平台证据、VERIFIED及失败余量；每次追加前再次核验。
候选数据库与manifest总逻辑/分配字节受原配额及计量；审计文件不计入candidate_bytes。满盘失败收尾不走正常推进的free检查，但仍有身份、完整性、槽数和同一硬时限。

## 7. 错误与回退

输入/布局不合法INVALID_INPUT；配置缺失POLICY_MISSING；策略绑定变化APPROVAL_MISMATCH；平台不能证明UNSUPPORTED_PLATFORM或ACCESS_BOUNDARY_UNPROVEN；超预算RESOURCE_LIMIT；容器撕裂/容量不足/审计sync失败AUDIT_UNAVAILABLE；文件替换IDENTITY_CHANGED。移动已发生且无可信完成则UNCERTAIN，不因为日志写不了伪造FAILED。所有失败保留文件，旧格式只读诊断，不重用ID。

## 8. 必需测试与门禁

新增codec向量测试（独立struct解码）、header/控制区/payload逐字段损坏、短写/零写/EIO、各fsync进程中断、非零尾/重复键/终态损坏、预分配大小与FIEMAP反例、拒绝旧新混写、跨pid/FD关闭、满盘/满inode失败审计、0槽完成确认、审批残留计费、真实creator/publisher/recover通道、准入每一步异常所有权。
所有功能编码完成后仅.10统一测试→联合验收→全量回归→独立终审→代码提交读回；协议mock和真实OS结果分开。断电未测不得以kill模拟替代。

## 9. 编码设计尚需终审解决的明确OPEN

1. storage_contract完整JSON字段与真实拓扑/FIEMAP/journal检查实现接口尚需细化；不得凭第4节新增SHA字段即签发。
2. Landlock装载后安全拒绝探测的具体无副作用系统调用及其与systemd子进程的兼容性尚需定稿。
3. 独立approver真实GID、组权限及审批预算核验器复用边界需补全。

本文件为编码设计首稿，不宣称上述OPEN已关闭。风险与对策：缺少可执行准入条件→不签发，验证由负对照和真实进程完成，回退为保持旧格式只读与新格式禁用。未改功能代码，未运行测试或部署。
