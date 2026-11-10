# Nikhil Chawla Add-on for AirWatch (Workspace ONE UEM)

Version 1.0.1. App ID (folder name): `TA-airwatch-uem`.

Collects **device GPS locations** from Workspace ONE UEM (AirWatch) over its REST API.
Each sample gets the device's serial number, friendly name, user name and e-mail
address, and the **nearest city** (offline, from the bundled GeoNames city list).
Per input, the samples go:

* to Splunk as JSON events, sourcetype `airwatch:gps`, with `_time` = the sample's time;
* and/or to one CEF file per run, for a syslog agent to read.

Everything is managed in Splunk Web:

* **Inputs** page: OAuth clients (token URL, client ID, client secret, optional proxy
  and CA file) and inputs (one per console): add, edit, test, enable/disable, run now,
  delete. Client secrets are stored encrypted in Splunk's credential store and never
  shown again.
* **Status** dashboard (classic Simple XML): last run per input, devices and samples
  per run, errors, indexed samples per hour and how old samples are when indexed. It
  carries its own searches, so it also works pasted onto a search head without the
  add-on.

> Independent project, not affiliated with or endorsed by the product's vendor.
> AirWatch and Workspace ONE are trademarks of their owners. See NOTICE.

## Requirements

* Splunk Enterprise 9.x or 10.x **heavy forwarder** on Linux or macOS. Runs on Splunk's
  bundled Python (3.13 where available, else 3.9) with the standard library only.
* Network: HTTPS (443) from the forwarder to the OAuth token URL and to the console's
  REST API server, directly or through an HTTP proxy. Certificates are checked against
  the server's trust store (`/etc/pki/tls/certs/ca-bundle.crt` and the like), or a CA
  file set on the OAuth client (for a proxy that inspects TLS).
* Console: an **OAuth client** (Groups & Settings > Configurations > OAuth Client
  Management > Add) whose role can read devices through the REST API, at the
  organization group whose devices you want.
* Search heads: optional. The add-on's search-time settings (CIM fields, `lat`/`lon`)
  are useful there; install it with no inputs. Without it the events are still JSON and
  every field is extracted.

## Install

1. Install the add-on on the heavy forwarder (Apps > Manage Apps > Install app from file,
   or untar into `$SPLUNK_HOME/etc/apps`) and restart Splunk.
2. Optionally install it on the search heads too (no inputs there).
3. Inputs are saved in this add-on's `local/inputs.conf`. A deployment server that pushes
   the add-on would replace them, so manage it on each forwarder directly.

## Set up a console

1. Open the add-on and go to **Inputs**.
2. **Add OAuth client**: a name, the token URL of the console's region (for example
   `na.uemauth.workspaceone.com/connect/token`; older consoles use
   `uemauth.vmwservices.com`), the client ID and secret, and optionally a proxy
   (`host:port`) and a CA file. With *Get a token before saving* (the default), it is
   saved only if the console accepts it.
3. **Add input**:
   * name, OAuth client, **API server** (Groups & Settings > All Settings > System >
     Advanced > Site URLs > REST API URL, without `/API`);
   * console name: the events' `host` (default: the API server's name);
   * **schedule**: seconds between runs (default 3600) or a cron schedule, for example
     `1 * * * *` (minute 1 of every hour);
   * output: *Index in Splunk* (and the index) and/or *Write CEF* (and the CEF folder).
4. **Test** (on the input's row, once it is added): gets a token and asks for the
   device count with the input's settings. The result stays on the page until you
   click it.
5. The first run starts within a minute (schedules in seconds) and asks for the last
   60 minutes. The Status column shows the last run: devices, samples, new ones.

## Events

sourcetype `airwatch:gps`, one JSON object per sample, host = the console name, source
= `airwatch_gps://<input>`, `_time` = `SampleTime` (UTC).

| Field | What |
|---|---|
| `SampleTime`, `OriginalEventTime` | when the device took the sample (UTC, `10/22/2026 6:51:04 PM`) |
| `Latitude`, `Longitude`, `Elevation`, `Speed` | the device's position, as the console reports it |
| `DeviceId`, `Id` | the device's ID (`Id` repeats it, as earlier exports of these events did) |
| `identifier` | the sample's own ID |
| `SerialNumber`, `DeviceFriendlyName`, `UserName`, `UserEmailAddress` | from the device list |
| `country_code`, `city`, `latitude`, `longitude`, `population`, `state`, `county`, `country` | the nearest city (GeoNames); `latitude`/`longitude` are the city's. `county` is missing where GeoNames has no second-level division |

The field names and their order are those of earlier exports of these events, so
existing searches keep working. Country names follow GeoNames and can change between
releases (1.0.1 changed 26 of them, for example `Russia`, `South Korea`); search on
`country_code` to be safe.

Search time (where the add-on is installed): `user` (= `UserName`), `dest`
(= `DeviceFriendlyName`), `vendor`, `product`, `vendor_product`, and `lat`/`lon`
(= the device's `Latitude`/`Longitude`, the names `geostats` uses by default):

    sourcetype="airwatch:gps" | geostats count by city

No CIM data model covers device locations, so the add-on sets no event types or tags.

**These events hold the positions of named people.** Limit who can search the index.

## CEF output

With *Write CEF* on, each run writes one file,
`<CEF folder>/<input>/<input>_<YYYY-MM-DD>_<HHMMSS>_w0.cef` (UTC), one line per sample:

    CEF:0|Workspace_one|Airwatch|1.0||||SampleTime=... Latitude=... ... UserEmailAddress=...

* Header and extension keys are those of earlier exports of these events, so parsers
  written for those keep matching. Values are escaped as CEF requires (`\=`, `\\`).
* Files are written under a hidden name and renamed when complete, so a reader never
  sees half a file. A syslog agent reads them (for example rsyslog `imfile` on
  `<CEF folder>/<input>/*.cef`), adds the syslog header and deletes them once read.
* The add-on deletes them only when the input's CEF files reach *CEF folder cap*
  (default 1024 MB): the oldest go first, and the run's log says so.
* *CEF folder* default: `$SPLUNK_HOME/var/log/airwatch_gps`. With SELinux, a folder under
  `/var/log` (for example `/var/log/airwatch_gps`) lets rsyslog read it. The user Splunk
  runs as must be able to write there.

## How it runs

* Modular input `airwatch_gps`. Splunk runs it on the input's schedule; each run:
  1. gets an OAuth token (client credentials);
  2. reads the device list, asking for 1,000 devices per request (some consoles return
     500; the add-on follows the page size the console reports);
  3. asks for the GPS samples of those devices since the window start, in one request.
     The console returns **one sample per device: its latest since the window start**.
     A device that reported several times between two runs appears once, with its
     latest position, so a shorter schedule gives more positions per device;
  4. adds each sample's device fields and nearest city;
  5. writes the CEF file, then sends the events to Splunk, then saves the checkpoint.
* **Window**: from the end of the last successful run, minus *Overlap* (default 60 min).
  After a late, failed or skipped run, the next run still gets each device's latest
  position since the last good run; the positions in between are not available from the
  console. A sample a device uploads late (the console filters on the time a sample was
  taken) is collected when it is still the device's newest. Samples the overlap returns
  again are recognised by their ID and skipped. The first run looks back *First run:
  look back* (default 60 min); after a long stop, a run goes back at most *Max look
  back* (default 24 h) and says what it skipped.
* A failed run (console unreachable, token refused, empty device list, disk full) is
  logged and changes nothing; the next run starts from the last good one. The
  checkpoint is saved only after the CEF file is written and the events are handed to
  Splunk. Network errors, HTTP 429 and 5xx
  are retried three times (5, 15, 45 s).
* Only one run per input at a time: a run that starts while the previous one is still
  going is skipped.
* Read-only towards the console: token, device search and GPS search. Nothing is
  changed there.

## Files

| Path | What |
|---|---|
| `bin/airwatch_gps.py` | the collector (modular input and command line) |
| `bin/airwatch_gps_api.py` | the REST calls (token, device search, GPS search) |
| `bin/airwatch_gps_geo.py` | nearest city, offline |
| `bin/airwatch_gps_store.py` | credential store, CEF files, status |
| `bin/airwatch_gps_rest.py` | REST API for the Inputs page |
| `lookups/airwatch_gps_cities.csv.gz`, `airwatch_gps_countries.csv` | GeoNames places (population 1,000+, and seats of administrative divisions) and country names |
| `$SPLUNK_HOME/var/lib/splunk/modinputs/airwatch_gps/<input>/state.json` | checkpoint: end of the last good run, the overlap's sample IDs, the last run's summary |
| `$SPLUNK_HOME/var/log/splunk/airwatch_gps_<input>.log` | collector log (index `_internal`, sourcetype `airwatch:gps:log`); 10 MB, 3 old copies |
| `$SPLUNK_HOME/var/log/splunk/airwatch_gps-webapi.log` | changes made on the Inputs page (sourcetype `airwatch:gps:webapi`) |

Each run ends with one line:

`run done: devices 4210 (9 pages), samples 312, new 298, repeated 14, without device 0, without location 0, splunk 298, cef 298 (uem_2026-10-22_190154_w0.cef), window 2026-10-22T17:01:54Z..2026-10-22T19:01:54Z, took 38.6 s`

* *repeated*: samples already written by an earlier run (the overlap).
* A device list shorter than the console's own count is read again once; if it is
  still short, the run says so and continues.
* *without device*: samples of a device that is not in the device list (still written,
  without the device fields).
* *without location*: no usable latitude/longitude (or 0,0), so no nearest city.

## Command line

One run by hand, for a test (the secret comes from the environment, never the command
line; use a test state folder, not the input's):

    export AIRWATCH_CLIENT_SECRET='...'
    $SPLUNK_HOME/bin/splunk cmd python3 $SPLUNK_HOME/etc/apps/TA-airwatch-uem/bin/airwatch_gps.py \
        --api-url https://asNNN.awmdm.com --token-url https://na.uemauth.workspaceone.com/connect/token \
        --client-id <id> --state-dir /tmp/aw-test/state --json-out /tmp/aw-test/events.json

It prints the run summary. `--help` lists every option.

## Troubleshooting

* Status *Waiting for the first run* for more than a few minutes: see the Status
  dashboard's panel "Splunk messages about the inputs".
* *OAuth token: HTTP 400 ... invalid_client*: wrong client ID or secret, the client is
  disabled, or the token URL is for another region. Edit the OAuth client.
* *HTTP 401 / 403* on the device list or GPS search: the OAuth client's role cannot read
  devices, or not at this organization group.
* *the server's certificate could not be verified*: a proxy inspects TLS. Set the OAuth
  client's CA file to its CA certificate (PEM).
* *Connection refused / timed out*: firewall or proxy. Set the proxy on the OAuth
  client.
* *the device list is empty*: the OAuth client's role does not see devices at its
  organization group. The checkpoint stays: the next run asks for the same window.
* The Inputs page and its REST API need the `admin_all_objects` capability (admin).
* *the last run ended N h ago; going back 24 h only*: the input was stopped for longer
  than *Max look back*; raise it to collect more on the next run.
* *CEF folder over N MB*: the syslog agent is not reading or deleting the CEF files.

## Release notes

**1.0.1**
* City data rebuilt from the current GeoNames dump (171,579 places); populations are
  updated. 1.0.0's country names came from a list bundled with a Python package (not
  GeoNames, as its NOTICE said); 1.0.1 uses GeoNames' own. **26 country names change**,
  for example `Russian Federation` → `Russia`, `Korea, Republic of` → `South Korea`,
  `Netherlands` → `The Netherlands`, `Czech Republic` → `Czechia`. Searches on `country`
  for these need updating; `country_code` is unchanged.
* Documented that the console returns one sample per device per run (its latest), what
  that means after a failed run, and that some consoles return 500 devices per page.
* Inputs page: Test results stay on the page until clicked.
* The input spec no longer contains text splunkd reads as a setting (a warning in
  `splunkd.log` at startup).

**1.0.0**
* First release.

## License and support

Apache License 2.0 (see LICENSE and NOTICE). Copyright 2026 Nikhil Chawla. City data:
GeoNames, CC BY 4.0. Community-supported, best effort, no warranty.
