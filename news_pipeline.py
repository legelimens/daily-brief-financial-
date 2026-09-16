"""Source collection and durable delivery history for the AI and Data daily."""
from __future__ import annotations

import hashlib
import json
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dates
from requests.adapters import HTTPAdapter


class SystemTrustAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        # Retain verification, combining OS roots with requests' CA bundle.
        kwargs['ssl_context'] = ssl.create_default_context()
        return super().init_poolmanager(*args, **kwargs)


def session():
    client = requests.Session()
    client.mount('https://', SystemTrustAdapter())
    client.headers['User-Agent'] = 'Mozilla/5.0 (compatible; AIDataBrief/2.0)'
    return client


def parse_date(value, tz_hours=0):
    if not value:
        return None
    try:
        normalized = re.sub(r'^(\d{4})\.(\d{1,2})\.(\d{1,2})', r'\1-\2-\3', str(value))
        dt = dates.parse(normalized)
        return dt.replace(tzinfo=timezone(timedelta(hours=tz_hours))) if dt.tzinfo is None else dt
    except (ValueError, OverflowError, TypeError):
        return None


def article(client, url):
    response = client.get(url, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    published = None
    for selector in ['meta[property="article:published_time"]', 'meta[name="publishdate"]',
                     'meta[name="pubdate"]', 'meta[name="date"]', 'meta[name="PubDate"]']:
        node = soup.select_one(selector)
        if node:
            published = node.get('content')
            break
    if not published:
        for node in soup.select('script[type="application/ld+json"]'):
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', node.get_text())
            if match:
                published = match[1]
                break
    for node in soup.select('script,style,nav,header,footer'):
        node.decompose()
    body = soup.select_one('article') or soup.select_one('main') or soup
    return published, body.get_text(' ', strip=True)[:10000]


def collect_one(source, track, now, hours):
    report = {'name': source['name'], 'track': track, 'source_region': source.get('region', 'global'),
              'status': 'ok', 'entries': 0, 'recent': 0, 'undated': 0, 'future': 0}
    rows = []
    try:
        with session() as client:
            response = client.get(source['url'], timeout=20)
            response.raise_for_status()
            if source.get('type', 'rss') == 'rss':
                feed = feedparser.parse(response.content)
                entries = []
                for entry in feed.entries:
                    # updated is deliberately never substituted for publication.
                    dt = None
                    if entry.get('published_parsed'):
                        dt = datetime(*entry['published_parsed'][:6], tzinfo=timezone.utc)
                    else:
                        dt = parse_date(entry.get('published'), source.get('timezone_hours', 0))
                    entries.append((entry.get('title', ''), entry.get('link', ''), dt,
                                    BeautifulSoup(entry.get('summary', ''), 'html.parser').get_text(' ', strip=True),
                                    entry.get('author', ''), False))
            else:
                soup = BeautifulSoup(response.content, 'html.parser')
                entries, seen = [], set()
                for node in soup.select(source['selector']):
                    anchor = node if node.name == 'a' else node.select_one('a[href]')
                    if not anchor:
                        continue
                    url = urljoin(source['url'], anchor.get('href', ''))
                    if url in seen or not re.search(source.get('link_pattern', '.'), url):
                        continue
                    seen.add(url)
                    text = node.get_text(' ', strip=True)
                    title = anchor.get('title') or anchor.get_text(' ', strip=True)
                    match = re.search(r'20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}', text)
                    date_only = bool(match)
                    dt = parse_date(match[0], source.get('timezone_hours', 0)) if match else None
                    body = ''
                    if not dt:
                        try:
                            pub, body = article(client, url)
                            dt = parse_date(pub, source.get('timezone_hours', 0))
                        except requests.RequestException:
                            report['body_failures'] = report.get('body_failures', 0) + 1
                    entries.append((title, url, dt, body, '', date_only))
                    if len(entries) >= source.get('max_articles', 12):
                        break
            report['entries'] = len(entries)
            if not entries:
                report['status'] = 'empty'
            for title, url, dt, body, author, date_only in entries:
                if not title or urlparse(url).scheme not in ('http', 'https'):
                    continue
                if dt is None:
                    report['undated'] += 1
                    continue
                if dt > now:
                    report['future'] += 1
                    continue
                if dt < now - timedelta(hours=hours):
                    continue
                rows.append(dict(title=title, url=url, published=dt, summary_raw=body,
                                 source_name=source['name'], region=source.get('region', 'global'),
                                 track_hint=track, author=author, date_only=date_only,
                                 source_kind=source.get('kind', 'media')))
            rows.sort(key=lambda row: row['published'], reverse=True)
            report['recent'] = len(rows)
            if entries and report['undated'] == len(entries):
                report['status'] = 'undated'
            if source.get('priority_keywords'):
                pattern = re.compile(source['priority_keywords'], re.I)
                rows.sort(key=lambda row: (not bool(pattern.search(row['title'])), -row['published'].timestamp()))
            rows = rows[:source.get('max_items', 15)]
            report['selected_candidates'] = len(rows)
            # Enrich the short official list entries; article failures remain visible.
            for row in rows:
                if len(row['summary_raw']) < 180:
                    try:
                        _, body = article(client, row['url'])
                        row['summary_raw'] = body
                    except requests.RequestException:
                        row['summary_raw'] = row['summary_raw'] or row['title']
                        report['body_failures'] = report.get('body_failures', 0) + 1
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = type(exc).__name__
    return rows, report


def collect(sources, hours=24):
    now = datetime.now(timezone.utc)
    jobs = [(src, track) for track, group in sources.items() for src in group]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: collect_one(*job, now, hours), jobs))
    return [row for rows, _ in results for row in rows], [report for _, report in results], now


def event_key(item):
    return item.event_key or hashlib.sha256(item.url.encode()).hexdigest()


def read_history(path, days=14):
    p = Path(path)
    if not p.exists():
        return []
    # Do not silently reset corrupted delivery history and resend everything.
    records = json.loads(p.read_text(encoding='utf-8'))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [r for r in records if datetime.fromisoformat(r['sent_at']) >= cutoff]


def select_new(items, history):
    urls = {r['url'] for r in history}
    keys = {r['event_key'] for r in history}
    result = []
    for item in sorted(items, key=lambda x: (getattr(x, 'supplemental', False), -x.score)):
        key = event_key(item)
        if item.url in urls or key in keys:
            continue
        result.append(item)
        urls.add(item.url)
        keys.add(key)
    return result


def read_test_deliveries(output_dir, recipients):
    """Recognize explicitly accepted test emails for these recipients as already sent."""
    root = Path(output_dir).resolve()
    records = []
    for receipt in root.glob('test_delivery_*.json'):
        data = json.loads(receipt.read_text(encoding='utf-8'))
        if data.get('status') != 'accepted_by_smtp' or data.get('recipient') not in recipients:
            continue
        sent_at = data.get('completed_at')
        if not sent_at or datetime.fromisoformat(sent_at) < datetime.now(timezone.utc) - timedelta(days=14):
            continue
        page = Path(data['html']).resolve()
        if root not in page.parents or not page.is_file():
            continue
        markup = page.read_text(encoding='utf-8')
        if hashlib.sha256(markup.encode()).hexdigest() != data.get('sha256'):
            continue
        for anchor in BeautifulSoup(markup, 'html.parser').select('a.news-title[href]'):
            url = anchor['href']
            records.append(dict(url=url, event_key=hashlib.sha256(url.encode()).hexdigest(), sent_at=sent_at))
    return records


def save_history(path, history, items):
    now = datetime.now(timezone.utc).isoformat()
    history = history + [dict(url=i.url, event_key=event_key(i), sent_at=now) for i in items]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix('.tmp')
    temp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(p)
