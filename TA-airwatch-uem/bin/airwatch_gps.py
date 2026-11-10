#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Nikhil Chawla
"""
airwatch_gps.py - Workspace ONE UEM (AirWatch) device GPS locations for Splunk and CEF.

Runs two ways:
  * Splunk modular input "airwatch_gps" (normal use). One input per console,
    managed on the app's Inputs page or in Settings > Data inputs. Splunk runs it
    on the input's schedule (interval: seconds or a cron schedule); each run
    collects the samples since the last run and exits.
  * Command line, for a test run against a console (see --help).

Each run:
  1. OAuth token (client credentials) from the token URL.
  2. Every device: GET /API/mdm/devices/search, asking for 1,000 per page (a
     console may return fewer; the page size it reports is followed).
  3. GPS samples of those devices since the window start:
     POST /API/mdm/devices/gps/search?startdatetime=<UTC>. The console returns
     one sample per device: its latest since the window start.
  4. Each sample gets its device's serial number, friendly name, user name and
     e-mail address, and the nearest city (offline, airwatch_gps_geo.py).
  5. Out: JSON events to Splunk (sourcetype airwatch:gps, _time = SampleTime,
     UTC) and/or one CEF file per run for a syslog agent.

Window: from the end of the last successful run, minus overlap_min (back to
max_lookback_hours at most), so after a late, failed or skipped run the next one
still gets each device's latest position since the last good run, and a sample a
device uploads late is collected when it is still the device's newest (the
console filters on SampleTime). Samples seen in the overlap are recognised by
their ID and written once. The first run reaches back first_lookback_min.

Read-only towards the console. Standard library only (Splunk's bundled Python).
"""
import argparse
import calendar
import fcntl
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import xml.etree.ElementTree as ElementTree
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import airwatch_gps_api as api_mod  # noqa: E402
import airwatch_gps_geo as geo  # noqa: E402
import airwatch_gps_store as store  # noqa: E402

LOG = logging.getLogger("airwatch_gps")

# CEF header: vendor, product and version as earlier exports of these events used them,
# so searches and parsers written for those keep matching.
CEF_HEADER = "CEF:0|Workspace_one|Airwatch|1.0||||"
DEVICE_FIELDS = ("SerialNumber", "DeviceFriendlyName", "UserName", "UserEmailAddress")
SAMPLE_TIME_FORMAT = "%m/%d/%Y %I:%M:%S %p"
SEEN_SLACK = 300                      # keep sample IDs this much longer than the overlap (clock differences)
LOG_MAX_BYTES = 10 << 20
LOG_KEEP = 3


# ------------------------------------------------------------------- events

def value_of(value):
    """The API wraps IDs as {"Value": n}."""
    if isinstance(value, dict) and "Value" in value:
        return value["Value"]
    return value


def sample_epoch(text):
    """SampleTime ("10/22/2026 6:51:04 PM", UTC) -> epoch seconds, or None."""
    try:
        return calendar.timegm(time.strptime((text or "").strip(), SAMPLE_TIME_FORMAT))
    except ValueError:
        return None


def build_event(sample, device, geocoder):
    """
    One sample -> (fields in output order, has_location).

    Field names and order: the sample's own fields (SampleTime, Latitude, Longitude,
    Elevation, Speed, DeviceId, Id), the nearest city (country_code, city, latitude,
    longitude, population, state, county, country), identifier (the sample's ID),
    OriginalEventTime (= SampleTime), then the device's SerialNumber,
    DeviceFriendlyName, UserName and UserEmailAddress. Id is the device ID, as in
    earlier exports of these events.
    """
    device_id = value_of(sample.get("DeviceId"))
    out = {}
    for key, value in sample.items():
        out[key] = device_id if key in ("DeviceId", "Id") else value_of(value)
    located = False
    if geocoder is not None:
        point = geo.coordinates(sample.get("Latitude"), sample.get("Longitude"))
        if point and point != (0.0, 0.0):              # 0,0: the device had no fix
            found = geocoder.lookup(*point)
            if found:
                out.update(found)
                located = True
    out["identifier"] = value_of(sample.get("Id"))
    out["OriginalEventTime"] = sample.get("SampleTime")
    if device:
        for key in DEVICE_FIELDS:
            if key in device:
                out[key] = device[key]
    return out, located


def cef_value(value):
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("=", "\\=").replace("\r", "\\r").replace("\n", "\\n")


def cef_line(fields):
    return CEF_HEADER + " ".join(f"{k}={cef_value(v)}" for k, v in fields.items())


def splunk_xml(fields, epoch, stanza, index, host, source):
    data = json.dumps(fields, ensure_ascii=True, separators=(",", ":"))        # \uXXXX: any locale, valid XML
    parts = [f'<event stanza="{xml_escape(stanza, {chr(34): "&quot;"})}">', f"<time>{epoch:.3f}</time>"]
    if index:
        parts.append(f"<index>{xml_escape(index)}</index>")
    if host:
        parts.append(f"<host>{xml_escape(host)}</host>")
    parts += [f"<sourcetype>{store.SOURCETYPE}</sourcetype>", f"<source>{xml_escape(source)}</source>",
              f"<data>{xml_escape(data)}</data></event>"]
    return "".join(parts)


# ---------------------------------------------------------------- CEF files

class CefFiles:
    """
    One file per run: <folder>/<prefix>_<YYYY-MM-DD>_<HHMMSS>_w0.cef (UTC). Written
    under a hidden name and renamed when complete, so a reader never sees half a
    file. The reader (a syslog agent) deletes files once read; max_mb caps the
    folder by deleting the oldest when the reader falls behind.
    """

    def __init__(self, folder, prefix, max_mb):
        self.folder, self.prefix, self.cap = folder, prefix, int(max_mb) << 20

    def write(self, lines, now=None):
        if not lines:
            return None
        os.makedirs(self.folder, exist_ok=True)
        start = int(now or time.time())
        while True:
            name = f"{self.prefix}_{time.strftime('%Y-%m-%d_%H%M%S', time.gmtime(start))}_w0.cef"
            path = os.path.join(self.folder, name)
            if not os.path.exists(path):
                break
            start += 1
        tmp = os.path.join(self.folder, f".{name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "wb") as fh:
            fh.write(("\n".join(lines) + "\n").encode("utf-8", "replace"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        self.trim(keep=path)
        return path

    def trim(self, keep=None):
        files = store.list_cef_files(self.folder)
        total = sum(f["size"] for f in files)
        if total <= self.cap:
            return
        dropped = dropped_bytes = 0
        for f in files:
            if total <= self.cap * 0.9:
                break
            if f["path"] == keep:
                continue
            try:
                os.remove(f["path"])
            except OSError:
                continue
            total -= f["size"]
            dropped += 1
            dropped_bytes += f["size"]
        if dropped:
            LOG.warning("CEF folder over %d MB (the syslog agent is not reading it?): deleted the %d oldest "
                        "files not read yet (%.1f MB) - those events are lost from CEF", self.cap >> 20, dropped,
                        dropped_bytes / (1 << 20))


# ------------------------------------------------------------------- state

class State:
    """<state dir>/state.json: end of the last successful run, and the sample IDs of the overlap."""

    def __init__(self, folder):
        self.path = os.path.join(folder, "state.json")
        self.doc = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.doc = json.load(fh)
        except FileNotFoundError:
            self.doc = {}
        except (OSError, ValueError) as exc:
            LOG.warning("checkpoint %s is unreadable (%s); this run starts as a first run", self.path, exc)
            self.doc = {}
        if not isinstance(self.doc, dict):
            self.doc = {}
        if not isinstance(self.doc.get("seen"), dict):
            self.doc["seen"] = {}

    @property
    def last_end(self):
        try:
            return float(self.doc.get("last_end") or 0)
        except (TypeError, ValueError):
            return 0.0

    def window_start(self, now, first_lookback_min, overlap_min, max_lookback_hours):
        """(start, note): where this run's window starts, and why."""
        floor = now - max_lookback_hours * 3600
        if self.last_end:
            start, note = self.last_end - overlap_min * 60, "since the last run"
        else:
            start, note = now - first_lookback_min * 60, "first run"
        if start < floor:
            note = (f"the last run ended {(now - self.last_end) / 3600:.1f} h ago; going back "
                    f"{max_lookback_hours} h only (max_lookback_hours) - older samples are skipped")
            start = floor
        return min(start, now), note

    def seen(self, sample_id):
        return str(sample_id) in self.doc["seen"]

    def save(self, end, new_ids, overlap_min, summary):
        """
        new_ids: [(sample ID, sample epoch)]. An ID is kept while a later window can still
        return its sample: until the later of this run's end and the sample's own time
        (a device clock that runs ahead) has dropped out of the overlap.
        """
        seen = {k: v for k, v in self.doc["seen"].items() if v >= end - overlap_min * 60 - SEEN_SLACK}
        for sample_id, epoch in new_ids:
            seen[str(sample_id)] = max(end, epoch)
        self.doc = {"last_end": end, "last_end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
                    "last_run": summary, "seen": seen}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.doc, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


# --------------------------------------------------------------------- run

def collect(api, settings, state, emit=None, cef=None, geocoder=None, clock=time.time, flush=None):
    """
    One run. emit(fields, epoch) sends an event to Splunk (None: no Splunk output),
    flush() makes sure Splunk has them; cef: a CefFiles (None: no CEF output). The CEF
    file is written before any event goes to Splunk, and the checkpoint is saved last,
    after flush: a run that fails part-way is repeated in full by the next run.
    Returns the run summary.
    """
    started = clock()
    start, note = state.window_start(started, settings["first_lookback_min"], settings["overlap_min"],
                                     settings["max_lookback_hours"])
    LOG.info("run started: window %s..now (%s)", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)), note)
    api.token()
    by_id, pages, total = device_list(api)
    if total and len(by_id) < total:                   # devices moved between pages while reading: read again
        LOG.warning("the device list had %d of %d devices; reading it again", len(by_id), total)
        by_id, pages, total = device_list(api)
        if len(by_id) < total:
            LOG.warning("the device list still has %d of the %d devices the console counts; samples of the "
                        "others are not asked for in this run", len(by_id), total)
    if not by_id:
        # An empty list is a console or permission problem, not "no devices": the
        # checkpoint stays, and the next run asks for this window again.
        raise api_mod.ApiError("the device list is empty (does the OAuth client's role see the devices, at "
                               "this organization group?)")
    end = clock()                                      # the window's end: just before the GPS search
    samples = api.gps(list(by_id), start)
    counts = dict(samples=len(samples), new=0, repeated=0, no_device=0, no_location=0, bad_time=0)
    events, new_ids, batch = [], [], set()
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        sample_id = value_of(sample.get("Id"))
        if sample_id is not None and (state.seen(sample_id) or sample_id in batch):
            counts["repeated"] += 1
            continue
        device = by_id.get(value_of(sample.get("DeviceId")))
        if device is None:
            counts["no_device"] += 1
        fields, located = build_event(sample, device, geocoder)
        if geocoder is not None and not located:
            counts["no_location"] += 1
        epoch = sample_epoch(sample.get("SampleTime"))
        if epoch is None:
            counts["bad_time"] += 1
            epoch = end
        events.append((fields, epoch))
        counts["new"] += 1
        if sample_id is not None:
            new_ids.append((sample_id, epoch))
            batch.add(sample_id)
    cef_path = cef.write([cef_line(f) for f, _ in events], end) if cef else None
    if emit:
        for fields, epoch in events:
            emit(fields, epoch)
        if flush:
            flush()
    summary = dict(counts, devices=len(by_id), pages=pages, window_start=start, window_end=end,
                   splunk=len(events) if emit else 0, cef=len(events) if cef else 0,
                   took=round(clock() - started, 1), api_calls=api.calls)
    state.save(end, new_ids, settings["overlap_min"], summary)
    if counts["bad_time"]:
        LOG.warning("%d samples had no readable SampleTime; they were given the run's time", counts["bad_time"])
    LOG.info("run done: devices %d (%d pages), samples %d, new %d, repeated %d, without device %d, "
             "without location %d, splunk %d, cef %d%s, window %s..%s, took %.1f s",
             len(by_id), pages, counts["samples"], counts["new"], counts["repeated"], counts["no_device"],
             counts["no_location"], summary["splunk"], summary["cef"],
             f" ({os.path.basename(cef_path)})" if cef_path else "",
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)), summary["took"])
    return summary


def device_list(api):
    """{device ID: device}, pages read, and the device count the console reports."""
    devices, pages, total = api.devices()
    by_id = {}
    for device in devices:
        device_id = value_of(device.get("Id")) if isinstance(device, dict) else None
        if device_id is not None:
            by_id[device_id] = device
    return by_id, pages, total


def setup_logging(path, level):
    handlers = []
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_KEEP))
    if sys.stderr.isatty() or not path:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=level, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")


def lock(state_dir):
    """An exclusive lock on the input's state folder, or None when another run holds it."""
    os.makedirs(state_dir, exist_ok=True)
    fh = open(os.path.join(state_dir, "collector.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# --------------------------------------------------------------- settings

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TRUE, FALSE = ("1", "true", "yes", "on", "t", "y"), ("0", "false", "no", "off", "f", "n")
API_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::\d{1,5})?/?$")
CRON_RE = re.compile(r"^\S+( \S+){4}$")
NUMBERS = {                       # setting: (low, high, default)
    "cef_max_mb": (1, 102400, 1024),
    "first_lookback_min": (1, 10080, 60),
    "overlap_min": (0, 1440, 60),
    "max_lookback_hours": (1, 720, 24),
}

# name, title, description, data_type, required_on_create
ARGS = [
    ("client", "OAuth client", "Name of the OAuth client set up on the app's Inputs page (token URL, client ID "
     "and secret, proxy).", "string", True),
    ("api_url", "API server", "The console's REST API server, e.g. https://asNNN.awmdm.com (Groups & Settings > "
     "All Settings > System > Advanced > Site URLs > REST API URL, without /API).", "string", True),
    ("console_name", "Console name (host)", "Stored as the events' host field. Default: the API server's host "
     "name.", "string", False),
    ("index_in_splunk", "Index in Splunk", "Send the samples to Splunk as JSON events (sourcetype "
     "airwatch:gps). Default 1.", "boolean", False),
    ("write_cef", "Write CEF", "Also write one CEF file per run (<name>_<date>_<time>_w0.cef) in the CEF folder, "
     "for a syslog agent to read and delete. Default 0.", "boolean", False),
    ("cef_dir", "CEF folder", "Absolute folder for the CEF files, one subfolder per input. Default "
     "$SPLUNK_HOME/var/log/airwatch_gps.", "string", False),
    ("cef_max_mb", "CEF folder cap (MB)", "When the input's CEF files reach this size (the syslog agent is not "
     "reading them), the oldest are deleted. Default 1024.", "number", False),
    ("first_lookback_min", "First run: look back (minutes)", "How far back the first run (no checkpoint yet) "
     "asks for samples. Default 60.", "number", False),
    ("overlap_min", "Overlap (minutes)", "Each run starts this long before the previous run ended, so a sample "
     "a device uploads late (the console filters on the time the sample was taken) is still collected when it "
     "is still the device's newest. Samples already written are recognised by their ID and skipped. Default "
     "60.", "number", False),
    ("max_lookback_hours", "Max look back (hours)", "After a long stop, a run goes back at most this far. "
     "Default 24.", "number", False),
    ("geocode", "Nearest city", "Add the nearest city (country_code, city, latitude, longitude, population, "
     "state, county, country) from the bundled city list. Default 1.", "boolean", False),
    ("log_level", "Log level", "DEBUG, INFO, WARNING or ERROR. Default INFO.", "string", False),
]


def flag(params, key, default):
    """A boolean setting; None when it is not a boolean."""
    value = str(params.get(key) or "").strip().lower()
    if not value:
        return default
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    return None


def number(params, key):
    low, high, default = NUMBERS[key]
    return int(params.get(key) or default)


def check_params(name, params):
    """Error message, or None when the input is valid."""
    if not store.NAME_RE.match(name or ""):
        return "Input name: letters, digits, '.', '_' or '-' (max 64), starting with a letter or digit"
    if not store.NAME_RE.match(params.get("client") or ""):
        return "client: the name of an OAuth client set up on the app's Inputs page"
    if not API_URL_RE.match(params.get("api_url") or ""):
        return "api_url: https://<host> of the console's REST API server, e.g. https://asNNN.awmdm.com"
    if params.get("console_name") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}", params["console_name"]):
        return "console_name: letters, digits, '.', '_' or '-'"
    for key, (low, high, default) in NUMBERS.items():
        value = params.get(key) or str(default)
        if not re.fullmatch(r"\d+", value) or not low <= int(value) <= high:
            return f"{key} must be a whole number from {low} to {high}"
    for key, default in (("index_in_splunk", True), ("write_cef", False), ("geocode", True)):
        if flag(params, key, default) is None:
            return f"{key} must be 1 or 0"
    if not flag(params, "index_in_splunk", True) and not flag(params, "write_cef", False):
        return "Choose at least one output: Index in Splunk and/or Write CEF"
    if (params.get("log_level") or "INFO").upper() not in LOG_LEVELS:
        return "log_level must be one of " + ", ".join(LOG_LEVELS)
    interval = (params.get("interval") or "3600").strip()
    if re.fullmatch(r"\d+", interval):
        if not 60 <= int(interval) <= 86400:
            return "interval: seconds from 60 to 86400, or a cron schedule (e.g. 1 * * * *)"
    elif not CRON_RE.match(interval):
        return "interval: seconds from 60 to 86400, or a cron schedule (e.g. 1 * * * *)"
    index = params.get("index") or "default"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,79}", index):
        return "index: lowercase letters, digits, '_' or '-', starting with a letter or digit"
    if params.get("cef_dir"):
        return store.check_cef_root(params["cef_dir"])
    return None


def settings_of(params):
    return {key: number(params, key) for key in NUMBERS}


def console_name(params):
    if params.get("console_name"):
        return params["console_name"]
    m = re.match(r"^https://([^/:]+)", params.get("api_url") or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------- modular input

def modinput_scheme():
    rows = []
    for name, title, desc, kind, required in ARGS:
        rows.append(
            f'      <arg name="{name}">\n'
            f"        <title>{xml_escape(title)}</title>\n"
            f"        <description>{xml_escape(desc)}</description>\n"
            f"        <data_type>{kind}</data_type>\n"
            f"        <required_on_create>{str(required).lower()}</required_on_create>\n"
            f"        <required_on_edit>false</required_on_edit>\n"
            f"      </arg>")
    print(f"""<scheme>
  <title>AirWatch device GPS</title>
  <description>Device GPS locations from Workspace ONE UEM (AirWatch), with the device's user and the nearest city, for Splunk and as CEF.</description>
  <use_external_validation>true</use_external_validation>
  <use_single_instance>false</use_single_instance>
  <streaming_mode>xml</streaming_mode>
  <endpoint>
    <args>
{chr(10).join(rows)}
    </args>
  </endpoint>
</scheme>""")


def read_config(text):
    """splunkd's XML (run or validate) -> dict with session info and [(name, params)]."""
    root = ElementTree.fromstring(text)
    cfg = {k: (root.findtext(k) or "") for k in ("server_host", "server_uri", "session_key", "checkpoint_dir")}
    stanzas = []
    for node in root.iter():
        if node.tag in ("stanza", "item"):
            name = node.get("name", "")
            if name.startswith(store.SCHEME + "://"):
                name = name[len(store.SCHEME) + 3:]
            params = {p.get("name"): (p.text or "").strip() for p in node.findall("param")}
            stanzas.append((name, params))
    cfg["stanzas"] = stanzas
    return cfg


def read_conf_stanzas(path):
    """Splunk .conf file -> {stanza: {key: value}} (comments, continuation lines)."""
    out, stanza, pending = {}, None, None
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        if pending is not None:
            key, value = pending
            value += "\n" + line.rstrip("\\")
            pending = (key, value) if line.endswith("\\") else None
            out[stanza][key] = value
            continue
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("[") and text.endswith("]"):
            stanza = text[1:-1]
            out.setdefault(stanza, {})
            continue
        if stanza is not None and "=" in text:
            key, value = (part.strip() for part in text.split("=", 1))
            out[stanza][key] = value.rstrip("\\").rstrip()
            if value.endswith("\\"):
                pending = (key, out[stanza][key])
    return out


def saved_settings(name):
    """
    The input's saved settings from the .conf files (this app's default stanza, then
    the input's stanza in any local/inputs.conf). Not via splunkd: during validation
    splunkd is busy with the request being validated, and a call to its inputs
    endpoint would wait until that times out.
    """
    keep = {arg[0] for arg in ARGS} | {"index", "interval"}
    app_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "default", "inputs.conf")
    merged = dict(read_conf_stanzas(app_default).get(store.SCHEME, {}))
    home = store.splunk_home()
    paths = [os.path.join(home, "etc", "system", "local", "inputs.conf")]
    apps = os.path.join(home, "etc", "apps")
    try:
        paths += [os.path.join(apps, app, "local", "inputs.conf") for app in sorted(os.listdir(apps))]
    except OSError:
        pass
    for path in paths:
        stanzas = read_conf_stanzas(path)
        merged.update(stanzas.get(store.SCHEME, {}))
        merged.update(stanzas.get(f"{store.SCHEME}://{name}", {}))
    return {k: v for k, v in merged.items() if k in keep}


def modinput_validate():
    cfg = read_config(sys.stdin.read())
    error = None
    for name, params in cfg["stanzas"]:
        # On an edit splunkd passes only the settings being changed: check them with the saved ones.
        params = {**saved_settings(name), **{k: v for k, v in params.items() if v}}
        error = check_params(name, params)
        if not error and flag(params, "write_cef", False):
            error = store.check_writable(store.cef_dir(params, name))
        if not error and cfg["session_key"]:
            try:
                store.Clients(store.Splunkd(cfg["session_key"])).get(params["client"])
            except store.StoreError as exc:
                if exc.status == 404:
                    error = f"{exc} - set it up on the app's Inputs page first"
            except Exception:                           # store unreachable: don't block the edit
                pass
        if error:
            break
    if error:
        print(f"<error><message>{xml_escape(error)}</message></error>")
        sys.exit(1)


def modinput_run():
    cfg = read_config(sys.stdin.read())
    if not cfg["stanzas"]:
        return
    name, params = cfg["stanzas"][0]
    error = check_params(name, params)
    if error:
        sys.stderr.write(f"{store.SCHEME}://{name}: {error}\n")      # lands in splunkd.log
        sys.exit(1)
    setup_logging(store.log_path(name), (params.get("log_level") or "INFO").upper())
    state_dir = os.path.join(cfg["checkpoint_dir"] or store.state_root(), name)
    held = lock(state_dir)
    if held is None:
        LOG.warning("the previous run of this input is still running - this run is skipped")
        return
    splunkd = store.Splunkd(cfg["session_key"])
    host = console_name(params)
    index = params.get("index") or ""
    stanza = f"{store.SCHEME}://{name}"
    out = sys.stdout
    emit = None
    if flag(params, "index_in_splunk", True):
        out.write("<stream>\n")

        def emit(fields, epoch):
            out.write(splunk_xml(fields, epoch, stanza, index, host, stanza) + "\n")
    cef = None
    if flag(params, "write_cef", False):
        cef = CefFiles(store.cef_dir(params, name), name, number(params, "cef_max_mb"))
    try:
        client = store.Clients(splunkd).get(params["client"])
        api = api_mod.Api(params["api_url"], client["token_url"], client["client_id"], client["client_secret"],
                          client.get("proxy_url") or None, client.get("ca_file") or None, log=LOG)
        geocoder = geo.Geocoder() if flag(params, "geocode", True) else None
        collect(api, settings_of(params), State(state_dir), emit, cef, geocoder, flush=out.flush)
    except (api_mod.ApiError, store.StoreError) as exc:
        LOG.error("run failed: %s - the next run starts from the last good one", exc)
    except Exception as exc:                                    # unexpected: keep the traceback
        LOG.exception("run failed: %s: %s", type(exc).__name__, exc)
    finally:
        if emit:
            out.write("</stream>\n")
        out.flush()
        held.close()


# ------------------------------------------------------------ command line

def build_parser():
    p = argparse.ArgumentParser(description="One collection run against a Workspace ONE UEM (AirWatch) console, "
                                            "for tests. The client secret is read from the environment variable "
                                            "AIRWATCH_CLIENT_SECRET, never from the command line.")
    p.add_argument("--api-url", required=True, help="REST API server, e.g. https://asNNN.awmdm.com")
    p.add_argument("--token-url", required=True, help="OAuth token URL, e.g. "
                   "https://na.uemauth.workspaceone.com/connect/token")
    p.add_argument("--client-id", required=True)
    p.add_argument("--proxy-url", help="http://host:port")
    p.add_argument("--ca-file", help="PEM CA bundle (default: the OS trust store)")
    p.add_argument("--state-dir", required=True, help="checkpoint folder (use a test folder, not the input's)")
    p.add_argument("--json-out", help="write the events here, one JSON object per line")
    p.add_argument("--cef-dir", help="also write a CEF file here")
    p.add_argument("--first-lookback-min", type=int, default=60)
    p.add_argument("--overlap-min", type=int, default=60)
    p.add_argument("--max-lookback-hours", type=int, default=24)
    p.add_argument("--no-geocode", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    secret = os.environ.get("AIRWATCH_CLIENT_SECRET")
    if not secret:
        sys.exit("set AIRWATCH_CLIENT_SECRET to the OAuth client secret first")
    setup_logging(None, args.log_level.upper())
    held = lock(args.state_dir)
    if held is None:
        sys.exit(f"another run holds {args.state_dir}")
    api = api_mod.Api(args.api_url, args.token_url, args.client_id, secret, args.proxy_url, args.ca_file, log=LOG)
    sink = open(args.json_out, "a", encoding="utf-8") if args.json_out else None

    def emit(fields, epoch):
        sink.write(json.dumps(fields, ensure_ascii=False, separators=(",", ":")) + "\n")
    cef = CefFiles(args.cef_dir, "cli", 1024) if args.cef_dir else None
    settings = {"first_lookback_min": args.first_lookback_min, "overlap_min": args.overlap_min,
                "max_lookback_hours": args.max_lookback_hours}
    try:
        summary = collect(api, settings, State(args.state_dir), emit if sink else None, cef,
                          None if args.no_geocode else geo.Geocoder())
    except api_mod.ApiError as exc:
        sys.exit(f"failed: {exc}")
    finally:
        if sink:
            sink.close()
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scheme":
        modinput_scheme()
    elif len(sys.argv) > 1 and sys.argv[1] == "--validate-arguments":
        modinput_validate()
    elif len(sys.argv) == 1:
        modinput_run()
    else:
        main()
