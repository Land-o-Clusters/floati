# PF-R7a independent re-derivation - the instrument behind docs/evidence/pf1-r7a-gate-2026-08-30.md.
# A MEASUREMENT RECEIPT MUST SHIP THE INSTRUMENT THAT PRODUCED IT.
import json, math, collections, datetime
import os, sys
# An instrument that hardcodes one machine's path is not an instrument anywhere else,
# and that path carries an operator identity and a private fleet coordinate.
J = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FLOATI_WATCH_JOURNAL"))
if not J:
    raise SystemExit("usage: pf1-r7a-attribution-verify.py <watcher-journal.jsonl>  (or set FLOATI_WATCH_JOURNAL)")
rows=0; ev=collections.Counter(); init=collections.Counter()
first={}; last={}; appends=collections.Counter(); ee=collections.Counter()
recs=[]
def parse(t):
    return datetime.datetime.strptime(t,"%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc).timestamp()
tmin=None;tmax=None
for line in open(J,encoding="utf-8"):
    line=line.strip()
    if not line: continue
    d=json.loads(line); rows+=1
    p=d.get("pid"); e=d.get("event"); ts=parse(d["ts"])
    ev[e]+=1
    if e=="init": init[p]+=1
    if p not in first or ts<first[p]: first[p]=ts
    if p not in last or ts>last[p]: last[p]=ts
    appends[p]+=1
    if e=="exit_empty": ee[p]+=1
    recs.append((ts,p,e))
    tmin=ts if tmin is None or ts<tmin else tmin
    tmax=ts if tmax is None or ts>tmax else tmax
print("rows",rows,"pids",len(first),"init",ev['init'],"exit_empty",ev['exit_empty'],"mail_seen",ev['mail_seen'])
print("exit_empty pct %.4f%%  mail_seen pct %.4f%%"%(100*ev['exit_empty']/rows,100*ev['mail_seen']/rows))
print("window",datetime.datetime.utcfromtimestamp(tmin).isoformat()+"Z","->",datetime.datetime.utcfromtimestamp(tmax).isoformat()+"Z")

def pearson(x,y):
    n=len(x); mx=sum(x)/n; my=sum(y)/n
    sx=math.sqrt(sum((a-mx)**2 for a in x)); sy=math.sqrt(sum((b-my)**2 for b in y))
    return sum((a-mx)*(b-my) for a,b in zip(x,y))/(sx*sy)
def rank(v):
    order=sorted(range(len(v)),key=lambda i:v[i]); r=[0.0]*len(v); i=0
    while i<len(order):
        j=i
        while j+1<len(order) and v[order[j+1]]==v[order[i]]: j+=1
        avg=(i+j)/2.0+1
        for k in range(i,j+1): r[order[k]]=avg
        i=j+1
    return r
def spearman(x,y): return pearson(rank(x),rank(y))

kept=[p for p in first if (last[p]-first[p])>=300]
tot=sum(appends[p] for p in kept)
print("retained PIDs",len(kept),"rows covered",tot,"(%.4f%%)"%(100*tot/rows))
inst=[init[p] for p in kept]
rate=[appends[p]/((last[p]-first[p])/60.0) for p in kept]
eer=[ee[p]/((last[p]-first[p])/60.0) for p in kept]
print("PROCESS-RATE  all-appends/min : pearson %.4f  spearman %.4f"%(pearson(inst,rate),spearman(inst,rate)))
print("PROCESS-RATE  exit_empty/min  : pearson %.4f  spearman %.4f"%(pearson(inst,eer),spearman(inst,eer)))
srt=sorted(inst); med=srt[len(srt)//2] if len(srt)%2 else (srt[len(srt)//2-1]+srt[len(srt)//2])/2
print("retained instances: median %s mean %.1f"%(med,sum(inst)/len(inst)))

for bucket in (60,300,900,3600):
    b_app=collections.Counter(); b_ee=collections.Counter()
    for ts,p,e in recs:
        k=int(ts//bucket); b_app[k]+=1
        if e=="exit_empty": b_ee[k]+=1
    b_inst=collections.Counter()
    for p in first:
        k0=int(first[p]//bucket); k1=int(last[p]//bucket)
        for k in range(k0,k1+1): b_inst[k]+=init[p]
    keys=sorted(k for k in b_app if b_inst[k]>0)
    X=[b_inst[k] for k in keys]; Y=[b_app[k]*(60.0/bucket) for k in keys]; Z=[b_ee[k]*(60.0/bucket) for k in keys]
    n=len(X); mx=sum(X)/n; my=sum(Y)/n; mz=sum(Z)/n
    den=sum((a-mx)**2 for a in X)
    sl=sum((a-mx)*(b-my) for a,b in zip(X,Y))/den
    slz=sum((a-mx)*(b-mz) for a,b in zip(X,Z))/den
    print("BUCKET %4ds  buckets %5d  r_all %.4f  slope_all %.4f  r_ee %.4f  slope_ee %.4f"%(bucket,n,pearson(X,Y),sl,pearson(X,Z),slz))
