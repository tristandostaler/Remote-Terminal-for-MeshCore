"""Region name brute-forcing and importing.

A packet's transport code is a 16-bit keyed MAC over its payload, so a region
name can never be read back out of stored traffic. It can only be *tested*: for
a candidate name, recompute the code each stored region-scoped packet would have
carried and see whether it matches. A real region matches every packet scoped to
it; a wrong name matches a given packet only by 1-in-65,536 chance, so a name
that matches two or more different packets is, for practical purposes, real.

This module supplies the candidate dictionary, the matching pass (CPU-bound, run
it in a thread) and a guarded fetch/parse for lists of region names published on
other websites.
"""

import hmac
import ipaddress
import itertools
import json
import re
import socket
import string
import time
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.path_utils import UNDEFINED_PAYLOAD_TYPES, parse_packet_envelope
from app.region_resolver import _region_key

# Region names use these characters (mirrors firmware RegionMap::is_name_char).
_NAME_RE = re.compile(r"^[A-Za-z0-9$-]{1,30}$")

# Work caps so a single request cannot pin the CPU indefinitely.
MAX_CANDIDATES = 500_000
DEFAULT_MIN_LETTERS = 2
DEFAULT_MAX_LETTERS = 3
MAX_LETTERS = 4
MAX_PACKETS = 2_000
MAX_SECONDS = 60.0
MAX_SCAN_ROWS = 250_000

# Import limits.
MAX_IMPORT_BYTES = 1_000_000
IMPORT_TIMEOUT_SECONDS = 10.0
MAX_IMPORTED_NAMES = 2_000
MAX_REDIRECTS = 3


def brute_force_candidates(
    min_letters: int = DEFAULT_MIN_LETTERS, max_letters: int = DEFAULT_MAX_LETTERS
) -> list[str]:
    """Every lowercase a-z name with between ``min_letters`` and ``max_letters`` letters."""
    letters = string.ascii_lowercase
    return [
        "".join(combo)
        for length in range(min_letters, max_letters + 1)
        for combo in itertools.product(letters, repeat=length)
    ]


def clean_candidate(raw: str) -> str | None:
    """Normalize a candidate: strip ``#``, reject anything that is not a region name."""
    name = (raw or "").strip()
    if name.startswith("#"):
        name = name[1:].strip()
    if not name or name == "*" or not _NAME_RE.match(name):
        return None
    return name


def expand_candidates(
    user_names: Iterable[str],
    *,
    min_letters: int | None = DEFAULT_MIN_LETTERS,
    max_letters: int | None = DEFAULT_MAX_LETTERS,
) -> list[str]:
    """Deduplicated candidate list: user names (as typed and lowercased), then every
    a-z name of ``min_letters``..``max_letters`` letters (skipped when either is None)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            out.append(name)

    for raw in user_names:
        name = clean_candidate(raw)
        if name is None:
            continue
        add(name)
        add(name.lower())
    if min_letters is not None and max_letters is not None:
        for name in brute_force_candidates(min_letters, max_letters):
            add(name)
    return out[:MAX_CANDIDATES]


@dataclass(frozen=True)
class ScopedPacket:
    payload_type: int
    payload: bytes
    transport_code: int


def scoped_packets_from_rows(
    rows: Iterable[bytes], known_regions: list[str], *, limit: int
) -> tuple[list[ScopedPacket], int]:
    """Pick distinct region-scoped packets that no known region already explains.

    Returns ``(packets, scoped_total)`` where ``scoped_total`` counts every
    scoped packet seen (resolved or not, distinct payloads only).
    """
    from app.region_resolver import resolve_region

    packets: list[ScopedPacket] = []
    seen: set[tuple[int, bytes, int]] = set()
    scoped_total = 0
    for raw in rows:
        envelope = parse_packet_envelope(bytes(raw))
        if envelope is None or envelope.transport_codes is None:
            continue
        # Corrupt captures claim undefined payload types; they cannot be real.
        if envelope.payload_type in UNDEFINED_PAYLOAD_TYPES:
            continue
        code = envelope.transport_codes[0]
        key = (envelope.payload_type, envelope.payload, code)
        if key in seen:
            continue
        seen.add(key)
        scoped_total += 1
        if resolve_region(envelope.payload_type, envelope.payload, code, known_regions):
            continue
        packets.append(ScopedPacket(envelope.payload_type, envelope.payload, code))
        if len(packets) >= limit:
            break
    return packets, scoped_total


@dataclass
class GuessResult:
    region: str
    hits: int


def guess_regions(
    packets: list[ScopedPacket],
    candidates: list[str],
    *,
    min_hits: int = 2,
    max_seconds: float = MAX_SECONDS,
) -> tuple[list[GuessResult], bool]:
    """Count, per candidate, how many packets its transport code explains.

    Returns ``(results, timed_out)`` with results sorted by hits descending and
    limited to candidates reaching ``min_hits``. CPU-bound: call from a thread.
    """
    keyed: list[tuple[str, bytes]] = []
    for name in candidates:
        key = _region_key(name)
        if key is not None:
            keyed.append((name, key))

    hits: dict[str, int] = {}
    deadline = time.monotonic() + max_seconds
    timed_out = False
    for packet in packets:
        if time.monotonic() > deadline:
            timed_out = True
            break
        message = bytes([packet.payload_type & 0xFF]) + packet.payload
        target = packet.transport_code
        for name, key in keyed:
            code = int.from_bytes(hmac.digest(key, message, "sha256")[:2], "little")
            # Firmware nudges the two reserved values.
            if code == 0:
                code = 1
            elif code == 0xFFFF:
                code = 0xFFFE
            if code == target:
                hits[name] = hits.get(name, 0) + 1

    results = [GuessResult(name, count) for name, count in hits.items() if count >= min_hits]
    results.sort(key=lambda r: (-r.hits, r.region))
    return results, timed_out


# --------------------------------------------------------------------------
# Import from a website
# --------------------------------------------------------------------------


class RegionImportError(Exception):
    """Raised with a user-presentable message when an import cannot proceed."""


def _assert_public_host(host: str) -> None:
    """Refuse hosts that resolve to loopback/private/link-local/reserved addresses."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RegionImportError(f"Could not resolve {host}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise RegionImportError("That address is not a public internet host")


def validate_import_url(url: str) -> str:
    """Return the normalized URL, or raise if it is not a public http(s) URL."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RegionImportError("Enter a full http:// or https:// URL")
    _assert_public_host(parsed.hostname)
    return parsed.geturl()


class _TextExtractor(HTMLParser):
    """Collects the text of list/table/code elements, and any ``#tag`` tokens."""

    _WANTED = {"li", "td", "th", "code", "pre", "option"}

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._buf: list[str] = []
        self.chunks: list[str] = []
        self.all_text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._WANTED:
            self._depth += 1

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._WANTED and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.chunks.append("".join(self._buf))
                self._buf = []

    def handle_data(self, data):  # noqa: ANN001
        if self._skip:
            return
        self.all_text.append(data)
        if self._depth:
            self._buf.append(data)


_JSON_KEYS = {"region", "regions", "name", "code", "id", "iata", "slug", "scope", "tag", "key"}
_HASHTAG_RE = re.compile(r"(?<![\w&])#([A-Za-z0-9$-]{1,30})\b")


def _walk_json(node, key: str | None, out: list[str]) -> None:  # noqa: ANN001
    if isinstance(node, str):
        if key is None or key.lower() in _JSON_KEYS:
            out.append(node)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, key, out)
    elif isinstance(node, dict):
        for k, v in node.items():
            # Dict keys that themselves look like region names (a map of name -> info).
            if isinstance(v, dict | list) and k.lower() not in _JSON_KEYS:
                out.append(str(k))
            _walk_json(v, str(k), out)


def extract_region_names(body: str, content_type: str = "") -> list[str]:
    """Pull plausible region names out of JSON, HTML, CSV or plain text.

    Names come back lowercased and deduplicated. Deliberately permissive about
    *what* it returns and strict about the shape of each name; the caller is expected to show a preview and, ideally, verify the
    names against stored packets before trusting them.
    """
    raw: list[str] = []
    text = body.lstrip()
    if text[:1] in "[{" or "json" in content_type.lower():
        try:
            _walk_json(json.loads(body), None, raw)
        except ValueError:
            raw = []
    if not raw:
        if "<" in body and ">" in body:
            parser = _TextExtractor()
            parser.feed(body)
            for chunk in parser.chunks:
                raw.extend(re.split(r"[\s,;|]+", chunk))
            raw.extend(m.group(1) for m in _HASHTAG_RE.finditer(" ".join(parser.all_text)))
        else:
            for line in body.splitlines():
                raw.extend(re.split(r"[\s,;|\t]+", line.strip()))

    names: list[str] = []
    seen: set[str] = set()
    for token in raw:
        name = clean_candidate(token.strip().strip("\"'`()[]{}<>.:"))
        if name is None:
            continue
        # Imported names are lowercased: region names are conventionally lowercase,
        # and their hash is case-sensitive, so a shouted "YUL" would never match.
        name = name.lower()
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= MAX_IMPORTED_NAMES:
            break
    return names


async def fetch_region_names(url: str) -> tuple[str, list[str]]:
    """Fetch ``url`` (public hosts only, size/time capped) and extract region names.

    Redirects are followed manually so every hop is re-validated. Returns the
    final URL and the names found.
    """
    import httpx

    current = validate_import_url(url)
    async with httpx.AsyncClient(
        timeout=IMPORT_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"Accept": "application/json, text/html, text/plain;q=0.9, */*;q=0.5"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RegionImportError("Redirect without a location")
                        current = validate_import_url(urljoin(current, location))
                        continue
                    if response.status_code >= 400:
                        raise RegionImportError(f"The site answered HTTP {response.status_code}")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_IMPORT_BYTES:
                            raise RegionImportError("The page is larger than 1 MB")
                        chunks.append(chunk)
                    body = b"".join(chunks).decode(response.encoding or "utf-8", "replace")
                    names = extract_region_names(body, response.headers.get("content-type", ""))
                    return current, names
            except httpx.HTTPError as exc:
                raise RegionImportError(
                    f"Could not fetch the page: {exc.__class__.__name__}"
                ) from exc
    raise RegionImportError("Too many redirects")


__all__ = [
    "RegionImportError",
    "GuessResult",
    "ScopedPacket",
    "brute_force_candidates",
    "expand_candidates",
    "extract_region_names",
    "fetch_region_names",
    "guess_regions",
    "scoped_packets_from_rows",
    "validate_import_url",
]
