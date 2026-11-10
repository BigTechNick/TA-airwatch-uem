# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Nikhil Chawla
"""
airwatch_gps_rest.py - REST API behind the app's Inputs page.

  /services/airwatch_gps/inputs
      GET                                       inputs, OAuth client names, indexes
      POST action=create|edit                   name + input settings
      POST action=enable|disable|restart|delete name
      POST action=test                          name: token + device count with the input's settings
  /services/airwatch_gps/clients
      GET                                       OAuth clients (never the secret)
      POST action=save                          name, token_url, client_id, client_secret (empty on an
                                                edit = keep the stored one), proxy_url, ca_file,
                                                test=1 (default): get a token before saving
      POST action=test|delete                   name

Every change is made with the signed-in user's own session, so Splunk's normal
permissions apply (admin rights to change inputs and the credential store).
"""
import json
import logging
import logging.handlers
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import airwatch_gps_api as api_mod  # noqa: E402
import airwatch_gps_store as store  # noqa: E402
from splunk.persistconn.application import PersistentServerConnectionApplication  # noqa: E402

INPUT_FIELDS = ("client", "api_url", "console_name", "index_in_splunk", "write_cef", "cef_dir", "cef_max_mb",
                "first_lookback_min", "overlap_min", "max_lookback_hours", "geocode", "log_level", "index",
                "interval")
INPUTS = f"data/inputs/{store.SCHEME}"
TEST_TIMEOUT = 20


def get_logger():
    log = logging.getLogger("airwatch_gps_rest")
    if not log.handlers:
        path = os.path.join(store.splunk_home(), "var", "log", "splunk", f"{store.SCHEME}-webapi.log")
        try:
            handler = logging.handlers.RotatingFileHandler(path, maxBytes=5 << 20, backupCount=2)
        except OSError:
            handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    return log


def form_of(req):
    form = {}
    for key in ("query", "form"):
        for pair in req.get(key) or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                form[pair[0]] = pair[1]
    payload = req.get("payload")
    if not req.get("form") and isinstance(payload, str) and payload:
        try:
            form.update(json.loads(payload))
        except ValueError:
            form.update({k: v[-1] for k, v in urllib.parse.parse_qs(payload, keep_blank_values=True).items()})
    return form


def reply(payload, status=200):
    return {"payload": payload, "status": status}


def with_scheme(value, scheme):
    """Adds scheme:// when the URL was entered without one."""
    value = (value or "").strip()
    if value and "://" not in value:
        value = f"{scheme}://{value}"
    return value


def api_server(value):
    """https://host[:port] of the REST API server: scheme added, trailing / and /API removed."""
    value = with_scheme(value, "https").rstrip("/")
    if value.lower().endswith("/api"):
        value = value[:-4]
    return value


def connection_test(api_url, client, timeout=TEST_TIMEOUT):
    """Token (and, with an API server, the device count) using a stored or entered client."""
    api = api_mod.Api(api_url or "https://unused.invalid", client["token_url"], client["client_id"],
                      client["client_secret"], client.get("proxy_url") or None, client.get("ca_file") or None,
                      timeout=timeout, retries=0)
    try:
        if api_url:
            return dict(api.test(), ok=True)
        api.token()
        return {"ok": True}
    except api_mod.ApiError as exc:
        raise store.StoreError(str(exc), 502 if exc.status is None else 400)


class Api:
    def __init__(self, splunkd, user, log):
        self.splunkd, self.user, self.log = splunkd, user, log
        self.clients = store.Clients(splunkd)

    # ---- inputs
    def inputs(self):
        body = self.splunkd.ok("GET", INPUTS, {"count": 0}, allow=(200,))
        out = []
        for entry in store.entries(body):
            content = entry.get("content", {})
            item = {k: content.get(k, "") for k in INPUT_FIELDS}
            item["name"] = entry.get("name")
            item["disabled"] = content.get("disabled") in (True, "1", 1, "true")
            item["cef_path"] = store.cef_dir(content, item["name"])
            item["status"] = store.read_status(item["name"])
            item["cef"] = store.folder_usage(item["cef_path"]) if str(content.get("write_cef")).lower() in (
                "1", "true") else {}
            out.append(item)
        return out

    def get_inputs(self):
        indexes = []
        status, body = self.splunkd.call("GET", "/services/data/indexes", {"count": 0, "datatype": "event"})
        if status == 200:
            indexes = sorted(e.get("name") for e in store.entries(body)
                             if not e.get("content", {}).get("disabled"))
        names = []
        try:
            names = [c["name"] for c in self.clients.list()]
        except store.StoreError as exc:
            self.log.warning("listing OAuth clients failed: %s", exc)
        return {"inputs": self.inputs(), "clients": names, "indexes": indexes}

    def post_inputs(self, form):
        action = form.get("action", "")
        name = (form.get("name") or "").strip()
        if not store.NAME_RE.match(name):
            raise store.StoreError("Input name: letters, digits, '.', '_' or '-' (max 64)")
        path = f"{INPUTS}/{store.quote(name)}"
        result = {"ok": True}
        if action in ("create", "edit"):
            args = {k: form[k] for k in INPUT_FIELDS if k in form}
            if "api_url" in args:
                args["api_url"] = api_server(args["api_url"])
            if action == "create":
                self.splunkd.ok("POST", INPUTS, dict(args, name=name))
            else:
                current = self.find(name)
                self.splunkd.ok("POST", path, args)
                if not current["disabled"]:                 # restart with the new settings
                    self.restart(path)
        elif action in ("enable", "disable"):
            self.splunkd.ok("POST", f"{path}/{action}")
        elif action == "restart":
            self.restart(path)
        elif action == "delete":
            self.find(name)
            self.splunkd.ok("DELETE", path)
        elif action == "test":
            item = self.find(name)
            client = self.clients.get(item["client"])
            result["test"] = connection_test(item["api_url"], client)
            self.log.info("user=%s input=%s action=test devices=%s", self.user, name, result["test"].get("devices"))
            return result
        else:
            raise store.StoreError("action must be create, edit, enable, disable, restart, delete or test")
        self.log.info("user=%s input=%s action=%s", self.user, name, action)
        result["inputs"] = self.inputs()
        return result

    def restart(self, path):
        self.splunkd.ok("POST", f"{path}/disable")
        self.splunkd.ok("POST", f"{path}/enable")

    def find(self, name):
        for item in self.inputs():
            if item["name"] == name:
                return item
        raise store.StoreError(f"input '{name}' not found", 404)

    # ---- OAuth clients
    def get_clients(self):
        clients = self.clients.list()
        try:
            used = {}
            for item in self.inputs():
                used.setdefault(item["client"], []).append(item["name"])
            for client in clients:
                client["used_by"] = used.get(client["name"], [])
        except store.StoreError:
            pass
        return {"clients": clients}

    def post_clients(self, form):
        action = form.get("action", "")
        name = (form.get("name") or "").strip()
        if action == "save":
            doc = {k: (form.get(k) or "").strip() for k in store.CLIENT_FIELDS}
            doc["token_url"] = with_scheme(doc["token_url"], "https")
            doc["proxy_url"] = with_scheme(doc["proxy_url"], "http")
            doc["client_secret"] = (form.get("client_secret") or "").strip()
            if not doc["client_secret"]:                    # edit without a new secret: keep the stored one
                try:
                    doc["client_secret"] = self.clients.get(name)["client_secret"]
                except store.StoreError:
                    raise store.StoreError("Client secret is required")
            error = store.check_client(doc)
            if error:
                raise store.StoreError(error)
            tested = None
            if str(form.get("test", "1")).lower() in ("1", "true", "on"):
                tested = connection_test(None, doc)
            saved = self.clients.save(name, doc, self.user)
            self.log.info("user=%s client=%s action=save token_url=%s tested=%s", self.user, name,
                          doc["token_url"], bool(tested))
            return {"ok": True, "client": saved, "tested": bool(tested)}
        if action == "test":
            connection_test(None, self.clients.get(name))
            self.log.info("user=%s client=%s action=test ok", self.user, name)
            return {"ok": True}
        if action == "delete":
            users = [i["name"] for i in self.inputs() if i["client"] == name]
            if users:
                raise store.StoreError(f"'{name}' is used by input(s) {', '.join(users)}; change or delete them first")
            self.clients.delete(name)
            self.log.info("user=%s client=%s action=delete", self.user, name)
            return {"ok": True}
        raise store.StoreError("action must be save, test or delete")


class Handler(PersistentServerConnectionApplication):
    def __init__(self, command_line=None, command_arg=None):
        PersistentServerConnectionApplication.__init__(self)
        self.log = get_logger()

    def handle(self, in_string):
        try:
            req = json.loads(in_string)
            session = req.get("session") or {}
            api = Api(store.Splunkd(session.get("authtoken")), session.get("user", ""), self.log)
            method = (req.get("method") or "GET").upper()
            path = (req.get("rest_path") or "").rstrip("/")
            form = form_of(req)
            if method == "DELETE":
                method, form["action"] = "POST", "delete"
            if path.endswith("/inputs"):
                return reply(api.get_inputs() if method == "GET" else api.post_inputs(form))
            if path.endswith("/clients"):
                return reply(api.get_clients() if method == "GET" else api.post_clients(form))
            return reply({"error": f"unknown path {path}"}, 404)
        except store.StoreError as exc:
            return reply({"error": str(exc)}, exc.status if 400 <= exc.status < 600 else 400)
        except Exception as exc:                                    # keep the handler alive
            self.log.exception("request failed")
            return reply({"error": f"{type(exc).__name__}: {exc}"}, 500)
