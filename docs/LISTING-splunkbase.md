# Splunkbase listing text — Nikhil Chawla Add-on for AirWatch (Workspace ONE UEM)

Drafted 2026-10-22 for the 1.0.1 submission. App ID `TA-airwatch-uem`. Category: Security, Fraud & Compliance. License: Apache-2.0. Screenshots: `1-inputs.png`, `2-add-input.png` (sample data, no real console).

## Summary

```
Collects device GPS locations from Workspace ONE UEM (AirWatch) over its REST API and indexes them as JSON events, sourcetype airwatch:gps. Each sample gets the device's serial number, friendly name, user name and e-mail address, and the nearest city, county, state and country from a bundled GeoNames city list (offline, no lookups to outside services).

- One input per console, several consoles per heavy forwarder
- Inputs page in Splunk Web: add OAuth clients (client secrets stored encrypted in Splunk's credential store) and inputs; test, enable, disable, and run now (for schedules in seconds)
- Status dashboard: last run per input, devices and samples per run, errors, indexed samples per hour
- Optional CEF output: one CEF file per run, for a syslog agent to forward to another SIEM
- Each run picks up where the last good run ended, with an overlap; each sample is written once
- Read-only towards the console: an OAuth token, the device list and the GPS search
- Python standard library only, no third-party packages

These events hold the positions of named people. Limit who can search the index.

Independent project, not affiliated with or endorsed by the product's vendor. AirWatch and Workspace ONE are trademarks of their owners. City data: GeoNames, CC BY 4.0.
```

## Short description

```
Device GPS locations from Workspace ONE UEM (AirWatch), with each device's user and the nearest city, as JSON events in Splunk (sourcetype airwatch:gps) and optionally as CEF files. OAuth clients, inputs and status are managed in Splunk Web. Independent project, not affiliated with the product's vendor.
```

## Details

```
Events
- sourcetype airwatch:gps, one JSON object per sample. _time: when the device took the sample (SampleTime, UTC).
- host: the input's console name (default: the API server's host name). source: airwatch_gps://<input>.
- Fields: SampleTime, OriginalEventTime (= SampleTime), Latitude, Longitude, Elevation, Speed, DeviceId, Id, identifier (the sample's ID), SerialNumber, DeviceFriendlyName, UserName, UserEmailAddress, and the nearest city: country_code, city, latitude, longitude, population, state, county, country.
- Search time (where the add-on is installed): user, dest, vendor, product, vendor_product, and lat/lon for geostats.

How it runs
- Modular input airwatch_gps, on a schedule (seconds, or cron). Each run gets an OAuth token, reads the device list, asks for the GPS samples since the last run in one request, adds the device fields and the nearest city, sends the events to Splunk and saves a checkpoint.
- The console returns one sample per device per run: its latest since the window start. A shorter schedule gives more positions per device.
- Window: from the end of the last good run minus an overlap (default 60 minutes). After a failed or skipped run, the next run still gets each device's latest position since the last good run; a late upload is collected when it is still the device's newest sample. Samples seen in the overlap are skipped by their ID.
- Country names follow GeoNames and can change between releases; search on country_code to be safe.
- A failed run (console unreachable, token refused, empty device list) changes nothing; the next run starts from the last good one. Network errors, HTTP 429 and 5xx are retried.
- HTTPS with certificate checks against the server's trust store, or a CA file set on the OAuth client. Optional HTTP proxy per OAuth client.

CEF output (optional)
- One file per run, <CEF folder>/<input>/<input>_<date>_<time>_w0.cef, one CEF line per sample, written under a hidden name and renamed when complete.
- A syslog agent (for example rsyslog imfile) reads and deletes the files. A size cap deletes the oldest files when nothing reads them.

Logs (index _internal)
- $SPLUNK_HOME/var/log/splunk/airwatch_gps_<input>.log (sourcetype airwatch:gps:log): one "run done" line per run with devices, samples, new, repeated and time taken.
- airwatch_gps-webapi.log: changes made on the Inputs page.
```

## Installation

```
Requirements
- Splunk Enterprise 9.x or 10.x heavy forwarder on Linux or macOS
- HTTPS (443) from the forwarder to the OAuth token URL of the console's region and to the console's REST API server, directly or through a proxy
- In the console: an OAuth client (Groups & Settings > Configurations > OAuth Client Management) with a role that can read devices, at the organization group whose devices you want

In Splunk
1. Install the add-on on the heavy forwarder and restart Splunk.
2. Optionally install it on your search heads too, without inputs, for the search-time fields and the Status dashboard.
3. Open the add-on, go to Inputs and choose Add OAuth client: a name, the token URL (for example na.uemauth.workspaceone.com/connect/token), the client ID and secret, and optionally a proxy and CA file. With "Get a token before saving" ticked (the default), it is saved only after the console issues a token.
4. Choose Add input: name, OAuth client, API server (Groups & Settings > All Settings > System > Advanced > Site URLs > REST API URL, without /API), schedule, index, and optionally Write CEF with a CEF folder.
5. Click Test on the input's row: it reports the console's device count. The first run starts within a minute and collects the last 60 minutes.

Notes
- Inputs are saved in the add-on's local/inputs.conf. A deployment server that pushes the add-on would replace them, so manage it on each forwarder directly.
- With SELinux, put the CEF folder under /var/log so rsyslog can read it.
- Upgrades: install over the old version and restart. OAuth clients, inputs and checkpoints are kept.
```

## Troubleshooting

```
Start with the collector log: $SPLUNK_HOME/var/log/splunk/airwatch_gps_<input>.log.

- Status stays "Waiting for the first run": see the Status dashboard panel "Splunk messages about the inputs".
- "OAuth token: HTTP 400 ... invalid_client": wrong client ID or secret, the client is disabled, or the token URL is for another region.
- HTTP 401 or 403 on the device list or GPS search: the OAuth client's role cannot read devices at this organization group.
- "the server's certificate could not be verified": a proxy inspects TLS. Set the OAuth client's CA file to its CA certificate (PEM).
- "the device list is empty": the OAuth client's role sees no devices. The checkpoint stays; the next run asks for the same window.
- "the device list had X of Y devices": devices moved between pages while being read. The list is read again once.
- "CEF folder over N MB": the syslog agent is not reading or deleting the CEF files.
- The Inputs page needs the admin_all_objects capability.
```

## Release notes (1.0.1)

```
- City data rebuilt from the current GeoNames dump (171,579 places); populations are updated.
- Country names now come from GeoNames itself (1.0.0 used a list bundled with a Python package). 26 names change, for example Russian Federation -> Russia, Korea, Republic of -> South Korea, Czech Republic -> Czechia. Searches on country for these need updating; country_code is unchanged.
- Documented that the console returns one sample per device per run (its latest), what that means after a failed run, and that some consoles return 500 devices per page.
- Inputs page: Test results stay on the page until clicked.
- The input spec no longer contains text splunkd reads as a setting.
```
