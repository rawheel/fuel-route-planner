from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price file, geocoded to its city's coordinates."""

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=8, decimal_places=5)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        ordering = ["state", "city", "name"]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
