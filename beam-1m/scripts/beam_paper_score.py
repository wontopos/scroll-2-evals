"""Score a BEAM run the way arXiv 2608.23920 does: per-question mean over 700, rubric mean for
630, normalized Kendall tau_b for the 70 event_ordering (the .eo.json value). Also other aggregations."""
import json, sys, statistics as st
def score(beam, eo):
    rows=json.load(open(beam))["rows"]; e=json.load(open(eo))
    per=[]; rub=[]; types={}
    for r in rows:
        q=r["question_id"]; t=r["type"]
        if t=="event_ordering":
            v=e.get(q)
            if v is None: continue
            v=float(v)
        else:
            v=r.get("beam_score")
            if v is None: continue
            rub.append(v)
        per.append(v); types.setdefault(t,[]).append(v)
    eo_rub=[r["beam_score"] for r in rows if r["type"]=="event_ordering" and r.get("beam_score") is not None]
    allrub=[r["beam_score"] for r in rows if r.get("beam_score") is not None]
    macro=st.mean(st.mean(v) for v in types.values())
    return dict(n=len(per), paper=100*st.mean(per), macro=100*macro, rubric_630=100*st.mean(rub),
                rubric_all700=100*st.mean(allrub), eo_tau=100*st.mean([float(x) for x in e.values()]),
                eo_rubric=100*st.mean(eo_rub) if eo_rub else None,
                types={t:round(100*st.mean(v),1) for t,v in sorted(types.items())})
if __name__=="__main__":
    for pre in sys.argv[1:]:
        s=score(pre+".beam.json", pre+".eo.json")
        print(pre.split("/")[-1], {k:(round(v,2) if isinstance(v,float) else v) for k,v in s.items() if k!="types"})
