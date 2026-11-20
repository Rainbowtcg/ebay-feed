# Builds feed.js (compact, prices in A$) for the finder's Feed tab from the eBay feed's listings.json.
# Usage: python3 -I build_feed.py <listings.json> <out feed.js> [want.txt] [stores.json]
# stores.json (optional): in-stock shop listings from store_scan.py, same item format plus url; added as rows with the shop URL.
# want.txt (optional): the user's want list, one line per card. Titles matching a want line are kept
# even when they look modern (post-2018), because want-list items are an exception to the era rule.
import json, os,sys,re
FX={'AUD':1,'JPY':0.009002,'USD':1.4232,'HKD':0.1813,'GBP':1.9062,'KRW':0.001073,'SGD':1.1138,'CAD':1.03,'NZD':0.91,'EUR':1.65}
d=json.load(open(sys.argv[1]))
MODERN=re.compile(r'\b(2019|202\d)\b|\b151\b|\bsar\b|\bsir\b|\bsv\d|scarlet|violet|vmax|vstar|master ball',re.I)
STOP={'under','below','psa','cgc','bgs','raw','near','mint','only','english','japanese','japan','era','holo','card','cards','pokemon','and','the','full','art','promo','promos'}
WANT=[]
if len(sys.argv)>3 and sys.argv[3]!='-':
  for l in open(sys.argv[3],encoding='utf8'):
    l=re.sub(r'(under|below|<)\s*(a\$|¥)?[\d,]+.*$','',l.lower())
    ws=[w for w in re.findall(r"[a-z0-9]+",l) if len(w)>2 and w not in STOP]
    if ws: WANT.append(ws)
def wanted(t):
  tl=t.lower(); return any(all(w in tl for w in ws) for ws in WANT)
rows=[]
flags={}  # id -> 'damage: crease' / 'unverified' from ebay_search.py; the finder hides damage by default
items=list(d['items'])
if len(sys.argv)>4:
  try:
    st=json.load(open(sys.argv[4]))
    items+=[dict(x,shop=1) for x in st.get('items',[]) if x.get('available',True)]
    print('shops:',st.get('count'),'items;',', '.join(f"{x['shop']} {x['items']}"+(' ERROR' if x.get('error') else '') for x in st.get('shops',[])))
  except Exception as e: print('stores.json not used:',e)
for it in items:
  t=it.get('title') or ''
  if MODERN.search(t) and not it.get('sealed') and not wanted(t) and not str(it.get('query') or '').startswith('+'): continue  # sealed ETBs/boxes: any year
  try: p=float(it.get('price') or 0)*FX.get(it.get('currency'),0)
  except: continue
  if not p: continue
  try: post=float(it.get('post') or 0)*FX.get(it.get('postCurrency') or it.get('currency'),0)
  except: post=0
  auc=1 if 'AUCTION' in (it.get('options') or []) else 0
  bo=1 if 'BEST_OFFER' in (it.get('options') or []) else 0
  if it.get('flag'): flags[str(it['id'])]=it['flag'][:40]
  rows.append([it['id'],t,round(p,2),round(post,2),it.get('country') or '',it.get('seller') or '',auc,bo,it.get('bids') or 0,(it.get('ends') or '')[:16]]+([it.get('url') or ''] if it.get('shop') else []))
out={'generated':d.get('generated'),'count':len(rows),'shops':sum(1 for r in rows if len(r)>10),'items':rows,'flags':flags}
# enrich.json (from enrich.py): categories, price history and card values, one entry per row in the same order as items.
# x[i] = 0 or [category indexes, days since first seen, previous price if it dropped, lowest A$ for this card in 30 days,
#              live copies of this card, card value A$ at this grade, value label, tags (n new, d dropped, r relisted, l lowest in 30 days)]
try:
  import datetime as _dt
  en=json.load(open('enrich.json',encoding='utf8')); day=_dt.date.fromisoformat(en['day'])
  def _x(i):
    v=en['x'].get(str(i))
    if not v: return 0
    try: age=(day-_dt.date.fromisoformat(v[1])).days
    except Exception: age=0
    return [v[0],age]+v[2:]
  def _trim(v):  # drop trailing empty fields; the page reads missing ones as empty
    if not v: return 0
    while len(v)>2 and v[-1] in (None,'',0,[]): v.pop()  # keep [cats, age] so a listing's age always survives
    return v
  out['cats']=en['cats']; out['day']=en['day']; out['x']=[_trim(_x(r[0])) for r in rows]
  print('enrich:',sum(1 for v in out['x'] if v),'rows with extras')
except Exception as e: print('enrich.json not used:',e)
try: out['guide']=json.load(open('guide.json',encoding='utf8'))
except Exception as e: print('guide.json not used:',e)
try: out['digest']=json.load(open('digest.json',encoding='utf8'))
except Exception as e: print('digest.json not used:',e)
try:
  out['shopPhotos']={x['shop']:x['photos'] for x in json.load(open(sys.argv[4])).get('shops',[]) if x.get('photos')}
except Exception as e: print('shop photo labels not used:',e)
try:
  out['sealed']=json.load(open('sealed.json',encoding='utf8')).get('items',[])
except Exception as e: print('sealed.json not used:',e)
s='window.FEED='+json.dumps(out,ensure_ascii=True,separators=(',',':'))+';'
open(sys.argv[2],'w').write(s)
# feed-lite.js: what the Deals tab needs when the page opens (price, post, bids, end time per eBay listing, plus the
# daily shop scan bits), so the full feed.js only loads when the Feed or Wants tab is opened.
lite={'generated':out.get('generated'),'shopPhotos':out.get('shopPhotos',{}),'sealed':out.get('sealed',[]),
      'items':[[r[0],r[2],r[3],r[6],r[8],r[9]] for r in rows if len(r)<=10]}
ls='window.FEEDLITE='+json.dumps(lite,ensure_ascii=True,separators=(',',':'))+';'
lp=os.path.join(os.path.dirname(os.path.abspath(sys.argv[2])),'feed-lite.js')
open(lp,'w').write(ls)
print('feed-lite.js',len(lite['items']),'rows',round(len(ls)/1e6,2),'MB')
print(len(rows),'items',round(len(s)/1e6,2),'MB')
