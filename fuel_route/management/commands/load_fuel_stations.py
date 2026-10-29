import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel_route.models import FuelStation
from fuel_route.services.geocoding import US_STATES, get_place_index
from fuel_route.services.stations import reset_station_index


class Command(BaseCommand):
    help = "Load the fuel price CSV into the database, geocoding stations offline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv", dest="csv_path", type=Path, default=settings.FUEL_PRICES_CSV
        )

    def handle(self, *args, csv_path: Path, **options):
        if not csv_path.exists():
            raise CommandError(f"{csv_path} does not exist")

        index = get_place_index()
        stations: dict[int, FuelStation] = {}
        rows = non_us = 0
        ungeocoded = set()
        with csv_path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                rows += 1
                city, state = row["City"].strip(), row["State"].strip().upper()
                if state not in US_STATES:
                    non_us += 1  # The file also lists Canadian truck stops.
                    continue
                point = index.lookup(city, state)
                if point is None:
                    ungeocoded.add((city, state))
                    continue
                try:
                    price = Decimal(row["Retail Price"].strip())
                except InvalidOperation:
                    continue
                opis_id = int(row["OPIS Truckstop ID"])
                # A station appears once per supplier rack; keep its cheapest price.
                if opis_id in stations and stations[opis_id].retail_price <= price:
                    continue
                rack_id = row["Rack ID"].strip()
                stations[opis_id] = FuelStation(
                    opis_id=opis_id,
                    name=row["Truckstop Name"].strip(),
                    address=row["Address"].strip(),
                    city=city,
                    state=state,
                    rack_id=int(rack_id) if rack_id.isdigit() else None,
                    retail_price=price,
                    latitude=point[0],
                    longitude=point[1],
                )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations.values(), batch_size=1000)
        reset_station_index()

        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(stations)} stations from {rows} rows "
                f"({non_us} non-US rows skipped, duplicates collapsed to the lowest price)."
            )
        )
        if ungeocoded:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped stations in {len(ungeocoded)} cities that could not be "
                    f"geocoded: {sorted(ungeocoded)}"
                )
            )
