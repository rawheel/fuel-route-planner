import time
from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import TripRequestSerializer
from .services.geocoding import GeocodingError, GeocodingServiceError
from .services.optimizer import NoFuelInRangeError
from .services.planner import TripPlan, plan_trip
from .services.routing import NoRouteError, RoutingError
from .services.stations import StationsNotLoadedError

# Most specific first: the service errors subclass the input errors.
ERROR_STATUS = (
    (GeocodingServiceError, status.HTTP_502_BAD_GATEWAY),
    (GeocodingError, status.HTTP_400_BAD_REQUEST),
    (NoRouteError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (NoFuelInRangeError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (RoutingError, status.HTTP_502_BAD_GATEWAY),
    (StationsNotLoadedError, status.HTTP_503_SERVICE_UNAVAILABLE),
)


class PlanningFailed(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _plan(**params) -> TripPlan:
    try:
        return plan_trip(params["start"], params["finish"], params.get("stop_penalty"))
    except tuple(error for error, _ in ERROR_STATUS) as exc:
        code = next(code for error, code in ERROR_STATUS if isinstance(exc, error))
        raise PlanningFailed(str(exc), code) from exc


class TripPlanView(APIView):
    """Plan the cheapest fuel stops for a drive between two US locations.

    `GET /api/route/?start=Chicago, IL&finish=Denver, CO` or `POST` the same
    fields as JSON.
    """

    def get(self, request):
        return self.plan(request, request.query_params)

    def post(self, request):
        return self.plan(request, request.data)

    def plan(self, request, data):
        started = time.perf_counter()
        serializer = TripRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        try:
            trip = _plan(**serializer.validated_data)
        except PlanningFailed as exc:
            return Response({"detail": str(exc)}, status=exc.status_code)

        body = trip.as_dict()
        query = urlencode(serializer.validated_data)
        body["map_url"] = request.build_absolute_uri(f"{reverse('trip-map')}?{query}")
        body["response_time_ms"] = round((time.perf_counter() - started) * 1000)
        return Response(body)


def trip_map(request):
    """Interactive Leaflet map of a planned trip (reuses cached geocoding/routing)."""
    serializer = TripRequestSerializer(data=request.GET)
    if not serializer.is_valid():
        return render(
            request,
            "fuel_route/map.html",
            {"error": "Pass ?start=...&finish=... to see a trip."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        trip = _plan(**serializer.validated_data)
    except PlanningFailed as exc:
        return render(
            request, "fuel_route/map.html", {"error": str(exc)}, status=exc.status_code
        )

    plan = trip.as_dict()
    plan["stations_considered"] = [
        [s.station.latitude, s.station.longitude, round(s.price, 3), s.station.name]
        for s in trip.candidates
    ]
    return render(request, "fuel_route/map.html", {"plan": plan})
