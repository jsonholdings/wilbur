"""Browser driver: runs INSIDE the jh-wilbur-browser container (containers/browser).

Speaks JSON lines: one request object per stdin line, one reply object per stdout
line. The host side is wilbur/tools/browser.py. This file must stay importable
without Playwright, because the host imports `url_refusal` from it so the same
URL policy is enforced on both sides of the container boundary.

Safety model:
- every request the page makes (navigation, redirect, sub-resource) passes
  `url_refusal`: only http/https, and no loopback/private/link-local/reserved
  address unless the host or network is allow-listed;
- a fresh browser context per session: no profile on disk, no saved logins;
- response bodies over `max_page_bytes` are aborted; page text is capped at
  `max_chars`;
- downloads stay in the container's tmpfs and are discarded with it.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import socket
import sys
from typing import Any
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


def _address_allowed(addr: str, allow: list[str]) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    for entry in allow:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def url_refusal(url: str, allow: list[str] | None = None, resolve: Any = None) -> str:
    """Why `url` may not be fetched ("" when it may).

    `allow` holds host names and IPs/CIDRs (config `browser_allow`); a match
    lifts the private-range refusal for that host. `resolve` is
    socket.getaddrinfo, injectable for tests.
    """
    allow = [str(a).strip().lower() for a in (allow or []) if str(a).strip()]
    if url.strip().lower() in ("about:blank",):
        return ""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return f"scheme {scheme or '(none)'!r} is not allowed (http and https only)"
    host = (parts.hostname or "").lower()
    if not host:
        return "URL has no host"
    if host in allow:
        return ""
    resolve = resolve or socket.getaddrinfo
    try:
        infos = resolve(host, parts.port or (443 if scheme == "https" else 80),
                        proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        return f"cannot resolve {host}: {exc}"
    addrs = sorted({info[4][0] for info in infos})
    if not addrs:
        return f"cannot resolve {host}"
    for addr in addrs:
        if not _address_allowed(addr, allow):
            return (f"{host} resolves to {addr}, a local or private address; add it to "
                    "browser_allow in the config to permit it")
    return ""


class Driver:
    def __init__(self, cfg: dict[str, Any]) -> None:
        from playwright.sync_api import sync_playwright  # container-only dependency

        self.allow = list(cfg.get("allow") or [])
        self.max_chars = int(cfg.get("max_chars", 12000))
        self.max_page_bytes = int(cfg.get("max_page_bytes", 5_000_000))
        self.timeout_ms = int(cfg.get("timeout_ms", 20000))
        self.refused: list[str] = []
        self.downloads: list[str] = []
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True)
        # new_context = throwaway in-memory profile; nothing persists.
        self.context = self.browser.new_context(accept_downloads=True,
                                                service_workers="block")
        self.context.set_default_timeout(self.timeout_ms)
        self.context.route("**/*", self._route)
        self.page = self.context.new_page()
        self.page.on("download", lambda d: self.downloads.append(d.suggested_filename))

    def _route(self, route: Any) -> None:
        url = route.request.url
        why = url_refusal(url, self.allow)
        if why:
            self.refused.append(f"{url}: {why}")
            route.abort("blockedbyclient")
            return
        try:
            response = route.fetch(max_redirects=0)
            body = response.body()
        except Exception as exc:  # network error: let the page see a failure
            self.refused.append(f"{url}: fetch failed: {exc}")
            route.abort("failed")
            return
        if len(body) > self.max_page_bytes:
            self.refused.append(f"{url}: {len(body)} bytes exceeds the "
                                f"{self.max_page_bytes}-byte page cap")
            route.abort("blockedbyclient")
            return
        route.fulfill(response=response, body=body)

    def _summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"url": self.page.url, "title": self.page.title()}
        if self.refused:
            out["refused"] = self.refused[-5:]
            self.refused.clear()
        if self.downloads:
            out["downloads"] = self.downloads[-5:]
            self.downloads.clear()
        return out

    def _text(self) -> dict[str, Any]:
        text = self.page.inner_text("body") if self.page.query_selector("body") else ""
        truncated = len(text) > self.max_chars
        return {"text": text[: self.max_chars], "truncated": truncated,
                "chars": len(text)}

    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "open":
            url = str(req.get("url", ""))
            why = url_refusal(url, self.allow)
            if why:
                return {"ok": False, "error": f"refused: {why}"}
            resp = self.page.goto(url, wait_until="load")
            out = self._summary()
            out["status"] = resp.status if resp else None
            out.update(self._text())
            return {"ok": True, **out}
        if op == "read":
            return {"ok": True, **self._summary(), **self._text()}
        if op == "click":
            self.page.click(str(req["selector"]))
            self.page.wait_for_load_state("load")
            return {"ok": True, **self._summary()}
        if op == "fill":
            self.page.fill(str(req["selector"]), str(req.get("value", "")))
            if req.get("submit"):
                self.page.press(str(req["selector"]), "Enter")
                self.page.wait_for_load_state("load")
            return {"ok": True, **self._summary()}
        if op == "wait":
            timeout = min(int(req.get("timeout_ms") or self.timeout_ms), self.timeout_ms)
            self.page.wait_for_selector(str(req["selector"]), timeout=timeout,
                                        state=str(req.get("state") or "visible"))
            return {"ok": True, **self._summary()}
        if op == "screenshot":
            png = self.page.screenshot(full_page=bool(req.get("full_page")))
            return {"ok": True, **self._summary(),
                    "png_b64": base64.b64encode(png).decode("ascii")}
        if op == "close":
            return {"ok": True, "closed": True}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def close(self) -> None:
        for step in (self.context.close, self.browser.close, self._pw.stop):
            try:
                step()
            except Exception:
                pass


def main(argv: list[str]) -> int:
    cfg = json.loads(argv[1]) if len(argv) > 1 else {}
    driver = Driver(cfg)
    print(json.dumps({"ok": True, "ready": True}), flush=True)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                req = json.loads(line)
                reply = driver.handle(req)
            except Exception as exc:  # report the failure, keep the session alive
                error = f"{type(exc).__name__}: {exc}".splitlines()[0]
                if driver.refused:  # say WHY a request failed (policy or size cap)
                    error += "; blocked: " + " | ".join(driver.refused[-3:])
                    driver.refused.clear()
                reply = {"ok": False, "error": error}
                req = {}
            print(json.dumps(reply), flush=True)
            if req.get("op") == "close":
                break
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
