from pathlib import Path
from datetime import timedelta
import sys,math
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import route_search as search
from generic_io import Step
from datetime import datetime
def mins(n):return timedelta(minutes=n)
def diff(a,b):return (a-b).total_seconds()/60

class RouteProblem:
 def __init__(self,c,budget,wait_options=()):
  self.c=c;self.budget=budget;self.evaluations=0;self.ride=10.;self.best=None;self.best_result=None;self.details={}
  self.mandatory={('ride',a) for a in c.required}|{('anchor',a) for a in c.anchors}
  available=[];self.excluded=[];self.weights={}
  for a in c.rides:
   u,source,flags=c.utility(a)
   if a in c.visited or a in c.reserved:continue
   if u is None and a not in c.required:self.excluded.append({'attraction_id':a,'reason':'undefined_utility'});continue
   node=('ride',a);available.append(node);self.weights[node]=u if u is not None else 0.
  for key,a in c.anchors.items():
   node=('anchor',key);available.append(node);aid=a.get('attraction_id');u=c.utility(aid)[0] if aid and aid not in c.visited else 0.;self.weights[node]=u if u is not None else 0.
  self.wait_options={t.isoformat():t for t in wait_options}
  for key in self.wait_options:
   available.append(('wait',key));self.weights[('wait',key)]=0.
  self.ids=tuple(available)
 def evaluate(self,route,charge=True):
  if charge:
   if self.evaluations>=self.budget:raise search.BudgetExceeded
   self.evaluations+=1
  if len(set(route))!=len(route) or not self.mandatory.issubset(route):return search.Result(False,0,math.inf,0,0,0)
  c=self.c;t=c.now;loc=c.current;steps=[];score=wait=walking=0.;rides=set(c.visited);unknown=[]
  for kind,key in route:
   if kind=='ride':
    depart=t;opening=c.rides[key].get('opens_at');leg=c.walk(loc,c.rides[key]['location_id'])
    if opening and leg is not None:depart=max(t,opening-mins(leg[0]))
    step,reason=c.ride(key,depart,loc);aid=key
   elif kind=='wait':
    until=self.wait_options[key]
    if until<t or until>c.deadline:return search.Result(False,score,math.inf,wait,walking,len(rides-c.visited))
    duration=diff(until,t);step=Step('wait_until',loc,t,until,until,0.,duration,('waiting_does_not_refresh_observations_or_forecasts',),{'kind':'wait_until','waiting_minutes':duration,'wait_until':until.isoformat()});aid=None
   else:step=c.anchor(key,t,loc);aid=c.anchors[key].get('attraction_id')
   if step is None:return search.Result(False,score,math.inf,wait,walking,len(rides-c.visited))
   if aid and aid not in rides:
    u=c.utility(aid)[0]
    if u is None:unknown.append(aid)
    else:score+=u
    rides.add(aid)
   steps.append(step);wait+=step.detail.get('planning_wait_minutes',0.);walking+=step.walking;t=step.end;loc=step.location
  tail=c.schedule(t,loc,(),())
  if tail is None:return search.Result(False,score,math.inf,wait,walking,len(rides-c.visited))
  finish=diff(tail.exit_at+mins(c.exit_buffer),c.now);walking+=tail.walking;r=search.Result(True,score,finish,wait,walking,len(rides-c.visited))
  if charge and (self.best_result is None or search.rank(r)>search.rank(self.best_result)):
   self.best=tuple(route);self.best_result=r;self.details={'steps':steps,'exit_at':tail.exit_at,'unknown_score_ids':unknown,'tail_conditions':tail.conditions}
  return r
