[airwatch_gps://<name>]
* One input per Workspace ONE UEM (AirWatch) console. <name>: letters, digits,
  '.', '_' or '-' (max 64), starting with a letter or digit.
* Created and edited in Splunk Web: app "Omnissa Workspace ONE UEM (AirWatch) Device Location (GPS) Add-on" > Inputs (or Settings > Data inputs > AirWatch device
  GPS).
* Runs on the input's schedule (interval: seconds, or a cron schedule such as
  "1 * * * *"; default 3600). Each run collects the device GPS samples since the
  previous run and exits: for Splunk as JSON events (sourcetype airwatch:gps,
  event time taken from SampleTime) and/or as one CEF file per run for a syslog
  agent. The console returns one sample per device per run: its latest since
  the window start.
* Read-only towards the console: an OAuth token, the device list
  (/API/mdm/devices/search) and the GPS search (/API/mdm/devices/gps/search).

client = <string>
* Required. Name of the OAuth client set up on the app's Inputs page: token
  URL, client ID, client secret (stored encrypted in the credential store,
  realm airwatch_gps_client), optional proxy and CA file.

api_url = <string>
* Required. The console's REST API server: https://<host>, without /API
  (Groups & Settings > All Settings > System > Advanced > Site URLs > REST API
  URL).

console_name = <string>
* Stored as the host field of this input's events.
* Default: empty (the API server's host name)

index_in_splunk = <boolean>
* Send the samples to Splunk as JSON events, sourcetype airwatch:gps.
* Default: 1

write_cef = <boolean>
* Also write one CEF file per run, <cef_dir>/<name>/<name>_<YYYY-MM-DD>_<HHMMSS>_w0.cef
  (UTC), one CEF line per sample, for a syslog agent (for example rsyslog
  imfile) to read and delete. The add-on does not index or delete them, except
  when they reach cef_max_mb.
* At least one of index_in_splunk and write_cef must be 1.
* Default: 0

cef_dir = <string>
* Absolute folder for the CEF files, one subfolder per input. The user Splunk
  runs as must be able to write there.
* Not inside $SPLUNK_HOME/etc, var/lib, var/log/splunk, var/spool or var/run.
* Default: empty ($SPLUNK_HOME/var/log/airwatch_gps)

cef_max_mb = <integer>
* When this input's CEF files reach this size (the syslog agent is not reading
  them), the oldest are deleted (1-102400).
* Default: 1024

first_lookback_min = <integer>
* How far back the first run (no checkpoint yet) asks for samples, in minutes
  (1-10080).
* Default: 60

overlap_min = <integer>
* Each run starts this many minutes before the previous run ended (0-1440), so
  a sample a device uploads late is still collected when it is still the
  device's newest: the console filters on the time a sample was taken, not when
  it arrived. Samples already written are recognised by their ID and skipped.
* Default: 60

max_lookback_hours = <integer>
* After a long stop, a run goes back at most this many hours (1-720). Older
  samples are skipped, and the run says so in its log.
* Default: 24

geocode = <boolean>
* Add the nearest city to each sample (country_code, city, latitude,
  longitude, population, state, county, country) from the bundled GeoNames
  city list. latitude/longitude are the city's; Latitude/Longitude stay the
  device's.
* Default: 1

log_level = DEBUG|INFO|WARNING|ERROR
* Default: INFO
