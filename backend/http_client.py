"""Polite HTTP client: robots.txt (RFC 9309), per-host crawl delay, retries with backoff.

robots.txt semantics follow RFC 9309:
  * 2xx  -> parse and obey (including Crawl-delay)
  * 4xx  -> no restrictions published (crawling allowed)
  * 5xx / network error -> assume full disallow until the next check
Key-authenticated official APIs (Adzuna, Jooble) are governed by their API terms
instead of robots.txt, so callers pass ``check_robots=False`` for those only.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.config import get_settings

log = logging.getLogger(__name__)

ROBOTS_TTL_SECONDS = 6 * 3600
# An unreachable robots.txt means "disallow all" (RFC 9309), but only until it is reachable
# again, so retry soon: e.g. Workday answers 5xx during maintenance windows.
ROBOTS_UNREACHABLE_TTL_SECONDS = 10 * 60


class RobotsDisallowed(Exception):
    pass


class RobotsUnreachable(RobotsDisallowed):
    """robots.txt answered 5xx or could not be fetched; treated as disallow for now."""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class PoliteClient:
    def __init__(self) -> None:
        s = get_settings()
        self.user_agent = s.http_user_agent
        self.min_delay = s.http_min_delay_seconds
        self.client = httpx.Client(
            timeout=s.http_timeout_seconds,
            headers={"User-Agent": self.user_agent, "Accept": "application/json, */*;q=0.5"},
            follow_redirects=True,
        )
        self._robots: dict[str, tuple[float, RobotFileParser | None, bool]] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ robots
    def _robots_for(self, scheme: str, host: str) -> tuple[RobotFileParser | None, bool]:
        """Returns (parser or None, allow_all_if_no_parser)."""
        cached = self._robots.get(host)
        if cached:
            reachable = cached[1] is not None or cached[2]
            if time.time() - cached[0] < (ROBOTS_TTL_SECONDS if reachable else ROBOTS_UNREACHABLE_TTL_SECONDS):
                return cached[1], cached[2]
        parser, allow_all = None, False
        try:
            resp = self.client.get(f"{scheme}://{host}/robots.txt", timeout=10)
            if resp.status_code < 300:
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
            elif resp.status_code < 500:
                allow_all = True
        except httpx.HTTPError as exc:
            log.warning("robots.txt fetch failed for %s: %s", host, exc)
        self._robots[host] = (time.time(), parser, allow_all)
        return parser, allow_all

    def _check_robots(self, url: str) -> float:
        parts = urlsplit(url)
        parser, allow_all = self._robots_for(parts.scheme, parts.netloc)
        if parser is None:
            if allow_all:
                return 0.0
            raise RobotsUnreachable(f"robots.txt for {parts.netloc} unreachable (5xx/network); skipping per RFC 9309")
        if not parser.can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        return float(parser.crawl_delay(self.user_agent) or 0.0)

    # ------------------------------------------------------------ throttling
    def _throttle(self, host: str, delay: float) -> None:
        # Reserve the next slot for this host under the lock, then sleep outside it, so a
        # crawl-delay on one host (e.g. api.lever.co) never stalls requests to other hosts.
        with self._lock:
            now = time.time()
            slot = max(now, self._last_hit.get(host, 0.0) + delay)
            self._last_hit[host] = slot
        if slot > now:
            time.sleep(slot - now)

    # ------------------------------------------------------------ requests
    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def request(self, method: str, url: str, *, check_robots: bool = True, **kwargs) -> httpx.Response:
        delay = self.min_delay
        if check_robots:
            delay = max(delay, self._check_robots(url))
        self._throttle(urlsplit(url).netloc, delay)
        resp = self.client.request(method, url, **kwargs)
        if resp.status_code == 304 and kwargs.get("headers", {}).get("If-None-Match"):
            return resp  # conditional GET: unchanged
        resp.raise_for_status()
        return resp

    def get_json(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs).json()

    def get_json_if_changed(self, url: str, etag: Optional[str], **kwargs) -> tuple[Any, Optional[str]]:
        """Conditional GET. Returns (None, etag) when the server answers 304 Not Modified,
        otherwise (parsed JSON, new ETag). Greenhouse, Lever and Ashby all support this,
        which makes a 2-minute poll of an unchanged board cost one tiny request."""
        headers = dict(kwargs.pop("headers", None) or {})
        if etag:
            headers["If-None-Match"] = etag
        resp = self.request("GET", url, headers=headers, **kwargs)
        if resp.status_code == 304:
            return None, etag
        return resp.json(), resp.headers.get("etag")

    def close(self) -> None:
        self.client.close()


_shared: Optional[PoliteClient] = None
_shared_lock = threading.Lock()


def shared_client() -> PoliteClient:
    """One long-lived client for background discovery, so robots.txt caching and per-host
    crawl delays hold across cycles instead of resetting every minute."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PoliteClient()
        return _shared


def reset_shared_client() -> None:
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.close()
        _shared = None
