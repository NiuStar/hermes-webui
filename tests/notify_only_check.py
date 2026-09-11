import ast,pathlib
s=pathlib.Path('api/streaming.py').read_text();n=next((n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name=='_completion_notice'),None)
assert n,'missing notice'
ns={};exec(compile(ast.Module(body=[n],type_ignores=[]),'notice','exec'),ns)
r=ns['_completion_notice']('sid','stream');assert r=={'session_id':'sid','stream_id':'stream','notification_only':True};assert 'messages' not in r and 'session' not in r
print('PASS completion notice has no transcript')
