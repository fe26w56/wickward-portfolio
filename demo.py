"""Seven synthetic days -> hourly median baseline -> original engine -> HTML."""
from __future__ import annotations
import csv
from datetime import datetime, timedelta, timezone
import html
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'engine/wickward-preplan-duration-input-v1'))
import duration_input as engine

DAY = datetime(2035, 4, 10, 9, tzinfo=timezone.utc)
AS_OF = DAY.replace(hour=0) - timedelta(seconds=1)
NAMES = {'sky': 'スカイ・ジャーニー', 'river': 'リバー・クルーズ', 'garden': 'ガーデン・レール', 'lights': '光のホール'}

def ts(minutes=0):
    return (DAY + timedelta(minutes=minutes)).isoformat()

def forecast(rows):
    """Reject incomplete/duplicate/future observations; never replace gaps by 0."""
    groups = {}
    seen = set()
    expected_dates = {(DAY.date() - timedelta(days=i)).isoformat() for i in range(1,8)}
    for row in rows:
        aid = row['attraction_id']
        stamp = datetime.fromisoformat(row['observed_at'])
        wait = float(row['wait_minutes'])
        if aid not in NAMES or stamp.tzinfo is None or stamp.utcoffset()!=timedelta(0) or stamp > AS_OF:
            raise ValueError('unknown attraction, non-UTC, or future observation')
        if stamp.date().isoformat() not in expected_dates or stamp.minute or stamp.second or not 9 <= stamp.hour < 17:
            raise ValueError('observations must cover the seven prior dates, hourly 09:00–16:00 UTC')
        if not math.isfinite(wait) or not 0 <= wait <= 240:
            raise ValueError('wait_minutes must be finite and between 0 and 240')
        key = (aid, stamp.isoformat())
        if key in seen:
            raise ValueError('duplicate observation')
        seen.add(key)
        groups.setdefault((aid, stamp.hour), []).append(wait)
    result=[]
    for aid in NAMES:
        for hour in range(9,17):
            samples=groups.get((aid,hour),[])
            if len(samples)!=7:
                raise ValueError(f'missing observations: {aid} {hour}:00 requires 7 days')
            result.append({'attraction_id':aid,'issued_at':AS_OF.isoformat(),
                'valid_from':DAY.replace(hour=hour).isoformat(),'valid_until':DAY.replace(hour=hour+1).isoformat(),
                'wait_minutes':statistics.median(samples),'source':'synthetic seven-day hourly median; not the C1 model',
                'scenario_id':'synthetic-week-median-v1'})
    return result

def make_request(predictions):
    ids=['gate',*NAMES]
    req={'schema_version':'wickward.generic-input.v1','now':ts(),'information_as_of':AS_OF.isoformat(),
        'exit_deadline':ts(480),'park_timezone':'UTC','current_location_id':'gate','exit_location_id':'gate',
        'locations':[{'id':x,'identity_verified':True,'identity_source':'fictional demo location'} for x in ids],
        'walking_edges':[{'from':a,'to':b,'minutes':4+abs(ids.index(a)-ids.index(b))*2,'quality':'estimate','source':'synthetic walking assumption'} for a in ids for b in ids if a!=b],
        'attractions':[{'id':x,'location_id':x,'ride_minutes':10,'utility':10-i,'utility_source':'synthetic visitor preference'} for i,x in enumerate(NAMES)],
        'observations':[],'forecasts':predictions,'must_ride_ids':['sky'],'visited_attraction_ids':[],'action_state':'idle',
        'commitments':[],'flexible_breaks':[],'legacy_non_preemptive_rests':[],
        'policy':{'wait_buffer_minutes':5,'wait_buffer_per_future_minute':0,'boarding_buffer_minutes':0,
                  'exit_buffer_minutes':5,'queue_admission_buffer_minutes':0,'freshness_minutes':10}}
    records=[{'attraction_id':x,'duration_kind':'fixed','duration_nominal_minutes':10,'duration_lower_minutes':None,
        'duration_upper_minutes':None,'source_url':f'https://example.invalid/synthetic/{x}','source_title':'Fictional demo assumption',
        'source_field_label':'duration','source_text':'Synthetic service duration: 10 minutes',
        'received_at':AS_OF.isoformat(),'published_at':None,'source_sha256':'0'*64,'source_artifact':'synthetic-demo-assumption'} for x in NAMES]
    return {'schema_version':engine.SCHEMA_VERSION,'availability_envelope':{'schema_version':engine.availability.SCHEMA_VERSION,
        'generic_request':req,'scheduled_availability':[]},'public_duration_records':records,
        'overhead_assumptions':[{'attraction_id':x,'overhead_minutes':5,'scenario_id':'synthetic-overhead5','assumption':'Synthetic five-minute boarding and exit allowance; not measured'} for x in NAMES]}

def render(result, predictions):
    primary=result.get('primary_result',{})
    steps=result.get('final_validation',{}).get('steps',primary.get('steps',[]))
    def escape(value): return html.escape(str(value))
    rows=[]
    for i,step in enumerate(steps,1):
        aid=step.get('id',step.get('attraction_id',''))
        title=NAMES.get(aid,aid or step.get('kind','移動'))
        arrival=step.get('arrival_at',step.get('start_at',''))
        finish=step.get('planning_finish_at',step.get('finish_at',''))
        rows.append(f'<li><span class="number">{i:02}</span><div><h3>{escape(title)}</h3><p>{escape(arrival)} → {escape(finish)}</p><details><summary>この区間の計算結果</summary><pre>{escape(json.dumps(step,ensure_ascii=False,indent=2))}</pre></details></div></li>')
    table=''.join(f'<tr><th>{escape(NAMES[a])}</th>'+''.join(f'<td>{next(p["wait_minutes"] for p in predictions if p["attraction_id"]==a and datetime.fromisoformat(p["valid_from"]).hour==h):g}</td>' for h in range(9,17))+'</tr>' for a in NAMES)
    return f'''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>wickward — Route experiment</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f6f8f5;color:#20342c;font-family:system-ui,sans-serif}}main{{max-width:1060px;margin:auto;padding:35px 24px}}header{{border-bottom:1px solid #cad7cd;padding-bottom:20px;display:flex;justify-content:space-between;gap:20px}}header strong{{font-size:25px}}h1{{font-size:36px;line-height:1.5;letter-spacing:-1px;margin:35px 0 14px}}p{{line-height:1.8;color:#53695c}}.grid{{display:grid;grid-template-columns:1.4fr 1fr;gap:35px;margin:30px 0}}h2{{font-size:20px}}h3{{margin:0;font-size:18px}}ol{{padding:0;list-style:none}}li{{display:flex;gap:18px;border-bottom:1px solid #cfddd1;padding:22px 0}}.number{{font-size:23px;color:#4d8660;font-weight:600}}li p{{font-size:12px;overflow-wrap:anywhere}}aside{{background:#e9f0e9;padding:24px;border-radius:12px}}details{{font-size:13px}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:15px;border-radius:8px;font-size:11px}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;background:white;font-size:13px}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #e0e7df;white-space:nowrap}}.note{{font-size:13px}}code{{font-size:12px}}a{{color:#246340}}@media(max-width:720px){{main{{padding:24px 16px}}h1{{font-size:27px}}.grid{{grid-template-columns:1fr}}header{{flex-direction:column}}}}
    </style><main><header><strong>wickward</strong><span>合成データによる再現可能なルート実験</span></header><h1>待ち時間だけでなく、<br>帰る時刻まで含めて順路を考える。</h1><p>7日分のサンプルを時刻別に集計し、既存の計画エンジンに渡した結果です。<br>訪問希望・歩行時間・体験時間・退出期限を、同じ入力で評価します。</p><div class="grid"><section><h2>提案されたルート</h2><p class="note">2035年4月10日 09:00–17:00 UTC · 架空のパーク</p><ol>{''.join(rows)}</ol></section><aside><h2>この結果の読み方</h2><p>状態：<code>{escape(result.get('status'))}</code></p><p>入力条件のもとでの実行可能性：<strong>{'確認済み' if result.get('final_validation',{}).get('feasible') else '未成立'}</strong></p><p>7日分の同じ時刻の中央値を予測値とする単純なベースラインです。既存C1予測器ではありません。</p><p>実測による予測精度・現地での有効性・大域的な最適性は未検証です。時刻や歩行時間も合成した条件です。</p><p class="note">モデル上の実行可能性と、現実での有効性を区別しています。</p></aside></div><h2>エンジンに渡した予測待ち時間（分）</h2><div class="table"><table><thead><tr><th>架空の施設</th>{''.join(f'<th>{h}:00</th>' for h in range(9,17))}</tr></thead><tbody>{table}</tbody></table></div><p class="note">入力224件 = 4施設 × 8時間帯 × 7日。欠損・重複・未来のデータがあれば処理を止めます。予測の誤差を測る将来の実測データは含みません。</p><details><summary>完全な計算結果を見る</summary><pre>{escape(json.dumps(result,ensure_ascii=False,indent=2))}</pre></details></main></html>'''

def main():
    with (ROOT/'data/synthetic-week.csv').open(newline='') as f: rows=list(csv.DictReader(f))
    predictions=forecast(rows)
    request=make_request(predictions)
    result=engine.create_plan(request,route_evaluation_budget=500,max_intentional_wait_minutes=30)
    if not result.get('final_validation',{}).get('feasible'):
        raise RuntimeError(json.dumps(result,ensure_ascii=False))
    out=ROOT/'output';out.mkdir(exist_ok=True)
    for name,data in [('forecast',predictions),('request',request),('route',result)]:
        (out/f'{name}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    (out/'route.html').write_text(render(result,predictions))
    print('Generated output/route.html from 224 synthetic observations using the original planning engine.')

if __name__=='__main__':main()
