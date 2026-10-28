"""Download the open, real-world-derived sources CareHarbor AI is built on.

Everything lands inside the project folder (sources/ and content/medlineplus/)
so it travels with the portable zip. Re-running is safe: existing files are
kept unless --force is given.

Sources
- Synthea sample EHR data (CSV, Apache-2.0): realistic but synthetic patient
  histories generated from real population health statistics. No real patients.
- MedlinePlus health topics (U.S. National Library of Medicine, public domain):
  plain-language summaries used as a reference corpus with citations.

    uv run python scripts/fetch_sources.py
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import httpx

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources"
SYNTHEA_DIR = SOURCES / "synthea"
MEDLINE_RAW = SOURCES / "medlineplus"
MEDLINE_MD = ROOT / "content" / "medlineplus"

SYNTHEA_URL = "https://synthetichealth.github.io/synthea-sample-data/downloads/latest/synthea_sample_data_csv_latest.zip"
SYNTHEA_ZIP = SYNTHEA_DIR / "synthea_sample_data_csv_latest.zip"
MEDLINE_API = "https://wsearch.nlm.nih.gov/ws/query"

# slug -> exact MedlinePlus health-topic title
MEDLINE_TOPICS = {
    "fever": "Fever",
    "headache": "Headache",
    "common-cold": "Common Cold",
    "cough": "Cough",
    "sore-throat": "Sore Throat",
    "heartburn": "Heartburn",
    "gerd": "GERD",
    "diarrhea": "Diarrhea",
    "dehydration": "Dehydration",
    "wounds-and-injuries": "Wounds and Injuries",
    "burns": "Burns",
    "sprains-and-strains": "Sprains and Strains",
    "back-pain": "Back Pain",
    "rashes": "Rashes",
    "hives": "Hives",
    "hay-fever": "Hay Fever",
    "healthy-sleep": "Healthy Sleep",
    "high-blood-pressure": "High Blood Pressure",
    "diabetic-foot": "Diabetic Foot",
    "heart-attack": "Heart Attack",
    "stroke": "Stroke",
    "antibiotics": "Antibiotics",
    "medicines": "Medicines",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_bytes(client: httpx.Client, url: str, attempts: int = 5) -> bytes:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            r = client.get(url)
            r.raise_for_status()
            return r.content
        except (httpx.HTTPError, OSError) as exc:  # flaky network: retry with backoff
            last = exc
            log(f"   retry {i}/{attempts} after error: {exc.__class__.__name__}")
            time.sleep(min(2**i, 20))
    raise RuntimeError(f"Failed to download {url}: {last}")


def fetch_synthea(client: httpx.Client, force: bool) -> None:
    SYNTHEA_DIR.mkdir(parents=True, exist_ok=True)
    if SYNTHEA_ZIP.exists() and not force:
        log(f"✓ Synthea zip already present ({SYNTHEA_ZIP.stat().st_size / 1e6:.1f} MB)")
        return
    log("↓ Downloading Synthea sample CSV data ...")
    data = fetch_bytes(client, SYNTHEA_URL)
    tmp = SYNTHEA_ZIP.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(SYNTHEA_ZIP)
    log(f"✓ Saved {SYNTHEA_ZIP.relative_to(ROOT)} ({len(data) / 1e6:.1f} MB)")


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def summary_to_markdown(summary_html: str) -> str:
    """Convert MedlinePlus FullSummary HTML into simple Markdown."""
    s = re.sub(r'</?span[^>]*>', "", summary_html)
    s = re.sub(r"<li>\s*", "\n- ", s)
    s = re.sub(r"</li>", "", s)
    s = re.sub(r"</?(ul|ol)>", "\n", s)
    s = re.sub(r"<h3>(.*?)</h3>", r"\n### \1\n", s)
    s = re.sub(r"<p>", "\n\n", s)
    s = re.sub(r"</p>", "", s)
    s = html.unescape(_strip_tags(s))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def fetch_medline_topic(client: httpx.Client, slug: str, title: str, force: bool) -> bool:
    raw_file = MEDLINE_RAW / f"{slug}.xml"
    md_file = MEDLINE_MD / f"{slug}.md"
    if raw_file.exists() and md_file.exists() and not force:
        return True
    url = f"{MEDLINE_API}?db=healthTopics&term=title:%22{quote_plus(title)}%22&retmax=10&tool=careharbor"
    xml_bytes = fetch_bytes(client, url)
    raw_file.write_bytes(xml_bytes)
    root = ET.fromstring(xml_bytes)
    chosen = None
    for doc in root.iter("document"):
        fields: dict[str, list[str]] = {}
        for c in doc.findall("content"):
            fields.setdefault(c.get("name", ""), []).append(html.unescape(c.text or ""))
        doc_title = _strip_tags(fields.get("title", [""])[0]).strip()
        if doc_title.lower() == title.lower():
            chosen = (doc.get("url"), doc_title, fields)
            break
    if chosen is None:
        log(f"   ! no exact match for '{title}', skipped")
        return False
    url, doc_title, fields = chosen
    alt = sorted({_strip_tags(a).strip() for a in fields.get("altTitle", [])})
    groups = sorted({_strip_tags(g).strip() for g in fields.get("groupName", [])})
    body = summary_to_markdown(fields.get("FullSummary", [""])[0])
    tags = ", ".join([doc_title.lower(), *[a.lower() for a in alt]])
    md = (
        "---\n"
        f"id: medlineplus-{slug}\n"
        f"title: {doc_title}\n"
        "type: reference\n"
        f"tags: {tags}\n"
        f"groups: {', '.join(groups)}\n"
        f"source_url: {url}\n"
        "source: MedlinePlus, U.S. National Library of Medicine\n"
        "licence: Public domain (U.S. Government work). Attribution: MedlinePlus.gov\n"
        f"retrieved: {date.today().isoformat()}\n"
        "---\n"
        f"# {doc_title}\n\n"
        f"*Reference summary from [MedlinePlus]({url}) (U.S. National Library of Medicine). General information, not clinic advice.*\n\n"
        f"## Summary\n{body}\n"
    )
    md_file.write_text(md, encoding="utf-8")
    return True


def fetch_medlineplus(client: httpx.Client, force: bool) -> None:
    MEDLINE_RAW.mkdir(parents=True, exist_ok=True)
    MEDLINE_MD.mkdir(parents=True, exist_ok=True)
    ok = 0
    log(f"↓ Fetching {len(MEDLINE_TOPICS)} MedlinePlus health topics ...")
    for slug, title in MEDLINE_TOPICS.items():
        if fetch_medline_topic(client, slug, title, force):
            ok += 1
        time.sleep(1.0)  # NLM acceptable-use policy: < 85 requests/minute
    log(f"✓ MedlinePlus topics saved: {ok}/{len(MEDLINE_TOPICS)} -> content/medlineplus/")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-download even if files exist")
    ap.add_argument("--only", choices=["synthea", "medlineplus"], default=None)
    args = ap.parse_args()
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0), follow_redirects=True, headers={"User-Agent": "CareHarbor-demo/0.1"}) as client:
        if args.only in (None, "synthea"):
            fetch_synthea(client, args.force)
        if args.only in (None, "medlineplus"):
            fetch_medlineplus(client, args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
