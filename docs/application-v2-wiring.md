# Application v2：工具、CLI、页面接线

本文件描述候选代码，不是部署批准、工具发行版批准或产品验收结果。仅在 `.10` 专属隔离目录验证，不修改生产。

## 入口矩阵

| 入口 | 调用路径 | 身份来源 |
|---|---|---|
| WebUI | panels.js → application_tasks.js → 已有认证服务器 → application_routes → ApplicationService | 可信配置 `APPLICATION_V2_WEB_PRINCIPAL`；profile cookie 不授予权限 |
| 单身份 CLI | scripts.application_task_entry → application_commands.dispatch → ApplicationService | 当前非 root UID 对应的 OS 用户 |
| 多身份 CLI | 相同 CLI → Unix socket → SO_PEERCRED → application_commands.dispatch → 同一服务存储 | 内核提供调用者 UID，服务配置映射权限；请求禁止 principal 字段 |
| 固定策略工具 | create preflight → T1/T2/T3 → fixed_policy_tool 意图 → 固定发行版进程 → 输出持久化读回 → 制品创建 | 部署绑定路径、SHA、独立验收材料、已有 systemd 服务身份；请求不能选择 shell、argv 或路径 |

create/approve/publish/recover 沿用严格 v2 request；旧预算模块不接入。固定工具只作为可选 create 前置检查，不冒充独立产品验收。没有固定工具配置时不解析工具，不影响默认业务和只读查询；配置存在但缺失或不可信时，在创建任务前拒绝，不假装该工具通过。

## CLI

从发行版目录运行：

```sh
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json init
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json create --task-id <32hex> --candidate-id <32hex>
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json approve --task-id <new-32hex> --candidate-id <32hex> --approval-id <32hex> --manifest-sha <64hex> --policy-sha <64hex> --reference '真实审批依据'
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json publish --task-id <new-32hex> --candidate-id <32hex> --approval-id <32hex>
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json inspect --task-id <interrupted-32hex> --candidate-id <32hex>
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json recover --task-id <new-32hex> --candidate-id <32hex> --interrupted-task-id <32hex> --decision close_failed --expected-evidence-sha <64hex>
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json get --task-id <32hex>
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json list --limit 20 --offset 0
python3 -m scripts.application_task_entry --config /absolute/trusted/config.json submit < request.json
```

这些尖括号是文档占位，不是已批准的实际标识。恢复 `finalize_existing/resume_publish` 必须使用原任务的审批 ID（适用时）。网络断开不得换任务 ID重试；先 get / inspect。stdout 单份 JSON、stderr 诊断；退出码：0 成功/只读成功，2 参数协议，3 权限，4 忙/依赖，5 结果未知或需对账，1 内部错误。帮助命令为正常文本。

### 多 OS 身份

`cli_socket` 和 `cli_broker_uid` 必须同时配置；UID 为专属非 root 服务用户。CLI 通过 socket 调用，不打开账本。服务的可信配置需要分别列明创建、审批、发布用户的权限和 task_read_all。独立审批仍由服务强制检查，不能靠 `--principal` 切换身份。

- socket 父目录归服务所有，无组/其他写权限；跨用户时可用 0711 仅开放遍历。
- socket 0660，调用者须已被部署允许访问服务组。这里不创建或修改用户/组。
- 服务存储继续 0700/0600，不对审批者开放直接文件写入。
- 启动入口：`python3 -m scripts.application_task_entry --config /absolute/trusted/config.json serve`。
- 不自动移除已有 socket；残留必须在确认原服务已停止后由操作员处理。
- 不创建 systemd unit、不启动后台分派。候选测试由测试父进程创建并收尾自己的服务子进程。

## 固定工具部署契约

可选 `fixed_tool` 字段：
`release_root`, `release_sha`, `acceptance_path`, `acceptance_sha`, `timeout_ms`, `output_max_bytes`, `service`, `source_binding_path`, `source_binding_sha`。

批准不设七天或其他日历到期限制。源码绑定必须包含实际候选 `source_commit` 和完整运行模块文件 SHA；源码文件及全部祖先目录必须 root-owned，禁止 group/other 写。源码、发行、策略、服务或执行限制绑定变化时旧批准失效。

复用 application_runner_release 的 root-owned 不可写发行版校验；所有文件集合和内容 SHA、解释器 SHA、固定 `-I -B scripts/application_fixed_policy_test.py` argv 都核对。不可使用任意测试发现或请求传参。

验收回执格式 v2 包含：format_version=2、tool_id=policy_suite、release_sha、policy_sha、source_commit、source_binding_sha、timeout_ms、output_max_bytes、service_sha、verdict、measurement_sha、review_sha。`service_sha` 是规范 JSON SHA。独立测量和独立复审原文位于回执同名 `.measurement.json` / `.review.json`，绑定相同字段，kind 为 measurement / independent_review。文件 root-owned，目录不可被服务用户替换。代码不签发、合成或迁移旧 PASS。

`service` 复用既有服务身份验证参数：systemctl_path、service_unit、show_timeout_ms、output_max_bytes、helper_term_grace_ms、helper_kill_wait_ms、stop_timeout_ms、membership_gap_ms、membership_total_deadline_ms、max_cgroup_entries。必须实证 MainPID/InvocationID/ControlGroup 匹配、KillMode=control-group、SendSIGKILL=yes 及有界停止，且无其他后代。普通直接 CLI 未运行在该已验证服务内时，固定工具执行失败关闭；多身份 CLI 交由已验证的 broker 服务执行。

输出保存在当前任务 `<task-id>.tool.json`，包含 release/acceptance SHA、返回码、超时标记、有限输出及 SHA。工具退出0还必须有合法 PASS JSON；工具失败不生成制品。恢复读取旧意图记录的 cgroup，未证明原后代退出则拒绝；不扫描宿主其他服务、不按 PID 任意杀进程。

**当前没有在候选配置中签发或启用新的固定工具发行版批准。** 固定脚本直接执行通过只证明脚本可运行，不等于可信发行版+systemd 安全门已通过。

## 页面与 API

- 中文字段保留 machine enum；能力接口显示服务绑定身份、权限和策略 SHA。
- 新建请求 ID是明确动作；完整请求在网络 I/O 前持久化，生成 ID回填表单。相同 ID改参数拒绝。
- GET 查询优先明确输入；恢复使用独立的原任务 ID，检查结果回填 evidence SHA，不自动提交。
- POST 禁用通用 API 的自动网络重试；所有错误保留 issue/scope/retry_action。
- 已接受但未完成任务 HTTP 202；不再用统一200表示所有状态。
- 新增 Node DOM/transport 检查不替代真实浏览器视觉验收。

## 本轮验证边界与回退

全部本轮接线编码完成后统一跑 application_v2 / application_wiring / CLI 多 OS 身份 / Node 检查；不提前全仓回归。完整工具验收材料、全部故障矩阵、全仓和真实浏览器验收仍属于后续统一门禁。

回退为候选文件级回退，不删除 ledger/artifacts、不迁移数据、不将 v2 自动路由回旧库。部署配置不启用 cli_socket/fixed_tool 即维持原有直接身份和默认内置业务链；已开始的固定工具任务恢复仍核对原意图，不能通过去掉配置绕过后代与证据检查。


## Real Chromium acceptance (isolated opt-in)

A 404 capabilities response from an unconfigured test server is not a missing route: `APPLICATION_V2_CONFIG` must bind a trusted initialized v2 ledger, and `APPLICATION_V2_WEB_PRINCIPAL` must bind the deployed identity. Do not change this to silently create a default database or accept a browser-selected principal.

`tests/test_application_browser_acceptance.py` explicitly supplies both variables to four loopback-only child servers, using the existing credential-scrubbed test server environment and private runtime fixture. Random test password authentication is enabled. First-run onboarding is completed in isolated state before browser boot to avoid racing the asynchronous startup probe. No production credentials, runtime configuration, or service are changed.

Run opt-in with `APPLICATION_BROWSER_ACCEPTANCE=1`, `APPLICATION_BROWSER_EVIDENCE=/absolute/private/evidence`, installed Playwright on `PYTHONPATH`, and `PLAYWRIGHT_BROWSERS_PATH` pointing at isolated Chromium. Use the existing Agent venv and `HERMES_WEBUI_AGENT_DIR`; keep `TMPDIR` short for Unix socket tests. Example test selection:

```sh
python -m pytest tests/test_application_browser_acceptance.py tests/test_application_v2.py tests/test_application_wiring.py tests/test_application_fault_matrix.py -q -p no:cacheprovider --junitxml=browser-joint.xml
```

The browser scenario covers authenticated creator/approver/publisher/observer, real create/approve/publish, duplicate reads without repost, refresh and changed-request conflict, all three recovery decisions after actual executor exit, permission denial, offline retry, expired login and relogin, and a mobile real create. Recovery failure injection runs only in test child processes; no HTTP response is mocked. Business approvals are synthetic isolated test approvals, not fixed-tool release authorization. DOM navigation/setup helpers are invoked in the real page; forms, login and submit/recovery buttons use Playwright interactions.

Evidence includes per-identity traces, screenshots, request metadata, checked assertions and terminated-child status. Passing this scenario does not imply fixed-tool release acceptance, whole-repository regression, or independent release review.
