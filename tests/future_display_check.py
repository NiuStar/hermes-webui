import ast,pathlib,random
s=pathlib.Path('api/streaming.py').read_text();n=next((n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name=='_future_display_match'),None);assert n,'missing indexed suffix check'
ns={};exec(compile(ast.Module(body=[n],type_ignores=[]),'future','exec'),ns);f=ns['_future_display_match'];random.seed(31)
for _ in range(100):
 keys=[random.randrange(15) for i in range(100)];remaining={k:1 for k in range(15)};last={k:i for i,k in enumerate(keys)};stack=sorted((i,k) for k,i in last.items())
 for i in range(len(keys)):
  if random.random()<.2:remaining.pop(random.randrange(15),None)
  assert f(stack,remaining,i)==any(k in remaining for k in keys[i+1:])
print('PASS indexed suffix equals original with duplicates and consumed keys')
