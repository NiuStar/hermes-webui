# 离线初始化实施进度（未完成，禁止放行）

受审设计固定提交5f1f44e6bf7fc610da4db259974629a39f412a49；本轮用户已授权第1—4步隔离编码、测试和终审，测试全部在10.126.126.10前台执行。追加明确授权：专用测试UID、隔离挂载和资源限制；现有服务不变。正式事件/terminal-outbox及业务连接工厂按用户最新决定另走设计门禁，不纳入本轮实现。

## 当前已实跑

- .10基线原schema+Binding：55通过1失败，真实WAL竞争把未知库delete模式变wal。旧源码位于/opt/hermes-bootstrap-tests/1614554c-baseline，原始归档implementation-baseline-1614554c.tar.gz的SHA为ed717a1bf91216ab117106c9075c54984a32e11ecd34828cbe7703da7ec3081b。
- 旧initialize入口改为访问任何db属性前拒绝：2项RED→2项GREEN；这叫退役危险入口，不叫修复原算法。
- Binding夹具改从批准文档SQL建立合成库，不授予生产资格；曾遗漏WAL产生13失败，补齐原测试前提后45项通过，未放宽断言。
- JSON规范字节、解析、审批字段校验逐轮RED/GREEN。解析器目前只校验JSON通用值，对象验证目前仅approval，其他对象与生命周期未完成。
- 当前邻域：test_display_bootstrap_contract、test_display_mutations、test_display_history、test_history_capture、test_history_jobs共121通过，0失败0跳过，JUnit已解析读回。不是完整项目回归，也不包含仍待迁移的旧test_display_schema。
- 证据：.10 /opt/hermes-bootstrap-tests/{baseline-02,legacy-red-01,legacy-green-01,binding-green-01,binding-green-02,neighbors-01,neighbors-02,codec-red-01,codec-green-01,codec-red-02,codec-green-02,parser-red-01,parser-green-01,approval-red-01,neighbors-03}.xml。原始失败与所有测试库保留；本地最终XML为/workspace/history-failure-evidence/implementation-on-10/neighbors-03.xml。

## 测试环境（不是生产策略）

新建独立解释器/opt/hermes-bootstrap-tests/venv，安装pytest及项目requirements；不修改现有Hermes解释器。系统ensurepip缺失，通过get-pip仅引导独立venv。

专用用户hermes-bootstrap-test UID999/GID987。候选限容ext4镜像64MiB，审计镜像32MiB，分别挂载/opt/hermes-bootstrap-tests/candidates-volume-01与audit-volume-01，nodev/nosuid/noexec。源与发布目录位于同一候选卷，审批根root所有且专用用户只读。/etc/hermes-display-bootstrap仅预置锁文件，尚无anchor/active/有效策略，不得创建候选。挂载尚未配置重启自动恢复，不能声称断电验证通过。

测量参考DDL库253952字节、RSS12760KiB、约0.067秒，仅用于测试预算规划。完整候选配额/审计余量负对照、cgroup硬内存及墙钟限制尚未验证。

环境设置后读回：hermes-webui-optimized容器d8726140de4a379e2b7a9d14bcb1c03ce17aebe6035d83dba86ec43a7d04af64，wechat-content-studio容器9744a94e962257c796fd35e7572c7483a532c206ee957969b1771217fde58586；两者重启数0且启动时间与变更前相同。未部署或切换GET。

## 最新前台增量（仍未完成）

身份与资源策略测试新增后合同测试26通过（identity/resource RED与GREEN XML在同一证据根）。专用UID实测可写候选/注册，不可写审批/锁父目录。systemd前台探针读回memory.max=67108864、memory.swap.max=0、pids.max=4；尚非内存/墙钟超限负对照。

限容负对照真实写入55988224字节后ENOSPC(errno28)，独立审计卷仍能fsync并读回审计JSON；审批根真实O_CREAT被拒。证据为.10 /opt/hermes-bootstrap-tests/audit-volume-01/registry/quota-negative-01.json。候选volume-01已被负对照填满，保留文件，不删除腾空间；后续功能测试须另建隔离卷并重新绑定身份，不能沿用此满卷。首次脚本因root默认文件权限不可读失败，仅将该无秘密测试脚本改为0644后执行，不放宽审批或数据权限。

最新补充：注册表字段/状态约束、Manifest字段及PlatformEvidence字段已追加RED→GREEN；截至platform-schema-green-01.xml合同测试30通过。最近一次邻域全组合neighbors-05.xml为126通过（在最后新增平台证据测试之前）。这些均不代表注册串链/真实发布恢复完成。

最后前台增量：DeploymentPolicy/anchor/active合同测试32通过；Linux no-replace及原子记录写入12项通过；注册串链与非法跃迁4项通过，合计邻域neighbors-06.xml实跑145通过。随后完成→隔离→试图恢复完成的串链反例补充通过，terminal-chain-01.xml为5通过。未实现候选构建与发布恢复入口，不能将这些基础层测试称为生命周期通过。Binding独立前台审查PASS，见session-formal-binding-slice.md及会话20260908_120021_6c953c，已核验45项及固定代码SHA。

## 剩余与门禁

完整DeploymentPolicy/ResourcePolicy/Manifest/Registry/结果联合严格验证、可信上下文与ACL/挂载/VFS/硬限制实证、候选构建冻结、审批绑定、no-replace发布、注册串链和崩溃恢复尚未实现完成。旧schema测试迁移、完整回归、Binding及实现独立终审、代码提交仍待完成。当前只有局部通过，整项NOT_ACCEPTED。

风险：局部全绿被误当完成；对策为保留逐层范围、明确缺项；验证以固定源码SHA、.10 JUnit及实际持久状态为准。回退保持未接线LEGACY，不删除任何库、镜像或审计文件，不改变现有服务。
