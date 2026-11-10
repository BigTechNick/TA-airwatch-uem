# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Nikhil Chawla
"""
airwatch_gps_api.py - the Workspace ONE UEM (AirWatch) REST calls the add-on makes.

  token()                    POST <token URL>  grant_type=client_credentials (OAuth client ID + secret)
  devices()                  GET  <API>/API/mdm/devices/search?page=N&pagesize=1000   (Accept version=2)
  gps(device_ids, start)     POST <API>/API/mdm/devices/gps/search?startdatetime=...   (Accept version=1)

Read-only: nothing is changed in the console. HTTPS with certificate checks
(the OS trust store, or the client's CA file), an optional HTTP proxy, a
timeout on every call and retries with backoff on network errors, HTTP 429
and 5xx.

Standard library only.
"""
import base64
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 1000
MAX_PAGES = 1000                      # a safety stop: 1,000,000 devices
TIMEOUT = 120
RETRIES = 3                           # attempts after the first, at 5, 15 and 45 s
USER_AGENT = "TA-airwatch-uem"
# OS trust stores (RHEL/CentOS/Amazon, Debian/Ubuntu, SUSE, macOS/BSD). They include CAs
# the organisation has installed (for example for a TLS-inspecting proxy).
SYSTEM_CA_FILES = ("/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/certs/ca-certificates.crt",
                   "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem")


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def ssl_context(ca_file=None):
    """Certificate-checking TLS context: the given CA file, else the OS trust store, else Python's own."""
    if ca_file:
        if not os.path.isfile(ca_file):
            raise ApiError(f"CA file {ca_file} not found")
        return ssl.create_default_context(cafile=ca_file)
    for path in SYSTEM_CA_FILES:
        if os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


class Api:
    def __init__(self, api_url, token_url, client_id, client_secret, proxy_url=None, ca_file=None,
                 log=None, sleep=time.sleep, opener=None, timeout=TIMEOUT, retries=RETRIES):
        self.api_url = api_url.rstrip("/")
        self.timeout, self.retries = timeout, retries
        self.token_url = token_url
        self.client_id, self.client_secret = client_id, client_secret
        self.log = log
        self.sleep = sleep
        self.access_token = None
        self.calls = 0
        if opener is None:
            handlers = [urllib.request.HTTPSHandler(context=ssl_context(ca_file))]
            if proxy_url:
                handlers.append(urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url}))
            opener = urllib.request.build_opener(*handlers)
        self.opener = opener

    # ---- HTTP

    def _request(self, method, url, headers, body=None, what="request"):
        """(status, parsed JSON or None). Retries network errors, 429 and 5xx."""
        delay = 5
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=body, method=method, headers=dict(headers, **{
                "User-Agent": USER_AGENT}))
            try:
                self.calls += 1
                with self.opener.open(req, timeout=self.timeout) as resp:
                    status, data = resp.status, resp.read()
                if status == 204 or not data:
                    return status, None
                try:
                    return status, json.loads(data.decode("utf-8"))
                except ValueError:
                    raise ApiError(f"{what}: the response is not JSON (HTTP {status}): {data[:200]!r}", status)
            except urllib.error.HTTPError as exc:
                text = exc.read()[:500].decode("utf-8", "replace") if exc.fp else ""
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    wait = _retry_after(exc) or delay
                    self._warn(f"{what}: HTTP {exc.code}, retrying in {wait} s")
                    self.sleep(wait)
                    delay *= 3
                    continue
                raise ApiError(f"{what}: HTTP {exc.code} {exc.reason}: {_short(text)}", exc.code)
            except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, ssl.SSLCertVerificationError):      # retrying will not help
                    raise ApiError(f"{what}: the server's certificate could not be verified "
                                   f"({reason.verify_message or reason}). If a proxy inspects TLS, set the "
                                   f"client's CA file to its CA certificate (PEM).")
                if attempt < self.retries:
                    self._warn(f"{what}: {reason}, retrying in {delay} s")
                    self.sleep(delay)
                    delay *= 3
                    continue
                raise ApiError(f"{what}: {reason}")
        raise ApiError(f"{what}: failed")                # not reached

    def _warn(self, text):
        if self.log:
            self.log.warning(text)

    # ---- calls

    def token(self):
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        status, data = self._request(
            "POST", self.token_url,
            {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
             "Accept": "application/json"},
            urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"), "OAuth token")
        token = (data or {}).get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ApiError(f"OAuth token: no access_token in the response (HTTP {status})", status)
        self.access_token = token
        return data

    def _api_headers(self, version):
        if not self.access_token:
            self.token()
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json",
                "Accept": f"application/json;version={version}"}

    def devices_page(self, page, page_size=PAGE_SIZE):
        url = f"{self.api_url}/API/mdm/devices/search?" + urllib.parse.urlencode(
            {"page": page, "pagesize": page_size})
        status, data = self._request("GET", url, self._api_headers(2), what=f"device list page {page}")
        if data is None:
            return [], 0, page_size
        if not isinstance(data, dict):
            raise ApiError(f"device list page {page}: unexpected response")
        return data.get("Devices") or [], int(data.get("Total") or 0), int(data.get("PageSize") or page_size)

    def devices(self):
        """Every device: ([device, ...], pages read, the device count the console reports)."""
        out, page, pages, total = [], 0, 1, 0
        while page < pages and page < MAX_PAGES:
            batch, total, size = self.devices_page(page)
            out.extend(batch)
            pages = -(-total // size) if size else 0
            page += 1
            if not batch:
                break
        return out, page, total

    def gps(self, device_ids, start):
        """GPS samples since start (UTC epoch seconds) for these device IDs: [sample, ...]."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(start)) + ".%03d" % int((start % 1) * 1000)
        url = f"{self.api_url}/API/mdm/devices/gps/search?" + urllib.parse.urlencode(
            {"startdatetime": stamp}, quote_via=urllib.parse.quote)
        body = json.dumps({"BulkValues": {"Value": list(device_ids)}}).encode("utf-8")
        status, data = self._request("POST", url, self._api_headers(1), body, "GPS search")
        if data is None:
            return []
        if not isinstance(data, list):
            raise ApiError("GPS search: unexpected response (not a list)")
        return data

    def test(self):
        """Token and the device count (one device fetched): for the Inputs page's Test button."""
        started = time.time()
        self.token()
        _batch, total, _size = self.devices_page(0, 1)
        return {"devices": total, "seconds": round(time.time() - started, 1)}


def _retry_after(exc):
    try:
        value = int(exc.headers.get("Retry-After", ""))
        return value if 0 < value <= 300 else None
    except (TypeError, ValueError, AttributeError):
        return None


def _short(text):
    return " ".join((text or "").split())[:300] or "(no details)"
