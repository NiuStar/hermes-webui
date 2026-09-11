# 有界增长实现状态（未完成，禁止生产接入）

本记录区分已写组件与完整运行链，不能当作验收报告。最终设计基线为v7映射、v8修订及v9长度证明。

## 已写入，尚未统一运行测试
- display_bootstrap_v4.py：资源V4精确schema、registry V2、整数边界。
- manifest及audit codec/scan/fixed registry：新record分派、旧codec拒绝V2、跨状态request_sha一致性、状态追加保留摘要。
- storage_contract/storage/storage_binding：新scope和七类证据、两个准入消费者拒绝旧耗尽报告；V1只读。
- growth_math：逐FS保守重复预留和严格余量计算。
- growth_authority：受保护authority读回与每操作独立open的flock；仅原语，不证明host_root是实际FS根。
- growth_fds：有界SCM_RIGHTS接收及规定目录/锁类型检查；尚未接quota业务通道。
- growth_inventory：受控根名称、大小、身份、悬挂和重复检查；尚缺内容关联、静态根总额和observer证据接线。
- growth_lease：内部不可序列化lease、PID/starttime、过期、转移/撤销；尚未由可信预算管理者接入，不能把回调当独立准入证据。
- output_guard：角色syscall过滤原语；尚未在正确启动时序安装，不能视为实际日志隔离。
- start_limiter：启动窗口和第六次请求拒绝；尚未接PID1控制入口。
- test_budget/test_cgroup：历史预算与旧cgroup检查原语；尚无完整固定service执行器。
- 两个新测试文件：bounded_growth_contracts、growth_math，已编写但未执行。

## 必须完成的运行接线
1. 受信root launcher：明确启动信道FD清单、root配置只读FD交付、逐操作authority/deployment锁顺序、已认证预连接RPC、各角色mount/proc/FD隔离及全进程就绪门。现有非root_prepare仍按路径读配置，quota每次新建连接，worker命令行含业务参数，不能直接安装新输出filter。
2. 可信增长管理者：完整FS/UUID及backing证据、静态对象清点、ledger内容关联、锁内观测、lease签发/刷新/receipt转移，每个增长点与rename前门禁。
3. quota双端：固定三个业务FD、同OFD外层锁保留到响应完成、内核inode quota及完整预算复核；禁止只替换版本号放宽准入。
4. create稳定operation_id=CID及request_sha；approval_id=CID、所有状态生产者、恢复/终态读回；目前旧create仍随机生成CID。
5. 固定测试service、全部后代监管、日志/产物总额与单批峰值；删除/扩卷未经授权禁止。
6. 全部编码后仅在测试主机隔离环境统一正常规模验证，再做联合验收、全仓回归、独立代码终审与提交读回。

## 实际验证
- 当前若干批次write_file/patch语法lint成功，最近一次全bootstrap AST检查141文件成功；后续growth_lease另通过写入语法lint。AST不是行为测试。
- 测试主机只读检查：固定hermes-bootstrap-tests.service为not-found；没有启动测试或安装service。
- 未功能验收、未部署、未提交推送。新V4真实主链尚未开通，保留现有版本拒绝门，不能声称全部编码完成。

## 本轮续编与真实阻断
- authority锁新增实际mount point的FD/inode比对，拒绝将同挂载下的普通子目录冒充FS根。
- create CLI及worker renderer要求显式candidate_id；admission传递稳定CID；V4 RESERVED构造绑定规范请求SHA。尚未完成同CID重试/预算/launcher全链，不宣称完成第4项。
- 测试主机单次无文件内核探针：子进程实际UID65534，预创建socket的SO_PEERCRED仍为UID0。证明不能直接以root预连接FD认证后续降权worker。
- 该冲突涉及受审启动时序或认证协议变更。候选修订及风险见workspace计划bootstrap-bounded-growth-peer-identity-blocker.md；未弱化认证、未部署，继续保留V4准入门。

## V11续编（尚未接通，禁止验收冒充）
- 新增observer_wire/session/capacity：V3严格帧、同连接重复观测、摘要/序号、CLOSE半关闭、容量包装及time namespace核验。未接实际launcher。
- credential_stream新增strict模式，预配置PASSCRED、SO_PEERCRED固定身份、单字节recvmsg；sendmsg发送、绝对期限无最小超时下限。
- GrowthLease改为请求起点+1秒，增长管理者撤销旧lease后刷新。目录创建、审计预留、DB创建/WAL/建表/checkpoint、manifest、rename增加V4拒绝门。完整永久清点及receipt转移仍未接通。
- 新增启动control/FD/身份降权/固定端点连接原语；output_guard补完整派生/身份/mount API禁止。尚未有完整root launcher执行器。
- quota_channel实现8次长连接信封及3FD保留到响应结束；未替换实际QuotaHandler/内核预算调用。
- 新增held_cgroup、测试unit renderer和日志有界执行器；测试entry未实现，renderer拒绝缺失entry，未安装service。
- worker/quota/observer模板关闭stdout/stderr journal和core；旧observer仍是Accept=yes实例，不符合V3固定发送者身份，不得作为V3使用。
- 新增observer协议与脚本peer回归用例，未执行。中途AST 154文件通过；不等于最终行为验证。

## 继续接线需要解决的实际冲突
既有worker/observer/quota身份与runner校验会调用busctl/systemctl子进程。最终filter禁止fork/exec，mount边界禁止D-Bus；V11又要求每帧持续核验runner。不能直接沿旧hold_worker/hold_observer，也不能只核验一次后缓存PASS。新增LivePeer只负责预开proc/cgroup FD的即时验证，unit_verify仍须真实可信证据；没有默认true。现有设计没有完整定义运行期PID1证据如何跨此边界提供；必须明确该控制路径并独立复审，不能偷偷开放D-Bus或保留任意派生。

## 已存在且未覆盖的工作树
开始时分支diag/session-fine-timing，HEAD 9f67638e；大量既有bootstrap文件未跟踪，另有两个修改的bootstrap测试及临时cjs。未清理、未批量stage这些文件，不推断它们全由本轮产生。
