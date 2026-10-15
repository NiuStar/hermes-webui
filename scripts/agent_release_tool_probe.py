"""Bounded real Agent tool-loop probe; no production credentials or external model."""
import json,os,pathlib,signal,subprocess,tempfile,sys
sys.path.insert(0,'/opt/hermes')
from tests.fakes.fake_llm_provider import FakeLLMServer,ToolCall,Text,write_hermes_home
from hermes_cli.version_info import get_code_identity
expected=sys.argv[1];version=sys.argv[2]
identity=get_code_identity(refresh=True)
assert identity['sha']==expected and identity['version']==version.removeprefix('v'),identity
with tempfile.TemporaryDirectory(prefix='release-tool-loop-') as d:
 root=pathlib.Path(d)
 with FakeLLMServer([ToolCall('terminal',{'command':'printf HERMES_RELEASE_TOOL_OK'}),Text('HERMES_RELEASE_FINAL_OK')]) as server:
  home=root/'home';write_hermes_home(home,server.base_url,extra_config='updates:\n  check: false\napprovals:\n  single_query_mode: approve\n')
  project=root/'project';project.mkdir()
  env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','TERM')}
  env.update(HOME=str(root),HERMES_HOME=str(home),PYTHONPATH='/opt/hermes',HERMES_STATE_DB_GUARD_BYPASS='1',HERMES_ACCEPT_HOOKS='1',HERMES_NO_UPDATE_CHECK='1',NO_COLOR='1')
  process=subprocess.Popen(['/app/venv/bin/python3','-m','hermes_cli.main','chat','-q','Use terminal to print the test marker.','-Q'],cwd=project,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
  try:
   stdout,stderr=process.communicate(timeout=180)
  except subprocess.TimeoutExpired:
   os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=10)
   raise RuntimeError('Agent tool-loop deadline exceeded')
  requests=server.main_requests()
  tools=[m for req in requests for m in req.get('messages',[]) if m.get('role')=='tool']
  assert process.returncode==0 and len(requests)>=2 and 'HERMES_RELEASE_TOOL_OK' in json.dumps(tools) and 'HERMES_RELEASE_FINAL_OK' in stdout
  print(json.dumps({'passed':True,'model_turns':len(requests),'tool_results':len(tools)}))
