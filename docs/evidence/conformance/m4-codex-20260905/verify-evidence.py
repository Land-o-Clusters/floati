import hashlib
import json
from collections import Counter
from pathlib import Path
from floati.mcp_pin import validate_mcp_observation

P=Path('\x2fprivate/tmp/floati-m4-codex-20260905')
responses={}
all_artifacts=[]
processes=[]
for name in ['surface','operator','peer','work']:
    rows=[json.loads(s) for s in (P/(name+'.wire.jsonl')).read_text().splitlines()]
    assert rows[-1] == {'exit':0,'tail':''}, (name,rows[-1])
    assert (P/(name+'.stderr')).read_bytes() == b''
    processes.append(rows[0]['pid'])
    for row in rows:
        if row.get('direction') != 'response':
            continue
        obj=json.loads(row['raw'])
        responses[(name,obj['id'])]=obj
        result=obj.get('result',{})
        if 'structuredContent' in result:
            a=result['structuredContent']
            assert json.loads(result['content'][0]['text']) == a
            all_artifacts.append(a)
    sample=next(x['socket_sample'] for x in rows if 'socket_sample' in x)
    assert sample['exit']==1 and sample['stdout']=='' and sample['stderr']==''
def artifact(batch,key):
    return responses[(batch,key)]['result']['structuredContent']
tools=responses[('surface','list')]['result']['tools']
pin=responses[('surface','initialize')]['result']['floatiIntegrationPin']
validate_mcp_observation(pin)
assert pin['network_posture']=='none' and pin['transport']=='stdio'
assert {t['name'] for t in pin['tools']} == {t['name'] for t in tools}
for p in pin['tools']:
    t=next(t for t in tools if t['name']==p['name'])
    assert p['schema']==t['inputSchema'] and p['description']==t['description']
commands=artifact('operator','describe')['evidence']['commands']
exposed={tuple(x['path']) for x in commands if x['executable'] and x['mcp_exposure']!='never'}
assert exposed == {tuple(t['_meta']['floati']['commandPath']) for t in tools}
for name in ['confluence_grant','confluence_revoke','inbox']:
    assert next(t for t in tools if t['name']==name)['_meta']['floati']['exposure']=='governed'
sent=artifact('operator','send')['evidence']['message']
assert sent==artifact('operator','send-repeat')['evidence']['message']
received=artifact('peer','inbox')['evidence']
assert received['messages']==[sent]
assert received['receipt']['item_ids']==[sent['id']]
assert received['acknowledgment']==artifact('peer','ack')['evidence']
assert received['acknowledgment']['acting_session_id']=='codex-m4-peer'
assert artifact('peer','ack-before-delivery')['evidence']['code']=='ack_item_not_delivered'
assert artifact('peer','inbox-empty')['status']=='intentional_silence'
assert artifact('peer','inbox-empty')['evidence']['used_node']=='peer'
for key in ['actor-switch','root-switch','session-switch']:
    assert artifact('operator',key)['evidence']['code']=='arguments_invalid'
assert artifact('operator','note-cap')['evidence']['code']=='note_invalid'
for (batch,key),obj in responses.items():
    if isinstance(key,str) and key.startswith('deny-'):
        assert obj['error']['code']==-32602
for key in ['work-claim','work-complete']:
    a=artifact('work',key)
    assert a['status']=='ok' and a['evidence']['actor']=='peer'
assert artifact('work','final-status')['evidence']['work_counts']=={'claimed':0,'completed':1,'open':0}
assert artifact('operator','doctor-scope-refusal')['evidence']['code']=='doctor_profile_invalid'
refusals=[a for a in all_artifacts if a['status']=='refused']
summary={'process_pids':processes,'process_exits':[0]*len(processes),'commands':len(commands),'tools':len(tools),'exposure_counts':dict(Counter(t['_meta']['floati']['exposure'] for t in tools)),'responses':len(responses),'tool_artifacts':len(all_artifacts),'refusals':len(refusals),'refusal_codes':dict(Counter(a['evidence']['code'] for a in refusals)),'remedy_types':dict(Counter(type(a['evidence']['remedy']).__name__ for a in refusals)),'doctor_health':'unmeasured_scope_blocked','socket_observation':'idle_ipv4_ipv6_samples_only','checks':'observed_structural_assertions_pass'}
(P/'verification.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps(summary,indent=2))
