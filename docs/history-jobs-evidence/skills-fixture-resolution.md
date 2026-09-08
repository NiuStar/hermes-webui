# Sprint 3：四项空技能 skip 关闭

## 根因与范围

审计的四项是 `test_skills_list`、`test_skills_list_has_required_fields`、`test_skills_content_known`、`test_skills_search_returns_subset`。私有空 HOME 下没有技能是合法初始状态，不能由空列表直接归因为跨测试污染。原测试依赖个人技能、空列表 skip，且 content 返回 error 也会成功；search 测试没有执行筛选。

仅修改测试，不修改生产代码、conftest 或个人技能。`owned_skills` 在服务启动后，于 conftest 的 `TEST_STATE_DIR/profiles/` 新建唯一临时 profile，写入两项固定 name/description/category/body 的自有技能。拒绝 profiles 符号链接，不触碰 `TEST_STATE_DIR/skills`（它可能链接个人目录）。请求携带独立 `hermes_profile` cookie，服务依据固定 `HERMES_BASE_HOME=TEST_STATE_DIR` 解析；无需切换或恢复全局 profile。临时 profile 由 context manager 在正常/异常退出时清理。

四测试强制检查精确技能集合、字段值、完整 content，缺失/error 均失败。API 仅支持 category 筛选，文本搜索在 `static/panels.js::renderSkills` 客户端执行；保留原 nodeid，以真实 `?category=` 验证单项严格子集和无匹配结果，不声称验证浏览器文本搜索。

## 真实执行证据

独立目录：`/workspace/history-failure-evidence/resume-fixes-skills/`。
`run.py` 使用仓库 `./scripts/test.sh`，每轮创建独立私有 HOME、test-state，清除凭据/生产路径环境，读取已安装 agent 代码，启用测试网络阻断，未固定端口，避免 reaper 干扰其他服务。每轮日志、JUnit、执行参数及状态均保留。

| 证据 | 结果 | 含义 |
|---|---|---|
| `baseline.xml` | 1 pass / 4 skip | 修改前空 HOME；不是 RED |
| `red-missing-v2.xml` | 1 pass / 4 fail / 0 skip | 仅抑制自有 SKILL.md 写入，真实 HTTP 返回空列表/技能缺失；四项均行为失败 |
| `green-v2.xml` | 31 pass / 0 skip | 整个原 test_sprint3.py |
| `lifecycle.xml` | 32 pass / 0 skip | 加生命周期测试后完整回归 |
| `red-filter.xml` | 1 fail | 保留技能，只从真实请求移除 category，筛选测试拒绝返回全集 |

`skills_negative.py`、`skills_filter_negative.py` 仅是证据目录内的 opt-in 负对照插件，不修改生产实现，不伪造 HTTP 响应。初轮 `red-missing`/`green` 有 conftest 导入名错误，已改为 `tests.conftest`；这些 setup errors 不算行为 RED，保留原始证据不覆盖。

新增 `tests/test_sprint3_skills_fixture.py` 实际请求列表和内容，并模拟消费者异常，验证只删除自有 profile、已有 profile 集合保持不变、前后 API active profile 不变。

未跑全仓全量测试；未提交或推送。未删除生产数据、数据库或备份；conftest 自行清理每轮新建测试状态。其他 agent 的并行修改不属于本任务。
