"""
OpenWeatherMap API integration for Smart Irrigation System.
Free tier: https://openweathermap.org/api
"""

import os
import math
import requests
from datetime import datetime, timezone
from typing import Optional


OPENWEATHER_BASE = "https://api.openweathermap.org/data/2.5"
# 1-Call 3.0 / UV index endpoint (free tier uses 2.5)
OPENWEATHER_UVI_BASE = "https://api.openweathermap.org/data/2.5/uvi"


class WeatherService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._cache: Optional[dict] = None
        self._cache_time: float = 0
        self._cache_ttl: float = 600  # 10 minutes

    def _is_cache_valid(self) -> bool:
        import time
        return self._cache is not None and (time.time() - self._cache_time) < self._cache_ttl

    def get_weather(self, lat: float, lon: float) -> dict:
        """Fetch current weather + 5-day forecast + UV index."""
        import time

        if self._is_cache_valid():
            return self._cache

        try:
            # Current weather
            current_resp = requests.get(
                f"{OPENWEATHER_BASE}/weather",
                params={
                    "lat": lat,
                    "lon": lon,
                    "appid": self.api_key,
                    "units": "metric",
                },
                timeout=10,
            )
            current_resp.raise_for_status()
            current = current_resp.json()

            # 5-day / 3-hour forecast
            forecast_resp = requests.get(
                f"{OPENWEATHER_BASE}/forecast",
                params={
                    "lat": lat,
                    "lon": lon,
                    "appid": self.api_key,
                    "units": "metric",
                    "cnt": 16,  # next 48 hours in 3-h steps
                },
                timeout=10,
            )
            forecast_resp.raise_for_status()
            forecast = forecast_resp.json()

            # UV index (free endpoint)
            uvi = 0.0
            try:
                uvi_resp = requests.get(
                    OPENWEATHER_UVI_BASE,
                    params={"lat": lat, "lon": lon, "appid": self.api_key},
                    timeout=10,
                )
                if uvi_resp.status_code == 200:
                    uvi = uvi_resp.json().get("value", 0.0)
            except Exception:
                pass

            result = self._parse(current, forecast, uvi)
            self._cache = result
            self._cache_time = time.time()
            return result

        except Exception as e:
            # Return mock data when API key is not set / network fails
            return self._mock_weather(lat, lon)

    def _parse(self, current: dict, forecast: dict, uvi: float) -> dict:
        rain_next_24h = False
        for item in forecast.get("list", [])[:8]:  # next 24h (8 × 3h)
            if "rain" in item or item.get("weather", [{}])[0].get("main") in ("Rain", "Drizzle", "Thunderstorm"):
                rain_next_24h = True
                break

        # Calculate sunlight score from UV + cloud cover
        clouds = current.get("clouds", {}).get("all", 0)  # %
        sunlight_score = max(0.0, uvi * (1 - clouds / 100))

        weather_main = current.get("weather", [{}])[0].get("main", "Clear")
        is_rainy = weather_main in ("Rain", "Drizzle", "Thunderstorm", "Snow")

        return {
            "temperature": round(current["main"]["temp"], 1),
            "feels_like": round(current["main"]["feels_like"], 1),
            "humidity": current["main"]["humidity"],
            "wind_speed": round(current["wind"]["speed"], 1),  # m/s
            "wind_speed_kmh": round(current["wind"]["speed"] * 3.6, 1),
            "clouds": clouds,
            "uvi": round(uvi, 1),
            "sunlight_score": round(sunlight_score, 2),
            "weather_main": weather_main,
            "weather_desc": current.get("weather", [{}])[0].get("description", "").title(),
            "weather_icon": current.get("weather", [{}])[0].get("icon", "01d"),
            "is_rainy": is_rainy,
            "rain_next_24h": rain_next_24h,
            "city": current.get("name", "Your Location"),
            "sunrise": current.get("sys", {}).get("sunrise"),
            "sunset": current.get("sys", {}).get("sunset"),
            "pressure": current["main"].get("pressure"),
            "source": "openweathermap",
            "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        }

    def _mock_weather(self, lat: float, lon: float) -> dict:
        """Deterministic mock based on time-of-day so UI looks dynamic."""
        hour = datetime.now(timezone.utc).replace(tzinfo=None).hour
        temp = 18 + 8 * math.sin(math.pi * (hour - 6) / 12)  # peaks at noon
        uvi = max(0, 5 * math.sin(math.pi * (hour - 6) / 12))
        clouds = 20
        sunlight_score = max(0, uvi * (1 - clouds / 100))

        return {
            "temperature": round(temp, 1),
            "feels_like": round(temp - 2, 1),
            "humidity": 55,
            "wind_speed": 3.2,
            "wind_speed_kmh": 11.5,
            "clouds": clouds,
            "uvi": round(uvi, 1),
            "sunlight_score": round(sunlight_score, 2),
            "weather_main": "Clear",
            "weather_desc": "Clear Sky (Demo Mode)",
            "weather_icon": "01d" if 6 <= hour < 20 else "01n",
            "is_rainy": False,
            "rain_next_24h": False,
            "city": "Demo City",
            "sunrise": None,
            "sunset": None,
            "pressure": 1013,
            "source": "mock",
            "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        }
