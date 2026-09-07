# 会话读写分离：测试扩展与风险反例

状态：全部NOT_RUN。本轮只写测试规格，未执行SQL或功能代码。主计划T01–T20保留；下表T21–T26对应R1–R6，T27–T42覆盖其余风险。先经设计终审及编码授权，才在本地隔离临时状态使用真实SQLite多连接/子进程屏障做RED→GREEN；禁止生产杀进程、填盘或清理数据。

| ID | 风险 | 屏障/故障注入及输入 | 观察断言 | 状态 |
|---|---|---|---|---|
| T21 | D01 | 暂停旧源写，另连接构建/发布/新mutation；两个token乱序seal；seal确认前退出 | 无新版本发布、无重叠源写，revision绑定原mutation；恢复不重跑工具 | NOT_RUN |
| T22 | D06 | 已启用WebUI会话由内部Agent、外部CLI、新进程、旧句柄分别写；撤资格事务失败 | 首次源写前有有效门；绕过可能存在就不授资格；父源撤资格覆盖子scope | NOT_RUN |
| T23 | D09/D16 | token验证前/后屏障，撤权与epoch切换；迟到旧工具回调与旧SSE重连 | 撤权排空在途写；旧回调无events插入、无seq消耗、无旧源写，返回reload | NOT_RUN |
| T24 | D07/D10/D13 | 订阅注册前后及追平后提交；丢全部通知；半帧断线；未知schema/对象缺版本/清单错 | 持久库补查最终无遗漏；重复幂等；不完整对象不COMPLETE；不得跳未交付页 | NOT_RUN |
| T25 | D24/D25 | 旧源写成功凭证提交前崩溃；凭证前写失败；旧备份替换源；一个进程不确认排空 | 水位无空洞、源变更使凭证失效；回滚重验；缺一源/进程则BLOCKED不卸载恢复组件 | NOT_RUN |
| T26 | D03/D14/D15 | 多连接同时领取、NULL基线首次发布、旧fence迟到、BLOCKED同修订重建、跨scope FK和发布后段修改 | 唯一attempt胜出、旧发布失败、重建新attempt、FK/不可变拒写、指针与job同事务 | NOT_RUN |
| T27 | D02/D21 | 隔离库提交前后进程退出、SQLITE_BUSY/IOERR，跨库一步成功一步失败 | FULL提交成功才确认；跨库不冒充原子；失败保留UNCERTAIN；重启读回已确认记录 | NOT_RUN |
| T28 | D03 | 两版本共享段，篡改候选hash/段数/offset；尝试更新已发布行 | 旧版不可改变；坏候选不发布，范围和内容hash吻合 | NOT_RUN |
| T29 | D04 | 超大单行/多模态及全量合并估算超预算，禁止靠进程OOM后才发现 | 构建前拒绝无预算证明任务；不截正文、不杀服务；记录估算/实际RSS差 | NOT_RUN |
| T30 | D05 | 后台线程与请求profile不同，恶意路径/符号链接，store独立导入 | 明确目标profile、拒不可信路径；无routes循环依赖，跨scope不串写 | NOT_RUN |
| T31 | D08 | 正常/失败/cancel/替换/压缩中断终态与job登记的每个提交点退出 | 每种终态有任务或明确BLOCKED；补偿幂等，工具调用计数不增加 | NOT_RUN |
| T32 | D11 | 上轮SEALED未发布时开下一轮，旧worker迟到；更新已covered对象 | 连续未归档事件保留且下一候选覆盖；covered对象修改撤active资格；单响应固定base | NOT_RUN |
| T33 | D12 | 多页活动范围，页中工具scene扩展/空页/超大对象，网络页失败 | resume等于实际交付末事件，不跳upper；offset与scene完整；重连不漏页 | NOT_RUN |
| T34 | D16 | 分支父链修改、源还原、导入、截断与构建并发 | 子依赖全部失效、epoch不混用，旧消息不复活；未知父写方保持LEGACY | NOT_RUN |
| T35 | D17 | 分页/SSE两批之间撤权限或删除；归档会话按合同访问；敏感字段 | 下批拒绝/关闭，不泄露旧缓存；归档不误删；日志/响应保留正确脱敏 | NOT_RUN |
| T36 | D18 | 页外Todo非空→空，工具事件多于展示消息，后台重建时间晚于活动 | Todo空生效、展示计数与oracle一致、维护不改变活动时间/未读状态 | NOT_RUN |
| T37 | D19 | 元数据探测永久阻塞，配置切换同名model不同provider/endpoint，缓存旧版本 | 正文无联网调用；stale/unknown明确，单飞和退避；推理容量校验原规则 | NOT_RUN |
| T38 | D20 | 持久队列达到条数/字节阈值，慢消费者，连续重试，超大待发事件 | 有界入队/背压错误明确，已确认事件不删；内存不随积压无限增长 | NOT_RUN |
| T39 | D21 | 隔离文件系统注入空间不足，长读阻WAL回收、候选增长 | 提前停新构建，短批读释放；不在GET checkpoint，不删备份腾空间；无预算不回填 | NOT_RUN |
| T40 | D22/D26 | 逐拟启用scope全页oracle比对，保留慢/失败样本；冷/热/浏览器分测 | 消息/顺序/工具/hash全部匹配才启用；实测p95独立计算，未测列NOT_RUN | NOT_RUN |
| T41 | D23/D26 | 新旧客户端混用、旧cursor、一个服务进程未更新开关代次 | 旧协议保持；新能力显式协商；未全员确认不撤保护/不宣称完整回滚 | NOT_RUN |
| T42 | D25/D26 | 审核迁移/恢复/回滚流程的删除与停双写分支，恢复source仅剩派生副本 | 无授权不删库/log/backup/staging；派生展示不作为推理权威；无授权不执行阶段切换 | NOT_RUN |

## 测试文件分配

T21/T22/T23/T34→tests/test_display_mutations.py；T24/T32/T33/T41→tests/test_display_live_events.py（分页断言同时纳入test_display_paging.py）；T25/T27/T31/T42→tests/test_display_recovery.py；T26/T28→tests/test_display_store.py及test_display_worker.py；T29/T38/T39→tests/test_display_budget.py；T30/T35→tests/test_display_security.py；T36/T40→tests/test_display_paging.py；T37→tests/test_session_display_metadata_nonblocking.py。均为拟建/拟补文件，本轮没有创建代码。

## 执行记录规范与批准边界

每个测试记录初始状态、屏障位置、故障点、预期状态转换、实际源/派生库读回、错误码、RED和GREEN日志、commit。并发不得用随机sleep代替屏障；mock可注入I/O故障，事务结果必须由真实SQLite读回。断电硬件语义不由kill进程测试证明，应列环境限制。

协议约束DDL并非完整migration；M0首先完成全部字段类型/FK/trigger并观察反例失败，不能把本文代码块当已冻结schema。T40生产数据比对和性能测量需单独受控执行授权，不在本轮自动开展。测试数量、风险覆盖通过只说明文档引用闭合，不等于任何功能、安全、性能或生产发布PASS。
