"""Opt-in public HTTPS intake: exact host allowlist, pinned DNS, bounded I/O, robots.

This retrieves documents; it does not certify a publisher or establish factual truth.
No proxy environment, browser cookies, model-selected URLs or arbitrary headers are used.
"""
import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import threading
import time
from urllib import robotparser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import trafilatura

from .. import db
from ..config import settings
from . import store

_LIMIT = 2 * 1024 * 1024
_gate = threading.BoundedSemaphore(2)


def enabled():
    return os.environ.get('RASAD_EXTERNAL_FETCH', '0') == '1'


def _target(url):
    if not enabled():
        raise ValueError('ورود URL بیرونی غیرفعال است')
    if len(url) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError('URL نامعتبر است')
    try:
        p = urlsplit(url)
        host = (p.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        port = p.port
    except (ValueError, UnicodeError) as e:
        raise ValueError('URL نامعتبر است') from e
    allow = {h.strip().lower().rstrip('.') for h in os.environ.get('RASAD_EXTERNAL_ALLOWED_HOSTS', '').split(',') if h.strip()}
    if p.scheme != 'https' or p.username is not None or p.password is not None or port not in (None, 443) or not host or host not in allow:
        raise ValueError('فقط HTTPS روی پورت ۴۴۳ و دامنه صریحاً مجاز، بدون اطلاعات ورود، پذیرفته می‌شود')
    try:
        addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError as e:
        raise ValueError('حل نام دامنه ناموفق بود') from e
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast for a in addresses):
        raise ValueError('نشانی شبکه خصوصی، محلی یا رزروشده ممنوع است')
    path = quote(p.path or '/', safe="/%:@!$&'()*+,;=-._~")
    query = quote(p.query, safe="/%?:@!$&'()*+,;=-._~")
    target = path + ('?' + query if query else '')
    normalized = urlunsplit(('https', host, path, query, ''))
    return host, addresses[0], target, normalized


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, port=443, timeout=10, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        raw = socket.create_connection((self._address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request(host, address, target, limit=_LIMIT):
    connection = _PinnedHTTPS(host, address)
    try:
        connection.request('GET', target, headers={'User-Agent': settings.user_agent, 'Accept-Encoding': 'identity',
                                                  'Accept': 'text/html,text/plain,application/xhtml+xml'})
        response = connection.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('پاسخ فشرده پذیرفته نمی‌شود')
        length = headers.get('content-length')
        if length and (not length.isdigit() or int(length) > limit):
            raise ValueError('اندازه پاسخ بیش از سقف مجاز است')
        data = bytearray()
        deadline = time.monotonic() + 30
        while True:
            if time.monotonic() > deadline:
                raise ValueError('مهلت دریافت سند تمام شد')
            part = response.read1(min(8192, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
            if len(data) > limit:
                raise ValueError('اندازه پاسخ بیش از سقف مجاز است')
        return response.status, headers, bytes(data)
    finally:
        connection.close()


def _robots(host, address, url):
    status, _, data = _request(host, address, '/robots.txt', 256 * 1024)
    if status == 404:
        return
    if status != 200:
        raise ValueError('دسترسی robots.txt روشن نیست؛ دریافت متوقف شد')
    parser = robotparser.RobotFileParser()
    parser.parse(data.decode('utf-8', 'replace').splitlines())
    if not parser.can_fetch(settings.user_agent, url):
        raise ValueError('دریافت این مسیر در robots.txt مجاز نیست')


def _reserve(user):
    key = 'external_intake:' + str(user['id']) + ':' + time.strftime('%Y-%m-%d')
    cap = max(0, int(os.environ.get('RASAD_EXTERNAL_DAILY_IMPORTS', '10')))
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO kv(k,v) VALUES(?, '0')", (key,))
        n = int(c.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()[0])
        if n >= cap:
            raise ValueError('سقف روزانه ورود منابع بیرونی تمام شده است')
        c.execute('UPDATE kv SET v=? WHERE k=?', (str(n+1), key))


def ingest(body, user):
    _target(body.url)  # Reject before consuming quota; each actual hop resolves and pins anew.
    _reserve(user)
    current = body.url
    with _gate:
        for hop in range(4):
            host, address, target, normalized = _target(current)
            _robots(host, address, normalized)
            status, headers, raw = _request(host, address, target)
            if status in (301, 302, 303, 307, 308):
                if hop == 3 or not headers.get('location'):
                    raise ValueError('تغییرمسیر بیش از حد یا نامعتبر است')
                current = urljoin(normalized, headers['location'])
                continue
            if status != 200:
                raise ValueError('دریافت سند موفق نبود؛ HTTP ' + str(status))
            mime = headers.get('content-type', '').split(';')[0].strip().lower()
            if mime == 'text/plain':
                text = raw.decode('utf-8', 'replace')
            elif mime in ('text/html', 'application/xhtml+xml'):
                text = trafilatura.extract(raw, include_comments=False, include_tables=True, favor_precision=True) or ''
            else:
                raise ValueError('URL فقط برای HTML/TXT است؛ فایل را از مسیر بارگذاری وارد کنید')
            if not text.strip():
                raise ValueError('متن قابل تحلیل استخراج نشد')
            metadata = {'url': normalized, 'requested_url': body.url, 'fetched_at': time.time(),
                        'source_type': body.source_type, 'source_type_is_user_assertion': True,
                        'method': 'public_https_intake', 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                        'mime': mime, 'coverage': 'extracted_text_not_full_page', 'origin_verified': False,
                        'robots_checked': True}
            did = store.snapshot(body.title, text, metadata, user['id'])
            root = settings.db_path.parent / 'analysis-media'
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / did
            if not path.exists():
                path.write_bytes(raw)
                path.chmod(0o600)
            db.audit(str(user['id']), 'analysis:external_import', did, {'host': host})
            return did
    raise ValueError('دریافت ناموفق بود')
