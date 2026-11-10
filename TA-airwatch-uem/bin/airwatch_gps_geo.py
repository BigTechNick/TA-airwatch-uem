# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Nikhil Chawla
"""
airwatch_gps_geo.py - offline reverse geocoding: latitude/longitude -> nearest city.

Data: lookups/airwatch_gps_cities.csv.gz (GeoNames cities1000 with the names of
their first- and second-level administrative divisions) and
lookups/airwatch_gps_countries.csv (country code -> name). See NOTICE.

The nearest city is the one with the smallest straight-line distance in degrees
of latitude and longitude, the same measure as a k-d tree built on the raw
coordinates. Cities are kept in 1 x 1 degree cells; a lookup searches rings of
cells around the point until no unsearched cell can hold a closer city.

Returned fields (only those the city has): country_code, city, latitude,
longitude (the city's own coordinates), population, state, county, country.

Standard library only.
"""
import csv
import gzip
import io
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LOOKUPS = os.path.join(os.path.dirname(HERE), "lookups")
CITIES = os.path.join(LOOKUPS, "airwatch_gps_cities.csv.gz")
COUNTRIES = os.path.join(LOOKUPS, "airwatch_gps_countries.csv")


class Geocoder:
    def __init__(self, cities_path=CITIES, countries_path=COUNTRIES):
        self.cells = {}
        self.cities = []
        with gzip.open(cities_path, "rb") as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
            for row in reader:
                lat, lon = float(row["latitude"]), float(row["longitude"])
                city = {"country_code": row["country_code"], "city": row["city"], "latitude": lat,
                        "longitude": lon, "population": int(row["population"] or 0)}
                if row.get("state"):
                    city["state"] = row["state"]
                if row.get("county"):
                    city["county"] = row["county"]
                index = len(self.cities)
                self.cities.append(city)
                self.cells.setdefault((math.floor(lat), math.floor(lon)), []).append(index)
        self.countries = {}
        with open(countries_path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                self.countries[row["country_code"]] = row["country"]
        lats = [k[0] for k in self.cells]
        lons = [k[1] for k in self.cells]
        # Rings beyond this cover no cell that has cities: the search can stop.
        self.max_ring = max(max(lats) - min(lats), max(lons) - min(lons)) + 2

    def nearest(self, lat, lon):
        """Index of the nearest city (lowest index on an exact tie), or None when there are none."""
        ci, cj = math.floor(lat), math.floor(lon)
        best, best_d2 = None, float("inf")
        ring = 0
        while ring <= self.max_ring:
            for i in range(ci - ring, ci + ring + 1):
                edge = i in (ci - ring, ci + ring)
                step = 1 if edge else 2 * ring        # inside the ring: only its left and right cells
                for j in range(cj - ring, cj + ring + 1, step if step else 1):
                    for index in self.cells.get((i, j), ()):
                        city = self.cities[index]
                        d2 = (city["latitude"] - lat) ** 2 + (city["longitude"] - lon) ** 2
                        if d2 < best_d2 or (d2 == best_d2 and index < best):
                            best, best_d2 = index, d2
            # Every city in a cell outside this ring is at least `ring` degrees away
            # (in latitude or longitude) from any point of the centre cell.
            if best is not None and best_d2 <= ring * ring:
                break
            ring += 1
        return best

    def lookup(self, lat, lon):
        """Fields of the nearest city as a new dict, or None."""
        index = self.nearest(lat, lon)
        if index is None:
            return None
        found = dict(self.cities[index])
        found["country"] = self.countries.get(found["country_code"], "")
        return found


def coordinates(latitude, longitude):
    """(lat, lon) floats from the API's text values, or None when they are not usable."""
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon
