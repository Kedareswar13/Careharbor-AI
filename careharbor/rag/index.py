"""Document tree index with BM25 ranking - no embeddings, no vector store."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from careharbor.config import CONTENT_DIR, get_settings
from careharbor.rag.text import expand_query, tokenize
from careharbor.security import Principal, can_read

# collection -> folders (glob patterns relative to content/). tenants/<id>/ holds other tenants' documents.
COLLECTIONS = {
    "care": ("leaflets", "medlineplus"),   # symptom guidance: clinic leaflets first, MedlinePlus reference second
    "faq": ("policies", "tenants/*"),      # clinic policies (tenant-filtered)
    "staff": ("staff",),                   # internal SOPs: audience=staff only
}
FOLDER_LABEL = {"leaflets": "Clinic care leaflet", "medlineplus": "MedlinePlus (NLM)", "policies": "Clinic policy", "staff": "Staff SOP", "tenants": "Clinic policy"}
FOLDER_BOOST = {"leaflets": 1.25, "medlineplus": 1.0, "policies": 1.0, "staff": 1.0, "tenants": 1.0}
FOLDER_DEFAULTS = {"medlineplus": ("public", "*"), "staff": ("staff", None)}  # (audience, tenant); None = home tenant
HOME_TENANT = "sunrise-clinic"
K1, B = 1.4, 0.75


@dataclass
class Node:
    node_id: str
    doc_id: str
    folder: str
    doc_title: str
    heading: str
    text: str
    source_url: str
    path: str
    tf: dict[str, int] = field(default_factory=dict)
    length: int = 0
    audience: str = "public"
    tenant: str = HOME_TENANT


@dataclass
class SearchHit:
    node_id: str
    doc_id: str
    title: str
    section: str
    text: str
    score: float
    source: str
    source_url: str
    path: str

    def citation(self) -> dict:
        return {"id": self.node_id, "title": self.title, "section": self.section, "source": self.source, "url": self.source_url or self.path}

    def as_dict(self) -> dict:
        return asdict(self)


def parse_markdown(path: Path, folder: str, content_dir: Path = CONTENT_DIR) -> tuple[dict, list[Node]]:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    m = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = raw[m.end():]
    doc_id = meta.get("id", path.stem)
    title = meta.get("title", path.stem)
    tags = meta.get("tags", "")
    rel = "content/" + path.relative_to(content_dir).as_posix()
    url = meta.get("source_url", "")
    default_aud, default_tenant = FOLDER_DEFAULTS.get(folder, ("public", None))
    if folder == "tenants":
        default_tenant = path.parent.name
    audience = meta.get("audience", default_aud)
    tenant = meta.get("tenant", default_tenant or HOME_TENANT)
    sections: list[tuple[str, list[str]]] = [("Overview", [])]
    for line in body.splitlines():
        h = re.match(r"^##\s+(.*)", line)
        if h:
            sections.append((h.group(1).strip(), []))
        elif not re.match(r"^#\s", line) and not line.startswith("*Reference summary"):
            sections[-1][1].append(line)
    nodes: list[Node] = []
    head_tokens = tokenize(title) * 3 + tokenize(tags) * 2
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:40]
        toks = head_tokens + tokenize(heading) * 2 + tokenize(text)
        nodes.append(Node(f"{doc_id}#{slug}", doc_id, folder, title, heading, text, url, rel, dict(Counter(toks)), len(toks), audience, tenant))
    doc = {"doc_id": doc_id, "title": title, "folder": folder, "tags": tags, "source_url": url, "path": rel, "audience": audience, "tenant": tenant,
           "sections": [n.heading for n in nodes], "node_ids": [n.node_id for n in nodes]}
    return doc, nodes


def content_fingerprint(content_dir: Path = CONTENT_DIR) -> str:
    h = hashlib.sha256()
    for f in sorted(content_dir.rglob("*.md")):
        h.update(f.relative_to(content_dir).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


class RagIndex:
    """Tree of documents/sections with per-collection BM25 statistics."""

    def __init__(self, docs: dict[str, dict], nodes: list[Node], version: str):
        self.docs = docs
        self.nodes = {n.node_id: n for n in nodes}
        self.version = version
        self._stats: dict[str, tuple[dict[str, float], float, list[Node]]] = {}
        for coll, folders in COLLECTIONS.items():
            keys = {f.split("/")[0] for f in folders}
            members = [n for n in nodes if n.folder in keys]
            df: Counter[str] = Counter()
            for n in members:
                df.update(n.tf.keys())
            N = len(members) or 1
            idf = {t: math.log(1 + (N - d + 0.5) / (d + 0.5)) for t, d in df.items()}
            avgdl = sum(n.length for n in members) / N
            self._stats[coll] = (idf, avgdl, members)

    # ----- building / persistence -----------------------------------------
    @classmethod
    def build(cls, content_dir: Path = CONTENT_DIR) -> "RagIndex":
        docs: dict[str, dict] = {}
        nodes: list[Node] = []
        for folders in COLLECTIONS.values():
            for pattern in folders:
                key = pattern.split("/")[0]
                for path in sorted(content_dir.glob(f"{pattern}/*.md")):
                    doc, doc_nodes = parse_markdown(path, key, content_dir)
                    docs[doc["doc_id"]] = doc
                    nodes.extend(doc_nodes)
        return cls(docs, nodes, content_fingerprint(content_dir))

    def save(self, path: Path) -> None:
        """Atomic write (temp file + rename) so a concurrent reader never sees half a file. The file is only a cache:
        if another process holds it (Windows file locks), keep the in-memory index and skip the write."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": self.version, "docs": self.docs, "nodes": [asdict(n) for n in self.nodes.values()]}
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path) -> "RagIndex":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["docs"], [Node(**n) for n in data["nodes"]], data["version"])

    # ----- search -----------------------------------------------------------
    def bm25(self, query: str, collection: str, limit: int = 8, who: Principal | None = None, expand: bool = True) -> list[SearchHit]:
        """Access control happens HERE, before scoring: unauthorised sections are never candidates.
        expand=False disables synonym/Hinglish expansion (used as the 'raw BM25' ablation baseline)."""
        idf, avgdl, members = self._stats[collection]
        who = who or Principal("anonymous", get_settings().tenant_id)
        q = expand_query(query) if expand else tokenize(query)
        if not q:
            return []
        qtf = Counter(q)
        scored: list[tuple[float, Node]] = []
        for n in members:
            if not can_read({"audience": n.audience, "tenant": n.tenant}, who):
                continue
            s = 0.0
            for term, qcount in qtf.items():
                f = n.tf.get(term)
                if not f:
                    continue
                s += idf.get(term, 0.0) * (f * (K1 + 1)) / (f + K1 * (1 - B + B * n.length / avgdl)) * (1 + 0.3 * (qcount - 1))
            if s > 0:
                scored.append((s * FOLDER_BOOST[n.folder], n))
        scored.sort(key=lambda x: (-x[0], x[1].node_id))
        return [self._hit(n, s) for s, n in scored[:limit]]

    def search(self, query: str, collection: str = "care", k: int | None = None, who: Principal | None = None) -> list[SearchHit]:
        """BM25 shortlist (access-filtered) diversified across documents."""
        k = k or get_settings().rag_top_k
        return self._diverse(self.bm25(query, collection, limit=max(8, k * 3), who=who), k)

    def _diverse(self, hits: list[SearchHit], k: int) -> list[SearchHit]:
        """Prefer sections from the best documents but avoid returning k copies of one doc's overview."""
        out: list[SearchHit] = []
        per_doc: Counter[str] = Counter()
        for h in hits:
            if per_doc[h.doc_id] < 2:
                out.append(h)
                per_doc[h.doc_id] += 1
            if len(out) == k:
                break
        return out

    def toc(self, doc_ids: list[str], previews: bool = False) -> str:
        """Compact table of contents for LLM navigation (headings only by default: fewer prompt tokens on CPU)."""
        lines = []
        for d in doc_ids:
            doc = self.docs[d]
            lines.append(f"[{doc['title']}] ({FOLDER_LABEL[doc['folder']]})")
            for nid, heading in zip(doc["node_ids"], doc["sections"]):
                preview = " - " + self.nodes[nid].text.replace("\n", " ")[:70] if previews else ""
                lines.append(f"  {nid} :: {heading}{preview}")
        return "\n".join(lines)

    def doc_sections(self, doc_id: str, who: Principal | None = None) -> list[SearchHit]:
        who = who or Principal("anonymous", get_settings().tenant_id)
        if not can_read(self.docs[doc_id], who):
            return []
        return [self._hit(self.nodes[n], 0.0) for n in self.docs[doc_id]["node_ids"]]

    def _hit(self, n: Node, score: float) -> SearchHit:
        return SearchHit(n.node_id, n.doc_id, n.doc_title, n.heading, n.text, round(score, 3), FOLDER_LABEL[n.folder], n.source_url, n.path)


_lock = threading.Lock()
_index: RagIndex | None = None


def get_index(rebuild: bool = False) -> RagIndex:
    """Load the persisted tree index, rebuilding automatically when content changes."""
    global _index
    with _lock:
        version = content_fingerprint()
        if _index is not None and not rebuild and _index.version == version:
            return _index
        path = get_settings().index_path / "tree_index.json"
        if path.exists() and not rebuild:
            try:
                idx = RagIndex.load(path)
                if idx.version == version:
                    _index = idx
                    return idx
            except (OSError, ValueError, KeyError, TypeError):
                pass
        _index = RagIndex.build()
        _index.save(path)
        return _index
