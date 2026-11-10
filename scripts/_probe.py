import base64, gzip, hashlib, json, os, statistics, sys
import xml.etree.ElementTree as ET
from pathlib import Path
root = Path(".data/logchunks/LogChunks")
ex = []
for xml in sorted((root / "build-failure-reason").rglob("*.xml")):
    for e in ET.parse(xml).getroot().iter("Example"):
        rel = e.findtext("Log")
        p = root / "logs" / rel
        ex.append({"xml": str(xml.relative_to(root)), "log": rel, "exists": p.exists(),
                   "size": p.stat().st_size if p.exists() else 0,
                   "chunk": e.findtext("Chunk") or "", "kw": e.findtext("Keywords"), "cat": e.findtext("Category")})
print("examples", len(ex), "xml", len({e["xml"] for e in ex}), "missing", sum(not e["exists"] for e in ex))
print("chunk lines median", statistics.median(len([l for l in e["chunk"].splitlines() if l.strip()]) for e in ex))
print("log size median", statistics.median(e["size"] for e in ex), "max", max(e["size"] for e in ex))
print("esc in chunk", sum("\x1b" in e["chunk"] for e in ex), "cr in chunk", sum("\r" in e["chunk"] for e in ex))
seen, picks = set(), []
for e in sorted(ex, key=lambda e: e["size"]):
    repo = e["log"].split("/failed/")[0]
    if repo in seen or e["size"] < 3000: continue
    seen.add(repo); picks.append(e)
    if len(picks) == 5: break
for e in picks:
    raw = (root / "logs" / e["log"]).read_bytes()
    print("SAMPLE", json.dumps({"log": e["log"], "kw": e["kw"], "cat": e["cat"], "chunk": e["chunk"],
        "gz": base64.b64encode(gzip.compress(raw, 9)).decode(), "sha256": hashlib.sha256(raw).hexdigest()}))
