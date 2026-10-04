from urllib.parse import urljoin, urlparse

DEFAULT_SCHEME = "https"


def normalize_domain(domain: str) -> str:
    """
    Приводит домен к виду scheme://host[:port] без trailing slash.
    Если схема не указана — подставляется https://.
    """
    d = domain.strip().rstrip("/")
    if not d:
        return ""
    parsed = urlparse(d)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        d = f"{DEFAULT_SCHEME}://{d}"
    return d


def build_full_urls(domain: str, lines: list[str]):
    """
    Возвращает (urls, invalid_lines).
    domain должен быть уже нормализован вызывающим кодом.
    """
    urls, invalid = [], []
    base = domain.rstrip("/") + "/"

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parsed = urlparse(line)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            urls.append(line)
        elif domain:
            urls.append(urljoin(base, line))
        else:
            invalid.append(line)
    return urls, invalid


def is_same_site(url: str, domain: str) -> bool:
    """Строгое сравнение: scheme + netloc. domain должен быть нормализован."""
    d = urlparse(domain)
    u = urlparse(url)
    return (
        u.scheme.lower() == d.scheme.lower()
        and u.netloc.lower() == d.netloc.lower()
    )


def split_by_domain(urls: list[str], domain: str):
    own, foreign = [], []
    for u in urls:
        (own if is_same_site(u, domain) else foreign).append(u)
    return own, foreign