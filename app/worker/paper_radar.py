import argparse
import base64
import email.message
import hashlib
import html
import json
import os
import re
import shutil
import smtplib
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "data" / "config.json"
PAPERS_PATH = ROOT / "data" / "papers.json"
STATE_PATH = ROOT / "data" / "state.json"
REPORTS_DIR = ROOT / "reports"


@dataclass
class Paper:
    stable_id: str
    title: str
    authors: list[str]
    abstract: str
    published_at: str
    source: str
    url: str
    pdf_url: str | None = None
    doi: str | None = None
    topics: list[str] = field(default_factory=list)


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_due(config, state, force=False):
    if force:
        return True, "manual force run"
    schedule = config.get("schedule", {})
    if not schedule.get("enabled", True):
        return False, "schedule disabled"

    mode = schedule.get("mode", "weekly")
    tz = ZoneInfo(schedule.get("timezone", "UTC"))
    local_now = now_utc().astimezone(tz)
    target_hour, target_minute = parse_time(schedule.get("time", "09:00"))
    last_run = parse_iso(state.get("last_successful_run_at"))
    if last_run:
        last_local_date = last_run.astimezone(tz).date()
    else:
        last_local_date = None

    if local_now.hour < target_hour or (local_now.hour == target_hour and local_now.minute < target_minute):
        return False, "scheduled time has not arrived"

    if mode == "hourly":
        return True, "hourly schedule"
    if mode == "daily":
        return last_local_date != local_now.date(), "daily schedule"
    if mode == "weekly":
        wanted = schedule.get("day_of_week", "monday").lower()
        if local_now.strftime("%A").lower() != wanted:
            return False, f"waiting for {wanted}"
        return last_local_date != local_now.date(), "weekly schedule"
    if mode == "monthly":
        wanted_day = int(schedule.get("day_of_month", 1))
        if local_now.day != wanted_day:
            return False, f"waiting for day {wanted_day}"
        return last_local_date != local_now.date(), "monthly schedule"
    return True, f"unknown schedule mode {mode}, running defensively"


def parse_time(value):
    match = re.match(r"^(\d{1,2}):(\d{2})$", str(value))
    if not match:
        return 9, 0
    return int(match.group(1)), int(match.group(2))


def normalize_title(title):
    return re.sub(r"\W+", " ", title.casefold()).strip()


def title_hash(title):
    return "title:" + hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()[:24]


def paper_keys(paper: Paper):
    keys = {paper.stable_id, title_hash(paper.title)}
    if paper.doi:
        keys.add("doi:" + paper.doi.lower().strip())
    return {k for k in keys if k}


def known_keys(history):
    keys = set()
    for item in history:
        for key in item.get("dedupe_keys", []):
            keys.add(key)
        if item.get("id"):
            keys.add(item["id"])
        if item.get("title"):
            keys.add(title_hash(item["title"]))
        if item.get("doi"):
            keys.add("doi:" + item["doi"].lower().strip())
    return keys


def relevant(paper: Paper, topic):
    haystack = f"{paper.title}\n{paper.abstract}".casefold()
    keywords = [k.casefold() for k in topic.get("keywords", []) if k.strip()]
    excludes = [k.casefold() for k in topic.get("exclude_keywords", []) if k.strip()]
    if keywords and not any(k in haystack for k in keywords):
        return False
    return not any(k in haystack for k in excludes)


def search_topic(topic, config):
    sources = topic.get("sources") or config.get("search", {}).get("default_sources", ["arxiv", "openalex"])
    found = []
    for source in sources:
        source = normalize_source(source)
        try:
            if source == "arxiv":
                found.extend(search_arxiv(topic, config))
            elif source == "openalex":
                found.extend(search_openalex(topic, config))
            elif source == "semantic_scholar":
                found.extend(search_semantic_scholar(topic, config))
            elif source == "crossref":
                found.extend(search_crossref(topic, config))
            elif source == "pubmed":
                found.extend(search_pubmed(topic, config))
            elif source == "europe_pmc":
                found.extend(search_europe_pmc(topic, config))
            elif source == "datacite":
                found.extend(search_datacite(topic, config))
            elif source == "doaj":
                found.extend(search_doaj(topic, config))
            elif source == "biorxiv":
                found.extend(search_biorxiv(topic, config, "biorxiv"))
            elif source == "medrxiv":
                found.extend(search_biorxiv(topic, config, "medrxiv"))
            else:
                print(f"Unknown source ignored: {source}")
        except Exception as exc:
            print(f"Source failed and was skipped: {source}: {exc}")
    return [paper for paper in found if relevant(paper, topic)]


def normalize_source(source):
    normalized = str(source).strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "semantic": "semantic_scholar",
        "semanticscholar": "semantic_scholar",
        "semantic_scholar_api": "semantic_scholar",
        "europepmc": "europe_pmc",
        "europe_pmc": "europe_pmc",
        "pmc": "europe_pmc",
        "bio_rxiv": "biorxiv",
        "med_rxiv": "medrxiv",
        "data_cite": "datacite",
    }
    return aliases.get(normalized, normalized)


def query_text(topic):
    return " OR ".join(f'"{k}"' if " " in k else k for k in topic.get("keywords", []))


def arxiv_query_text(topic):
    parts = []
    for keyword in topic.get("keywords", []):
        keyword = keyword.strip()
        if not keyword:
            continue
        value = f'"{keyword}"' if " " in keyword else keyword
        parts.append("all:" + value)
    return " OR ".join(parts)


def search_arxiv(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    search_query = arxiv_query_text(topic)
    if not search_query:
        return []
    params = urllib.parse.urlencode({
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    url = f"https://export.arxiv.org/api/query?{params}"
    data = fetch_bytes(url, timeout=30)
    root = ET.fromstring(data)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    papers = []
    for entry in root.findall("atom:entry", ns):
        entry_id = text_of(entry, "atom:id", ns)
        arxiv_id = entry_id.rsplit("/", 1)[-1]
        links = entry.findall("atom:link", ns)
        pdf_url = None
        page_url = entry_id
        for link in links:
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href")
            if link.attrib.get("rel") == "alternate":
                page_url = link.attrib.get("href", page_url)
        doi = text_of(entry, "arxiv:doi", ns) or None
        authors = [text_of(author, "atom:name", ns) for author in entry.findall("atom:author", ns)]
        papers.append(Paper(
            stable_id=f"arxiv:{arxiv_id}",
            title=clean_text(text_of(entry, "atom:title", ns)),
            authors=[a for a in authors if a],
            abstract=clean_text(text_of(entry, "atom:summary", ns)),
            published_at=text_of(entry, "atom:published", ns),
            source="arxiv",
            url=page_url,
            pdf_url=pdf_url,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_openalex(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    search_query = " ".join(topic.get("keywords", []))
    if not search_query:
        return []
    params = urllib.parse.urlencode({
        "search": search_query,
        "per-page": max_results,
        "sort": "publication_date:desc",
    })
    url = f"https://api.openalex.org/works?{params}"
    data = json.loads(fetch_bytes(url, timeout=30).decode("utf-8"))
    papers = []
    for item in data.get("results", []):
        title = item.get("title") or ""
        authors = [
            authorship.get("author", {}).get("display_name", "")
            for authorship in item.get("authorships", [])
        ]
        primary_location = item.get("primary_location") or {}
        source_url = item.get("doi") or item.get("id") or ""
        pdf_url = ((primary_location.get("source") or {}).get("homepage_url"))
        open_access = item.get("open_access") or {}
        if open_access.get("oa_url"):
            pdf_url = open_access.get("oa_url")
        abstract = openalex_abstract(item.get("abstract_inverted_index"))
        doi = item.get("doi")
        if doi and doi.startswith("https://doi.org/"):
            doi = doi.replace("https://doi.org/", "", 1)
        papers.append(Paper(
            stable_id="openalex:" + item.get("id", title_hash(title)),
            title=clean_text(title),
            authors=[a for a in authors if a],
            abstract=abstract,
            published_at=item.get("publication_date") or "",
            source="openalex",
            url=source_url,
            pdf_url=pdf_url,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_semantic_scholar(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    search_query = " ".join(topic.get("keywords", []))
    if not search_query:
        return []
    fields = ",".join([
        "paperId",
        "title",
        "abstract",
        "authors",
        "year",
        "publicationDate",
        "url",
        "externalIds",
        "openAccessPdf",
    ])
    params = urllib.parse.urlencode({
        "query": search_query,
        "limit": max_results,
        "fields": fields,
    })
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    headers = {}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    data = json.loads(fetch_bytes(url, timeout=30, headers=headers).decode("utf-8"))
    papers = []
    for item in data.get("data", []):
        external = item.get("externalIds") or {}
        doi = external.get("DOI")
        arxiv_id = external.get("ArXiv")
        pdf_url = (item.get("openAccessPdf") or {}).get("url")
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        authors = [(author or {}).get("name", "") for author in item.get("authors", [])]
        papers.append(Paper(
            stable_id="semantic_scholar:" + (item.get("paperId") or title_hash(item.get("title", ""))),
            title=clean_text(item.get("title") or ""),
            authors=[a for a in authors if a],
            abstract=clean_text(item.get("abstract") or ""),
            published_at=item.get("publicationDate") or str(item.get("year") or ""),
            source="semantic_scholar",
            url=item.get("url") or "",
            pdf_url=pdf_url,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_crossref(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    search_query = " ".join(topic.get("keywords", []))
    if not search_query:
        return []
    cutoff = (now_utc() - timedelta(days=int(config.get("search", {}).get("lookback_days", 14)))).date().isoformat()
    mailto = os.getenv("MAIL_FROM") or config.get("email", {}).get("from", "")
    params = {
        "query.bibliographic": search_query,
        "rows": max_results,
        "sort": "published",
        "order": "desc",
        "filter": f"from-pub-date:{cutoff}",
    }
    if mailto:
        params["mailto"] = extract_email(mailto)
    url = f"https://api.crossref.org/works?{urllib.parse.urlencode(params)}"
    data = json.loads(fetch_bytes(url, timeout=30).decode("utf-8"))
    papers = []
    for item in data.get("message", {}).get("items", []):
        title = first_value(item.get("title")) or ""
        authors = []
        for author in item.get("author", []):
            name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
            if name:
                authors.append(name)
        published_at = crossref_date(item)
        pdf_url = crossref_pdf_url(item)
        abstract = strip_markup(item.get("abstract") or "")
        doi = item.get("DOI")
        papers.append(Paper(
            stable_id="crossref:" + (doi.lower() if doi else title_hash(title)),
            title=clean_text(title),
            authors=authors,
            abstract=clean_text(abstract),
            published_at=published_at,
            source="crossref",
            url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
            pdf_url=pdf_url,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_pubmed(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    terms = [k.strip() for k in topic.get("keywords", []) if k.strip()]
    if not terms:
        return []
    lookback = int(config.get("search", {}).get("lookback_days", 14))
    search_query = "(" + " OR ".join(terms) + f") AND last {lookback} days[pdat]"
    email_param = extract_email(os.getenv("MAIL_FROM") or config.get("email", {}).get("from", ""))
    params = {
        "db": "pubmed",
        "term": search_query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "pub+date",
    }
    if email_param:
        params["email"] = email_param
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    search_data = json.loads(fetch_bytes(search_url, timeout=30).decode("utf-8"))
    ids = search_data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    summary_params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
    }
    if email_param:
        summary_params["email"] = email_param
    summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(summary_params)
    summary_data = json.loads(fetch_bytes(summary_url, timeout=30).decode("utf-8"))
    papers = []
    for pmid in ids:
        item = summary_data.get("result", {}).get(pmid, {})
        title = item.get("title") or ""
        authors = [(author or {}).get("name", "") for author in item.get("authors", [])]
        doi = pubmed_doi(item)
        papers.append(Paper(
            stable_id="pubmed:" + pmid,
            title=clean_text(title),
            authors=[a for a in authors if a],
            abstract="",
            published_at=parse_pubmed_date(item.get("pubdate") or item.get("epubdate") or ""),
            source="pubmed",
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            pdf_url=None,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_europe_pmc(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    search_query = " ".join(topic.get("keywords", []))
    if not search_query:
        return []
    lookback = int(config.get("search", {}).get("lookback_days", 14))
    cutoff = (now_utc() - timedelta(days=lookback)).date().isoformat()
    query = f'({search_query}) AND FIRST_PDATE:[{cutoff} TO 3000-01-01] sort_date:y'
    params = urllib.parse.urlencode({
        "query": query,
        "format": "json",
        "pageSize": max_results,
        "resultType": "core",
    })
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{params}"
    data = json.loads(fetch_bytes(url, timeout=30).decode("utf-8"))
    papers = []
    for item in data.get("resultList", {}).get("result", []):
        doi = item.get("doi")
        pmid = item.get("pmid")
        pmcid = item.get("pmcid")
        pdf_url = europe_pmc_pdf_url(item)
        url_value = item.get("fullTextUrlList", {}).get("fullTextUrl", [{}])[0].get("url")
        if not url_value:
            url_value = f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', pmid or pmcid or '')}"
        papers.append(Paper(
            stable_id="europe_pmc:" + (pmid or pmcid or doi or title_hash(item.get("title", ""))),
            title=clean_text(item.get("title") or ""),
            authors=split_authors(item.get("authorString") or ""),
            abstract=clean_text(strip_markup(item.get("abstractText") or "")),
            published_at=parse_europe_pmc_date(item.get("firstPublicationDate") or item.get("pubYear") or ""),
            source="europe_pmc",
            url=url_value,
            pdf_url=pdf_url,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_datacite(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    search_query = " ".join(topic.get("keywords", []))
    if not search_query:
        return []
    params = urllib.parse.urlencode({
        "query": search_query,
        "page[size]": max_results,
        "sort": "published:desc",
    })
    url = f"https://api.datacite.org/dois?{params}"
    data = json.loads(fetch_bytes(url, timeout=30).decode("utf-8"))
    papers = []
    for item in data.get("data", []):
        attributes = item.get("attributes", {})
        doi = attributes.get("doi")
        title = first_value(attributes.get("titles") or [{}]).get("title", "") if attributes.get("titles") else ""
        authors = []
        for creator in attributes.get("creators", []):
            if creator.get("name"):
                authors.append(creator["name"])
        descriptions = attributes.get("descriptions") or []
        abstract = ""
        for description in descriptions:
            if (description.get("descriptionType") or "").casefold() in {"abstract", "other"}:
                abstract = description.get("description") or ""
                break
        published = attributes.get("published") or attributes.get("publicationYear") or ""
        papers.append(Paper(
            stable_id="datacite:" + (doi.lower() if doi else item.get("id", title_hash(title))),
            title=clean_text(title),
            authors=authors,
            abstract=clean_text(strip_markup(abstract)),
            published_at=str(published),
            source="datacite",
            url=attributes.get("url") or (f"https://doi.org/{doi}" if doi else ""),
            pdf_url=datacite_pdf_url(attributes),
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_doaj(topic, config):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    search_query = " ".join(topic.get("keywords", []))
    if not search_query:
        return []
    encoded_query = urllib.parse.quote(search_query)
    params = urllib.parse.urlencode({"page": 1, "pageSize": max_results})
    url = f"https://doaj.org/api/search/articles/{encoded_query}?{params}"
    data = json.loads(fetch_bytes(url, timeout=30).decode("utf-8"))
    papers = []
    for item in data.get("results", []):
        bibjson = item.get("bibjson", {})
        title = bibjson.get("title") or ""
        authors = [(author or {}).get("name", "") for author in bibjson.get("author", [])]
        doi = doaj_identifier(bibjson, "doi")
        pdf_url = doaj_pdf_url(bibjson)
        published = str(bibjson.get("year") or "")
        papers.append(Paper(
            stable_id="doaj:" + (doi.lower() if doi else item.get("id", title_hash(title))),
            title=clean_text(title),
            authors=[a for a in authors if a],
            abstract=clean_text(bibjson.get("abstract") or ""),
            published_at=published,
            source="doaj",
            url=doaj_article_url(bibjson) or (f"https://doi.org/{doi}" if doi else ""),
            pdf_url=pdf_url,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def search_biorxiv(topic, config, server):
    max_results = int(config.get("search", {}).get("max_results_per_source", 25))
    lookback = int(config.get("search", {}).get("lookback_days", 14))
    start = (now_utc() - timedelta(days=lookback)).date().isoformat()
    end = now_utc().date().isoformat()
    base = "https://api.biorxiv.org" if server == "biorxiv" else "https://api.medrxiv.org"
    url = f"{base}/details/{server}/{start}/{end}/0/json"
    data = json.loads(fetch_bytes(url, timeout=30).decode("utf-8"))
    papers = []
    for item in data.get("collection", [])[:max_results]:
        doi = item.get("doi")
        pdf_url = f"https://www.{server}.org/content/{doi}v{item.get('version', '1')}.full.pdf" if doi else None
        papers.append(Paper(
            stable_id=f"{server}:" + (doi.lower() if doi else title_hash(item.get("title", ""))),
            title=clean_text(item.get("title") or ""),
            authors=split_authors(item.get("authors") or ""),
            abstract=clean_text(item.get("abstract") or ""),
            published_at=item.get("date") or "",
            source=server,
            url=f"https://www.{server}.org/content/{doi}" if doi else "",
            pdf_url=pdf_url,
            doi=doi,
        ))
    return filter_lookback(papers, config)


def europe_pmc_pdf_url(item):
    for entry in item.get("fullTextUrlList", {}).get("fullTextUrl", []):
        url = entry.get("url")
        document_style = (entry.get("documentStyle") or "").casefold()
        availability = (entry.get("availability") or "").casefold()
        if url and ("pdf" in document_style or url.casefold().endswith(".pdf")) and "free" in availability:
            return url
    return None


def parse_europe_pmc_date(value):
    value = str(value or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value
    if re.match(r"^\d{4}$", value):
        return value
    return value


def split_authors(value):
    if not value:
        return []
    return [part.strip() for part in re.split(r",|;|\band\b", value) if part.strip()]


def datacite_pdf_url(attributes):
    for content_url in attributes.get("contentUrl") or []:
        if str(content_url).casefold().endswith(".pdf"):
            return content_url
    for related in attributes.get("relatedIdentifiers") or []:
        value = related.get("relatedIdentifier")
        if value and str(value).casefold().endswith(".pdf"):
            return value
    return None


def doaj_identifier(bibjson, kind):
    for identifier in bibjson.get("identifier", []):
        if (identifier.get("type") or "").casefold() == kind:
            return identifier.get("id")
    return None


def doaj_article_url(bibjson):
    links = bibjson.get("link", [])
    for link in links:
        if (link.get("type") or "").casefold() == "fulltext":
            return link.get("url")
    return links[0].get("url") if links else ""


def doaj_pdf_url(bibjson):
    for link in bibjson.get("link", []):
        url = link.get("url")
        content_type = (link.get("content_type") or link.get("type") or "").casefold()
        if url and ("pdf" in content_type or url.casefold().endswith(".pdf")):
            return url
    return None


def first_value(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def crossref_date(item):
    for key in ["published-print", "published-online", "published", "created", "issued"]:
        parts = item.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            year = parts[0][0]
            month = parts[0][1] if len(parts[0]) > 1 else 1
            day = parts[0][2] if len(parts[0]) > 2 else 1
            return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def crossref_pdf_url(item):
    for link in item.get("link", []):
        content_type = (link.get("content-type") or "").casefold()
        url = link.get("URL")
        if url and ("pdf" in content_type or url.casefold().endswith(".pdf")):
            return url
    return None


def pubmed_doi(item):
    for article_id in item.get("articleids", []):
        if article_id.get("idtype") == "doi":
            return article_id.get("value")
    return None


def parse_pubmed_date(value):
    match = re.search(r"(\d{4})(?:\s+([A-Za-z]{3}))?(?:\s+(\d{1,2}))?", value or "")
    if not match:
        return ""
    year = int(match.group(1))
    month = month_number(match.group(2)) if match.group(2) else 1
    day = int(match.group(3)) if match.group(3) else 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def month_number(value):
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    return months.get(str(value).casefold()[:3], 1)


def strip_markup(value):
    return re.sub(r"<[^>]+>", " ", value or "")


def extract_email(value):
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value or "")
    return match.group(0) if match else ""


def filter_lookback(papers, config):
    days = int(config.get("search", {}).get("lookback_days", 14))
    cutoff = now_utc() - timedelta(days=days)
    kept = []
    for paper in papers:
        published = parse_published(paper.published_at)
        if not published or published >= cutoff:
            kept.append(paper)
    return kept


def parse_published(value):
    if not value:
        return None
    value = str(value)
    try:
        if re.match(r"^\d{4}$", value):
            return datetime(int(value), 1, 1, tzinfo=timezone.utc)
        if re.match(r"^\d{4}-\d{2}$", value):
            return datetime.fromisoformat(value + "-01").replace(tzinfo=timezone.utc)
        if len(value) == 10:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def openalex_abstract(index):
    if not index:
        return ""
    words = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    return " ".join(word for _, word in sorted(words))


def text_of(parent, path, ns):
    node = parent.find(path, ns)
    return node.text if node is not None and node.text else ""


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_bytes(url, timeout=30, headers=None):
    request_headers = {"User-Agent": "PaperRadar/1.0 (mailto:paper-radar@example.com)"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def sort_papers(papers, sort_by):
    if sort_by == "title":
        return sorted(papers, key=lambda p: p.title.casefold())
    return sorted(papers, key=lambda p: parse_published(p.published_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def download_pdf(paper, tmp_dir):
    if not paper.pdf_url:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", paper.title)[:90].strip("_") or "paper"
    path = Path(tmp_dir) / f"{safe}.pdf"
    data = fetch_bytes(paper.pdf_url, timeout=60)
    if not data.startswith(b"%PDF"):
        return None
    path.write_bytes(data)
    return path


def enrich_pdf_url(paper, config):
    if paper.pdf_url or not paper.doi:
        return paper
    email_value = extract_email(os.getenv("MAIL_FROM") or config.get("email", {}).get("from", ""))
    if not email_value:
        return paper
    try:
        doi = urllib.parse.quote(paper.doi)
        url = f"https://api.unpaywall.org/v2/{doi}?email={urllib.parse.quote(email_value)}"
        data = json.loads(fetch_bytes(url, timeout=20).decode("utf-8"))
        best = data.get("best_oa_location") or {}
        paper.pdf_url = best.get("url_for_pdf") or best.get("url")
    except Exception as exc:
        print(f"Unpaywall lookup failed for {paper.doi}: {exc}")
    return paper


def run(config, history, tmp_dir):
    keys = known_keys(history)
    report = {
        "started_at": now_utc().isoformat(),
        "topics": [],
        "sent_papers": [],
    }
    for topic in config.get("topics", []):
        if not topic.get("enabled", True):
            continue
        candidates = search_topic(topic, config)
        unique = []
        seen_this_topic = set()
        for paper in candidates:
            keys_for_paper = paper_keys(paper)
            if keys_for_paper & keys or keys_for_paper & seen_this_topic:
                continue
            seen_this_topic.update(keys_for_paper)
            paper.topics.append(topic.get("name", "Untitled"))
            unique.append(paper)
        selected = sort_papers(unique, topic.get("sort_by", "newest"))[: int(topic.get("max_downloads_per_run", 10))]
        downloads = []
        for paper in selected:
            paper = enrich_pdf_url(paper, config)
            try:
                pdf_path = download_pdf(paper, tmp_dir) if paper.pdf_url else None
            except Exception as exc:
                pdf_path = None
                paper.abstract = f"{paper.abstract}\n\nPDF download failed: {exc}".strip()
            downloads.append((paper, pdf_path))
        report["topics"].append({
            "name": topic.get("name", "Untitled"),
            "searched": len(candidates),
            "new": len(unique),
            "selected": len(selected),
            "limit": int(topic.get("max_downloads_per_run", 10)),
            "papers": [paper_to_record(paper) for paper, _ in downloads],
        })
        report["sent_papers"].extend((topic, paper, pdf_path) for paper, pdf_path in downloads)
    return report


def paper_to_record(paper):
    return {
        "id": paper.stable_id,
        "title": paper.title,
        "authors": paper.authors,
        "doi": paper.doi,
        "source": paper.source,
        "published_at": paper.published_at,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "abstract": paper.abstract,
        "topics": paper.topics,
        "dedupe_keys": sorted(paper_keys(paper)),
    }


def build_markdown(report):
    lines = [
        f"# Paper Radar Report - {datetime.now(timezone.utc).date().isoformat()}",
        "",
        f"Run started: {report['started_at']}",
        "",
    ]
    for topic in report["topics"]:
        lines += [
            f"## {topic['name']}",
            "",
            f"- Searched: {topic['searched']}",
            f"- New after dedupe: {topic['new']}",
            f"- Sent/downloaded this run: {topic['selected']}",
            f"- Topic limit: {topic['limit']}",
            "",
        ]
        if not topic["papers"]:
            lines += ["No new papers selected.", ""]
            continue
        for idx, paper in enumerate(topic["papers"], 1):
            authors = ", ".join(paper["authors"][:8])
            if len(paper["authors"]) > 8:
                authors += " et al."
            lines += [
                f"### {idx}. {paper['title']}",
                "",
                f"- Authors: {authors or 'Unknown'}",
                f"- Published: {paper['published_at'] or 'Unknown'}",
                f"- Source: {paper['source']}",
                f"- DOI: {paper['doi'] or 'None'}",
                f"- Link: {paper['url']}",
                f"- PDF: {paper['pdf_url'] or 'None'}",
                "",
                paper["abstract"] or "No abstract available.",
                "",
            ]
    return "\n".join(lines).strip() + "\n"


def html_email(markdown):
    escaped = html.escape(markdown)
    linked = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', escaped)
    return "<html><body><pre style=\"white-space:pre-wrap;font-family:Arial,sans-serif\">" + linked + "</pre></body></html>"


def send_email(config, markdown, sent_items):
    if not sent_items:
        print("No new papers selected; skipping email.")
        return False
    recipients = set(config.get("email", {}).get("default_recipients", []))
    for topic, _, _ in sent_items:
        recipients.update(topic.get("recipients", []))
    recipients = sorted(r for r in recipients if r)
    if not recipients:
        print("No recipients configured; skipping email.")
        return False

    email_config = config.get("email", {})
    sender = os.getenv("MAIL_FROM") or email_config.get("from")
    if not sender:
        print("No MAIL_FROM configured; skipping email.")
        return False

    subject = f"{email_config.get('subject_prefix', '[Paper Radar]')} {datetime.now(timezone.utc).date().isoformat()}"
    attachments = collect_attachments(config, sent_items)
    provider = email_config.get("provider", "resend")
    if provider == "smtp":
        send_smtp(sender, recipients, subject, markdown, attachments)
    else:
        send_resend(sender, recipients, subject, markdown, attachments)
    return True


def collect_attachments(config, sent_items):
    email_config = config.get("email", {})
    if not email_config.get("attach_pdfs", True):
        return []
    limit = int(email_config.get("max_total_attachment_mb", 20)) * 1024 * 1024
    total = 0
    attachments = []
    for topic, paper, path in sent_items:
        if not topic.get("attach_pdfs", True) or not path or not Path(path).exists():
            continue
        size = Path(path).stat().st_size
        if total + size > limit:
            continue
        total += size
        attachments.append((paper.title + ".pdf", Path(path).read_bytes()))
    return attachments


def send_resend(sender, recipients, subject, markdown, attachments):
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    payload = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "html": html_email(markdown),
        "text": markdown,
    }
    if attachments:
        payload["attachments"] = [
            {
                "filename": filename,
                "content": base64.b64encode(content).decode("ascii"),
            }
            for filename, content in attachments
        ]
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        response.read()


def send_smtp(sender, recipients, subject, markdown, attachments):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    if not host or not username or not password:
        raise RuntimeError("SMTP settings are incomplete")
    message = email.message.EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(markdown)
    message.add_alternative(html_email(markdown), subtype="html")
    for filename, content in attachments:
        message.add_attachment(content, maintype="application", subtype="pdf", filename=filename)
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)


def append_history(history, report, emailed):
    sent_at = now_utc().isoformat()
    records = []
    for topic in report["topics"]:
        for paper in topic["papers"]:
            record = dict(paper)
            record["first_seen_at"] = sent_at
            record["sent_at"] = sent_at if emailed else None
            records.append(record)
    return history + records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run regardless of schedule")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {})
    history = load_json(PAPERS_PATH, [])
    state = load_json(STATE_PATH, {"last_successful_run_at": None})
    due, reason = is_due(config, state, force=args.force)
    if not due:
        print(f"Not due: {reason}")
        return 0

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="paper-radar-")
    try:
        report = run(config, history, tmp_dir)
        markdown = build_markdown(report)
        report_path = REPORTS_DIR / f"{datetime.now(timezone.utc).date().isoformat()}.md"
        report_path.write_text(markdown, encoding="utf-8")
        emailed = send_email(config, markdown, report["sent_papers"])
        if report["sent_papers"] and emailed:
            history = append_history(history, report, emailed)
            save_json(PAPERS_PATH, history)
            state["last_successful_run_at"] = now_utc().isoformat()
            save_json(STATE_PATH, state)
        elif report["sent_papers"]:
            print("Email was not sent, so sent-paper history and last-run state were not updated.")
        else:
            print("No sent-paper history was added.")
            state["last_successful_run_at"] = now_utc().isoformat()
            save_json(STATE_PATH, state)
        print(f"Report written to {report_path}")
        print(f"Temporary PDFs cleaned from {tmp_dir}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
