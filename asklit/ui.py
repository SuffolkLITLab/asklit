import html
from urllib.parse import urlparse


def escape_html(value):
    return html.escape(str(value or ""), quote=True)


def is_safe_url(url):
    if not url:
        return False

    parsed = urlparse(str(url))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def safe_url(url):
    return str(url) if is_safe_url(url) else ""
