"""Navigation request policy; this does not claim to isolate renderer network traffic."""
from urllib.parse import urlsplit


def origin(url):
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def navigation_refusal(url, allowed_origins=()):
    from .cdp import _host_resolves, _is_blocked_host
    try:
        parsed = urlsplit(url)
        requested = origin(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return "REFUSED: navigation requires an absolute http(s) URL without embedded credentials"
        if requested in {origin(value) for value in allowed_origins}:
            return None
        if _is_blocked_host(parsed.hostname):
            return "REFUSED: private destination requires an explicit host allowed_web_origins entry"
        if not _host_resolves(parsed.hostname):
            return "REFUSED: destination host does not resolve"
    except ValueError:
        return "REFUSED: invalid destination URL"
    return None
