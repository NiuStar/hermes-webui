# 离线初始化：.10只读平台证据

状态：PLATFORM_PARTIAL / DESIGN_BLOCKED。绑定设计提交06dbaab81d8dc99d22d94d1482ab930980228a34。本轮仅SSH只读检查，没有创建UID、候选库或改变权限，没有功能编码、发布、清理。

## 实际结果

- 主机10.126.126.10：Linux 6.12.63+deb13-amd64。
- `/opt/hermes-webui-optimized`所在挂载：`/dev/vda1`，ext4，`rw,relatime,discard,errors=remount-ro`。
- df采样：总98233262080字节，已用36125478912，可用58064625664。只是采样，不是构建峰值预算。
- 容器hermes-webui-optimized：ID `d8726140de4a379e2b7a9d14bcb1c03ce17aebe6035d83dba86ec43a7d04af64`，重启数0，healthy。
- `/workspace`来自`/opt/hermes-webui-optimized/data/workspace`，bind/rw/rprivate。
- `/home/hermeswebui/.hermes`来自`/opt/hermes-webui-optimized/data/hermes-home`，bind/rw/rprivate。
- Agent源码为单独ro bind，不代表数据目录只读。
- 容器实际解释器`/app/venv/bin/python`，Python3.12.14，SQLite3.53.0，执行用户UID1024，附加组100/1024。
- SQLite compile_options包含THREADSAFE=1、DEFAULT_SYNCHRONOUS=2、DEFAULT_WAL_SYNCHRONOUS=2、DEFAULT_WAL_AUTOCHECKPOINT=1000、DEFAULT_PAGE_SIZE=4096；这些默认值不能替代每个连接读回。
- 容器privileged=false，userns为空，CapAdd=null，SecurityOpt=null，内存上限4294967296字节。未据此宣称完整能力集或同UID隔离已证明。
- `/opt`与`/opt/hermes-webui-optimized`：root:root、0755。
- `/opt/hermes-webui-optimized/data`：1024:1024、0755。
- `/opt/hermes-webui-optimized/data/hermes-home`：1024:1024、0700；容器UID1024的os.access(W_OK)为True（只读权限查询，未尝试写入）。
- getfacl不在主机PATH；未安装。ACL完整检查未完成，不能由mode位推断没有扩展ACL。

## 当前会话复核

现有hermes-home及其data父目录由WebUI同UID控制，**不能直接用作设计中的独立创建/发布信任边界**。0700只约束其他UID，不防同UID写方。必须保留设计阻断，不能因本地ext4或容器非privileged而放行。

建议候选父目录位于root控制且不向WebUI以rw映射的独立路径，由获批专用UID负责创建/发布；本轮没有创建路径、UID、修改compose或赋权。该路线需明确批准，随后才可进行访问负对照。

## 风险 / 对策 / 验证 / 回退

| 风险 | 对策 | 待验证 | 回退 |
|---|---|---|---|
| 复用同UID数据目录无法隔离 | 独立权限域，拒绝复用当前hermes-home | 祖先权限、ACL、挂载别名、跨UID真实拒绝 | 不创建候选，LEGACY |
| ext4被当成原子/断电保证 | 实测no-replace和文件/目录同步 | 双发布竞争、已存在目标、各崩溃点 | 无能力证据不发布 |
| 空闲空间被当成预算 | 测量候选/WAL/临时I/O/RSS并批准阈值 | 配额或硬上限能力、保留候选上限 | 预算缺失拒绝构建 |
| SQLite默认值被当成永久设置 | 连接工厂逐连接设置FK/FULL并读回 | 重连、池重建、重启、WAL关闭侧文件 | 不授予业务资格 |

## 下一门禁

1. 当前设计先确认专用UID/受保护父目录方向；批准设计不等于执行用户/权限变更。
2. 明确批准有限可行性实验范围：仅新建测试目录与自有SQLite候选，验证WAL关闭/immutable只读、no-replace与目录同步；保留全部候选，不操作真实库。
3. 完成平台/访问闭包/资源证据后，修订设计并完成新版本独立终审，再进入编码设计。

当前WAL竞争产品缺陷仍OPEN；此前binding45通过及组合105通过3失败不是本轮新测试。所有新生命周期实验NOT_RUN。服务与真实历史读取继续LEGACY。
