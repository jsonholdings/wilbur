"""Model discovery for `/search`.

Two sources, and the difference is stated to the user rather than blurred:

LOCAL   wilbur/catalog.py -- every model this portfolio actually uses, with a
        real description and a measured-or-not flag. Works offline, always.

REMOTE  ollama.com/search -- best effort. Ollama publishes NO search API:
        the endpoint returns HTML, and registry.ollama.ai/v2/ is a 404 at the
        root (both checked 2026-09-06). So this scrapes markup, which will
        break when the site changes. It is additive -- a remote failure never
        costs you the local results, and results are labelled by source so a
        stale scrape can never masquerade as a curated entry.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import catalog

SEARCH_URL = "https://ollama.com/search?q=%s"
USER_AGENT = "wilbur-code/2.0 (+local coding agent)"


@dataclass
class Result:
    name: str
    description: str
    source: str               # "local" | "ollama.com"
    gib: float | None = None
    origin: str = ""
    measured: bool = False
    installed: bool = False
    pullable: bool = True

    @property
    def fits_24gb(self) -> bool | None:
        return None if self.gib is None else self.gib <= 21.0


def _local(query: str) -> list[Result]:
    out = []
    for m in catalog.search(query):
        out.append(Result(
            name=m.name, description=m.note, source="local",
            gib=m.gib, origin=m.origin, measured=m.measured,
        ))
    # Surface the known-unpullable ones too, so a user searching for one is told
    # why it cannot be installed rather than getting silence and trying a pull
    # that fails.
    q = query.strip().lower()
    for name, why in catalog.NOT_PULLABLE.items():
        if not q or q in name.lower() or q in why.lower():
            out.append(Result(name=name, description=why, source="local", pullable=False))
    return out


def _remote(query: str, timeout: float = 8.0) -> list[Result]:
    """Scrape ollama.com/search. Returns [] on any failure, never raises."""
    try:
        req = urllib.request.Request(
            SEARCH_URL % urllib.parse.quote(query),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []

    results: list[Result] = []
    seen = set()

    # The page embeds a JSON-LD / __NEXT_DATA__ blob on some revisions; prefer
    # it when present because it is far more stable than the visual markup.
    for blob in re.findall(r'<script[^>]+type="application/(?:ld\+)?json"[^>]*>(.*?)</script>',
                           html, re.S):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for item in _walk_for_models(data):
            if item["name"] not in seen:
                seen.add(item["name"])
                results.append(Result(name=item["name"], description=item.get("desc", ""),
                                      source="ollama.com"))

    if not results:
        # Fall back to the model cards. Deliberately loose: match the href that
        # every card links to, then take the nearest description paragraph.
        for m in re.finditer(r'href="/library/([a-z0-9][a-z0-9._-]*)"', html):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            tail = html[m.end():m.end() + 900]
            desc = re.search(r'<p[^>]*>(.*?)</p>', tail, re.S)
            text = re.sub(r"<[^>]+>", "", desc.group(1)).strip() if desc else ""
            results.append(Result(name=name, description=" ".join(text.split())[:300],
                                  source="ollama.com"))
    return results


def _walk_for_models(node) -> list[dict]:
    """Pull {name, desc} pairs out of an arbitrary decoded JSON blob."""
    found = []
    if isinstance(node, dict):
        name = node.get("name") or node.get("model")
        if isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9._/-]*(:[\w.-]+)?", name):
            desc = node.get("description") or node.get("summary") or ""
            if isinstance(desc, str):
                found.append({"name": name, "desc": " ".join(desc.split())[:300]})
        for v in node.values():
            found.extend(_walk_for_models(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_walk_for_models(v))
    return found


def search(query: str, *, remote: bool = True, installed: set[str] | None = None) -> list[Result]:
    """Local matches first, then anything remote adds. Never raises."""
    installed = installed or set()
    results = _local(query)
    known = {r.name for r in results}
    if remote:
        for r in _remote(query):
            # A remote hit for something already curated adds nothing but noise.
            if r.name in known or any(k.split(":")[0] == r.name for k in known):
                continue
            results.append(r)
    for r in results:
        r.installed = r.name in installed
    return results
