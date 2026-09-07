# 编码设计：展示库候选schema

状态：CANDIDATE_PENDING_INDEPENDENT_REVIEW；SQL NOT_RUN。仅Markdown设计，不是已验证migration。配套transactions、writers、events文档组成候选；生产保持LEGACY。以下采用SQLite，所有连接必须foreign_keys=ON、synchronous=FULL，初始化WAL；连接验证失败拒绝新读写。时间用整数UTC毫秒；租约用同一可信服务时钟，进程启动ID避免PID复用。所有DELETE默认拒绝（本阶段无清理API）。

## 1. 完整表定义

scope主键是不透明服务端身份；profile_identity包含规范home及源文件身份，不取客户端路径。revision按scope递增，seq按scope连续递增且跨epoch不复用。JSON为已验证规范文本；对象hash、源manifest hash由应用计算，SQLite不冒充密码学验证器。

```sql
CREATE TABLE scopes(
 scope_id TEXT PRIMARY KEY NOT NULL, profile_identity TEXT NOT NULL, session_id TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch>=0), revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 last_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_seq>=0), eligibility TEXT NOT NULL DEFAULT 'LEGACY' CHECK(eligibility IN('LEGACY','ELIGIBLE')),
 eligibility_version INTEGER NOT NULL DEFAULT 0 CHECK(eligibility_version>=0), dirty_reason TEXT,
 published_generation TEXT, UNIQUE(profile_identity,session_id),
 FOREIGN KEY(scope_id,published_generation) REFERENCES generations(scope_id,generation) DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE mutations(
 scope_id TEXT NOT NULL REFERENCES scopes(scope_id), mutation_id TEXT NOT NULL, operation_key TEXT NOT NULL, input_hash TEXT NOT NULL,
 epoch INTEGER NOT NULL CHECK(epoch>=0), actual_revision INTEGER NOT NULL CHECK(actual_revision>0),
 writer_token TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN('APPEND','REWRITE')),
 state TEXT NOT NULL CHECK(state IN('PREPARED','UNCERTAIN','SEALED')), source_manifest_sha TEXT,
 created_at INTEGER NOT NULL CHECK(created_at>=0), sealed_at INTEGER CHECK(sealed_at>=0),
 PRIMARY KEY(scope_id,mutation_id), UNIQUE(scope_id,operation_key), UNIQUE(scope_id,actual_revision),
 UNIQUE(scope_id,mutation_id,epoch,actual_revision,writer_token),
 CHECK(state!='SEALED' OR (sealed_at IS NOT NULL AND source_manifest_sha IS NOT NULL)));
CREATE UNIQUE INDEX mutation_open_scope ON mutations(scope_id) WHERE state IN('PREPARED','UNCERTAIN');
CREATE TABLE runs(
 scope_id TEXT NOT NULL, run_id TEXT NOT NULL, mutation_id TEXT NOT NULL, epoch INTEGER NOT NULL, actual_revision INTEGER NOT NULL, writer_token TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN('OPEN','TERMINAL_PENDING','SEALED')),
 base_generation TEXT, base_revision INTEGER CHECK(base_revision>=0), base_covered_seq INTEGER NOT NULL CHECK(base_covered_seq>=0),
 terminal_seq INTEGER, active_allowed INTEGER NOT NULL DEFAULT 0 CHECK(active_allowed IN(0,1)),
 PRIMARY KEY(scope_id,run_id), UNIQUE(scope_id,mutation_id), UNIQUE(scope_id,run_id,epoch,actual_revision,writer_token),
 FOREIGN KEY(scope_id,mutation_id,epoch,actual_revision,writer_token) REFERENCES mutations(scope_id,mutation_id,epoch,actual_revision,writer_token),
 FOREIGN KEY(scope_id,base_generation) REFERENCES generations(scope_id,generation),
 FOREIGN KEY(scope_id,terminal_seq) REFERENCES events(scope_id,session_seq) DEFERRABLE INITIALLY DEFERRED,
 CHECK(active_allowed=0 OR (base_generation IS NOT NULL AND base_revision IS NOT NULL)));
CREATE TABLE events(
 scope_id TEXT NOT NULL, session_seq INTEGER NOT NULL CHECK(session_seq>0), epoch INTEGER NOT NULL, run_id TEXT NOT NULL,
 actual_revision INTEGER NOT NULL, writer_token TEXT NOT NULL, event_key TEXT NOT NULL,
 schema_version INTEGER NOT NULL CHECK(schema_version=1), anchor_id TEXT NOT NULL, object_id TEXT NOT NULL,
 object_version INTEGER NOT NULL CHECK(object_version>0), kind TEXT NOT NULL CHECK(kind IN('message_upsert','activity_upsert','tool_result','todo_replace','terminal')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
 created_at INTEGER NOT NULL CHECK(created_at>=0), PRIMARY KEY(scope_id,session_seq), UNIQUE(scope_id,run_id,event_key),
 UNIQUE(scope_id,run_id,object_id,object_version),
 FOREIGN KEY(scope_id,run_id,epoch,actual_revision,writer_token) REFERENCES runs(scope_id,run_id,epoch,actual_revision,writer_token));
CREATE UNIQUE INDEX run_terminal_once ON events(scope_id,run_id) WHERE kind='terminal';
CREATE INDEX event_replay ON events(scope_id,epoch,session_seq);
CREATE TABLE objects(
 scope_id TEXT NOT NULL, run_id TEXT NOT NULL, object_id TEXT NOT NULL, object_version INTEGER NOT NULL CHECK(object_version>0),
 last_seq INTEGER NOT NULL, kind TEXT NOT NULL, object_hash TEXT NOT NULL CHECK(length(object_hash)=64),
 object_json TEXT NOT NULL CHECK(json_valid(object_json)), display_order INTEGER NOT NULL CHECK(display_order>=0),
 PRIMARY KEY(scope_id,run_id,object_id),
 FOREIGN KEY(scope_id,run_id,object_id,object_version) REFERENCES events(scope_id,run_id,object_id,object_version),
 FOREIGN KEY(scope_id,last_seq) REFERENCES events(scope_id,session_seq));
CREATE TABLE jobs(
 scope_id TEXT NOT NULL REFERENCES scopes(scope_id), job_id TEXT NOT NULL, target_revision INTEGER NOT NULL CHECK(target_revision>0),
 state TEXT NOT NULL CHECK(state IN('PENDING','LEASED','RETRY','BLOCKED','SUPERSEDED','PUBLISHED')),
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0), fence INTEGER NOT NULL DEFAULT 0 CHECK(fence>=0),
 retry_at INTEGER NOT NULL DEFAULT 0 CHECK(retry_at>=0), reason TEXT,
 PRIMARY KEY(scope_id,job_id), UNIQUE(scope_id,target_revision));
CREATE INDEX job_ready ON jobs(state,retry_at);
CREATE TABLE job_attempts(
 scope_id TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt>0), fence INTEGER NOT NULL CHECK(fence>0),
 epoch INTEGER NOT NULL CHECK(epoch>=0), target_revision INTEGER NOT NULL CHECK(target_revision>0), target_mutation_id TEXT NOT NULL,
 base_generation TEXT, covered_seq INTEGER NOT NULL CHECK(covered_seq>=0), source_manifest_sha TEXT NOT NULL,
 owner TEXT NOT NULL, lease_until INTEGER NOT NULL CHECK(lease_until>=0),
 state TEXT NOT NULL CHECK(state IN('LEASED','RETRY','BLOCKED','SUPERSEDED','PUBLISHED')),
 PRIMARY KEY(scope_id,job_id,attempt), UNIQUE(scope_id,job_id,fence),
 FOREIGN KEY(scope_id,job_id) REFERENCES jobs(scope_id,job_id),
 FOREIGN KEY(scope_id,target_mutation_id) REFERENCES mutations(scope_id,mutation_id),
 FOREIGN KEY(scope_id,base_generation) REFERENCES generations(scope_id,generation));
CREATE TABLE generations(
 scope_id TEXT NOT NULL, generation TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN('BUILDING','VERIFIED','PUBLISHED','REJECTED')),
 row_count INTEGER NOT NULL CHECK(row_count>=0), manifest_sha TEXT NOT NULL CHECK(length(manifest_sha)=64),
 PRIMARY KEY(scope_id,generation), UNIQUE(scope_id,job_id,attempt),
 FOREIGN KEY(scope_id,job_id,attempt) REFERENCES job_attempts(scope_id,job_id,attempt));
CREATE TABLE segments(
 scope_id TEXT NOT NULL REFERENCES scopes(scope_id), segment_id TEXT NOT NULL, row_count INTEGER NOT NULL CHECK(row_count>=0),
 byte_count INTEGER NOT NULL CHECK(byte_count>=0), content_sha TEXT NOT NULL CHECK(length(content_sha)=64),
 sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN(0,1)), PRIMARY KEY(scope_id,segment_id));
CREATE TABLE rows(
 scope_id TEXT NOT NULL, segment_id TEXT NOT NULL, row_offset INTEGER NOT NULL CHECK(row_offset>=0),
 object_identity TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), row_hash TEXT NOT NULL CHECK(length(row_hash)=64),
 PRIMARY KEY(scope_id,segment_id,row_offset), UNIQUE(scope_id,segment_id,object_identity),
 FOREIGN KEY(scope_id,segment_id) REFERENCES segments(scope_id,segment_id));
CREATE TABLE generation_segments(
 scope_id TEXT NOT NULL, generation TEXT NOT NULL, segment_index INTEGER NOT NULL CHECK(segment_index>=0), segment_id TEXT NOT NULL,
 start_offset INTEGER NOT NULL CHECK(start_offset>=0), PRIMARY KEY(scope_id,generation,segment_index), UNIQUE(scope_id,generation,segment_id),
 FOREIGN KEY(scope_id,generation) REFERENCES generations(scope_id,generation), FOREIGN KEY(scope_id,segment_id) REFERENCES segments(scope_id,segment_id));
CREATE TABLE scene_bounds(
 scope_id TEXT NOT NULL, generation TEXT NOT NULL, anchor_id TEXT NOT NULL,
 first_offset INTEGER NOT NULL CHECK(first_offset>=0), end_offset INTEGER NOT NULL CHECK(end_offset>=first_offset),
 PRIMARY KEY(scope_id,generation,anchor_id), FOREIGN KEY(scope_id,generation) REFERENCES generations(scope_id,generation));
CREATE INDEX scene_page ON scene_bounds(scope_id,generation,first_offset,end_offset);
CREATE TABLE required_sources(
 scope_id TEXT NOT NULL, run_id TEXT NOT NULL, source_kind TEXT NOT NULL CHECK(source_kind IN('sidecar','state_db','journal')),
 source_identity TEXT NOT NULL, mapping_version TEXT NOT NULL, PRIMARY KEY(scope_id,run_id,source_kind),
 FOREIGN KEY(scope_id,run_id) REFERENCES runs(scope_id,run_id));
CREATE TABLE compat_receipts(
 scope_id TEXT NOT NULL, run_id TEXT NOT NULL, source_kind TEXT NOT NULL, through_seq INTEGER NOT NULL CHECK(through_seq>0),
 epoch INTEGER NOT NULL CHECK(epoch>=0), source_identity TEXT NOT NULL, source_revision TEXT NOT NULL,
 canonical_hash TEXT NOT NULL CHECK(length(canonical_hash)=64), verifier_version TEXT NOT NULL, verified_at INTEGER NOT NULL CHECK(verified_at>=0),
 PRIMARY KEY(scope_id,run_id,source_kind,through_seq),
 FOREIGN KEY(scope_id,run_id,source_kind) REFERENCES required_sources(scope_id,run_id,source_kind),
 FOREIGN KEY(scope_id,through_seq) REFERENCES events(scope_id,session_seq));
CREATE TABLE recovery_tasks(
 scope_id TEXT NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN('PENDING','RUNNING','BLOCKED','DONE')),
 owner TEXT, lease_until INTEGER, reason TEXT, PRIMARY KEY(scope_id,run_id),
 FOREIGN KEY(scope_id,run_id) REFERENCES runs(scope_id,run_id));
CREATE TABLE control_barriers(
 barrier_id TEXT PRIMARY KEY NOT NULL, config_generation INTEGER NOT NULL CHECK(config_generation>0),
 state TEXT NOT NULL CHECK(state IN('DRAINING','VERIFIED','BLOCKED')), manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)));
CREATE TABLE barrier_members(
 barrier_id TEXT NOT NULL REFERENCES control_barriers(barrier_id), process_start_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN('PENDING','DRAINED','PROVEN_STOPPED')), evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 PRIMARY KEY(barrier_id,process_start_id));
```

所有FK默认NO ACTION，无级联清理。generation的epoch/revision/base/fence等输入规范化存于唯一attempt，不复制可分歧字段；查询必须连接attempt，不能从scope当前值补猜。DDL创建顺序允许先声明后置表引用；事务提交前必须满足FK。bootstrap空基线也通过revision>0的REWRITE、SEALED和普通job发布，不能插入伪已发布版本。

## 2. 必须生成的数据库拒写trigger清单

下列是确定性展开规格，不省略保护表：M0将每行展开为具名CREATE TRIGGER，禁止靠调用者约定代替。静态展开不等于运行验证。

| 表 | INSERT门 | UPDATE门 | DELETE门 |
|---|---|---|---|
| scopes | LEGACY初始、seq/revision/epoch为0 | seq只+1；epoch/revision不倒退；指针见发布门 | 拒绝 |
| mutations | 当前scope绑定、PREPARED | 仅PREPARED→UNCERTAIN/SEALED、UNCERTAIN→SEALED；绑定不可变 | 拒绝 |
| runs | PREPARED匹配当前绑定 | 仅OPEN→TERMINAL_PENDING→SEALED；基线/绑定不可变；active只1→0 | 拒绝 |
| events | 当前OPEN/PREPARED且scope资格/epoch/revision/token匹配，seq=scope.last_seq+1，版本连续，terminal唯一 | 拒绝 | 拒绝 |
| objects | 对应完整event且v1 | 对应event、同身份v+1；终态后拒绝 | 拒绝 |
| jobs | PENDING且目标mutation存在 | 下方状态图；attempt/fence仅领取+1 | 拒绝 |
| job_attempts | 对应job当前attempt/fence、目标SEALED | 输入不变；LEASED→RETRY/BLOCKED/SUPERSEDED/PUBLISHED；仅LEASED允许同owner续租 | 拒绝 |
| generations | BUILDING且对应有效attempt | 内容不变，仅BUILDING→VERIFIED/REJECTED，VERIFIED→PUBLISHED/REJECTED | 拒绝 |
| segments | sealed=0 | 仅sealed 0→1，其余字段不变 | 拒绝 |
| rows | segment未sealed | 拒绝 | 拒绝 |
| generation_segments/scene_bounds | generation BUILDING且引用段sealed | 拒绝 | 拒绝 |
| required_sources | run OPEN且尚无首事件 | 拒绝 | 拒绝 |
| compat_receipts | 同run事件范围、epoch及required_sources身份匹配 | 拒绝 | 拒绝 |
| recovery_tasks | terminal同事务 | PENDING→RUNNING；RUNNING→PENDING/BLOCKED/DONE；BLOCKED→PENDING须授权 | 拒绝 |
| control_barriers | DRAINING | DRAINING→VERIFIED/BLOCKED；成员未全确认禁止VERIFIED | 拒绝 |
| barrier_members | barrier DRAINING且清单包含启动ID | PENDING→DRAINED/PROVEN_STOPPED且证据非空 | 拒绝 |

job状态图：PENDING/RETRY→LEASED；LEASED→RETRY/BLOCKED/SUPERSEDED/PUBLISHED；BLOCKED/SUPERSEDED→PENDING需要记录授权与源重验；PUBLISHED为终态。相同状态只允许所列续租或原因补充，不得修改身份/输入。mutation UNCERTAIN恢复必须逐源核验；数据库只能约束状态图，不能证明外部核验真伪。

事件AFTER INSERT必须原子更新scope.last_seq到NEW.session_seq，故commit_event不能先手工增seq再插入。对象索引与terminal任务登记由同事务入口写，事务提交前验证完整；terminal写后再更新run状态。范围连续指有效当前epoch事件，旧epoch序号保留，不能把跨epoch数字区间当可重放集合。

发布指针BEFORE UPDATE保护：连接NEW.generation→attempt→job→mutation；要求VERIFIED候选、job/attempt LEASED且attempt/fence当前、目标mutation SEALED、无开放mutation、scope ELIGIBLE且epoch/revision匹配、OLD指针IS attempt.base；所有段sealed、段数/row_count/offset及场景范围一致。外部源hash、租约时间及owner由持源门入口再次验证。先CAS指针，再将generation/job/attempt置PUBLISHED，同事务结束；发布状态更新须指针已命中该generation。禁止独立改变PUBLISHED状态。指针FK仅证明存在，不取代上述保护。

## 3. 事务级验证与尚未冻结项

所有写入口在提交前运行invariant_check(scope,changed_run/job)：terminal_seq属于本run终态；对象索引last_seq对应其版本；SEALED run与mutation同步；恢复任务存在；发布三状态与指针一致；receipt范围无空洞且全required_sources覆盖。验证失败ROLLBACK。SQLite无跨表通用提交trigger，因此服务写权限/事务入口是必需边界，DDL不能对任意同UID SQL提供安全保证。

风险：触发器复杂可能死锁或遗漏；措施为固定锁顺序、短事务和反例驱动。T21/T23/T26逐一尝试非法转换、跨scope、旧token、NULL基线、旧fence和中途退出；失败维持旧读。JSON和哈希的SQLite验证能力有限；应用schema验证/规范hash与T24/T25读回不可省。重复字段已规范化以减少漂移；新增查询JOIN成本由T16/T40测量，不用文档推算收益。

明确完成边界：全表字段与保护规则已定义；本稿尚以展开规格描述trigger而非全量CREATE TRIGGER正文，不能标“完整DDL已交付/冻结”。独立审查必须把此项列为未决，直到展开正文并静态核对，随后经授权执行M0才可报告真实SQL验证。

## 4. 已展开的基础trigger正文

以下按§2规则展开第一组数据库硬约束，仅文本生成，未执行SQL。其余发布复合门及提交检查按§2/3，仍须独立终审识别完整性缺口。

```sql
CREATE TRIGGER scopes_no_delete BEFORE DELETE ON scopes BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER mutations_no_delete BEFORE DELETE ON mutations BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER objects_no_delete BEFORE DELETE ON objects BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER jobs_no_delete BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER job_attempts_no_delete BEFORE DELETE ON job_attempts BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER generations_no_delete BEFORE DELETE ON generations BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER segments_no_delete BEFORE DELETE ON segments BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER rows_no_delete BEFORE DELETE ON rows BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER generation_segments_no_delete BEFORE DELETE ON generation_segments BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER scene_bounds_no_delete BEFORE DELETE ON scene_bounds BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER required_sources_no_delete BEFORE DELETE ON required_sources BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER compat_receipts_no_delete BEFORE DELETE ON compat_receipts BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER recovery_tasks_no_delete BEFORE DELETE ON recovery_tasks BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER control_barriers_no_delete BEFORE DELETE ON control_barriers BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER barrier_members_no_delete BEFORE DELETE ON barrier_members BEGIN SELECT RAISE(ABORT,'retention_no_delete'); END;
CREATE TRIGGER events_immutable BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER rows_immutable BEFORE UPDATE ON rows BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER generation_segments_immutable BEFORE UPDATE ON generation_segments BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER scene_bounds_immutable BEFORE UPDATE ON scene_bounds BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER required_sources_immutable BEFORE UPDATE ON required_sources BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER compat_receipts_immutable BEFORE UPDATE ON compat_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER rows_insert_guard BEFORE INSERT ON rows WHEN NOT EXISTS(SELECT 1 FROM segments p WHERE p.scope_id=NEW.scope_id AND p.segment_id=NEW.segment_id AND p.sealed=0) BEGIN SELECT RAISE(ABORT,'closed_parent'); END;
CREATE TRIGGER generation_segments_insert_guard BEFORE INSERT ON generation_segments WHEN NOT EXISTS(SELECT 1 FROM generations p WHERE p.scope_id=NEW.scope_id AND p.generation=NEW.generation AND p.state='BUILDING') BEGIN SELECT RAISE(ABORT,'closed_parent'); END;
CREATE TRIGGER scene_bounds_insert_guard BEFORE INSERT ON scene_bounds WHEN NOT EXISTS(SELECT 1 FROM generations p WHERE p.scope_id=NEW.scope_id AND p.generation=NEW.generation AND p.state='BUILDING') BEGIN SELECT RAISE(ABORT,'closed_parent'); END;
CREATE TRIGGER events_binding BEFORE INSERT ON events WHEN NOT EXISTS(
 SELECT 1 FROM runs r JOIN mutations m ON m.scope_id=r.scope_id AND m.mutation_id=r.mutation_id
 JOIN scopes s ON s.scope_id=r.scope_id WHERE r.scope_id=NEW.scope_id AND r.run_id=NEW.run_id
 AND r.state='OPEN' AND m.state='PREPARED' AND s.eligibility='ELIGIBLE'
 AND s.epoch=NEW.epoch AND s.revision=NEW.actual_revision AND m.writer_token=NEW.writer_token
 AND NEW.session_seq=s.last_seq+1)
 BEGIN SELECT RAISE(ABORT,'invalid_binding'); END;
CREATE TRIGGER events_version BEFORE INSERT ON events WHEN NEW.object_version !=
 COALESCE((SELECT MAX(object_version)+1 FROM events WHERE scope_id=NEW.scope_id AND run_id=NEW.run_id AND object_id=NEW.object_id),1)
 BEGIN SELECT RAISE(ABORT,'version_gap'); END;
CREATE TRIGGER events_advance AFTER INSERT ON events BEGIN UPDATE scopes SET last_seq=NEW.session_seq WHERE scope_id=NEW.scope_id; END;
CREATE TRIGGER scopes_seq BEFORE UPDATE OF last_seq ON scopes WHEN NEW.last_seq!=OLD.last_seq+1 OR NOT EXISTS(
 SELECT 1 FROM events WHERE scope_id=NEW.scope_id AND session_seq=NEW.last_seq)
 BEGIN SELECT RAISE(ABORT,'sequence_gap'); END;
CREATE TRIGGER scopes_monotonic BEFORE UPDATE ON scopes WHEN NEW.epoch<OLD.epoch OR NEW.revision<OLD.revision OR NEW.eligibility_version<OLD.eligibility_version
 BEGIN SELECT RAISE(ABORT,'scope_regression'); END;
CREATE TRIGGER segments_seal BEFORE UPDATE ON segments WHEN OLD.sealed!=0 OR NEW.sealed!=1 OR NEW.scope_id IS NOT OLD.scope_id OR NEW.segment_id IS NOT OLD.segment_id OR NEW.row_count IS NOT OLD.row_count OR NEW.byte_count IS NOT OLD.byte_count OR NEW.content_sha IS NOT OLD.content_sha OR NEW.row_count!=(SELECT count(*) FROM rows WHERE scope_id=OLD.scope_id AND segment_id=OLD.segment_id)
 BEGIN SELECT RAISE(ABORT,'segment_mutation'); END;
CREATE TRIGGER mutations_transition BEFORE UPDATE OF state ON mutations WHEN NEW.state!=OLD.state AND NOT((OLD.state='PREPARED' AND NEW.state IN('UNCERTAIN','SEALED')) OR (OLD.state='UNCERTAIN' AND NEW.state IN('SEALED'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER runs_transition BEFORE UPDATE OF state ON runs WHEN NEW.state!=OLD.state AND NOT((OLD.state='OPEN' AND NEW.state IN('TERMINAL_PENDING')) OR (OLD.state='TERMINAL_PENDING' AND NEW.state IN('SEALED'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER jobs_transition BEFORE UPDATE OF state ON jobs WHEN NEW.state!=OLD.state AND NOT((OLD.state='PENDING' AND NEW.state IN('LEASED')) OR (OLD.state='RETRY' AND NEW.state IN('LEASED')) OR (OLD.state='LEASED' AND NEW.state IN('RETRY','BLOCKED','SUPERSEDED','PUBLISHED')) OR (OLD.state='BLOCKED' AND NEW.state IN('PENDING')) OR (OLD.state='SUPERSEDED' AND NEW.state IN('PENDING'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER job_attempts_transition BEFORE UPDATE OF state ON job_attempts WHEN NEW.state!=OLD.state AND NOT((OLD.state='LEASED' AND NEW.state IN('RETRY','BLOCKED','SUPERSEDED','PUBLISHED'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER generations_transition BEFORE UPDATE OF state ON generations WHEN NEW.state!=OLD.state AND NOT((OLD.state='BUILDING' AND NEW.state IN('VERIFIED','REJECTED')) OR (OLD.state='VERIFIED' AND NEW.state IN('PUBLISHED','REJECTED'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER recovery_tasks_transition BEFORE UPDATE OF state ON recovery_tasks WHEN NEW.state!=OLD.state AND NOT((OLD.state='PENDING' AND NEW.state IN('RUNNING')) OR (OLD.state='RUNNING' AND NEW.state IN('PENDING','BLOCKED','DONE')) OR (OLD.state='BLOCKED' AND NEW.state IN('PENDING'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER control_barriers_transition BEFORE UPDATE OF state ON control_barriers WHEN NEW.state!=OLD.state AND NOT((OLD.state='DRAINING' AND NEW.state IN('VERIFIED','BLOCKED'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER barrier_members_transition BEFORE UPDATE OF state ON barrier_members WHEN NEW.state!=OLD.state AND NOT((OLD.state='PENDING' AND NEW.state IN('DRAINED','PROVEN_STOPPED'))) BEGIN SELECT RAISE(ABORT,'illegal_transition'); END;
CREATE TRIGGER mutations_input_immutable BEFORE UPDATE ON mutations WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.mutation_id IS NOT OLD.mutation_id OR NEW.operation_key IS NOT OLD.operation_key OR NEW.input_hash IS NOT OLD.input_hash OR NEW.epoch IS NOT OLD.epoch OR NEW.actual_revision IS NOT OLD.actual_revision OR NEW.writer_token IS NOT OLD.writer_token OR NEW.kind IS NOT OLD.kind OR NEW.created_at IS NOT OLD.created_at BEGIN SELECT RAISE(ABORT,'input_mutation'); END;
CREATE TRIGGER runs_input_immutable BEFORE UPDATE ON runs WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.run_id IS NOT OLD.run_id OR NEW.mutation_id IS NOT OLD.mutation_id OR NEW.epoch IS NOT OLD.epoch OR NEW.actual_revision IS NOT OLD.actual_revision OR NEW.writer_token IS NOT OLD.writer_token OR NEW.base_generation IS NOT OLD.base_generation OR NEW.base_revision IS NOT OLD.base_revision OR NEW.base_covered_seq IS NOT OLD.base_covered_seq BEGIN SELECT RAISE(ABORT,'input_mutation'); END;
CREATE TRIGGER jobs_input_immutable BEFORE UPDATE ON jobs WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.job_id IS NOT OLD.job_id OR NEW.target_revision IS NOT OLD.target_revision BEGIN SELECT RAISE(ABORT,'input_mutation'); END;
CREATE TRIGGER job_attempts_input_immutable BEFORE UPDATE ON job_attempts WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.job_id IS NOT OLD.job_id OR NEW.attempt IS NOT OLD.attempt OR NEW.fence IS NOT OLD.fence OR NEW.epoch IS NOT OLD.epoch OR NEW.target_revision IS NOT OLD.target_revision OR NEW.target_mutation_id IS NOT OLD.target_mutation_id OR NEW.base_generation IS NOT OLD.base_generation OR NEW.covered_seq IS NOT OLD.covered_seq OR NEW.source_manifest_sha IS NOT OLD.source_manifest_sha OR NEW.owner IS NOT OLD.owner BEGIN SELECT RAISE(ABORT,'input_mutation'); END;
CREATE TRIGGER generations_input_immutable BEFORE UPDATE ON generations WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.generation IS NOT OLD.generation OR NEW.job_id IS NOT OLD.job_id OR NEW.attempt IS NOT OLD.attempt OR NEW.row_count IS NOT OLD.row_count OR NEW.manifest_sha IS NOT OLD.manifest_sha BEGIN SELECT RAISE(ABORT,'input_mutation'); END;
CREATE TRIGGER required_sources_input_immutable BEFORE UPDATE ON required_sources WHEN NEW.scope_id IS NOT OLD.scope_id OR NEW.run_id IS NOT OLD.run_id OR NEW.source_kind IS NOT OLD.source_kind OR NEW.source_identity IS NOT OLD.source_identity OR NEW.mapping_version IS NOT OLD.mapping_version BEGIN SELECT RAISE(ABORT,'input_mutation'); END;
```
