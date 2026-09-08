# 配置缓存与网络隔离：修复证据

范围：基于 `78b9382d` 的共享工作区；仅修改 `tests/conftest.py`、
`tests/test_conftest_network_isolation.py`，新增
`tests/test_conftest_config_isolation.py`。未修改产品 `api/config.py` 或
`server.py`，未提交、推送或部署。其他代理的并行变更不属于本文范围。

## 根因与恢复边界

原触发项为
`tests/test_issue1384_local_provider.py::TestResolveModelProviderHealsLegacyLocal::test_provider_local_normalised_to_custom`。
它 monkeypatch `_get_config_path` 后调用 `reload_config()`；测试内部 finally
再次 reload 时，monkeypatch 尚未撤销，仍加载临时配置。因此测试退出后，路径函数
已恢复，但 `_cfg_cache`、`_cfg_path`、`_cfg_mtime`、`_cfg_fingerprint` 仍属于临时路径。
后续模型测试安装内存配置后，产品正确的 path-changed 分支重载磁盘，覆盖内存设置。
只失效模型 TTL 无法恢复这组关联状态。

修复复用现有 `_invalidate_models_cache_after_test` 边界：依赖 home/config-path
恢复 fixture，在测试局部 fixture 前保存完整缓存深拷贝、原字典引用、cfg 别名和
磁盘身份；finally 在锁内按原引用恢复缓存内容及身份，前后继续清空模型 TTL。
不在每次测试前强制 reload，不清空产品配置，不吞掉恢复异常，不禁用产品路径变化
检查。回归覆盖正常/异常退出、缓存内容及对象身份、元数据、后续内存覆盖，且确认
同一测试内切换磁盘路径仍真实重载。相邻 mtime/override/stampede 测试也通过。

网络问题不是隔离失效：全 collection 导入 `server.py` 后，其 guard 包裹
conftest guard；公网仍被拒绝，但旧 `__qualname__` 断言要求最外层必须是
conftest 函数。保持现有 wrapper 链和 opt-in fixture 的原样恢复机制，改用行为断言：
公网 IPv4/IPv6 阻断、直接 socket.connect 阻断、真实 loopback 连通、内网地址穿过
两层 guard 到达桩 transport。opt-in 用 public address 到达桩 DNS resolver 证明解除，
不实际访问公网；正常/异常退出均验证恢复完全相同的 wrapper 链，再检查阻断和
loopback。未扩大地址白名单，未放宽网络安全边界。

## RED/GREEN 与证据

证据目录：`/workspace/history-failure-evidence/resume-fixes-isolation/`。
`run.py` 复用既有 restart runner，经 `./scripts/test.sh` 执行；过滤凭据/provider
环境，使用独立 HOME/HERMES_HOME/HERMES_BASE_HOME/config/state/test-state，并启用
`HERMES_WEBUI_TEST_NETWORK_BLOCK=1`。保留日志、JUnit XML、参数、退出码、时长和沙箱；
未访问真实 home。框架自身仍执行既有临时测试服务器与临时目录清理，不涉及部署或真实数据。

| 证据标签 | 真实结果 |
| --- | --- |
| `config-regression-red-behavior` | 2 failed：缓存内容/磁盘身份未恢复 |
| `collection-red` | 14 passed、2 failed、1 collection skip；网络名字断言及 LMStudio 内存配置失败 |
| `pair-0-negative` 至 `pair-4-negative` | 每对 1 passed、1 failed；仅以插件替换缓存 fixture 为旧 TTL-only 实现 |
| `pair-0-green` 至 `pair-4-green` | 五对分别 2 passed |
| `original-six-green` | 原触发项加五模型测试：6 passed |
| `config-green` | 受影响整文件及新增缓存回归：35 passed |
| `network-green` | 网络隔离整文件：16 passed |
| `neighbors-green` | 配置 override/mtime/YAML/stampede/import-order 邻域：39 passed |
| `collection-green` | 全 collection 后选原组合及隔离回归：24 passed、1 collection skip |
| `collection-final-green` | 同上并加原 restart 和包遮蔽 restart 回归：26 passed、1 collection skip |

五对 negative 是独立进程对照：每个后续项均紧接原触发项，避免第一项自身重载
修正路径后掩盖后四项。相反，`original-six-negative` 顺序六项仅首个模型测试失败
（5 passed、1 failed），这是同一状态机的预期现象，不应宣称六项同时出现五个失败。
最初 `config-regression-red` 使用尚不存在的 fixture 名称而失败，不作为行为 RED 证据；
有效 RED 为修正到既有 fixture 后的 `config-regression-red-behavior`。

`verified-accounting.json` 逐一解析 XML testcase，未使用带 subtest 计数的汇总猜测。
collection skip 是既有 manual 测试缺少 `websockets`，不是新增 skip；全 collection
RED 另有既有 SyntaxWarning。仅完成全 collection + 小组合，不声称全套测试通过。

复现：

```sh
python /workspace/history-failure-evidence/resume-fixes-isolation/pairs.py
python /workspace/history-failure-evidence/resume-fixes-isolation/run.py collection-final-green tests/ -p select_isolation
```

注意 runner 标签会覆盖同名日志/XML，重新审计请换新标签。`select_isolation.py`
通过 collection hook 只选择执行集合，不规避其余测试模块的 collection/import。

最终 `ruff check`（三个本任务 Python 文件）及 `git diff --check` 通过。
fixture 与测试已冻结，交父代理独立 review 和全分片验证；无新增依赖。

## 独立早审P2与父修复

独立审查发现：`cfg is not _cfg_cache` 时仅恢复cfg引用而未恢复其嵌套内容。
父新增 `test_config_boundary_restores_independent_cfg_contents`，实跑
`parent-cfg-independent-red`：1失败，实际值仍为leaked而非original。
修复对独立cfg字典另存深拷贝并在同一锁内原引用恢复；别名相同时不重复恢复。
`parent-cfg-independent-green` 以正常60秒限制回归配置、网络和5项模型相关整文件：
**52通过、0失败、0跳过**。证据位于本机 `restart-resolution` 目录。

风险：仅恢复引用仍会留下原地修改；对策是区分共享别名与独立override并验证两者。
失败回退：不改产品reload行为、不放宽公网阻断；门禁保持失败并保留全部诊断。
