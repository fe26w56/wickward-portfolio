"""General-input preday order search using the shared audited input contract.

Provides constraint-correct forecast-scenario plans, not a validated empirical
winner over popular-first. Historical scenario experiments remain separate.
"""
from pathlib import Path
from datetime import timedelta
from itertools import permutations
import sys,random,json,hashlib,math
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT/'common'))
from generic_io import build_context,evaluate_sequence,InputError,SearchBudgetExceeded
from route_problem import RouteProblem
import route_search as search


def plan(request,*,route_evaluation_budget=5000,max_intentional_wait_minutes=60):
 try:
  if isinstance(route_evaluation_budget,bool) or not isinstance(route_evaluation_budget,int) or not 100<=route_evaluation_budget<=30000:raise InputError('route_evaluation_budget must be100..30000')
  if isinstance(max_intentional_wait_minutes,bool) or not isinstance(max_intentional_wait_minutes,(int,float)) or not math.isfinite(max_intentional_wait_minutes) or not 0<=max_intentional_wait_minutes<=180:raise InputError('max_intentional_wait_minutes must be0..180')
  c=build_context(request)
  if c.raw.get('action_state','idle')!='idle':return evaluate_sequence(c,[],complete_remaining=False)
  wait_options=set()
  seed_locations={c.current}|{c.rides[a]['location_id'] for a in c.required}|{a.get('location_id') for a in c.anchors.values() if a.get('location_id') is not None}
  for fc in getattr(c.forecast_provider,'forecasts',[]):
   if fc.issued_at>c.information_as_of:continue
   for location in seed_locations:
    leg=c.walk(location,c.rides[fc.attraction_id]['location_id'])
    if leg is None:continue
    at=fc.valid_from-timedelta(minutes=leg[0])
    if c.now<at<=c.now+timedelta(minutes=max_intentional_wait_minutes) and at<c.deadline:wait_options.add(at)
  wait_options=sorted(wait_options)[:16]
  p=RouteProblem(c,route_evaluation_budget,wait_options)
  seeds=[]
  for at in [c.now]+wait_options:
   base=c.schedule(at,c.current,tuple(sorted(c.required)),tuple(sorted(c.anchors)))
   if base is None:continue
   seed=tuple(('ride',x.key) if x.detail['kind']=='ride' else ('anchor',x.key) for x in base.steps)
   if at>c.now:seed=(('wait',at.isoformat()),)+seed
   p.evaluate(seed);seeds.append(seed)
  # An early commitment can make waiting at the start invalid. Try one explicit
  # intermediate wait while preserving every mandatory node, under the same
  # route-evaluation budget. The final shared evaluator remains authoritative.
  if p.best is None and wait_options:
   try:
    for sequence in permutations(sorted(p.mandatory)):
     for until in wait_options:
      for at in range(1,len(sequence)):
       candidate=sequence[:at]+(('wait',until.isoformat()),)+sequence[at:]
       if p.evaluate(candidate).feasible:break
      if p.best is not None:break
     if p.best is not None:break
   except search.BudgetExceeded:pass
  rng=random.Random(20261005)
  if p.best is None:return {'status':'no_verified_plan','reason':'No plan verified under supplied inputs and bounded ordering/wait actions; this does not prove infeasibility','score_kind':'予測スコア'}
  try:
   current=search.insertion(p,p.best)
   while True:
    current=search.local_search(p,current);current=search.insertion(p,current,randomized=True,rng=rng);p.evaluate(current)
    current=list(p.best);optional=[x for x in current if x not in p.mandatory]
    if optional:
     for x in rng.sample(optional,min(len(optional),1+rng.randrange(3))):current.remove(x)
    else:rng.shuffle(current)
    current=tuple(current)
  except search.BudgetExceeded:pass
  route=[{'kind':'wait_until','until':a} if k=='wait' else {'kind':'ride' if k=='ride' else 'flexible_break' if c.anchors[a]['kind']=='flexible_break' else 'commitment','id':a} for k,a in p.best]
  verified=evaluate_sequence(c,route,complete_remaining=False)
  if not verified['feasible']:return {'status':'internal_validation_failed','reason':'Shared evaluator rejected optimized route','details':verified}
  assert abs(verified['known_utility_subtotal']-p.best_result.score)<1e-8
  return {**verified,'status':'conditional_model_plan','score_kind':'予測スコア','route':route,'excluded_from_utility_optimization':p.excluded,'route_evaluations':p.evaluations,'intentional_wait_candidate_count':len(wait_options),'max_intentional_wait_minutes':max_intentional_wait_minutes,'wait_search_limit':'At most16earliest source-available forecast boundaries adjusted for current/mandatory locations; seed search includes at most one intermediate wait; not exhaustive continuous-time search','search':'bounded insertion/relocate/swap/reverse/perturbation with full-time recomputation','development_status':'General-input constrained forecast adapter; no demonstrated overall winning historical method','empirical_score_gate_passed':False}
 except (InputError,SearchBudgetExceeded) as ex:return {'status':'needs_input_or_budget','reason':str(ex),'score_kind':'予測スコア'}

if __name__=='__main__':json.dump(plan(json.load(sys.stdin)),sys.stdout,ensure_ascii=False,indent=2);print()
