# Python 3.12 EntryPoints 兼容性修复证据

## 结论与范围

仅修改 `tests/test_cli_entry_point.py`：将测试 helper 的整数下标改为
`next(iter(selected), None)`，保留原 `ep.value == EXPECTED_TARGET` 和
`callable(ep.load())` 断言与未安装时的 skip 语义。新增参数化回归使用真实
stdlib `EntryPoint` / `EntryPoints`，仅替换 metadata 查询边界，不伪造集合类型。
覆盖 0/1/2 个匹配项、错误名字、错误 group，以及重复匹配时返回首项。

Python 3.12 的 `EntryPoints.__getitem__` 按名字查找，`selected[0]` 导致
`KeyError: 0`；空结果原本通过，非空单项和多项在 RED 中均真实失败。
本 helper 的唯一业务断言调用者为同文件 installed wiring 测试，未修改产品代码。

## 固定源码及真实安装

- 源提交：`f509dcd675198dbee090fa6735cfca9795c58d96`。
- 复用既有 `remaining-skip-audit/source.tar`，SHA256：
  `7e1d061e4c16357afed969e3e0d4eb799127bdf6be718670917ff7f99b5fb987`。
- 解压后逐项比对原 manifest，再仅复制本次测试文件；不复制并行工作区布局修改。
- 新私有根：`/workspace/history-failure-evidence/entrypoint-compatibility/`。
- 最终测试文件 SHA256：`479db630d720269c2a5d63546490a6807d7c49101c9ff9c64d7ea198d9e176fc`。
- 原 `scripts/test.sh` 和原 `audit_guard.py` 字节一致；见 `final-identity.json`。
- Python **3.12.14**；pip 从私有源码真实构建、安装到私有 `installed/`，退出 0。
  wheel SHA256：`6698ffb817af078230b4280bf19925045edd4fdb80b0115fbeddff9256cadb51`。
  archive 无 Git metadata，仅提供 SCM 版本 `0.0.0+audit.f509dcd6`，未替换入口元数据。
- `installed-origin/output.log` 证明 distribution 来自私有 dist-info，
  `direct_url.json` 指向本次私有 source，且脱离源码路径后 `ep.load()` 真正加载
  `installed/bootstrap.py`，SHA256：
  `d0cfff984cae64d52ceea9f807c3db235817c32740128c2e9785870b461e9242`，与固定源码相同。

## 实跑记录

以下路径均相对私有证据根；每个 run 保留 `command.json`（完整 env-i 命令）、
`output.log`、`exit.json`、`junit.xml`、collection/events/isolation JSONL。
计数由 XML 解析汇总于 `results.json`，无跳过。

| run / XML | 结果 | exit |
|---|---|---:|
| `run-01-red/junit.xml` | 3 项：1 passed，2 failed，均为 KeyError: 0 | 1 |
| `run-02-green/junit.xml` | 完整入口文件 6 passed | 0 |
| `run-03-installed/junit.xml` | 原安装入口 wiring 1 passed | 0 |
| `run-04-neighbors/junit.xml` | 94 项：93 passed，1 failed（私有依赖缺失） | 1 |
| `run-05-neighbors/junit.xml` | 同上，首次依赖安装未实际落入私有 venv | 1 |
| `run-06-neighbors/junit.xml` | 入口及四个 bootstrap 邻接文件 94 passed | 0 |

邻接文件：`test_bootstrap_python_selection.py`、`test_bootstrap_dotenv.py`、
`test_bootstrap_foreground.py`、`test_bootstrap_discover_agent.py`。

邻接失败为 `test_package_python_discovers_agent_before_skip_install_gate`：
测试主动清空 PYTHONPATH 后子解释器无法 import yaml。初次 pip 认为共享只读
PYTHONPATH 中 PyYAML 已满足，没有写入私有环境；保留失败日志，随后使用
`--ignore-installed --no-deps PyYAML` 真正安装到新私有 venv，再完整重跑通过。
没有改测试断言、wrapper、产品或隔离策略来绕过失败。

## 风险、对策及验证边界

- 所有测试及 pip 安装通过 `env -i` 与原 Landlock guard；文件写权限仅私有根，
  共享 `.venv` 仅作为只读依赖导入，不安装、不清理。测试网络限制仍由原 guard
  按 fixture 随机端口实施，未放开 8787。
- HOME / HERMES_HOME / HERMES_WEBUI_STATE_DIR / TMPDIR 均为本次私有目录；
  未操作真实 profile、备份、原 tmp 或线上服务。原测试中展示的 8787 日志来自
  已有 mocked launcher 用例，不是启动/停止线上 8787 的操作。
- 改动只有测试 helper；重复匹配保持原首项语义，无依赖新增至仓库。
- 已验证 Python 3.12.14，未声称实跑 3.11/3.13 或全仓全量测试。
- smoke 只验证 metadata/value/load callable，不调用 bootstrap.main 启动用户服务。
- 实跑脚本 `verify.py`、`supplement.py`、`finalize.py` 及全部失败证据保留；
  脚本使用独占 run 目录，避免重跑覆盖历史，复验应使用新私有根。

## 回退与交付

未提交。父任务统一审查；回退仅撤销本次测试文件 diff 和本文，不触碰并行布局三文件。
若回退修复而保留新增回归，Python 3.12 非空集合的两项回归应再次失败；
已保存 `red-test.py` 供审计。无需产品迁移、真实环境重装或服务重启。
