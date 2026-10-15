"""Observed cohorts, not a counterfactual portfolio or fitted calibration."""
from collections import Counter, defaultdict
import math


def selection_review(details):
    groups = defaultdict(list)
    excluded = Counter()
    variants = defaultdict(list)
    for d in details:
        if not d or not d.get("trade"):
            excluded["DETAIL_OR_TRADE_MISSING"] += 1
            continue
        trade=d["trade"]; md=d["decision"].get("metadata") or {}
        roles = md.get("role_baseline") or {}
        for name, variant in (roles.get("entry_variants") or {}).items():
            key = (md.get("build_commit"), trade["side"], roles.get("version"), roles.get("setup"), name, variant.get("eligible"))
            variants[key].append(trade)
        selection=(d.get("risk") or {}).get("details",{}).get("entry_selection") or md.get("entry_selection")
        if not selection or "would_accept" not in selection:
            excluded["SELECTION_NOT_RECORDED"] += 1
            continue
        key=(md.get("build_commit"),d["decision"].get("prompt_version"),trade["side"],
             selection.get("version"),selection.get("mode"),selection.get("setup"),selection["would_accept"])
        groups[key].append(d)
    result=[]
    for key, rows in groups.items():
        probabilities=[]; calibration_excluded=Counter()
        for d in rows:
            t=d["trade"]; md=d["decision"].get("metadata") or {}; plan=md.get("jev_trade_plan") or {}
            p=md.get("jev_continuation")
            if t["exit_reason"] not in {"TARGET","STOP"}:
                calibration_excluded["NON_PRICE_EXIT"]+=1
            elif not plan or not math.isclose(plan.get("target",0),t["target"],rel_tol=1e-8) or not math.isclose(plan.get("stop",0),t["stop"],rel_tol=1e-8):
                calibration_excluded["ASSESSED_PLAN_MISMATCH"]+=1
            elif not isinstance(p,(float,int)) or not math.isfinite(p) or not 0<=p<=1:
                calibration_excluded["INVALID_PROBABILITY"]+=1
            else: probabilities.append((p,int(t["exit_reason"]=="TARGET")))
        buckets=[]
        for lo,hi in [(0,.4),(.4,.5),(.5,.6),(.6,.7),(.7,1.00001)]:
            pairs=[(p,y) for p,y in probabilities if lo<=p<hi]
            if pairs: buckets.append(dict(lower=lo,upper=min(hi,1),n=len(pairs),mean_probability=sum(p for p,y in pairs)/len(pairs),target_rate=sum(y for p,y in pairs)/len(pairs)))
        result.append(dict(zip(("build_commit","prompt_version","side","selection_version","mode","setup","would_accept"),key)) |
            dict(n=len(rows),net=sum(d["trade"]["net_pnl"] for d in rows),fees=sum(d["trade"]["fees"] for d in rows),
                 calibration_n=len(probabilities),calibration_excluded=dict(calibration_excluded),
                 brier=sum((p-y)**2 for p,y in probabilities)/len(probabilities) if probabilities else None,reliability=buckets))
    variant_rows = [dict(zip(("build_commit", "side", "version", "setup", "variant", "eligible"), key)) |
                    dict(n=len(rows), net=sum(t["net_pnl"] for t in rows), fees=sum(t["fees"] for t in rows))
                    for key, rows in variants.items()]
    return dict(cohorts=result,entry_variants=variant_rows,excluded=dict(excluded),interpretation="OBSERVATIONAL_UNCALIBRATED_NOT_COUNTERFACTUAL")
