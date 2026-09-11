"""Standalone AST check exercises telemetry without importing the agent."""
import ast, pathlib, time, os
p=pathlib.Path('api/streaming.py');tree=ast.parse(p.read_text());node=next((n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_timing_probe_payload'),None)
assert node is not None,'server timing helper missing'
ns={'time':time,'os':os};exec(compile(ast.Module(body=[node],type_ignores=[]),str(p),'exec'),ns)
f=ns['_timing_probe_payload'];assert f([('session_save',.1)],10,20,clock=lambda:12,wall=lambda:23,environ={}) is None
r=f([('session_save',.1)],10,20,clock=lambda:12,wall=lambda:23,environ={'HERMES_TIMING_PROBE':'1'})
assert r=={'stages':[{'stage':'session_save','duration_ms':100.0}],'writeback_ms':2000,'turn_ms':3000}
print('PASS opt-in and deterministic server stages')
