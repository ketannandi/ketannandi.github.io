#!/usr/bin/env python3
"""Weekly updater for the website's publication list and citation metrics.

Run automatically by GitHub Actions (.github/workflows/update-data.yml).
Sources: OpenAlex (papers, citations, h-index) and ORCID (to catch papers
OpenAlex has not linked to his profile yet). Uses only the Python standard
library, so nothing needs installing.

Reads  data/config.json
Writes data/site-data.json   (the website reads this file)

If anything goes wrong, the old data file is left untouched.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "config.json"
OUTPUT = ROOT / "data" / "site-data.json"
OPENALEX = "https://api.openalex.org"

# Overridable in tests
def http_get_json(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "academic-website-updater/1.0"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network hiccup: retry
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last}")


# ---------------------------------------------------------------- helpers
def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())

def norm_doi(d):
    if not d:
        return ""
    d = d.strip().lower()
    return re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)

def short_name(full):
    """'Ketan Kumar Nandi' or 'K. K. Nandi' -> 'Nandi, K. K.'"""
    full = (full or "").strip()
    if not full:
        return ""
    if "," in full:  # already 'Surname, Given'
        sur, given = [p.strip() for p in full.split(",", 1)]
    else:
        parts = full.replace(".", ". ").split()
        sur, given = parts[-1], " ".join(parts[:-1])
    initials = " ".join(p[0].upper() + "." for p in re.split(r"[\s.\-]+", given) if p)
    return f"{sur}, {initials}".strip().rstrip(",")

def clean_title(t):
    t = re.sub(r"<[^>]+>", "", t or "")  # strip <i>, <sub> etc.
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- OpenAlex
def get_author(orcid, mailto):
    q = f"?mailto={urllib.parse.quote(mailto)}" if mailto else ""
    return http_get_json(f"{OPENALEX}/authors/orcid:{orcid}{q}")

def get_works_for_author(author_id, mailto):
    short = author_id.rsplit("/", 1)[-1]
    works, cursor = [], "*"
    while cursor:
        params = {"filter": f"author.id:{short}", "per_page": "200", "cursor": cursor}
        if mailto:
            params["mailto"] = mailto
        data = http_get_json(f"{OPENALEX}/works?" + urllib.parse.urlencode(params))
        works.extend(data.get("results", []))
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not data.get("results"):
            break
    return works

def get_work_by_doi(doi, mailto):
    q = f"?mailto={urllib.parse.quote(mailto)}" if mailto else ""
    try:
        return http_get_json(f"{OPENALEX}/works/doi:{urllib.parse.quote(doi, safe='/')}{q}")
    except RuntimeError:
        return None

def orcid_dois(orcid):
    """DOIs listed on his ORCID record (empty list if ORCID is unreachable)."""
    try:
        data = http_get_json(f"https://pub.orcid.org/v3.0/{orcid}/works")
    except RuntimeError as e:
        print("  ORCID not reachable, continuing with OpenAlex only:", e)
        return []
    dois = []
    for g in data.get("group", []):
        for ext in (g.get("external-ids") or {}).get("external-id", []):
            if (ext.get("external-id-type") or "").lower() == "doi":
                d = norm_doi(ext.get("external-id-value"))
                if d:
                    dois.append(d)
    return sorted(set(dois))


# ---------------------------------------------------------------- conversion
SKIP_TYPES = {"paratext", "erratum", "editorial", "letter", "peer-review", "dataset",
              "preprint", "dissertation", "retraction", "supplementary-materials", "other"}

def classify(work):
    wtype = (work.get("type") or "").lower()
    src = ((work.get("primary_location") or {}).get("source") or {})
    stype = (src.get("type") or "").lower()
    if wtype in ("book-chapter", "book"):
        return "chapter"
    if wtype == "proceedings-article" or stype == "conference":
        return "conference"
    if stype == "book-series":
        return "chapter"
    if wtype in ("article", "review") and stype == "journal":
        return "journal"
    return None  # repositories, preprints etc. are not shown

def details_of(work):
    b = work.get("biblio") or {}
    vol, iss, fp, lp = b.get("volume"), b.get("issue"), b.get("first_page"), b.get("last_page")
    out = ""
    if vol:
        out = str(vol) + (f"({iss})" if iss else "")
    if fp:
        pages = f"{fp}–{lp}" if lp and lp != fp else str(fp)
        out = f"{out}, {pages}" if out else pages
    return out

def is_me(authorship, my_ids, orcid):
    a = authorship.get("author") or {}
    if a.get("id") in my_ids:
        return True
    if a.get("orcid") and a["orcid"].endswith(orcid):
        return True
    return False

def convert(work, my_ids, orcid, quartiles):
    kind = classify(work)
    if not kind or work.get("is_retracted") or work.get("is_paratext"):
        return None
    if (work.get("type") or "").lower() in SKIP_TYPES:
        return None
    src = ((work.get("primary_location") or {}).get("source") or {})
    venue = src.get("display_name") or ""
    authors = []
    for au in work.get("authorships", []):
        name = au.get("raw_author_name") or (au.get("author") or {}).get("display_name")
        authors.append({"name": short_name(name), "me": is_me(au, my_ids, orcid)})
    if authors and not any(a["me"] for a in authors):
        # fall back to surname match (his profile may be split in OpenAlex)
        for a in authors:
            if a["name"].lower() == "nandi, k. k.":
                a["me"] = True
    if not any(a["me"] for a in authors):
        return None  # not actually his paper
    item = {
        "type": kind,
        "year": work.get("publication_year"),
        "title": clean_title(work.get("display_name") or work.get("title")),
        "venue": venue,
        "details": details_of(work),
        "doi": norm_doi(work.get("doi")),
        "citations": work.get("cited_by_count") or 0,
        "authors": authors,
    }
    q = quartiles.get(venue.lower()) if kind == "journal" else None
    if q:
        item["quartile"] = q
    return item

def extra_item(e):
    authors = [{"name": n, "me": n.lower().startswith("nandi, k")} for n in e.get("authors", [])]
    return {"type": e["type"], "year": e["year"], "title": e["title"], "venue": e.get("venue", ""),
            "details": e.get("details", ""), "doi": norm_doi(e.get("doi")), "citations": 0,
            "authors": authors, "manual": True}


# ---------------------------------------------------------------- main
def build(cfg):
    orcid, mailto = cfg["orcid"], cfg.get("contact_email", "")
    quartiles = {k.lower(): v for k, v in cfg.get("journal_quartiles", {}).items()}
    exclude = {norm_doi(x) for x in cfg.get("exclude", [])} | {norm_title(x) for x in cfg.get("exclude", [])}

    print("Fetching OpenAlex author profile…")
    author = get_author(orcid, mailto)
    my_ids = {author["id"]}
    print("  ", author.get("display_name"), author["id"])

    print("Fetching works…")
    works = get_works_for_author(author["id"], mailto)
    print(f"  {len(works)} works on OpenAlex profile")

    have = {norm_doi(w.get("doi")) for w in works if w.get("doi")}
    missing = [d for d in orcid_dois(orcid) if d not in have]
    for d in missing:
        w = get_work_by_doi(d, mailto)
        if w:
            works.append(w)
    if missing:
        print(f"  {len(missing)} extra DOI(s) checked from ORCID")

    items = {}
    def add(item):
        if not item or not item["title"]:
            return
        if item["doi"] in exclude or norm_title(item["title"]) in exclude:
            return
        key = norm_title(item["title"])
        old = items.get(key)
        rank = {"journal": 3, "chapter": 2, "conference": 1}
        if old is None:
            items[key] = item
        else:  # duplicate: keep the better record but the higher citation count
            better = item if (rank[item["type"]], bool(item["doi"])) > (rank[old["type"]], bool(old["doi"])) else old
            better["citations"] = max(item["citations"], old["citations"])
            items[key] = better

    for w in works:
        add(convert(w, my_ids, orcid, quartiles))
    for e in cfg.get("extra_publications", []):
        key = norm_title(e["title"])
        if key not in items:
            add(extra_item(e))

    pubs = sorted(items.values(), key=lambda p: (-(p["year"] or 0), {"journal": 0, "chapter": 1, "conference": 2}[p["type"]], p["title"]))

    stats = author.get("summary_stats") or {}
    by_year = sorted(({"year": c["year"], "citations": c.get("cited_by_count", 0)}
                      for c in author.get("counts_by_year", []) if c.get("year")), key=lambda c: c["year"])
    while by_year and by_year[0]["citations"] == 0:
        by_year.pop(0)

    return {
        "updated": date.today().isoformat(),
        "source": "OpenAlex",
        "metrics": {
            "citations": author.get("cited_by_count", 0),
            "h_index": stats.get("h_index", 0),
            "i10_index": stats.get("i10_index", 0),
        },
        "citations_by_year": by_year,
        "publications": pubs,
    }


def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    try:
        data = build(cfg)
    except Exception as e:
        print("Update failed, keeping the existing data file:", e)
        sys.exit(1)

    # Safety check: never replace a good list with a suspiciously short one
    if OUTPUT.exists():
        old = json.loads(OUTPUT.read_text(encoding="utf-8"))
        old_n = sum(1 for p in old.get("publications", []) if p["type"] == "journal")
        new_n = sum(1 for p in data["publications"] if p["type"] == "journal")
        if new_n < max(1, old_n // 2):
            print(f"Only {new_n} journal papers found (previously {old_n}); keeping the existing data file.")
            sys.exit(1)

    OUTPUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    m = data["metrics"]
    print(f"Saved {len(data['publications'])} publications · {m['citations']} citations · h-index {m['h_index']}")


if __name__ == "__main__":
    main()
