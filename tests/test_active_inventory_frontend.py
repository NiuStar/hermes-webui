"""Frontend status count contract: server count, not current page or inferred SSE."""
from pathlib import Path
import subprocess
import shutil
import pytest


def test_inventory_count_uses_server_authority_and_unknown_on_failure():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for real JS runtime assertion")
    source = (Path(__file__).resolve().parents[1]/"static/sessions.js").read_text()
    start = source.index("function _activeSessionInventoryView(")
    stop = source.index("\nfunction _renderActiveSessionInventory(", start)
    script = "const S={session:{session_id:'A'}}; const t=x=>({active_sessions_count:'运行中:{0}',active_sessions_unknown:'未知'}[x]||x);\n"+source[start:stop]+"""
const data={known:true,count:4,sessions:[{session_id:'A'},{session_id:'B'},{session_id:'child'}],
 background:[{status:'running',bg_session_id:'child',task_id:'t'}, {status:'done',bg_session_id:'old'}],
 auxiliary:[{type:'delegation',count:2}]};
const first=_activeSessionInventoryView(data);
if(first.count!==4||first.entries.length!==2)throw Error(JSON.stringify(first));
S.session.session_id='B';
if(_activeSessionInventoryView(data).count!==4)throw Error('changed with foreground');
if(_activeSessionInventoryView({known:false}).count!==null)throw Error('failure became zero');
console.log('inventory count OK');
"""
    run=subprocess.run([node,"-"],input=script,text=True,capture_output=True,timeout=10)
    assert run.returncode==0, run.stderr
    assert "inventory count OK" in run.stdout
