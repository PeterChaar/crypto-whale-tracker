#!/usr/bin/env python3
"""
Keep the bot alive when the local resolver refuses to answer.

On 2026-08-02 this machine stopped resolving api.telegram.org while Telegram
itself was perfectly reachable: pinning the IP returned 200 in 0.3s, the
system resolver returned "nodename nor servname provided". The bot process
died and every Pro alert failed to send. Nothing in the code was wrong, and
nothing in the code could recover, because the failure was one layer below it.

Importing this module installs a fallback under socket.getaddrinfo:

  1. Ask the system resolver, but only give it FAST_TIMEOUT seconds. A broken
     resolver does not always fail fast, this one hung for 30s per lookup,
     which is longer than any HTTP client's connect timeout, so the hang alone
     took the bot down.
  2. Otherwise ask DNS-over-HTTPS, addressed by IP so it works even when DNS
     is the thing that is broken.
  3. Cache the answer, and check that cache first, so one slow failure does
     not become a slow failure on every single request.

Requests keep using the real hostname, so TLS verification is unaffected.
"""

import json
import time
import socket
import logging
import urllib.request
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

log = logging.getLogger(__name__)

FAST_TIMEOUT = 3.0     # how long the system resolver gets before we route around it
CACHE_TTL = 300.0      # seconds to trust a DoH answer

# Addressed by IP on purpose: these certificates carry the IP in their SANs,
# so TLS still verifies without a working resolver.
DOH_ENDPOINTS = [
    ("https://1.1.1.1/dns-query?name={name}&type=A", {"accept": "application/dns-json"}),
    ("https://8.8.8.8/resolve?name={name}&type=A", {}),
]

# Last resort if even DoH is unreachable. DoH is tried first precisely so a
# stale entry here is never fatal.
STATIC_FALLBACK = {
    "api.telegram.org": ["149.154.167.220", "149.154.166.110", "149.154.175.50"],
}

# Hosts this process depends on, warmed at import so the first alert is not
# the request that pays for the lookup.
WARM_HOSTS = ["api.telegram.org"]

_cache: dict[str, tuple[list[str], float]] = {}
_lock = Lock()
_original_getaddrinfo = socket.getaddrinfo
_resolver_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")


def _cached(hostname: str) -> list[str] | None:
    with _lock:
        entry = _cache.get(hostname)
    if not entry:
        return None
    ips, expires = entry
    if time.time() > expires:
        with _lock:
            _cache.pop(hostname, None)
        return None
    return ips


def _remember(hostname: str, ips: list[str]):
    with _lock:
        _cache[hostname] = (ips, time.time() + CACHE_TTL)


def _doh_lookup(hostname: str) -> list[str]:
    for template, headers in DOH_ENDPOINTS:
        try:
            req = urllib.request.Request(template.format(name=hostname), headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                answers = json.load(r).get("Answer", [])
            ips = [a["data"] for a in answers if a.get("type") == 1]
            if ips:
                log.warning(f"system DNS failed for {hostname}, using DoH: {', '.join(ips)}")
                return ips
        except Exception:
            continue
    return STATIC_FALLBACK.get(hostname, [])


def _build(ips: list[str], port, family, type_, proto, flags) -> list:
    results = []
    for ip in ips:
        try:
            results.extend(_original_getaddrinfo(ip, port, family, type_, proto, flags))
        except socket.gaierror:
            continue
    return results


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # anyio (so httpx's async client, so every alert send) encodes the host to
    # bytes before it gets here. Missing that meant the fallback silently did
    # nothing on exactly the path that matters most.
    if isinstance(host, (bytes, bytearray)):
        name = bytes(host).decode("ascii", "ignore")
    elif isinstance(host, str):
        name = host
    else:
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    # A host we already had to route around: skip the slow path entirely.
    cached = _cached(name)
    if cached:
        results = _build(cached, port, family, type, proto, flags)
        if results:
            return results

    future = _resolver_pool.submit(
        _original_getaddrinfo, host, port, family, type, proto, flags
    )
    try:
        return future.result(timeout=FAST_TIMEOUT)
    except (FutureTimeout, socket.gaierror, OSError):
        # The abandoned lookup finishes on its own thread and is discarded.
        ips = _doh_lookup(name)
        if not ips:
            return future.result()  # let the real error surface
        _remember(name, ips)
        results = _build(ips, port, family, type, proto, flags)
        if not results:
            return future.result()
        return results


def install():
    """Idempotent: safe to call from every entry point."""
    if socket.getaddrinfo is not _patched_getaddrinfo:
        socket.getaddrinfo = _patched_getaddrinfo
        log.info("DNS fallback installed (system resolver, then DoH)")
    for host in WARM_HOSTS:
        if not _cached(host):
            try:
                socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
            except Exception:
                pass


install()
