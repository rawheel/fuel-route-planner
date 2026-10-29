"""One-off data preparation. The outputs are committed, so this never runs at request time.

1. Downloads the US Census 2024 gazetteer (places + county subdivisions) and
   writes ``data/us_places.csv``: one ``name,state,lat,lon`` row per place, in
   priority order (incorporated places/CDPs first, larger land area first).
2. Finds fuel-station cities the gazetteer can't resolve (mostly unincorporated
   communities) and looks them up once via Nominatim, writing
   ``data/geocode_overrides.csv``.
"""

import csv
import io
import re
import time
import zipfile

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from fuel_route.services.geocoding import US_STATES, get_place_index

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
    "2024_Gaz_{kind}_national.zip"
)
# Legal/statistical area descriptions appended to gazetteer names.
LSAD_SUFFIX = re.compile(
    r"\s+(city and borough|consolidated government|metropolitan government|"
    r"unified government|urban county|charter township|city|town|township|village|"
    r"cdp|borough|municipality|plantation|ccd)$",
    re.IGNORECASE,
)


def clean_gazetteer_name(name: str) -> str:
    # Strip exactly one descriptor: "Kansas City city" must stay "Kansas City".
    return LSAD_SUFFIX.sub("", name.replace("(balance)", "").strip()).strip()


class Command(BaseCommand):
    help = "Rebuild data/us_places.csv and data/geocode_overrides.csv (needs network)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-gazetteer",
            action="store_true",
            help="Reuse the existing us_places.csv and only refresh overrides.",
        )

    def handle(self, *args, skip_gazetteer=False, **options):
        session = requests.Session()
        session.headers["User-Agent"] = settings.EXTERNAL_API_USER_AGENT
        if not skip_gazetteer:
            self.build_places(session)
        get_place_index.cache_clear()
        self.build_overrides(session)
        get_place_index.cache_clear()

    def build_places(self, session):
        places = []
        for rank, kind in enumerate(("place", "cousubs")):
            self.stdout.write(f"Downloading gazetteer: {kind}")
            response = session.get(GAZETTEER_URL.format(kind=kind), timeout=120)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                text = archive.read(archive.namelist()[0]).decode("utf-8")
            reader = csv.reader(io.StringIO(text), delimiter="\t")
            header = [column.strip() for column in next(reader)]
            for values in reader:
                row = dict(zip(header, (v.strip() for v in values)))
                name = clean_gazetteer_name(row["NAME"])
                if not name or row["USPS"] not in US_STATES:
                    continue
                places.append(
                    (rank, -float(row["ALAND"]), name, row["USPS"],
                     row["INTPTLAT"], row["INTPTLONG"])
                )  # fmt: skip
        places.sort()
        path = settings.DATA_DIR / "us_places.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "state", "lat", "lon"])
            writer.writerows(p[2:] for p in places)
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(places)} places to {path}"))

    def build_overrides(self, session):
        path = settings.DATA_DIR / "geocode_overrides.csv"
        index = get_place_index()
        with settings.FUEL_PRICES_CSV.open(newline="", encoding="utf-8-sig") as f:
            cities = sorted(
                {
                    (row["City"].strip(), row["State"].strip())
                    for row in csv.DictReader(f)
                    if row["State"].strip() in US_STATES
                }
            )
        missing = [c for c in cities if index.lookup(*c) is None]
        self.stdout.write(f"{len(missing)} station cities need Nominatim lookups")

        resolved, unresolved = [], []
        for city, state in missing:
            # Structured search first, then free text (handles "Pueblo Of Acoma").
            for params in (
                {"city": city, "state": state, "country": "us"},
                {"q": f"{city}, {state}", "countrycodes": "us"},
            ):
                time.sleep(1.1)  # Nominatim usage policy: max 1 request/second.
                response = session.get(
                    f"{settings.NOMINATIM_BASE_URL}/search",
                    params={**params, "format": "jsonv2", "limit": 1},
                    timeout=30,
                )
                response.raise_for_status()
                if results := response.json():
                    resolved.append((city, state, results[0]["lat"], results[0]["lon"]))
                    break
            else:
                unresolved.append((city, state))

        # Keep previously resolved overrides (including hand-added ones).
        existing = []
        if path.exists():
            with path.open(newline="", encoding="utf-8") as f:
                existing = [
                    (r["name"], r["state"], r["lat"], r["lon"]) for r in csv.DictReader(f)
                ]
        rows = {(r[0], r[1]): r for r in existing + resolved}
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "state", "lat", "lon"])
            writer.writerows(sorted(rows.values()))
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} overrides to {path}"))
        if unresolved:
            self.stdout.write(self.style.WARNING(f"Still unresolved: {unresolved}"))
