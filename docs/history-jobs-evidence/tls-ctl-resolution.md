# TLS fixture 与 ctl 测试环境修复

## 范围与结论

基准 HEAD：`78b9382d22651effdf1a7f8cc923eb1abe4a296d`。
只修测试层：`tests/test_tls_aware_probe.py` 的 HTTP 响应 framing，新增
`tests/run_tls_ctl.py` 专用 Linux 隔离执行器；未修改产品代码、ctl 原断言、
共享 conftest 或 `scripts/test.sh`。共享工作树其他代理的修改不属于本报告。

- TLS fixture 原先没有 Content-Length，依赖连接关闭表示响应结束；
  BaseHTTPRequestHandler 的 TLS socket 关闭不保证 close_notify，当前 curl/OpenSSL
  因此把本应成功的响应视为未正常结束。为同一个实际发送的 bytes body 设置准确长度，
  不改变证书校验、self-signed 警告、insecure opt-in 静默或 HTTP fallback 断言。
- ctl 依赖真实 `ps` 查询进程身份；本容器默认 PATH 无 ps。孤儿测试 daemon 被不回收
  zombie 的容器 PID 1 接管时，`kill -0` 仍成功，退出断言失败。不能把 zombie
  当作“进程消失”来放宽断言，也无需改产品停止逻辑。
- 专用执行器检查真实 ps/bash/curl/openssl，使用私有 Linux subreaper 接管自身
  后代，仅 waitpid 已退出的直接子进程，排除由 Popen 管理的 pytest 子进程。
  不安装系统包、不向宿主或生产进程发信号；退出时记录未释放子进程并使其导致失败。
  HOME、agent home、state、test-state、workspace 均为新目录；移除凭据和代理环境，
  明确开启测试网络拦截。通过原 `scripts/test.sh` 使用受支持的 repo venv。

## 可重复命令

在仓库根目录运行（Linux；需要真实 procps、curl、openssl、bash）：

```bash
.venv/bin/python tests/run_tls_ctl.py \
  --output /tmp/tls-ctl-new-evidence \
  --agent-dir /path/to/hermes-agent
```

`--output` 必须不存在；执行器保留日志、JUnit、status 和私有状态，不覆盖旧证据。
可追加 pytest node/file 路径，默认完整运行两个目标模块。
无 ps 会在启动测试前明确返回 2，不伪造 ps、不跳过测试。

## 本次真实 RED / GREEN

原始证据目录（本机交接路径，不是生产状态）：
`/workspace/history-failure-evidence/resume-fixes-tls-ctl/`。

RED 先于修复运行，脚本保存在该目录 `red.py`；复用已有 triage `run.py` 安全环境。
命令：`.venv/bin/python /workspace/history-failure-evidence/resume-fixes-tls-ctl/red.py`。

| 证据子目录 | 真实结果 | 判定 |
| --- | --- | --- |
| `red-tls` | 12 passed, 2 failed | 原 self-signed 与 insecure-opt-in helper 都在 returncode 成功断言失败 |
| `red-ctl-no-reaper` | 1 failed | 已提供真实 procps，故意不实时回收，原 inline-overrides 测试报 `process 106395 did not exit` |
| `green-all` | 24 passed, 0 skipped | 两个原测试模块完整回归；回收 7 个后代，remaining_children=[] |
| `green-neighbors` | 56 passed, 1 skipped | 目标模块加 3 个邻接模块；回收 14 个后代，remaining_children=[] |

RED ctl 的外部祖先在测试结束后才回收自身后代，以复现不实时回收的环境且不把
本次测试 zombie 留给 PID 1。GREEN 保持原 `assert_process_exits` / `kill -0`
语义，不以 process state 替代进程实际消失。

本容器 GREEN 命令前缀：

```bash
PATH=/workspace/history-failure-evidence/triage-next/procps-local/usr/bin:$PATH \
LD_LIBRARY_PATH=/workspace/history-failure-evidence/triage-next/procps-local/usr/lib/x86_64-linux-gnu \
.venv/bin/python tests/run_tls_ctl.py \
  --output /workspace/history-failure-evidence/resume-fixes-tls-ctl/green-all \
  --agent-dir /home/hermeswebui/.hermes/hermes-agent
```

邻接运行使用相同前缀，output 改为 `green-neighbors`，追加：

```text
tests/test_tls_aware_probe.py tests/test_ctl_script.py tests/test_tls_support.py
tests/test_ctl_foreign_server_guard.py tests/test_ctl_bash32_compat.py
```

`counts.json` 是解析 JUnit testcase 得到的计数，不以终端摘要推算。

父复核将执行器恢复为批准的正常 `--timeout=60`（初轮45秒为诊断遗留），
并以 `parent-60-gate` 重跑两个完整目标模块：返回码0，24通过、0跳过，
回收7个后代、remaining_children=[]。这不是增加超时规避原失败：原失败根因为
响应边界和子进程回收，修复前后的断言保持不变。
`.venv/bin/python -m ruff check tests/run_tls_ctl.py tests/test_tls_aware_probe.py`
实际返回 `All checks passed!`；`git diff --check` 通过。

## 明确边界 / 阻断

- 邻接测试现有 `test_start_on_default_port_allowed_when_unit_configured_elsewhere`
  因宿主 8787 已占用自行 skip（源码原有逻辑）。没有新增 skip，没有为获得全绿停止
  该端口服务。该一项未验证；两个目标模块没有 skip。
- 没有运行整个仓库套件，没有宣称跨平台已验证。专用 subreaper 执行器仅限 Linux，
  macOS/Windows 仍使用既有 `scripts/test.sh` 和正常回收子进程的宿主环境。
- 使用 agent 源码仅作依赖，所有可写 home/state 为隔离目录；未部署、未切换读路径、
  未删除数据库/备份/残片，未碰 `tmpz7959or8.cjs`，未提交或推送。
