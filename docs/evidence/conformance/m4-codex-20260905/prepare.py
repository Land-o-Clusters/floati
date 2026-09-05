import json
from pathlib import Path
P=Path('\x2fprivate/tmp/floati-m4-codex-20260905')
hello=json.loads((P/'surface.requests.json').read_text())[:2]
def call(i,name,args):
    return {'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':name,'arguments':args}}
send={'recipient':'peer','repo':'floati','sha':'1a17f706f5ece1cc3a3d0e5efa4a05b85bb5aade','doc':'docs/status/QUEUE-2026-09-01.md','note':'Codex MCP scratch conformance envelope','idempotency_key':'codex-m4-mail-1'}
rows=hello+[call('describe','describe',{}),call('send','send',send),call('send-repeat','send',send),call('send-missing','send',{}),call('actor-switch','send',dict(send,sender='peer')),call('root-switch','status',{'root':str(P/'other-fleet')}),call('session-switch','inbox',{'session':'other-session'}),call('note-cap','send',dict(send,note='x'*1025,idempotency_key='cap-1')),call('doctor-scope-refusal','doctor',{'source':str(P/'source'),'profile':'scratch-isolated'}),call('bundle-no-grant','confluence_bundle',{'consumer':'consumer','out':str(P/'bundle')})]
for name in ['intake_adopt','intake_dispatch','confluence_adopt','confluence_release','epoch_roll','snapshot','repair_quarantine','signature_sign','init','node_add','mcp_serve','uninstall','purge','wake_arm','survey','grant']:
    rows.append(call('deny-'+name,name,{}))
rows += [{'jsonrpc':'2.0','id':'unknown-method','method':'not/a/method'},'{bad-json']
(P/'operator.requests.json').write_text(json.dumps(rows))
