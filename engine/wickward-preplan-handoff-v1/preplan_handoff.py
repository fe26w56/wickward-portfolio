"""Supplied-input preplan and an observed, verified alternative; no live feeds."""
from pathlib import Path
from datetime import datetime,timezone
from types import ModuleType
import builtins,copy,hashlib,importlib,importlib.util,json,os,sys,threading
HERE=Path(__file__).resolve().parent
NAV=Path(os.environ.get('WICKWARD_NAV_RUNTIME',str(HERE.parent/'wickward-unified-navigation-runtime-v3-ff1')))
oldpath=list(sys.path)
try:
 sys.path.insert(0,str(NAV));kernel=importlib.import_module('unified_navigation_v3.kernel');boundary=importlib.import_module('unified_navigation_v3.boundary')
finally:sys.path[:]=oldpath
_adapter=kernel._adapter
_generic=ModuleType('_preplan_generic_contract')
for name in ['build_context','evaluate_sequence','InputError','SearchBudgetExceeded','Step']:setattr(_generic,name,getattr(_adapter,name))
_imports={'generic_io':_generic};_lock=threading.RLock()
def _import(name,globals=None,locals=None,fromlist=(),level=0):
 if level==0 and name in _imports:return _imports[name]
 return builtins.__import__(name,globals,locals,fromlist,level)
def _load(name,path):
 manifest=json.loads((HERE/'VENDORED_SOURCES.json').read_text());raw=path.read_bytes();rel=str(path.relative_to(HERE));assert hashlib.sha256(raw).hexdigest()==manifest[rel],'vendored preplan source mismatch'
 mod=ModuleType('_wickward_preplan_handoff_v1.'+name);mod.__file__=str(path);mod.__dict__['__builtins__']={**vars(builtins),'__import__':_import};sys.modules[mod.__name__]=mod;old=list(sys.path)
 try:exec(compile(raw,str(path),'exec'),mod.__dict__)
 finally:sys.path[:]=old
 _imports[name]=mod;return mod
search=_load('route_search',HERE/'_vendor/scripts/route_search.py');problem=_load('route_problem',HERE/'_vendor/engine_api/route_problem.py');engine=_load('plan_common',HERE/'_vendor/engine_api/plan_common.py')
def request_sha256(request):return hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
def _encode(nodes,c):
 return [{'kind':'wait_until','until':key} if kind=='wait' else {'kind':'ride' if kind=='ride' else 'flexible_break' if c.anchors[key]['kind']=='flexible_break' else 'commitment','id':key} for kind,key in nodes]
def create_handoff(request,*,route_evaluation_budget=5000,max_intentional_wait_minutes=60):
 """Return an explicit handoff plus proofs; alternatives never change utility."""
 with _lock:
  try:ledger=boundary.validate_request(request)
  except (kernel.InputError,kernel.SearchBudgetExceeded) as exc:return {'status':'needs_input','reason':str(exc),'handoff':None}
  if ledger['missed_commitment_ids']:
   return {'status':'needs_state_resolution','reason':'Reported missed commitments remain unresolved; preday planning cannot restore their completion','handoff':None,'missed_commitment_ids':ledger['missed_commitment_ids']}
  pending_visited=sorted(row['id'] for row in request.get('commitments',[]) if not row.get('completed',False) and row.get('attraction_id') in ledger['visited_attraction_ids'])
  if pending_visited:
   return {'status':'needs_state_resolution','reason':'Visited linked attractions have pending commitments; explicit service-state resolution is required','handoff':None,'unresolved_linked_commitment_ids':pending_visited}
  original=copy.deepcopy(request);instances=[];Base=engine.RouteProblem
  class ObservedProblem(Base):
   def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.feasible_candidates={};instances.append(self)
   def evaluate(self,route,charge=True):
    result=super().evaluate(route,charge=charge)
    if charge and result.feasible:self.feasible_candidates.setdefault(tuple(route),result)
    return result
  try:
   engine.RouteProblem=ObservedProblem;primary=engine.plan(request,route_evaluation_budget=route_evaluation_budget,max_intentional_wait_minutes=max_intentional_wait_minutes)
  finally:engine.RouteProblem=Base
  assert request==original,'preplan mutated caller input'
  if primary.get('status')!='conditional_model_plan' or not primary.get('feasible'):return {'status':primary.get('status','no_verified_plan'),'handoff':None,'primary_result':primary,'fallback_result':None}
  fresh=_adapter.evaluate_sequence(request,primary['route'],complete_remaining=False)
  if not fresh['feasible']:return {'status':'internal_validation_failed','handoff':None,'primary_result':primary,'fallback_result':None}
  p=instances[0];first_optional=next((key for kind,key in p.best if kind=='ride' and ('ride',key) not in p.mandatory),None);fallback=[];fallback_result=None;reason='No evaluated feasible route omits the first optional primary attraction'
  if first_optional is not None:
   candidates=[(nodes,z) for nodes,z in p.feasible_candidates.items() if ('ride',first_optional) not in nodes]
   if candidates:
    nodes,z=max(candidates,key=lambda x:search.rank(x[1]));alternative=_encode(nodes,p.c);check=_adapter.evaluate_sequence(request,alternative,complete_remaining=False)
    if check['feasible']:
     fallback=alternative;fallback_result=check;reason='Best feasible candidate encountered in the same bounded search after excluding the first optional primary attraction'
  issued=datetime.now(timezone.utc).isoformat();asof=request.get('information_as_of',request['now']);source=copy.deepcopy(request.get('provenance',{}).get('score_definition',{'kind':'sum_of_supplied_unique_attraction_utilities','not_a_catalog_percent_or_real_visit_score':True}))
  handoff={'schema_version':'wickward.plan-handoff.v2','generic_request':original,'generic_request_sha256':request_sha256(original),'issued_at':issued,'plan_information_as_of':asof,'ordered_route':copy.deepcopy(primary['route']),'fallback_order':fallback,'provenance':{'producer':'wickward-preplan-handoff-v1','computed_at':issued,'issuance_semantics':'issued_at is actual generation time; historical examples require explicit simulation replay','primary_conditions':primary.get('conditions',[]),'fallback_source':reason,'fallback_validation':'verified_under_supplied_scenario' if fallback_result else 'none','fallback_for_skipped_attraction_id':first_optional,'fallback_conditions':fallback_result.get('conditions',[]) if fallback_result else [],'fallback_requires_current_time_and_observation_revalidation':True,'score_definition':source,'score_kind':'予測スコア','real_visit_or_global_optimum_claim':False,'empirical_quality_gate_passed':False}}
  return {'status':'handoff_ready','handoff':handoff,'primary_result':primary,'fallback_result':fallback_result,'statistics':{'route_evaluations':p.evaluations,'distinct_feasible_candidates_observed':len(p.feasible_candidates),'additional_search_evaluations_for_fallback':0,'fallback_final_validation_calls':int(fallback_result is not None)}}
def parse_request_json(payload):
 if not isinstance(payload,bytes) or len(payload)>boundary.MAX_INPUT_BYTES:raise kernel.InputError('request JSON must be bounded UTF8 bytes')
 try:
  text=payload.decode('utf-8',errors='strict');boundary._bounded_nesting(text);return json.loads(text,object_pairs_hook=boundary._pairs,parse_constant=boundary._constant,parse_float=boundary._float)
 except (UnicodeError,ValueError,RecursionError) as exc:raise kernel.InputError('invalid request JSON') from exc
if __name__=='__main__':
 try:result=create_handoff(parse_request_json(sys.stdin.buffer.read(boundary.MAX_INPUT_BYTES+1)))
 except kernel.InputError as exc:result={'status':'needs_input','reason':str(exc),'handoff':None}
 json.dump(result,sys.stdout,ensure_ascii=False,allow_nan=False,indent=2);print()
