# Omnissa Workspace ONE UEM (AirWatch) Device Location (GPS) Add-on

Splunk add-on `TA-airwatch-uem`, version 1.0.2.

Collects device GPS locations from Workspace ONE UEM (AirWatch) with each device's user and the nearest city, for Splunk (sourcetype airwatch:gps) and/or as CEF files. OAuth clients, inputs and status are managed in Splunk Web. Not affiliated with the product's vendor.

## Layout

- `TA-airwatch-uem/`: the app exactly as shipped to Splunkbase (unpacked from the release package)
- `tools/package.py`: builds the Splunkbase package and runs offline AppInspect-style checks
- `docs/`: Splunkbase listing text and screenshots

## Build

```
python3 tools/package.py TA-airwatch-uem dist --vendor omnissa
```

Good: `RESULT: OK`. Then AppInspect:

```
splunk-appinspect inspect dist/TA-airwatch-uem-<version>.tgz --mode precert --included-tags cloud
```

## Packages

`packages/` holds each version's package exactly as uploaded to Splunkbase, with `MD5SUMS`.
Each version is also its own commit (`git log`).

## License

Apache-2.0. Independent project, not affiliated with or endorsed by Omnissa.
