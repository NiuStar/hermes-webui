# 离线初始化编码设计候选

状态：CODING_DESIGN_DRAFT。用户在本会话明确确认“新的设计方案已评审通过”。接受的方案正文为06dbaab81d8dc99d22d94d1482ab930980228a34中的session-offline-schema-bootstrap-design.md；平台证据53e2befa6a89e74168b5dbb4dfffd79a9d41970f。此记录是用户确认，不伪造独立审查者身份、报告或实验结果。旧方案文档的REVIEW_BLOCKED为历史状态，以本记录中的用户确认为阶段状态；未实测的平台条件仍是实现/运行门禁。

本阶段只编写编码设计并审查；没有功能编码、SQL执行、创建UID、改变权限、发布或删除授权。

## 模块分工（拟定，尚未创建）

- api/display_bootstrap_policy.py：不可变平台/预算/路径策略；缺失字段失败关闭，不携带凭据。拒绝bool整数、越界预算、未支持平台及未受控父目录。
- api/display_bootstrap_artifact.py：新候选生命周期；不接受任意已有SQLite连接，不覆盖、不自动修复、不删除失败候选。引用已有批准DDL常量，禁止复制分叉DDL。
- api/display_bootstrap_manifest.py：规范清单和外部批准记录的结构/摘要验证，不从候选内容授予批准。
- api/display_bootstrap_publish.py：完整目录no-replace发布、同步、身份幂等读回；不激活运行库。
- api/display_connection.py：仅为可信已激活库创建受控连接，FK/FULL逐连接设置并读回，禁止journal_mode切换。当前阶段不接routes。
- api/display_schema.py：现有initialize保留阻断；迁移调用点及旧反例去向必须在编码设计终审前明确，不允许留下生产绕过路径。

## 编码设计必须细化的接口

1. PlatformEvidence、ResourcePolicy、CandidateId、Manifest、ApprovalRecord与PublishResult的精确字段、类型、状态、错误码和版本。
2. 创建、冻结、验证、批准、发布、持久确认、隔离状态转换；每个转换的所有者、锁范围、幂等键、失败返回和审计证据。
3. 文件身份绑定：目录句柄寿命、祖先权限/ACL/别名检查、SQLite主库及sidecar受控命名空间、检查顺序和异常资源释放。
4. 发布系统调用封装：renameat2能力检测、RENAME_NOREPLACE、EXDEV/EEXIST/ENOSYS处理、文件及目录fsync顺序、批准记录与恢复匹配。
5. 资源硬上限：未测定阈值不填虚构默认值；候选上限、保留上限、预算检查和不删除的失败退出。
6. 每个阶段的精确测试nodeid及故障注入点，保留原WAL竞争失败，不用改变测试契约冒充旧实现修复。

## 风险、对策、验证与回退

| 风险 | 对策 | 验证设计 | 回退 |
|---|---|---|---|
| 方案接受被当部署授权 | 分离设计/编码设计/实施/激活四道门 | 检查每项任务授权范围 | 停在当前获批阶段 |
| 模块过多而没有垂直闭环 | 接口职责最小化，可在终审时合并纯内部模块 | 一条新库候选到发布未激活的状态图 | 不接运行入口 |
| 同UID路径替换 | 专用权限域是运行前提，不靠随机名或advisory lock | 跨UID拒绝、同UID威胁范围、ACL/挂载检查 | 前提缺失BLOCKED |
| 失败候选误复用 | 外部状态与批准记录匹配、未知状态隔离 | 中断重入与候选替换 | 保留文件，人工授权清理 |
| WAL遗留或immutable误用 | 仅冻结无sidecar候选适用immutable | hot journal/WAL残余/并发替换矩阵 | 拒绝发布 |
| 发布可见但不持久 | 明确rename线性化与目录同步分离 | 每步中断与重启读回 | 不激活、不覆盖 |
| 新连接遗漏FK/FULL | 连接工厂逐次配置读回 | 池重建、重启、并发连接 | 关闭新连接 |

## 当前交付边界

这里只建立编码设计的模块划分、必须完成的接口和风险清单；不是完整可编码规范，也不是编码设计终审通过。下一步补齐数据类型、状态转换与测试矩阵后提交当前会话复核，再完成编码设计独立终审。用户最新要求仍为前台执行，不派发后台任务。
