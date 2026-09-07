"""Offline immutable history candidates, NOT a production publication authority.

The shadow database is deliberately separate from the protocol display store.
No caller can grant ELIGIBLE here: actual source-writer isolation is unproven.
Only build reads full history; pages use persisted offset/renderable indexes.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import uuid


@dataclass(frozen=True)
class ScopeKey:
    profile_identity: str
    session_id: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in (self.profile_identity, self.session_id)):
            raise ValueError("explicit scope identity required")

    @property
    def key(self):
        return json.dumps([self.profile_identity, self.session_id], ensure_ascii=False)


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shadow_generations(
                    scope TEXT NOT NULL, generation TEXT NOT NULL, total INTEGER NOT NULL,
                    PRIMARY KEY(scope,generation));
                CREATE TABLE IF NOT EXISTS shadow_captures(
                    scope TEXT NOT NULL, generation TEXT NOT NULL, capture_sha256 TEXT NOT NULL,
                    manifest TEXT NOT NULL, PRIMARY KEY(scope,generation),
                    FOREIGN KEY(scope,generation) REFERENCES shadow_generations(scope,generation));
                CREATE TABLE IF NOT EXISTS shadow_rows(
                    scope TEXT NOT NULL, generation TEXT NOT NULL, pos INTEGER NOT NULL,
                    renderable INTEGER NOT NULL, tail_end INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope,generation,pos),
                    FOREIGN KEY(scope,generation) REFERENCES shadow_generations(scope,generation));
                CREATE INDEX IF NOT EXISTS shadow_visible ON shadow_rows(scope,generation,renderable,pos);
                CREATE TABLE IF NOT EXISTS shadow_tools(
                    scope TEXT NOT NULL, generation TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    anchor INTEGER, payload TEXT NOT NULL,
                    PRIMARY KEY(scope,generation,ordinal),
                    FOREIGN KEY(scope,generation) REFERENCES shadow_generations(scope,generation));
                CREATE TABLE IF NOT EXISTS shadow_scenes(
                    scope TEXT NOT NULL, generation TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    ref TEXT NOT NULL, anchor INTEGER, record_key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope,generation,ordinal),
                    FOREIGN KEY(scope,generation) REFERENCES shadow_generations(scope,generation));
                CREATE INDEX IF NOT EXISTS shadow_scene_ref ON shadow_scenes(scope,generation,ref);
                CREATE INDEX IF NOT EXISTS shadow_scene_anchor ON shadow_scenes(scope,generation,anchor);
                CREATE TABLE IF NOT EXISTS shadow_seals(
                    scope TEXT NOT NULL, generation TEXT NOT NULL,
                    PRIMARY KEY(scope,generation),
                    FOREIGN KEY(scope,generation) REFERENCES shadow_generations(scope,generation));
                CREATE INDEX IF NOT EXISTS shadow_tool_page ON shadow_tools(scope,generation,anchor);
            """)
            for table in ("shadow_rows", "shadow_tools", "shadow_scenes", "shadow_captures"):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_sealed BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM shadow_seals WHERE scope=NEW.scope AND generation=NEW.generation) BEGIN SELECT RAISE(ABORT,'sealed_history'); END")
            for table in ("shadow_generations", "shadow_rows", "shadow_tools", "shadow_seals", "shadow_scenes", "shadow_captures"):
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_history'); END")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def build_shadow(self, scope, messages, *, tool_calls=(), scenes=None, max_bytes, capture=None):
        """Atomically materialize one isolated merged capture; never modify sources."""
        from api.routes import (_message_counts_as_renderable_for_window,
                                _tool_call_ids_in_messages, _tool_result_matches_call_ids)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("explicit positive candidate byte budget required")
        if capture is not None:
            from api.history_capture import verify_capture
            verify_capture(capture)
            if capture['scope'] != [scope.profile_identity, scope.session_id]:
                raise ValueError('capture scope mismatch')
        generation = uuid.uuid4().hex
        encoded = [json.dumps(m, ensure_ascii=False, allow_nan=False) for m in messages]
        tools = [json.dumps(t, ensure_ascii=False, allow_nan=False) for t in tool_calls]
        scene_rows = []
        for ordinal, (key, record) in enumerate((scenes or {}).items()):
            if not isinstance(record, dict):
                continue
            try:
                anchor = int(record.get("message_index"))
            except (ValueError, TypeError):
                anchor = None
            scene_rows.append((scope.key, generation, ordinal, str(record.get("message_ref") or key or ""),
                               anchor, key, json.dumps(record, ensure_ascii=False, allow_nan=False)))
        if sum(len(s.encode()) for s in encoded + tools + [r[-1] for r in scene_rows]) > max_bytes:
            raise ValueError("candidate byte budget exceeded")
        visible = [_message_counts_as_renderable_for_window(m) for m in messages]
        ends = list(range(1, len(messages) + 1))
        ids = set()
        last = None
        for pos, message in enumerate(messages):
            ids.update(_tool_call_ids_in_messages([message]))
            if visible[pos]:
                last = pos
            elif last is not None:
                if _tool_result_matches_call_ids(message, ids):
                    ends[last] = pos + 1
                else:
                    last = None
        with self._connect() as db:
            db.execute("INSERT INTO shadow_generations VALUES(?,?,?)", (scope.key, generation, len(messages)))
            db.executemany("INSERT INTO shadow_rows VALUES(?,?,?,?,?,?)",
                           [(scope.key, generation, i, int(visible[i]), ends[i], s) for i, s in enumerate(encoded)])
            db.executemany("INSERT INTO shadow_tools VALUES(?,?,?,?,?)", [
                (scope.key, generation, i, t.get('assistant_msg_idx') if type(t.get('assistant_msg_idx')) is int else None, s)
                for i, (t, s) in enumerate(zip(tool_calls, tools, strict=True))])
            db.executemany("INSERT INTO shadow_scenes VALUES(?,?,?,?,?,?,?)", scene_rows)
            if capture is not None:
                db.execute("INSERT INTO shadow_captures VALUES(?,?,?,?)", (
                    scope.key, generation, capture['sha256'], json.dumps(capture['manifest'], ensure_ascii=False)))
            db.execute("INSERT INTO shadow_seals VALUES(?,?)", (scope.key, generation))
        return generation

    def page_shadow(self, scope, generation, *, limit, before=None):
        """Read a fixed capture, not fresh authoritative state. No source I/O."""
        from api.routes import _messages_for_limited_payload, _tool_calls_for_message_window
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        if before is not None and type(before) is not int:
            raise ValueError("before must be an integer")
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT total FROM shadow_generations JOIN shadow_seals USING(scope,generation) WHERE scope=? AND generation=?", (scope.key, generation)).fetchone()
            if row is None:
                raise KeyError("unknown scoped generation")
            total = row[0]
            stop = total if before is None else max(0, min(before, total))
            visible = db.execute("SELECT pos,tail_end FROM shadow_rows WHERE scope=? AND generation=? AND renderable=1 AND pos<? ORDER BY pos DESC LIMIT ?", (scope.key, generation, stop, limit)).fetchall()
            if visible:
                start = visible[-1][0] if len(visible) == limit else 0
                end = min(stop, visible[0][1])
            else:
                start, end = max(0, stop-limit), stop
            messages = _messages_for_limited_payload([json.loads(r[0]) for r in db.execute("SELECT payload FROM shadow_rows WHERE scope=? AND generation=? AND pos>=? AND pos<? ORDER BY pos", (scope.key, generation, start, end))])
            truncated = before is not None or len(messages) < total
            if truncated:
                tools = [json.loads(r[0]) for r in db.execute("SELECT payload FROM shadow_tools WHERE scope=? AND generation=? AND anchor>=? AND anchor<? ORDER BY ordinal", (scope.key, generation, start, start+len(messages)))]
                scene_tools = tools
                tools = _tool_calls_for_message_window(tools, start, len(messages))
            else:
                tools = [json.loads(r[0]) for r in db.execute("SELECT payload FROM shadow_tools WHERE scope=? AND generation=? ORDER BY ordinal", (scope.key, generation))]
                scene_tools = tools
            from api.routes import _assistant_anchor_scene_message_ref, _hydrate_anchor_activity_scenes
            records = {}
            for i, message in enumerate(messages):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    ref = _assistant_anchor_scene_message_ref(message)
                    for ordinal, key, payload in db.execute("SELECT ordinal,record_key,payload FROM shadow_scenes WHERE scope=? AND generation=? AND (ref=? OR anchor=?)", (scope.key, generation, ref, start+i)):
                        records[ordinal] = (key, json.loads(payload))
            messages = _hydrate_anchor_activity_scenes(messages, dict(records[i] for i in sorted(records)),
                                                      message_offset=start, tool_calls=scene_tools)
        return {"messages": messages, "tool_calls": tools, "message_count": total,
                "_messages_offset": start, "_messages_truncated": start > 0}

    def read_candidate(self, scope, *, limit, before=None):
        """Fail closed: no production scope has a proven source-writer gate.

        This is a routing decision (None means use the unchanged legacy GET),
        not a stubbed page or a claim of implemented controlled publication.
        """
        return None
