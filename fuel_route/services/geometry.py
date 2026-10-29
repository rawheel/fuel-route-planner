"""Vectorised geometry helpers for working with route polylines.

Coordinates are always ``(lat, lon)`` numpy arrays of shape ``(N, 2)`` in
degrees, except for GeoJSON output, which is ``[lon, lat]`` by spec.
"""

import numpy as np

EARTH_RADIUS_MILES = 3958.7613


def decode_polyline(encoded: str, precision: int = 6) -> np.ndarray:
    """Decode a Google encoded polyline (OSRM ``polyline6`` by default)."""
    values = []
    value = shift = 0
    for char in encoded:
        byte = ord(char) - 63
        value |= (byte & 0x1F) << shift
        shift += 5
        if byte < 0x20:
            values.append(~(value >> 1) if value & 1 else value >> 1)
            value = shift = 0
    coords = np.cumsum(np.asarray(values, dtype=np.float64).reshape(-1, 2), axis=0)
    return coords / 10**precision


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def segment_miles(coords: np.ndarray) -> np.ndarray:
    """Length of each consecutive segment of a polyline."""
    return haversine_miles(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])


def cumulative_miles(coords: np.ndarray) -> np.ndarray:
    """Distance along the polyline at each vertex, starting at 0."""
    return np.concatenate(([0.0], np.cumsum(segment_miles(coords))))


def densify(coords: np.ndarray, max_step_miles: float) -> np.ndarray:
    """Insert interpolated vertices so no segment is longer than ``max_step_miles``.

    Long straight highway segments can have vertices miles apart; densifying
    lets a nearest-vertex lookup stand in for an exact point-to-segment
    projection.
    """
    if len(coords) < 2:
        return coords
    pieces = np.maximum(np.ceil(segment_miles(coords) / max_step_miles).astype(int), 1)
    if pieces.max() == 1:
        return coords
    starts = np.repeat(coords[:-1], pieces, axis=0)
    ends = np.repeat(coords[1:], pieces, axis=0)
    # Fraction along each segment: 0, 1/k, ..., (k-1)/k
    offsets = np.arange(pieces.sum()) - np.repeat(np.cumsum(pieces) - pieces, pieces)
    t = (offsets / np.repeat(pieces, pieces))[:, None]
    return np.vstack((starts + (ends - starts) * t, coords[-1:]))


def to_unit_vectors(lat, lon) -> np.ndarray:
    """Project lat/lon onto the unit sphere so Euclidean KD-trees work globally."""
    lat, lon = np.radians(lat), np.radians(lon)
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def miles_to_chord(miles):
    """Great-circle distance in miles -> chord length on the unit sphere."""
    return 2 * np.sin(np.asarray(miles) / (2 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2, 0.0, 1.0))


def simplify(coords: np.ndarray, tolerance_deg: float) -> np.ndarray:
    """Ramer-Douglas-Peucker simplification, used to keep API responses small."""
    if len(coords) < 3:
        return coords
    # Scale longitude so the planar distance test is roughly isotropic.
    scale = np.cos(np.radians(coords[:, 0].mean()))
    points = np.column_stack((coords[:, 0], coords[:, 1] * scale))
    keep = np.zeros(len(points), dtype=bool)
    keep[[0, -1]] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        a, b = points[first], points[last]
        segment = points[first + 1 : last]
        ab = b - a
        norm = np.hypot(*ab)
        if norm == 0:
            dists = np.hypot(*(segment - a).T)
        else:
            dists = np.abs(ab[0] * (segment[:, 1] - a[1]) - ab[1] * (segment[:, 0] - a[0])) / norm
        index = int(np.argmax(dists))
        if dists[index] > tolerance_deg:
            split = first + 1 + index
            keep[split] = True
            stack.extend(((first, split), (split, last)))
    return coords[keep]
