# 两阶段准入编码与实测状态

本次已实现：独立quota传输、审计终结Landlock边界、Git原始对象/执行文件发行校验、准备期审计扫描、receipt对象身份登记/一次消费/资源转移、创建及发布恢复准备、限制后复核、execute_operation命令接线。旧acquire继续拒绝；Context._issue仅接受receipt。尚未达到完整验收，禁止生产接入。

实际测试主机仅10.126.126.10，隔离副本/opt/hermes-bootstrap-tests/two-stage-20260909-01。
- 新增schema/ownership测试首轮52 PASS。
- bootstrap全套首轮493 PASS、32 FAIL、34 ERROR。
- 补齐server/docs、隔离源码读权限及EOF修复后全套559项，22 FAIL、34 ERROR、503 PASS（XML two-stage-all-02.xml）。
- EOF真实socket复现确认Linux空数据EOF可附全零SCM_CREDENTIALS；仅允许此清理记录，不更新peer。定向3 PASS。
- command新编排mock迁移后，command/ownership/credential定向14 PASS。
- 本地静态检查126个相关Python文件AST PASS，7个新增主模块导入PASS；不是运行验收。

剩余：旧admission/context/lifecycle测试迁移；真实worker发行验证正反例；两阶段完整状态链/限制后真实角色及quota observer联合验收；全量回归和独立代码终审；提交推送读回。不得把旧V1生命周期fixture直接视为V3覆盖。

环境阻断：findmnt确认既有candidates-volume-01是/dev/loop0 ext4，df仅2.0K可用、100%；audit-volume-01 /dev/loop1约25M可用。保留原测试数据，不删除/扩容既有卷。新隔离卷尚未建立，不能在当前满卷宣称完整测试通过。


## 本轮续作实测

- 准入顺序/角色/Context句柄测试迁移至两阶段接口，旧acquire仍失败关闭。
- V3真实SQLite与固定审计介质状态机27项通过，覆盖创建/审批/发布/恢复、资源故障、deadline、角色、证据与异常脱敏。OS准入在该夹具中被替代，不是联合验收。
- 发行源码集合完整性修复：缺失已提交api/scripts文件拒绝；真实Git原始对象与隔离系统Python正反例5项通过。
- 明确未发生rename的EXDEV/ENOSYS返回BLOCKED/UNSUPPORTED_PLATFORM；移动后完成写失败仍UNCERTAIN。
- worker unit新增-S，避免site导入非系统目录导致发行拒绝；unit定向13项通过（加强断言后仍待最终复跑）。
- .10完整副本two-stage-full-01使用scripts/test.sh运行bootstrap/schema/mutation：628 PASS，JUnit two-stage-combined-02.xml。
- 全仓首跑已完成：15581通过、45失败、281跳过、2 xfailed、1 xpassed；two-stage-full-01.xml/log。26个V3失败来自全仓pytest RSS超过夹具1GiB，夹具预算改4GiB（非生产）；14个失败来自缺Git元数据，隔离副本git init后修复。定向128项127通过，剩余sprint3 profiles HTTP500；model resolver单独通过但全仓次序污染未解决；2个root权限负例仍待非root基线验证。全回归不通过。
- 隔离卷位于two-stage-volumes-03。候选512MiB/4KiB/prjquota；audit-large为1GiB/4KiB独立ext4。此前128MiB审计卷因保留历史耗尽，全部保留，不删除或重格式化。另保留最初1KiB块审计卷的对齐拒绝证据。
- 正式准入配置仍无anchor/active/Storage合同验收；真实observer/quota/worker联合验收、独立终审、提交推送尚未完成。未动生产。

## 第二轮全仓与硬限制读回

- `.10` 全仓 `two-stage-full-02.xml/log`：15748 passed、4 failed、169 skipped、1 xfailed、2 xpassed，另45 subtests passed；JUnit errors=0。未全绿。
- 两个权限负例在专用非root用户下通过；另外两个 `test_model_resolver.py` 用例仍有全仓次序依赖，尚未定位根因。profiles HTTP500已证实为缺少Hermes源码路径，使用已有源码只读运行后通过。
- 非root systemd真实负对照：`hermes-bootstrap-memory-negative-03` MemoryMax=67108864、MemorySwapMax=0，Result=oom-kill、ExecMainStatus=9；`hermes-bootstrap-time-negative-03` RuntimeMaxUSec=2s，Result=timeout、ExecMainStatus=9。只是独立内核探针，不是worker联合验收。
- 当前审计镜像宿主是根文件系统 `/dev/vda1`。Storage acceptance要求host_blocks_full等真实证据；不能在运行WebUI的宿主根盘上执行写满实验，不能以嵌套loop替代受审的direct-disk拓扑，更不能生成虚构PASS报告。真实联合验收保持BLOCKED，须提供可独立耗尽的直连测试磁盘/普通分区，或先完成替代验收方案设计与独立终审。
- 未提交推送；全部工作未完成。

