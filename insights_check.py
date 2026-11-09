"""One-off check: do these eBay keys have Marketplace Insights (sold data) access?
Uses the same EBAY_CLIENT_ID / EBAY_CLIENT_SECRET secrets as ebay_search.py.
Prints GRANTED with sample sales, or the refusal reason. Changes nothing in the repo."""
import base64, json, os, sys, urllib.error, urllib.parse, urllib.request

API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"

def call(req):
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
if not cid or not secret:
    sys.exit("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET secrets.")

auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
status, body = call(urllib.request.Request(API + "/identity/v1/oauth2/token", data=data,
    headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"}))
if status != 200:
    print("RESULT: NOT GRANTED (token refused)")
    print(f"HTTP {status}: {body[:500]}")
    print("Apply for Marketplace Insights access in the eBay developer portal (Application Growth Check).")
    sys.exit(0)

tok = json.loads(body)["access_token"]
q = urllib.parse.urlencode({"q": "mew southern island psa", "category_ids": "183454", "limit": "5"})
status, body = call(urllib.request.Request(f"{API}/buy/marketplace_insights/v1_beta/item_sales/search?{q}",
    headers={"Authorization": "Bearer " + tok, "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU"}))
if status != 200:
    print("RESULT: TOKEN OK BUT SEARCH REFUSED")
    print(f"HTTP {status}: {body[:500]}")
    sys.exit(0)

res = json.loads(body)
print(f"RESULT: GRANTED - {res.get('total', 0)} sold matches for test search")
for it in res.get("itemSales", [])[:5]:
    p = it.get("lastSoldPrice", {})
    print(f"  {it.get('lastSoldDate','')[:10]}  {p.get('value')} {p.get('currency')}  {it.get('title','')[:70]}")
