# 离线初始化编码设计终审通过记录

状态：CODING_DESIGN_REVIEW_ACCEPTED，限文档设计。受审正文自身REVIEW_PENDING及旧轮次OPEN保留为历史；本记录是最终固定版本的门禁索引，不修改已受审正文。

- 固定提交：5f1f44e6bf7fc610da4db259974629a39f412a49。
- 受审文件：docs/session-offline-bootstrap-coding-design.md。
- SHA256：d85c29fe8b8ddea22a642ef9681914042e4dd544f8d694c8cb146ce5fbe0aea8。
- 独立审查：.10宿主机现有Hermes CLI前台限时执行，新会话20260908_112115_87baa6；退出码0。
- 原始输出：/workspace/history-failure-evidence/review-on-10/coding-v4-review5.json。
- 原文结论：PASS。未发现实质性P1/P2。candidate_directory_identity已作为Manifest必填字段与M、注册链及审批绑定；重启、发布、恢复均要求目录FD读回dev/ino/uid/gid/mode五字段，内容相同但目录inode被替换会QUARANTINED/IDENTITY_CHANGED；BUILDING中断明确转FAILED且不得复用，不再依赖易失内存身份。未实施的平台能力、测试NOT_RUN及部署条件均已定义为失败关闭的运行门禁，不构成本轮编码设计阻断。

## 父会话核验

终审后实际sha256sum与受审摘要完全相同；未改变正文。批准coding-schema的全部SQL fences按既定换行连接后与api/_display_schema_ddl.py的SQL常量逐字相等，工具计算SHA为578f80789de98456324d430142631c7b7f984f80f3ffe30fc7c31d0634bdcd9d，与新增受信固定值一致。不是执行DDL测试。

本轮持续整改并进行前台独立文本审查，保留各轮不通过结果coding-v4.json、coding-v4-final.json、coding-v4-review3.json、coding-v4-review4.json；未隐去失败。关闭范围包括目录稳定身份、持久平台证据、首次审批绑定、固定DDL、拒绝优先级、当前生效策略、跨策略固定锁和候选目录身份持久绑定。

## 明确未授权与未验证

本次用户要求“继续设计整改，直到审查通过”，因此本轮止于设计闭环；没有新生命周期功能编码、SQL执行、UID/ACL/权限配置、候选创建、发布、激活或删除授权。产品仍LEGACY，既有binding修复代码未被本次文档终审冒充代码审查通过。

独立审查是固定文本推理，不调用工具，测试NOT_RUN；父会话只核验文档/SQL字节，不冒充平台故障验证。后续实施必须另获授权，并验证专用权限域、固定锁/active锚点、硬资源限制、真实SQLite/WAL/no-replace/fsync及崩溃恢复。缺前提失败关闭。设计PASS不是生产PASS。
