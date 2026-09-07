# 会话读写分离：完整事件编码候选

状态：**完整设计候选待独立审查；未实现、未迁移、未启用新读，不构成上线许可。**
契约依据：[protocol-v2](session-read-write-separation-protocol-v2.md) R4/R5；R2/R3 写入资格与围栏保持前置，不能由编码正确替代。
范围：只定义五种事件、确定性归约及旧恢复映射；不执行 SQL、不修改功能代码、不提交。

## 1. 公共信封与身份

所有字段必填，不把缺字段等同 null；未知 schema_version 或 kind 拒绝，不猜测兼容。
| 字段 | 类型与约束 |
|---|---|
| schema_version | integer，固定 1 |
| scope_id | 非空 string，服务端由授权 profile 与实际会话存储身份解析，不接受客户端自选 |
| epoch | 非负安全整数；破坏性修改换 epoch |
| run_id | 非空 string，服务端注册且绑定 scope/epoch/revision/mutation/token |
| event_key | 非空 string，同一 run 的逻辑提交幂等键，重试必须复用 |
| session_seq | 正安全整数，验证通过后服务端持久分配，scope 内有序 |
| anchor_id | 非空 string，当前 run 注册的展示锚点；terminal 也引用该 run 主锚点 |
| kind | message_upsert / activity_upsert / tool_result / todo_replace / terminal |
| object_id | 非空 string，身份是 (scope_id,epoch,run_id,object_id)，类型和所属 anchor 不可变 |
| object_version | 正安全整数，首次 1，更新必须恰为前版本加一 |
| payload_hash | string，64 位小写十六进制（无前缀）；按下节规范计算 |
| payload | object，严格遵循对应 kind 字段表；完整替换，绝非文本 delta |

安全整数上限为 9007199254740991；布尔不能冒充整数。ID 为不透明值，不做大小写、Unicode 或路径规范化，不拼接成未验证文件路径。
提交接口另传服务端 writer_token、actual_revision、mutation_id 并验证 R3 绑定；这些权限值不向 GET/SSE 暴露、不进入展示正文。
相同 event_key 必须比较除 session_seq 外的整个规范信封，而非只比正文 hash；完全相同返回原 seq，不同即冲突。
旧 epoch/token/revision 或已关闭 run 的新增提交拒绝且不耗 seq；合法已提交重试只读回原结果，不重新授权写入。

## 2. 规范编码与 hash

固定编码名 `event-c14n-v1`：UTF-8，无 BOM，无尾换行；对象键按 Unicode 码点递增，数组保持原顺序，不进行 Unicode NFC 或换行转换。
字符串仅转义双引号、反斜杠及 U+0000–001F（统一小写 `\u00xx`），其他标量原样 UTF-8；拒绝孤立 surrogate 与重复 JSON 键。
数字只允许上述安全整数，十进制最短表示，禁止 -0、浮点、NaN/Infinity；原始参数若含小数应使用完整原始 JSON 文本 string，不能四舍五入。
`payload_hash = hex(SHA256(C(payload)))`；C 为本节编码。hash 不包括信封、自身、seq、收包时间或 token。
对象清单使用各对象最后版本的 payload_hash；幂等比较另覆盖 kind、anchor、身份和版本，因此相同内容不会导致跨对象合并。
未知多模态字段完整保留在 content 内参与 hash，但标记能力不支持并隔离展示；不得静默删字段后声称 COMPLETE 或可回滚。

## 3. 五种完整样例与字段语义

以下为人为设计的协议fixture，不是真实业务事件；hash由Python实际计算，但未执行事件系统。它们按数组顺序归约；终态清单排除terminal自身，避免自引用hash。同一对象不可换kind/anchor。工具调用登记来自message.tool_calls，tool_result必须与同run登记的ID/name/arguments一致；取消工具结果也必须明确status，不伪造执行成功。

```json
[
  {
    "schema_version": 1,
    "scope_id": "scope1",
    "epoch": 1,
    "run_id": "run1",
    "event_key": "event1",
    "session_seq": 1,
    "anchor_id": "anchor1",
    "kind": "message_upsert",
    "object_id": "m1",
    "object_version": 1,
    "payload_hash": "72ddac95b12943633b7dda63918024e152b4bab90e9d01da133964ae817df88a",
    "payload": {
      "role": "assistant",
      "content": "答案",
      "timestamp": 1,
      "tool_calls": [
        {
          "id": "t1",
          "name": "read_file",
          "arguments": "{\"path\":\"demo.txt\"}"
        }
      ],
      "display_order": 0
    }
  },
  {
    "schema_version": 1,
    "scope_id": "scope1",
    "epoch": 1,
    "run_id": "run1",
    "event_key": "event2",
    "session_seq": 2,
    "anchor_id": "anchor1",
    "kind": "activity_upsert",
    "object_id": "a1",
    "object_version": 1,
    "payload_hash": "29e60fb72619b0b5cda7693424412461e7b186a1aef5e9e98424f658c0686e7c",
    "payload": {
      "owner_anchor": "anchor1",
      "activity_kind": "process",
      "state": "completed",
      "content": "已读取",
      "display_order": 1
    }
  },
  {
    "schema_version": 1,
    "scope_id": "scope1",
    "epoch": 1,
    "run_id": "run1",
    "event_key": "event3",
    "session_seq": 3,
    "anchor_id": "anchor1",
    "kind": "tool_result",
    "object_id": "t1",
    "object_version": 1,
    "payload_hash": "cd2559206fffbdd8771d606e0966aa009d30ca8c46f7c60e6ba34a43ed8e73c5",
    "payload": {
      "tool_call_id": "t1",
      "name": "read_file",
      "arguments": "{\"path\":\"demo.txt\"}",
      "result": "hello",
      "status": "completed",
      "owner_anchor": "anchor1",
      "display_order": 2
    }
  },
  {
    "schema_version": 1,
    "scope_id": "scope1",
    "epoch": 1,
    "run_id": "run1",
    "event_key": "event4",
    "session_seq": 4,
    "anchor_id": "anchor1",
    "kind": "todo_replace",
    "object_id": "todo1",
    "object_version": 1,
    "payload_hash": "e7fddb504aad3220910340c54fc58ad06ae45cb856ae8bca3d91e6b33ca353ab",
    "payload": {
      "todos": [],
      "owner_anchor": "anchor1",
      "display_order": 3
    }
  },
  {
    "schema_version": 1,
    "scope_id": "scope1",
    "epoch": 1,
    "run_id": "run1",
    "event_key": "event5",
    "session_seq": 5,
    "anchor_id": "anchor1",
    "kind": "terminal",
    "object_id": "terminal1",
    "object_version": 1,
    "payload_hash": "e374029d90f26408af47848ccfa43ad071792f74c7a0aaef02c231a9eb4ba85f",
    "payload": {
      "outcome": "completed",
      "last_content_seq": 4,
      "object_manifest": [
        {
          "object_id": "m1",
          "object_version": 1,
          "payload_hash": "72ddac95b12943633b7dda63918024e152b4bab90e9d01da133964ae817df88a"
        },
        {
          "object_id": "a1",
          "object_version": 1,
          "payload_hash": "29e60fb72619b0b5cda7693424412461e7b186a1aef5e9e98424f658c0686e7c"
        },
        {
          "object_id": "t1",
          "object_version": 1,
          "payload_hash": "cd2559206fffbdd8771d606e0966aa009d30ca8c46f7c60e6ba34a43ed8e73c5"
        },
        {
          "object_id": "todo1",
          "object_version": 1,
          "payload_hash": "e7fddb504aad3220910340c54fc58ad06ae45cb856ae8bca3d91e6b33ca353ab"
        }
      ],
      "completeness": "COMPLETE"
    }
  }
]
```

message.role为user/assistant/system/tool中旧schema支持值；content完整字符串或受支持多模态数组；timestamp整数毫秒；tool_calls完整数组；display_order非负安全整数。activity_kind使用已登记活动类型，state为running/completed/failed/cancelled，content为完整展示内容。tool_result.status为completed/failed/cancelled，result完整字符串或受支持JSON；arguments为原始完整JSON文本，禁止预览截断。todo.todos完整对象数组，空数组有效；todos对象保持旧schema，不自行删除字段。terminal.outcome为completed/failed/cancelled/replaced，completeness为COMPLETE/INCOMPLETE；INCOMPLETE禁止active能力和归档发布，恢复组件保留。

归约：按当前epoch的session_seq递增应用，版本从1连续增长；全对象替换且保留未知字段原值，稳定展示按display_order/object_id。终态检查所有非terminal对象最后版本和hash集合精确相等、工具登记与终态匹配；缺一对象或多一对象均不能COMPLETE。版本缺口不能用后版本覆盖掩盖。终态后拒新事件；合法同键重试读回原seq。旧epoch不参与当前视图。

## 4. 旧恢复源字段映射与扩展

源码依据WebUI基线1ad7b693：api/models.py:1543–1555实际保存messages、tool_calls、anchor_activity_scenes及非私有extra字段；session_ops.py:918–933同时维护messages/context_messages/truncation_watermark/truncation_boundary。既有journal的done可能仅metadata，不能单独复原全部内容。

| 新规范 | 旧源字段/恢复方式 | 拟适配位置与验证 |
|---|---|---|
| message身份/role/content/timestamp | sidecar.messages中的对应角色正文与时序；旧消息未必有object_id/version | models序列化新增projection_compat_v1.identity_map，以旧稳定消息ID+anchor映射；严禁仅用文本或数组位置匹配 |
| tool_calls/result | sidecar.tool_calls与message工具关联；state.db消息工具调用/结果 | display_compat适配器将规范ID映射旧tool_call_id，分别重建参数和结果后比对；旧预览不算完整 |
| activity | sidecar.anchor_activity_scenes[anchor] | 使用原scene schema适配；保存所有完整对象及版本映射，不把摘要当活动全量 |
| todo | sidecar现有Todo对应快照，空列表须保留 | 未能确定旧版本字段时不猜名称；兼容扩展projection_compat_v1.todo_snapshot保存，旧恢复适配器必须新增读取路径 |
| terminal | journal终态+sidecar运行恢复字段 | 扩展projection_compat_v1.terminal保存outcome/last_content_seq/manifest，journal引用该快照hash；旧解码器不识别则禁止回滚到该版本 |
| 模型上下文 | sidecar.context_messages及Agent state.db的上下文消息链 | 单独从旧上下文恢复器读取，保留active=0、工具关联、父链和截断；不能由展示事件推导未展示的上下文 |
| epoch/seq/规范全对象 | 旧源无原生一一字段 | sidecar新增projection_compat_v1={schema_version,epoch,run_id,through_seq,objects,identity_map,todo_snapshot,terminal,source_manifest}；非私有extra机制能保存，不代表旧恢复器已会使用 |

每个新扩展必须与原字段双写并使用同一次原子sidecar保存；恢复适配器新建api/display_compat.py，在session_recovery及journal恢复装配入口调用，不修改Agent权威schema。旧state.db具体列适配需按目标Agent schema版本绑定并比对消息完整结构；无法建立无损映射的运行保持LEGACY/回滚BLOCKED，而不是删字段满足hash。完整上下文语义不由五种展示事件独立涵盖，source_manifest必须绑定真实上下文快照的版本和hash。

required_sources按run创建时固定：WebUI受控运行必需sidecar/state_db/journal三种；纯无Agent展示维护不创建可active run，走REWRITE稳定构建。缺任一源不可自动降成两源。新增映射必须提升verifier_version，旧凭证不得沿用。

## 5. 连续凭证算法和风险

对固定run/epoch取截至through_seq的全部持久事件，归约得到规范状态；持scope门重新打开每个required source，经明确版本恢复适配器重建其负责的字段集合，逐字段比对并计算规范hash，同时核对真实文件/DB身份和source revision。全部成功才在展示库事务插入各源receipt。receipt through_seq必须属于本run；连续覆盖指该run截至水位的所有事件，不要求跨run所有整数属于该run。事务提交前重新检查epoch/token/源manifest未变。

回滚覆盖集合为全部已确认run的全部事件；逐run逐源无缺口，源后续变化使旧凭证不可复用。回滚屏障中重验所有目标，不能只查最大through_seq或verified_at。写成功凭证前退出重读；凭证前源失败保持UNCERTAIN；不重跑工具。

风险/措施/验证/回退：完整对象与兼容扩展放大写入→未确认更新合并、字节预算→T24/T38→背压但不删已确认数据；旧schema能力不匹配→绑定版本和显式映射→T25/T42→LEGACY、保留恢复组件；多模态浮点不在v1规范域→保留原源并拒新能力，不转换正文→T09/T24→旧路径；上下文与展示不等价→双轨逐字段验证→T14/T25→不得宣称可独立模型恢复。
