"""Run BEFORE building:  python scripts/preflight.py
Checks Python, libraries and that the public APIs answer the way we expect.
Saves raw responses to data/cache with provenance. Makes no scientific claims."""
import json, sys, time, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
results = []

def report(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" : " + str(detail)[:300]) if detail else ""))

def save(name, url, payload):
    rec = {"source_url": url, "retrieved_at": datetime.datetime.utcnow().isoformat() + "Z", "payload": payload}
    (CACHE / f"preflight_{name}.json").write_text(json.dumps(rec, indent=2))

report("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
for lib in ("requests", "streamlit", "hypothesis", "pytest"):
    try:
        __import__(lib); report(f"import {lib}", True)
    except Exception:
        report(f"import {lib}", False, "run: pip install -r requirements.txt")
try:
    import requests
except Exception:
    print("requests missing, stopping."); sys.exit(1)

OT = "https://api.platform.opentargets.org/api/v4/graphql"
def gql(query, variables=None):
    r = requests.post(OT, json={"query": query, "variables": variables or {}}, timeout=40)
    return r.status_code, r.json()

ENSG = None
try:
    code, js = gql('query($q:String!){ search(queryString:$q, entityNames:["target"], page:{index:0,size:1}){ hits{ id name entity } } }', {"q": "EGFR"})
    save("ot_search", OT, js)
    hits = js.get("data", {}).get("search", {}).get("hits", [])
    ENSG = hits[0]["id"] if hits else None
    report("Open Targets: resolve EGFR", bool(ENSG), json.dumps(hits[:1]))
except Exception as e:
    report("Open Targets: resolve EGFR", False, repr(e))

if ENSG:
    try:
        q = '''query($id:String!){ target(ensemblId:$id){ id approvedSymbol approvedName
          associatedDiseases(page:{index:0,size:5}){ count rows{ score datatypeScores{ id score } disease{ id name } } } } }'''
        code, js = gql(q, {"id": ENSG}); save("ot_associations", OT, js)
        tgt = (js.get("data") or {}).get("target") or {}
        rows = (tgt.get("associatedDiseases") or {}).get("rows", [])
        report("Open Targets: associations with datatype scores", bool(rows) and "errors" not in js,
               (json.dumps(js.get("errors"))[:300] if "errors" in js else f"top: {rows[0]['disease']['name']} score={rows[0]['score']:.3f}"))
    except Exception as e:
        report("Open Targets: associations", False, repr(e))
    try:
        code, js = gql('{ __type(name:"Target"){ fields{ name } } }'); save("ot_target_fields", OT, js)
        names = [f["name"] for f in js["data"]["__type"]["fields"]]
        interesting = [n for n in names if any(k in n.lower() for k in ("drug", "express", "tractab", "associated", "safety", "constraint"))]
        report("Open Targets: Target fields available", bool(interesting), ", ".join(interesting))
    except Exception as e:
        report("Open Targets: schema", False, repr(e))

try:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    out = {}
    for label, term in (("gene", "EGFR[Title/Abstract]"), ("gene_and_disease", "EGFR[Title/Abstract] AND lung neoplasms[MeSH Terms]")):
        r = requests.get(base, params={"db": "pubmed", "term": term, "retmax": 3, "retmode": "json", "tool": "claimledger", "email": "hackathon@example.com"}, timeout=30)
        js = r.json(); save(f"pubmed_{label}", r.url, js)
        out[label] = js["esearchresult"]["count"]; time.sleep(0.4)
    report("PubMed counts", all(int(v) > 0 for v in out.values()), json.dumps(out))
except Exception as e:
    report("PubMed counts", False, repr(e))

try:
    r = requests.get("https://www.ebi.ac.uk/chembl/api/data/target/search.json", params={"q": "EGFR", "limit": 1}, timeout=30)
    js = r.json(); save("chembl_target", r.url, js)
    t = js.get("targets", [])
    report("ChEMBL target search", r.status_code == 200 and bool(t), t[0].get("target_chembl_id", "") if t else "")
except Exception as e:
    report("ChEMBL target search", False, repr(e))

bad = [n for n, ok in results if not ok]
print("\nSUMMARY:", "ALL GOOD" if not bad else "PROBLEMS: " + "; ".join(bad))
print("If anything failed, copy this whole output to Claude.")
