import ast,pathlib,time,os,json,io,types
src=pathlib.Path('api/streaming.py').read_text();node=next(n for n in ast.parse(src).body if isinstance(n,ast.FunctionDef) and n.name=='_sse')
ns={'os':os,'time':time,'json':json,'logger':types.SimpleNamespace(info=lambda *a:None)};exec(compile(ast.Module(body=[node],type_ignores=[]),'sse','exec'),ns)
os.environ['HERMES_TIMING_PROBE']='1';h=types.SimpleNamespace(wfile=io.BytesIO());ns['_sse'](h,'done',{'session':{'session_id':'abc'},'content':'original'})
s=h.wfile.getvalue().decode();assert 'event: timing_probe' in s;assert s.index('event: timing_probe')<s.index('event: done');assert 'done_serialize' in s;assert s.count('original')==1
os.environ.pop('HERMES_TIMING_PROBE');h=types.SimpleNamespace(wfile=io.BytesIO());ns['_sse'](h,'done',{});assert 'timing_probe' not in h.wfile.getvalue().decode()
print('PASS wire timing opt-in and original payload preserved')
