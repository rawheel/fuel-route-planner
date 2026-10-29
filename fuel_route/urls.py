from django.urls import path

from . import views

urlpatterns = [
    path("route/", views.TripPlanView.as_view(), name="trip-plan"),
    path("route/map/", views.trip_map, name="trip-map"),
]
