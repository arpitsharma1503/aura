"""Minimal Chrome DevTools Protocol client (stdlib only) for AURA browser tests.

Drives the system Chromium in headless mode. Supports isolated browser
contexts (separate cookie jars, like separate phones), viewport/touch/media
emulation, request interception for fixtures, console-error capture and
screenshots.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request
from urllib.parse import urlparse


class WS:
    def __init__(self, url: str) -> None:
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, self.rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.decode(errors="replace"))

    def _read(self, n: int) -> bytes:
        while len(self.rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.rest += chunk
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def recv(self) -> str:
        message = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(n)
            opcode = b1 & 0x0F
            if opcode == 8:
                raise ConnectionError("closed by browser")
            if opcode in (9, 10):
                continue
            message += payload
            if b1 & 0x80:
                return message.decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Session:
    """One DevTools websocket (a page, or the browser itself)."""

    def __init__(self, ws_url: str) -> None:
        self.ws = WS(ws_url)
        self.next_id = 0
        self.handlers: dict[str, callable] = {}
        self.errors: list[str] = []

    def call(self, method: str, **params):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if "method" in msg:
                self._event(msg)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def send_nowait(self, method: str, **params) -> None:
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))

    def _event(self, msg: dict) -> None:
        m, p = msg["method"], msg.get("params", {})
        if m == "Runtime.exceptionThrown":
            d = p["exceptionDetails"]
            self.errors.append(d.get("exception", {}).get("description") or d.get("text"))
        elif m == "Runtime.consoleAPICalled" and p.get("type") == "error":
            self.errors.append(" ".join(str(a.get("value", a.get("description"))) for a in p.get("args", [])))
        elif m == "Log.entryAdded" and p["entry"]["level"] == "error":
            self.errors.append(f"{p['entry'].get('source')}: {p['entry'].get('text')} {p['entry'].get('url', '')}")
        handler = self.handlers.get(m)
        if handler:
            handler(p)


class Page(Session):
    def __init__(self, port: int, target_id: str) -> None:
        super().__init__(f"ws://127.0.0.1:{port}/devtools/page/{target_id}")
        self.target_id = target_id
        self.fixtures: dict[str, tuple[int, str]] = {}
        self.call("Runtime.enable")
        self.call("Page.enable")
        self.call("Log.enable")

    # --- emulation ---
    def viewport(self, width: int, height: int, mobile: bool = False, scale: float = 1) -> None:
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=scale, mobile=mobile)
        self.call("Emulation.setTouchEmulationEnabled", enabled=mobile, maxTouchPoints=5 if mobile else 1)

    def reduced_motion(self, on: bool) -> None:
        self.call("Emulation.setEmulatedMedia",
                  features=[{"name": "prefers-reduced-motion", "value": "reduce" if on else "no-preference"}])

    def clock(self, epoch_ms: float) -> None:
        """From the next navigation on, the page's clock starts at `epoch_ms` and keeps
        ticking (Date and Date.now are shifted; timers are untouched). Lets a fixture's
        time of day and the page's own clock agree."""
        self.call("Page.addScriptToEvaluateOnNewDocument", source="""(() => {
          const Real = Date, offset = %r - Real.now();
          class ShiftedDate extends Real {
            constructor(...a) { if (a.length) super(...a); else super(Real.now() + offset); }
            static now() { return Real.now() + offset; }
          }
          window.Date = ShiftedDate;
        })();""" % float(epoch_ms))

    # --- fixtures: answer matching requests with canned JSON ---
    def fixture(self, url_suffix: str, body, status: int = 200) -> None:
        self.fixtures[url_suffix] = (status, json.dumps(body))
        patterns = [{"urlPattern": f"*{suffix}*", "requestStage": "Request"} for suffix in self.fixtures]
        self.handlers["Fetch.requestPaused"] = self._fulfil
        self.call("Fetch.enable", patterns=patterns)

    def clear_fixtures(self) -> None:
        self.fixtures.clear()
        self.call("Fetch.disable")

    def _fulfil(self, p: dict) -> None:
        url = p["request"]["url"]
        for suffix, (status, body) in self.fixtures.items():
            if suffix in url:
                self.send_nowait("Fetch.fulfillRequest", requestId=p["requestId"], responseCode=status,
                                 responseHeaders=[{"name": "Content-Type", "value": "application/json"},
                                                  {"name": "Cache-Control", "value": "no-store"}],
                                 body=base64.b64encode(body.encode()).decode())
                return
        self.send_nowait("Fetch.continueRequest", requestId=p["requestId"])

    # --- navigation & queries ---
    def goto(self, url: str, ready: str = "document.readyState === 'complete'") -> None:
        self.call("Page.navigate", url=url)
        self.wait_for(ready)

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
        return r["result"].get("value")

    def wait_for(self, expr: str, timeout: float = 10.0, interval: float = 0.05) -> float:
        start = time.monotonic()
        last_err = None
        while time.monotonic() - start < timeout:
            try:
                if self.js(expr):
                    return time.monotonic() - start
            except RuntimeError as e:  # page still loading
                last_err = e
            time.sleep(interval)
        raise TimeoutError(f"Timed out waiting for: {expr}" + (f" (last error: {last_err})" if last_err else ""))

    def text(self, selector: str) -> str:
        """Text content (not CSS-transformed), whitespace-collapsed."""
        return self.js(f"(document.querySelector({json.dumps(selector)})?.textContent || '').replace(/\\s+/g, ' ').trim()")

    def click(self, selector: str) -> None:
        ok = self.js(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); if (!e) return false; e.click(); return true; }})()")
        if not ok:
            raise AssertionError(f"no element {selector}")

    def screenshot(self, path: str) -> None:
        data = self.call("Page.captureScreenshot", format="png")["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))


class Browser:
    """Headless Chromium, controlled over the DevTools protocol."""

    def __init__(self, port: int) -> None:
        self.port = port
        info = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version"))
        self.session = Session(info["webSocketDebuggerUrl"])
        self.contexts: list[str] = []
        self.pages: list[Page] = []

    def new_page(self, url: str = "about:blank", isolated: bool = True) -> Page:
        params = {"url": url}
        if isolated:  # own cookies/storage, like a separate device
            ctx = self.session.call("Target.createBrowserContext", disposeOnDetach=True)["browserContextId"]
            self.contexts.append(ctx)
            params["browserContextId"] = ctx
        target = self.session.call("Target.createTarget", **params)["targetId"]
        page = Page(self.port, target)
        self.pages.append(page)
        return page

    def close_pages(self) -> None:
        """Close every page and context opened so far (keeps tests independent)."""
        for page in self.pages:
            page.ws.close()
            try:
                self.session.call("Target.closeTarget", targetId=page.target_id)
            except RuntimeError:
                pass
        for ctx in self.contexts:
            try:
                self.session.call("Target.disposeBrowserContext", browserContextId=ctx)
            except RuntimeError:
                pass
        self.pages.clear()
        self.contexts.clear()

    def close(self) -> None:
        self.session.ws.close()
