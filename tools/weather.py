"""查天气工具（LEGACY / 已退役）：默认用免费的 Open-Meteo（无需注册 key）。

注意：当前版本不再把本工具注册给模型，天气查询改由通用工具 web_fetch 完成。
本文件仅作历史保留，见标签 v1-specialized-tools。
"""
import requests

# WMO 天气代码 → 中文描述
WEATHER_CODES = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒", 80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def get_weather(city: str) -> str:
    """查询某城市当前天气 + 未来几天，返回一段中文描述。"""
    city = (city or "").strip()
    if not city:
        return "错误：请提供城市名。"

    # 1. 地理编码：城市名 -> 经纬度
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    try:
        geo = requests.get(
            geo_url,
            params={"name": city, "count": 1, "language": "zh", "format": "json"},
            timeout=10,
        ).json()
    except Exception as e:
        return f"查询天气失败（网络错误）：{e}"

    results = geo.get("results")
    if not results:
        return f"找不到城市「{city}」，请换种写法（如城市名+国家，或拼音）。"
    r = results[0]
    name, admin, country = r.get("name", city), r.get("admin1", ""), r.get("country", "")
    lat, lon = r["latitude"], r["longitude"]

    # 2. 当前天气 + 未来 3 天
    w_url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto", "forecast_days": 3,
    }
    try:
        w = requests.get(w_url, params=params, timeout=10).json()
    except Exception as e:
        return f"查询天气失败（网络错误）：{e}"

    cur = w.get("current", {})
    desc = WEATHER_CODES.get(cur.get("weather_code", 0), "未知")
    temp = cur.get("temperature_2m", "?")
    hum = cur.get("relative_humidity_2m", "?")
    wind = cur.get("wind_speed_10m", "?")

    daily = w.get("daily", {})
    days = []
    for i in range(len(daily.get("time", []))):
        wc = WEATHER_CODES.get(daily["weather_code"][i], "未知")
        days.append(f"{daily['time'][i]} {daily['temperature_2m_min'][i]}~{daily['temperature_2m_max'][i]}°C {wc}")

    loc = f"{name}（{admin}，{country}）"
    out = f"{loc} 当前：{desc}，气温 {temp}°C，湿度 {hum}%，风速 {wind} km/h。\n未来几天：" + "；".join(days)
    return out
