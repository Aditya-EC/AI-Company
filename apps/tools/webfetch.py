"""Fetch public web pages for research agents, with SSRF protection.

Only http(s); every hop (including redirects) must resolve exclusively to public IP addresses;
size, time and redirect limits apply; HTML is reduced to text. Content is untrusted data.
"""
import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings

from apps.core.models import CompanySettings

MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 15


class FetchError(Exception):
    pass


def _is_public(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise FetchError("host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def check_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("only http(s) URLs are allowed")
    if parsed.username or parsed.password:
        raise FetchError("credentials in URLs are not allowed")
    host = parsed.hostname.lower()
    allowed = [d.lower().lstrip(".") for d in CompanySettings.get().web_allowed_domains]
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        raise FetchError("domain not in the company's allowed list")
    if not getattr(settings, "VCOOS_WEB_FETCH_ALLOW_PRIVATE", False) and not _is_public(host):
        raise FetchError("destination is not a public address")
    return url


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def fetch(url):
    session = requests.Session()
    session.trust_env = True  # honor the deployment's outbound proxy settings
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True,
                           headers={"User-Agent": "VCoOS-research/1.0", "Accept": "text/html,text/plain,application/json"})
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, resp.headers.get("Location", ""))
            resp.close()
            continue
        body = b""
        for chunk in resp.iter_content(16384):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        resp.close()
        ctype = resp.headers.get("Content-Type", "")
        text = body[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")
        if "html" in ctype:
            text = html_to_text(text)
        return {"url": url, "status": resp.status_code, "content_type": ctype[:100], "text": text[:20000]}
    raise FetchError("too many redirects")
