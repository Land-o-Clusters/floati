import json
from pathlib import Path
P=Path('\x2fprivate/tmp/floati-m4-codex-20260905')
def call(i,name,args):
    return {'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':name,'arguments':args}}
hello=json.loads((P/'surface.requests.json').read_text())[:2]
responses=[json.loads(json.loads(x)['raw']) for x in (P/'operator.wire.jsonl').read_text().splitlines() if json.loads(x).get('direction')=='response']
message=next(x for x in responses if x.get('id')=='send')['result']['structuredContent']['evidence']['message']['id']
work=json.loads(json.loads((P/'setup.jsonl').read_text().splitlines()[-1])['stdout'])['evidence']['id']
rows=hello+[call('ack-before-delivery','ack',{'message_ids':[message]}),call('inbox','inbox',{}),call('ack','ack',{'message_ids':[message]}),call('inbox-empty','inbox',{}),call('claim-owner-no-authority','work_claim',{'item_id':work}),call('claim-identity-switch','work_claim',{'item_id':work,'actor':'operator'}),call('complete-unclaimed','work_complete',{'item_id':work}),call('receipts','receipts',{'node':'peer'})]
(P/'peer.requests.json').write_text(json.dumps(rows))
