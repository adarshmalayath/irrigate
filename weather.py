"""Coordinate-based weather data for the Irrigate dashboard."""

import math
import time
from datetime import datetime, timezone
import requests


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL_SECONDS = 600
FAILURE_CACHE_TTL_SECONDS = 60


class WeatherService:
    """Fetch current and near-term weather for the supplied garden coordinates."""

    def __init__(self):
        self._cache: dict[tuple[float, float], tuple[float, dict]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    @staticmethod
    def _location_label(lat: float, lon: float) -> str:
        if lat == 0 and lon == 0:
            return "Garden location not set"
        return f"Garden GPS {lat:.5f}, {lon:.5f}"

    def _unavailable(self, lat: float, lon: float, message: str) -> dict:
        return {
            "available": False,
            "temperature": None,
            "feels_like": None,
            "humidity": None,
            "wind_speed": None,
            "wind_speed_kmh": None,
            "clouds": None,
            "uvi": None,
            "sunlight_score": None,
            "weather_main": "Unavailable",
            "weather_desc": message,
            "weather_icon": "",
            "is_rainy": False,
            "rain_next_24h": False,
            "city": self._location_label(lat, lon),
            "sunrise": None,
            "sunset": None,
            "pressure": None,
            "source": "unavailable",
            "error": message,
            "latitude": lat,
            "longitude": lon,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def get_weather(self, lat: float, lon: float) -> dict:
        """Fetch current conditions and the next 24 hours at this location."""
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return self._unavailable(0, 0, "Garden location not set")

        if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return self._unavailable(0, 0, "Garden location is invalid")
        if lat == 0 and lon == 0:
            return self._unavailable(lat, lon, "Garden location not set")

        # Weather grids do not change at GPS precision; rounding to about 1 km
        # avoids repeat requests for tiny differences in saved coordinates.
        location_key = (round(lat, 2), round(lon, 2))
        now = time.time()
        cached = self._cache.get(location_key)
        if cached is not None:
            cached_at, cached_weather = cached
            ttl = CACHE_TTL_SECONDS if cached_weather.get("available") else FAILURE_CACHE_TTL_SECONDS
            if now - cached_at < ttl:
                # Keep the exact device coordinates in the UI even when nearby
                # devices share a weather-grid cache entry.
                result = cached_weather.copy()
                result["latitude"] = lat
                result["longitude"] = lon
                result["city"] = self._location_label(lat, lon)
                return result

        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ",".join((
                "temperature_2m", "relative_humidity_2m", "apparent_temperature",
                "precipitation", "rain", "showers", "snowfall", "weather_code",
                "cloud_cover", "pressure_msl", "wind_speed_10m", "uv_index", "is_day",
            )),
            "hourly": "precipitation_probability,precipitation",
            "daily": "sunrise,sunset",
            "forecast_hours": 24,
            "timezone": "auto",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
        }

        try:
            response = requests.get(OPEN_METEO_URL, params=params, timeout=12)
            response.raise_for_status()
            payload = response.json()
            result = self._parse(payload, lat, lon)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            # Never present generated demo values as real local weather.
            result = self._unavailable(lat, lon, f"Weather service unavailable ({type(exc).__name__})")

        self._cache[location_key] = (now, result)
        if len(self._cache) > 128:
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            del self._cache[oldest]
        return result

    def _parse(self, payload: dict, lat: float, lon: float) -> dict:
        current = payload["current"]
        hourly = payload.get("hourly", {})
        daily = payload.get("daily", {})
        code = int(current.get("weather_code", 0))
        main, description, icon = self._condition(code, bool(current.get("is_day", 1)))
        wind_kmh = float(current.get("wind_speed_10m") or 0)
        clouds = int(current.get("cloud_cover") or 0)
        uvi = float(current.get("uv_index") or 0)

        current_time = current.get("time", "")
        times = hourly.get("time", [])
        probabilities = hourly.get("precipitation_probability", [])
        precipitation = hourly.get("precipitation", [])
        next_hours = [i for i, stamp in enumerate(times) if stamp >= current_time][:24]
        rain_next_24h = any(
            (i < len(probabilities) and (probabilities[i] or 0) >= 50)
            or (i < len(precipitation) and (precipitation[i] or 0) > 0.1)
            for i in next_hours
        )

        sunrise = (daily.get("sunrise") or [None])[0]
        sunset = (daily.get("sunset") or [None])[0]
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        return {
            "available": True,
            "temperature": round(float(current["temperature_2m"]), 1),
            "feels_like": round(float(current.get("apparent_temperature", current["temperature_2m"])), 1),
            "humidity": int(current.get("relative_humidity_2m", 0)),
            "wind_speed": round(wind_kmh / 3.6, 1),  # m/s for irrigation thresholds
            "wind_speed_kmh": round(wind_kmh, 1),
            "clouds": clouds,
            "uvi": round(uvi, 1),
            "sunlight_score": round(max(0.0, uvi * (1 - clouds / 100)), 2),
            "weather_main": main,
            "weather_desc": description,
            "weather_icon": icon,
            "is_rainy": main in ("Rain", "Drizzle", "Thunderstorm", "Snow"),
            "rain_next_24h": rain_next_24h,
            "city": self._location_label(lat, lon),
            "sunrise": sunrise,
            "sunset": sunset,
            "pressure": current.get("pressure_msl"),
            "source": "open-meteo",
            "latitude": lat,
            "longitude": lon,
            "timestamp": timestamp,
        }

    @staticmethod
    def _condition(code: int, is_day: bool) -> tuple[str, str, str]:
        if code == 0:
            return "Clear", "Clear sky", "01d" if is_day else "01n"
        if code == 1:
            return "Clear", "Mainly clear", "02d" if is_day else "02n"
        if code in (2, 3):
            return "Clouds", "Partly cloudy" if code == 2 else "Overcast", "03d"
        if code in (45, 48):
            return "Fog", "Fog", "50d"
        if code in (51, 53, 55, 56, 57):
            return "Drizzle", "Drizzle", "09d"
        if code in (61, 63, 65, 66, 67, 80, 81, 82):
            return "Rain", "Rain showers" if code >= 80 else "Rain", "10d"
        if code in (71, 73, 75, 77, 85, 86):
            return "Snow", "Snow", "13d"
        if code in (95, 96, 99):
            return "Thunderstorm", "Thunderstorm", "11d"
        return "Clouds", "Variable conditions", "03d"
