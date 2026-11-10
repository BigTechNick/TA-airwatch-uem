# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Nikhil Chawla
"""
airwatch_gps_store.py - shared helpers for TA-airwatch-uem.

  * Splunkd: small REST client for splunkd (Splunk's own splunk.rest inside
    Splunk; tests pass their own transport).
  * Clients: the OAuth clients (token URL, client ID and secret, proxy, CA
    file) the inputs use, stored encrypted in Splunk's credential store
    (storage/passwords). The secret is never returned to the Inputs page.
  * Paths, CEF files and the status an input logs after each run.

Standard library only; runs on Splunk's bundled Python 3.9 and 3.13.
"""
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse

APP = "TA-airwatch-uem"
SCHEME = "airwatch_gps"
SOURCETYPE = "airwatch:gps"
REALM = "airwatch_gps_client"
CHUNK = 255                         # credential store values are stored in parts of this size
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
# CEF files, one folder per input: <input>_<UTC start date>_<HHMMSS>_w0.cef
CEF_RE = re.compile(r"^(?P<prefix>.+)_(?P<start>\d{4}-\d{2}-\d{2}_\d{6})_w(?P<writer>\d+)\.cef$")
CLIENT_FIELDS = ("token_url", "client_id", "proxy_url", "ca_file")


class StoreError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def splunk_home():
    return os.environ.get("SPLUNK_HOME") or os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))


def default_cef_root():
    return os.path.join(splunk_home(), "var", "log", SCHEME)


def state_root():
    return os.path.join(splunk_home(), "var", "lib", "splunk", "modinputs", SCHEME)


def cef_dir(params, name):
    return os.path.join(os.path.normpath(params.get("cef_dir") or default_cef_root()), name)


def log_path(name):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return os.path.join(splunk_home(), "var", "log", "splunk", f"{SCHEME}_{safe}.log")


def check_cef_root(folder):
    """Error text if the CEF folder is somewhere Splunk itself uses (config, its own logs, indexes)."""
    if not os.path.isabs(folder):
        return "cef_dir must be an absolute path"
    real = os.path.realpath(folder)
    home = os.path.realpath(splunk_home())
    for sub in (("etc",), ("var", "log", "splunk"), ("var", "spool"), ("var", "run"), ("var", "lib")):
        reserved = os.path.join(home, *sub)
        if real == reserved or real.startswith(reserved + os.sep):
            return f"cef_dir must not be inside {reserved} (Splunk uses it); e.g. {default_cef_root()}"
    return None


def check_writable(folder):
    """Error text if this process (the splunk user) cannot create and write files in folder."""
    try:
        os.makedirs(folder, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".write_test_", dir=folder)
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        try:
            import pwd
            user = pwd.getpwuid(os.geteuid()).pw_name
        except Exception:
            user = str(os.geteuid())
        return (f"Splunk (user {user}) cannot write to {folder}: {exc.strerror or exc}. "
                f"Use a folder the {user} user can write, e.g. {default_cef_root()}")
    return None


# ------------------------------------------------------------------ CEF files

def list_cef_files(folder):
    """CEF files in folder, oldest first: [{path, start, size, mtime}]."""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for fname in names:
        m = CEF_RE.match(fname)
        if not m:
            continue
        path = os.path.join(folder, fname)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"path": path, "start": m.group("start"), "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: (f["start"], f["path"]))
    return out


def folder_usage(folder):
    """{bytes, files, free_bytes} for an input's CEF folder (free space of its file system)."""
    files = list_cef_files(folder)
    probe = folder
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    try:
        free = shutil.disk_usage(probe or "/").free
    except OSError:
        free = None
    return {"bytes": sum(f["size"] for f in files), "files": len(files), "free_bytes": free}


def delete_cef_files(folder):
    """Remove this add-on's CEF files (only names it writes) and the folder if it is then empty."""
    removed = 0
    for f in list_cef_files(folder):
        try:
            os.remove(f["path"])
            removed += 1
        except OSError:
            pass
    try:
        os.rmdir(folder)
    except OSError:
        pass
    return removed


# ------------------------------------------------------------------ splunkd

class Splunkd:
    """
    Calls splunkd REST as the given session (user token or the modular input's
    system token). Returns (status, parsed JSON or text).
    """

    def __init__(self, token, transport=None):
        self.token = token
        self.transport = transport or _splunk_rest

    def call(self, method, path, args=None):
        if not path.startswith("/"):
            path = f"/servicesNS/nobody/{APP}/{path}"
        args = dict(args or {})
        args["output_mode"] = "json"
        status, body = self.transport(method, path, args, self.token)
        try:
            body = json.loads(body) if isinstance(body, (str, bytes)) and body else body
        except ValueError:
            pass
        return status, body

    def ok(self, method, path, args=None, allow=(200, 201)):
        status, body = self.call(method, path, args)
        if status not in allow:
            raise StoreError(error_text(body) or f"splunkd returned {status} for {path}", status)
        return body


def _splunk_rest(method, path, args, token):
    import splunk.rest               # Splunk's own Python library (present inside Splunk)
    getargs = args if method in ("GET", "DELETE") else {"output_mode": args.get("output_mode", "json")}
    postargs = None if method in ("GET", "DELETE") else {k: v for k, v in args.items() if k != "output_mode"}
    try:
        response, content = splunk.rest.simpleRequest(
            path, sessionKey=token, method=method, getargs=getargs, postargs=postargs,
            raiseAllErrors=False)
        return int(response.status), content
    except Exception as exc:                 # splunk.RESTException and subclasses (404, 403, ...)
        status = getattr(exc, "statusCode", None)
        if not status:
            raise
        return int(status), json.dumps(
            {"messages": [{"type": "ERROR", "text": getattr(exc, "msg", None) or str(exc)}]})


def error_text(body):
    if isinstance(body, dict):
        msgs = body.get("messages") or []
        text = "; ".join(m.get("text", "") for m in msgs if isinstance(m, dict))
        return text or body.get("error", "")
    if isinstance(body, (bytes, str)):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        found = re.findall(r"<msg[^>]*>([^<]*)</msg>", text)
        return "; ".join(found) or text[:300]
    return ""


def entries(body):
    return body.get("entry", []) if isinstance(body, dict) else []


def quote(name):
    return urllib.parse.quote(name, safe="")


# ------------------------------------------------------------- OAuth clients

URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::\d{1,5})?(?:/[^\s]*)?$")
PROXY_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?/?$")


def check_client(doc):
    """Error text for a client's settings, or None."""
    if not URL_RE.match(doc.get("token_url") or ""):
        return "Token URL: an https:// URL, e.g. https://na.uemauth.workspaceone.com/connect/token"
    if not (doc.get("client_id") or "").strip() or len(doc["client_id"]) > 200:
        return "Client ID is required"
    if not (doc.get("client_secret") or "").strip() or len(doc["client_secret"]) > 500:
        return "Client secret is required"
    if doc.get("proxy_url") and not PROXY_RE.match(doc["proxy_url"]):
        return "Proxy: http://host:port (no user name or password in the URL)"
    if doc.get("ca_file") and not os.path.isabs(doc["ca_file"]):
        return "CA file: an absolute path to a PEM file on this server"
    return None


class Clients:
    """OAuth clients in storage/passwords (one JSON document each, split into CHUNK-sized parts)."""

    def __init__(self, splunkd):
        self.splunkd = splunkd

    def _parts(self):
        body = self.splunkd.ok("GET", "storage/passwords", {"count": 0, "search": f"realm={REALM}"})
        parts = {}
        for entry in entries(body):
            content = entry.get("content", {})
            if content.get("realm") != REALM:
                continue
            m = re.match(r"^(.*)\.(\d{3})$", content.get("username", ""))
            if m:
                parts.setdefault(m.group(1), {})[int(m.group(2))] = (
                    content.get("clear_password", ""), entry.get("name", ""))
        return parts

    @staticmethod
    def _assemble(chunks):
        text, i = "", 0
        while i in chunks:
            text += chunks[i][0]
            i += 1
        return json.loads(text)

    def list(self):
        """Every client without its secret."""
        out = []
        for name, chunks in sorted(self._parts().items()):
            try:
                doc = self._assemble(chunks)
            except ValueError:
                out.append({"name": name, "error": "stored client is incomplete; save it again"})
                continue
            out.append(dict(public(doc), name=name))
        return out

    def get(self, name):
        """The full client document, secret included (modular input and connection tests only)."""
        chunks = self._parts().get(name)
        if not chunks:
            raise StoreError(f"OAuth client '{name}' is not set up", 404)
        try:
            return self._assemble(chunks)
        except ValueError:
            raise StoreError(f"OAuth client '{name}' is incomplete; save it again", 500)

    def save(self, name, doc, user):
        if not NAME_RE.match(name or ""):
            raise StoreError("Client name: letters, digits, '.', '_' or '-' (max 64)")
        error = check_client(doc)
        if error:
            raise StoreError(error)
        doc = {k: (doc.get(k) or "").strip() for k in CLIENT_FIELDS + ("client_secret",)}
        doc.update(saved=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), saved_by=user or "")
        text = json.dumps(doc, separators=(",", ":"))
        self.delete(name, missing_ok=True)
        for i in range(0, len(text), CHUNK):
            self.splunkd.ok("POST", "storage/passwords",
                            {"name": f"{name}.{i // CHUNK:03d}", "realm": REALM, "password": text[i:i + CHUNK]})
        return dict(public(doc), name=name)

    def delete(self, name, missing_ok=False):
        chunks = self._parts().get(name)
        if not chunks:
            if missing_ok:
                return
            raise StoreError(f"OAuth client '{name}' not found", 404)
        for _, entry_name in chunks.values():
            self.splunkd.ok("DELETE", f"storage/passwords/{quote(entry_name)}", allow=(200, 404))


def public(doc):
    """A client document without its secret."""
    return {k: v for k, v in doc.items() if k != "client_secret"}


# ------------------------------------------------------------------ status

# Written by the collector at the end of every run (see airwatch_gps.py).
RUN_RE = re.compile(r"run done: devices (?P<devices>\d+) \(\d+ pages\), samples (?P<samples>\d+), "
                    r"new (?P<new>\d+), .*?took (?P<took>[\d.]+) s")
LINE_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} (?P<level>[A-Z]+) (?P<msg>.*)$")


def read_status(name):
    """The input's last run, from its own log file on this instance (works without local indexing)."""
    try:
        with open(log_path(name), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    out = {}
    for line in reversed(lines):
        m = LINE_RE.match(line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        out.setdefault("last_seen", ts)
        run = RUN_RE.search(m.group("msg"))
        if run:
            out.update(last_run=ts, devices=run.group("devices"), samples=run.group("samples"),
                       new=run.group("new"), took=run.group("took"))
            break
        if m.group("level") in ("ERROR", "CRITICAL") and "last_error" not in out:
            out["last_error"] = m.group("msg")            # newer than the last completed run
            out["last_error_time"] = ts
    return out
