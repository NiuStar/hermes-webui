import ast,pathlib
s=pathlib.Path('api/streaming.py').read_text();n=next((n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name=='_terminal_message_window'),None)
assert n is not None,'missing terminal window'
ns={};exec(compile(ast.Module(body=[n],type_ignores=[]),'window','exec'),ns)
f=ns['_terminal_message_window'];rows=[{'id':i} for i in range(3347)];raw={'messages':rows,'message_count':3347,'regeneration_revision':'rev','session_id':'s'}
r=f(raw);assert len(r['messages'])==30 and r['messages'][-1]['id']==3346;assert r['_messages_offset']==3317 and r['_messages_truncated'];assert r['message_count']==3347 and r['regeneration_revision']=='rev';assert len(raw['messages'])==3347
for count in [0,1,30]:
 r=f({'messages':rows[:count],'message_count':count});assert len(r['messages'])==count and not r['_messages_truncated']
r=f({'messages':[{'role':'assistant'},{'role':'user'},{'role':'assistant'}], 'tool_calls':[{'assistant_msg_idx':0,'tid':'old'},{'assistant_msg_idx':2,'tid':'current'}]});assert [t['tid'] for t in r['tool_calls']]==['current']
print('PASS bounded terminal payload, total, offset, revision and non-mutation')
