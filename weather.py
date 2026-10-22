"""Coordinate-based weather data from OpenWeather for the Irrigate dashboard."""

import math
import os
import time
from datetime import datetime, timezone

import requests


OPENWEATHER_BASE = "https://api.openweathermap.org/data/2.5"
CACHE_TTL_SECONDS = 600
FAILURE_CACHE_TTL_SECONDS = 60


class WeatherService:
    """Fetch current conditions and a short forecast for the garden coordinates."""

    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or os.getenv("OPENWEATHER_API_KEY", "")).strip()
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
            "uvi": 0.0,
            "uvi_available": False,
            "sunlight_score": None,
            "weather_main": "Unavailable",
            "weather_desc": message,
            "weather_icon": "",
            "is_rainy": False,
            "rain_next_24h": False,
            "precipitation_mm_24h": None,
            "precipitation_available": False,
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
        """Fetch current conditions and the next 24 hours for the given garden point."""
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return self._unavailable(0, 0, "Garden location not set")

        if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return self._unavailable(0, 0, "Garden location is invalid")
        if lat == 0 and lon == 0:
            return self._unavailable(lat, lon, "Garden location not set")
        if not self.api_key:
            return self._unavailable(lat, lon, "OpenWeather API key is not configured")

        # Cache by weather-grid location so small GPS precision changes don't
        # consume another pair of API requests.
        location_key = (round(lat, 2), round(lon, 2))
        now = time.time()
        cached = self._cache.get(location_key)
        if cached is not None:
            cached_at, cached_weather = cached
            ttl = CACHE_TTL_SECONDS if cached_weather.get("available") else FAILURE_CACHE_TTL_SECONDS
            if now - cached_at < ttl:
                result = cached_weather.copy()
                result["latitude"] = lat
                result["longitude"] = lon
                result["city"] = self._location_label(lat, lon)
                return result

        params = {"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric"}
        try:
            current_response = requests.get(f"{OPENWEATHER_BASE}/weather", params=params, timeout=12)
            self._raise_api_error(current_response)
            forecast_response = requests.get(f"{OPENWEATHER_BASE}/forecast", params=params, timeout=12)
            self._raise_api_error(forecast_response)
            result = self._parse(current_response.json(), forecast_response.json(), lat, lon)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                message = "OpenWeather rejected the API key; check that it is valid and active"
            elif status == 429:
                message = "OpenWeather request limit reached"
            else:
                message = f"OpenWeather service error ({status or 'HTTP'})"
            result = self._unavailable(lat, lon, message)
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
            result = self._unavailable(lat, lon, f"OpenWeather unavailable ({type(exc).__name__})")

        self._cache[location_key] = (now, result)
        if len(self._cache) > 128:
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            del self._cache[oldest]
        return result

    @staticmethod
    def _raise_api_error(response) -> None:
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and int(payload.get("cod", 200)) >= 400:
            response.raise_for_status()
            raise requests.HTTPError(f"OpenWeather API error {payload['cod']}", response=response)

    def _parse(self, current_payload: dict, forecast_payload: dict, lat: float, lon: float) -> dict:
        current = current_payload["main"]
        conditions = current_payload["weather"][0]
        wind = current_payload.get("wind", {})
        clouds = int(current_payload.get("clouds", {}).get("all", 0))
        wind_ms = float(wind.get("speed") or 0)
        weather_main = conditions.get("main", "Unknown")
        weather_id = int(conditions.get("id", 0))
        current_rain = current_payload.get("rain", {})
        current_snow = current_payload.get("snow", {})
        is_rainy = weather_main in ("Rain", "Drizzle", "Thunderstorm", "Snow") or bool(current_rain or current_snow)

        now_epoch = datetime.now(timezone.utc).timestamp()
        next_24h = [
            item for item in forecast_payload.get("list", [])
            if now_epoch <= float(item.get("dt", 0)) <= now_epoch + 24 * 60 * 60
        ]
        rain_next_24h = any(
            float(item.get("pop", 0) or 0) >= 0.5
            or float(item.get("rain", {}).get("3h", 0) or 0) > 0.1
            or float(item.get("snow", {}).get("3h", 0) or 0) > 0.1
            or item.get("weather", [{}])[0].get("main") in ("Rain", "Drizzle", "Thunderstorm", "Snow")
            for item in next_24h
        )
        precipitation_mm_24h = round(sum(
            float(item.get("rain", {}).get("3h", 0) or 0)
            + float(item.get("snow", {}).get("3h", 0) or 0)
            for item in next_24h
        ), 1)

        observed_at = datetime.fromtimestamp(
            float(current_payload.get("dt", now_epoch)), timezone.utc
        ).isoformat().replace("+00:00", "Z")
        uvi = 0.0  # The current-weather and 5-day forecast endpoints don't include UV.

        return {
            "available": True,
            "temperature": round(float(current["temp"]), 1),
            "feels_like": round(float(current.get("feels_like", current["temp"])), 1),
            "humidity": int(current.get("humidity", 0)),
            "wind_speed": round(wind_ms, 1),
            "wind_speed_kmh": round(wind_ms * 3.6, 1),
            "clouds": clouds,
            "uvi": uvi,
            "uvi_available": False,
            "sunlight_score": None,
            "weather_main": weather_main,
            "weather_desc": conditions.get("description", "").title(),
            "weather_icon": conditions.get("icon", ""),
            "is_rainy": is_rainy,
            "rain_next_24h": rain_next_24h,
            "precipitation_mm_24h": precipitation_mm_24h,
            "precipitation_available": bool(next_24h),
            "city": current_payload.get("name") or self._location_label(lat, lon),
            "sunrise": current_payload.get("sys", {}).get("sunrise"),
            "sunset": current_payload.get("sys", {}).get("sunset"),
            "pressure": current.get("pressure"),
            "source": "openweathermap",
            "latitude": lat,
            "longitude": lon,
            "timestamp": observed_at,
            "weather_id": weather_id,
        }
