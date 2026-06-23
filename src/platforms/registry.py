"""Fast, host-based platform resolution for the main runtime loop."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse


PLATFORM_MODULES = {
    "cityline": "platforms.cityline",
    "facebook": "platforms.facebook",
    "famiticket": "platforms.famiticket",
    "fansigo": "platforms.fansigo",
    "funone": "platforms.funone",
    "hkticketing": "platforms.hkticketing",
    "ibon": "platforms.ibon",
    "kham": "platforms.kham",
    "kktix": "platforms.kktix",
    "nolworld": "platforms.nolworld",
    "ticketplus": "platforms.ticketplus",
    "tixcraft": "platforms.tixcraft",
}


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


@lru_cache(maxsize=512)
def resolve_platform(url: str) -> str | None:
    """Resolve only from the hostname, avoiding query-string misrouting."""

    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.lower()
    if not host:
        return None
    if _matches(host, "facebook.com") and path.startswith("/login.php"):
        return "facebook"
    if _matches(host, "kktix.com") or _matches(host, "kktix.cc"):
        return "kktix"
    if (
        _matches(host, "tixcraft.com")
        or _matches(host, "indievox.com")
        or _matches(host, "ticketmaster.com")
        or host.startswith("ticketmaster.")
        or host.startswith("www.ticketmaster.")
        or ".ticketmaster." in host
    ):
        return "tixcraft"
    if _matches(host, "famiticket.com.tw") or _matches(host, "famiticket.com"):
        return "famiticket"
    if _matches(host, "ibon.com.tw") or _matches(host, "ibon.com"):
        return "ibon"
    if (
        _matches(host, "kham.com.tw")
        or _matches(host, "ticket.com.tw")
        or _matches(host, "tickets.udnfunlife.com")
    ):
        return "kham"
    if _matches(host, "ticketplus.com.tw") or _matches(host, "ticketplus.com"):
        return "ticketplus"
    if _matches(host, "cityline.com"):
        return "cityline"
    if (
        _matches(host, "hkticketing.com")
        or _matches(host, "galaxymacau.com")
        or _matches(host, "ticketek.com")
    ):
        return "hkticketing"
    if (
        _matches(host, "nol.com")
        or _matches(host, "interpark.com")
        or _matches(host, "globalinterpark.com")
    ):
        return "nolworld"
    if _matches(host, "tickets.funone.io"):
        return "funone"
    if _matches(host, "fansi.me") or "cognito" in host:
        return "fansigo"
    return None
