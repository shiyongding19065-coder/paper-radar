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
        if source == "arxiv":
            found.extend(search_arxiv(topic, config))
        elif source == "openalex":
            found.extend(search_openalex(topic, config))
    return [paper for paper in found if relevant(paper, topic)]


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
    try:
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


def fetch_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "PaperRadar/1.0 (mailto:paper-radar@example.com)"})
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
        if emailed:
            history = append_history(history, report, emailed)
            save_json(PAPERS_PATH, history)
            state["last_successful_run_at"] = now_utc().isoformat()
            save_json(STATE_PATH, state)
        else:
            print("Email was not sent, so sent-paper history and last-run state were not updated.")
        print(f"Report written to {report_path}")
        print(f"Temporary PDFs cleaned from {tmp_dir}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
