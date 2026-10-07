from __future__ import annotations

from urllib.parse import quote, urljoin, urlparse

DEFAULT_SCHEME = "https"
DEFAULT_PORTS = {"http": "80", "https": "443"}


def _idna_host(host: str) -> str:
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return host


def _normalize_netloc(netloc: str, scheme: str = "") -> str:
    if not netloc:
        return ""
    userinfo, at, hostport = netloc.rpartition("@")
    prefix = f"{userinfo}@" if at else ""

    if ":" in hostport:
        host, _, port = hostport.rpartition(":")
        if not port.isdigit():
            host, port = hostport, ""
    else:
        host, port = hostport, ""

    host = _idna_host(host).lower()

    if port and DEFAULT_PORTS.get(scheme.lower()) == port:
        port = ""

    return f"{prefix}{host}:{port}" if port else f"{prefix}{host}"


def normalize_domain(domain: str) -> str:
    d = domain.strip().rstrip("/")
    if not d:
        return ""
    parsed = urlparse(d)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        d = f"{DEFAULT_SCHEME}://{d}"
        parsed = urlparse(d)
    scheme = parsed.scheme.lower()
    netloc = _normalize_netloc(parsed.netloc, scheme)
    if not netloc:
        return ""
    return f"{scheme}://{netloc}"


def _looks_like_absolute(line: str) -> bool:
    parsed = urlparse(line)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def clean_url_line(line: str) -> str:
    line = line.lstrip("\ufeff")
    line = line.rstrip("\r\n")
    line = line.strip()
    if len(line) >= 2 and line[0] == line[-1] and line[0] in ("'", '"'):
        line = line[1:-1].strip()
    return line


def _normalize_spaces(url: str) -> str:
    """
    Одна операция: пробелы превращаются в %20, уже закодированные
    последовательности (%20, %D0%B0) остаются без изменений,
    потому что % входит в safe-список quote().
    """
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")


def _has_whitespace(s: str) -> bool:
    return any(ch.isspace() for ch in s)


def _is_fragment_only(line: str) -> bool:
    return line.lstrip().startswith("#")


def build_full_urls(domain: str, lines):
    """
    Принимает любой iterable строк (list или generator).
    Возвращает (urls, invalid_lines).
    """
    urls, invalid = [], []
    d = urlparse(domain)
    origin = f"{d.scheme}://{d.netloc}" if d.netloc else ""
    base = origin + "/" if origin else ""

    for raw in lines:
        line = clean_url_line(raw)
        if not line:
            continue
        if _is_fragment_only(line):
            continue

        if _looks_like_absolute(line):
            if _has_whitespace(line):
                line = _normalize_spaces(line)
                if _has_whitespace(line):
                    invalid.append(raw.strip())
                    continue
            urls.append(line)
            continue

        if not base:
            invalid.append(line)
            continue

        combined = urljoin(base, line)
        if _has_whitespace(combined):
            combined = _normalize_spaces(combined)
            if _has_whitespace(combined):
                invalid.append(line)
                continue

        parsed = urlparse(combined)
        if (parsed.scheme not in ("http", "https")
                or not parsed.netloc
                or _normalize_netloc(parsed.netloc, parsed.scheme)
                    != _normalize_netloc(d.netloc, d.scheme)):
            invalid.append(line)
            continue

        urls.append(combined)
    return urls, invalid


def is_same_site(url: str, domain: str) -> bool:
    d = urlparse(domain)
    u = urlparse(url)
    return (
        u.scheme.lower() == d.scheme.lower()
        and _normalize_netloc(u.netloc, u.scheme)
            == _normalize_netloc(d.netloc, d.scheme)
    )


def split_by_domain(urls, domain: str):
    own, foreign = [], []
    for u in urls:
        (own if is_same_site(u, domain) else foreign).append(u)
    return own, foreign
