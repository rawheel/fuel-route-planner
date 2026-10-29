from rest_framework import serializers


class TripRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=200,
        help_text='Origin in the USA: "City, ST", a street address, or "lat,lon".',
    )
    finish = serializers.CharField(max_length=200, help_text="Destination in the USA.")
    stop_penalty = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=1000,
        help_text="USD cost assigned to each fuel stop when optimising. 0 = cheapest "
        "fuel regardless of how many stops it takes.",
    )
