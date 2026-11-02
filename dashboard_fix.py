#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Classic Mac OS X Dashboard "dead service" revival server.

Brings back the networked widgets in Mac OS X 10.4 Tiger / 10.5 Leopard /
10.6 Snow Leopard / 10.7 Lion by impersonating the long-dead backends they
call, and serving live data from free, keyless modern APIs (Open-Meteo).

All widgets load a shared parser from
file:///System/Library/WidgetResources/.parsers/ (confirmed in the widget
HTML), NOT the parser.js left inside each .wdgt bundle -- that bundled copy
is dead code. The live parsers show:

  * Weather 10.4 / 10.5 (identical parser), AccuWeather backend, GET:
        /adcbin/apple/Apple_find_city.asp
        /adcbin/apple/Apple_Weather_Data.asp          (host wu.apple.com)
  * Weather 10.6 / 10.7 (identical parser), Apple "dgw" gateway -- the same
    gateway used by the first-gen iPhone Weather app, host iphone-wu.apple.com:
        POST /dgw  type=getlocationid              -> city list
        POST /dgw  type=getforecastbylocationid    -> forecast, Yahoo 0..47
  * Stocks 10.4-10.7, POST /dgw, query type routes across hosts:
        getquotes  -> wu-quotes.apple.com  (price/change/changepercent/status)
        getsymbol  -> wu-stocks.apple.com  (company name / exchange)
        getchart   -> wu-charts.apple.com  (<point timestamp unix-secs close>)
        getnews    -> iphone-wu.apple.com  (10.7 only; we return an empty list)
  * Unit Converter currency 10.4-10.7, POST /dgw:
        convertcurrency -> wu-converter.apple.com (<conversion><to>/<convertedamount>)
  * Translation 10.4-10.7, POST /trans (host wu.apple.com; the w4.systranlinks
    URL in the parser is commented out).

The query type is globally unique, so do_POST routes /dgw on the body type
rather than the Host header. Data: weather Open-Meteo + geocoding, stocks
Tencent realtime + Sina daily history, rates open.er-api.com, translation
via Baidu (free standard tier; key set through BAIDU_APPID/BAIDU_KEY env
vars) -- all reachable from inside China.

Everything from the old Mac is plain HTTP (no TLS). Point the names above
at this host via /etc/hosts on the old Macs. This server then talks HTTPS
to the modern APIs on the modern side. Python 3 standard library only.
"""

# ---------------------------------------------------------------------------
# 中文维护速查
#   * 老 Mac 上所有 *.apple.com 由 dnsmasq/SmartDNS 指到本机，全程明文 HTTP；
#   * /dgw 按请求体里的 query type 路由（getquotes / getchart /
#     convertcurrency / getforecastbylocationid …），不依赖 Host 头；
#   * 股票/汇率 parser 的 XML 声明是 encoding='utf−8'（U+2212，不是 '-'），
#     必须在 parse_dgw_request 里剥掉声明，否则 ElementTree 直接报错；
#   * 10.6/10.7 的 getchart 需要 <meta>；10.7 的 getquotes 需要完整字段；
#   * 货币下拉完全由 convertcurrency 的 <conversion> 生成，USD 也必须返回。
# ---------------------------------------------------------------------------


import hashlib
import json
import re
import math
import os
import sys
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 80
HERE = os.path.dirname(os.path.abspath(__file__))
GEO_CACHE = os.path.join(HERE, "geo_cache.json")
ZIPCODES_FILE = os.path.join(HERE, "zipcodes.json")

HTTP_TIMEOUT = 12
USER_AGENT = "DashboardRevival/1.0 (classic mac widgets)"

# ---------------------------------------------------------------------------
# Small HTTP helper / 小型 HTTP 请求封装
# ---------------------------------------------------------------------------

# 发 GET 取 JSON（统一 UA，12 秒超时）
def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


# 发 GET 取纯文本
def http_get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# US state / Canada province abbreviations / 州省全名转两位缩写
# ---------------------------------------------------------------------------

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
CA_PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "northwest territories": "NT", "nova scotia": "NS", "nunavut": "NU",
    "ontario": "ON", "prince edward island": "PE", "quebec": "QC",
    "saskatchewan": "SK", "yukon": "YT",
}


# Open-Meteo 返回州/省全名，这里转成 US/CA 两位缩写
def region_abbrev(country_code, admin1):
    if not admin1:
        return ""
    key = admin1.strip().lower()
    if country_code == "US":
        return US_STATES.get(key, admin1)
    if country_code == "CA":
        return CA_PROVINCES.get(key, admin1)
    return admin1


# ---------------------------------------------------------------------------
# Persistent geo cache / 地理缓存：postal token -> 地点
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache = {}


# 载入地理缓存（坐标 token -> 地点），文件不存在就空表
def load_cache():
    global _cache
    try:
        with open(GEO_CACHE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}


# 原子写回地理缓存（先写 .tmp 再 replace）
def save_cache():
    tmp = GEO_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, GEO_CACHE)


# 10.6/10.7 built-in Zipcodes.js: WOEID -> (city, state, country). Used when
# the widget first launches with a preset city (e.g. Cupertino) before the
# user searches one.
_zipcodes = {}


# 载入 10.6/10.7 首屏 WOEID 表（Zipcodes.js 解析结果）
def load_zipcodes():
    global _zipcodes
    try:
        with open(ZIPCODES_FILE, "r", encoding="utf-8") as f:
            _zipcodes = json.load(f)
    except Exception:
        _zipcodes = {}


# 复合 ID "USCA0273|2388327" 或纯 WOEID -> 地点
def resolve_woeid(raw):
    """Map a dgw location id (WOEID, or 'USCA0273|2388327' compound) to a
    place via Open-Meteo name search against the Zipcodes.js table."""
    woeid = str(raw or "").split("|")[-1].strip()
    info = _zipcodes.get(woeid)
    if not info:
        return None
    try:
        res = _geocode(info["city"], "en", count=20)
    except Exception:
        res = {}
    want_state = info.get("state") or ""
    want_country = info.get("country") or ""
    for r in res.values():
        cc = (r.get("country_code") or "").upper()
        if want_country == "US":
            if cc == "US" and region_abbrev("US", r.get("admin1") or "") == want_state:
                return place_from_result(r)
        elif (r.get("country") or "") == want_country:
            return place_from_result(r)
    if res:
        return place_from_result(next(iter(res.values())))
    return None


# 坐标 -> widget 可存回的不透明 token（om<lat>_<lon>）
def encode_postal(lat, lon):
    """Encode coordinates into an opaque token the widget stores/passes back."""
    return "om%d_%d" % (round(lat * 10000), round(lon * 10000))


# 解坐标 token；不是我们的格式返回 None
def decode_postal(postal):
    """Return (lat, lon) for one of our tokens, or None."""
    if postal and postal.startswith("om"):
        try:
            la, lo = postal[2:].split("_")
            return int(la) / 10000.0, int(lo) / 10000.0
        except Exception:
            return None
    return None


# 规范化 Open-Meteo 地理编码结果
def place_from_result(r):
    """Normalize an Open-Meteo geocoding result."""
    cc = (r.get("country_code") or "").upper()
    admin1 = r.get("admin1") or ""
    return {
        "name": r.get("name") or "",
        "lat": float(r["latitude"]),
        "lon": float(r["longitude"]),
        "country": r.get("country") or "",
        "country_code": cc,
        "admin1": admin1,
        "state": region_abbrev(cc, admin1),
        "tz": r.get("timezone") or "auto",
    }


# Open-Meteo 地理编码，按城市 id 建索引
def _geocode(query, lang, count=20):
    url = ("https://geocoding-api.open-meteo.com/v1/search?"
           + urllib.parse.urlencode({"name": query, "count": count,
                                     "language": lang, "format": "json"}))
    data = http_get_json(url)
    return {r["id"]: r for r in (data.get("results") or [])}


# 城市搜索：zh/en 各查一次再合并（中文取简体名，英文取全州名）
def lookup_city_text(query, count=12):
    """Search by free text (Chinese / English / pinyin). Returns places.

    Chinese queries only hit in language=zh (which also gives simplified
    names), while English queries give canonical names and full admin1 names
    (needed for US/CA state abbreviations). Merge by Open-Meteo city id.
    """
    q = query.strip()
    # US ZIP code (5 digits)
    if q.isdigit() and len(q) == 5:
        places = lookup_us_zip(q)
        if places:
            return places
    try:
        zh = _geocode(q, "zh")
    except Exception:
        zh = {}
    try:
        en = _geocode(q, "en")
    except Exception:
        en = {}
    ordered_ids = []
    for r in list(en.values()) + list(zh.values()):
        if r["id"] not in ordered_ids:
            ordered_ids.append(r["id"])
    out = []
    for cid in ordered_ids:
        rz, re_ = zh.get(cid), en.get(cid)
        base = re_ or rz
        cc = (base.get("country_code") or "").upper()
        # Chinese cities: use the simplified localized name + province
        if cc == "CN" and rz:
            out.append(place_from_result(rz))
        else:
            out.append(place_from_result(base))
    return out[:count]


# 美国 5 位 ZIP -> 地点（zippopotam 免费接口）
def lookup_us_zip(zipcode):
    """Resolve a US ZIP code via the free keyless zippopotam service."""
    try:
        data = http_get_json("https://api.zippopotam.us/us/%s" % zipcode)
    except Exception:
        return []
    out = []
    for p in data.get("places", []):
        cc = "US"
        admin1 = p.get("state") or ""
        out.append({
            "name": p.get("place name") or "",
            "lat": float(p["latitude"]),
            "lon": float(p["longitude"]),
            "country": "United States",
            "country_code": cc,
            "admin1": admin1,
            "state": region_abbrev(cc, admin1),
            "tz": "auto",
        })
    return out


# 缓存丢失时按坐标反查城市名（BigDataCloud 免费接口）
def reverse_geocode(lat, lon):
    """Keyless reverse geocoding via BigDataCloud's free client endpoint.

    Used when a coordinate token comes back after the geo cache was lost
    (redeploy/restart) so the widget still gets a real city name. Returns a
    normalized place dict, or None if nothing usable is available."""
    url = ("https://api.bigdatacloud.net/data/reverse-geocode-client?"
           + urllib.parse.urlencode({"latitude": lat, "longitude": lon,
                                     "localityLanguage": "zh"}))
    d = http_get_json(url)
    name = (d.get("city") or d.get("locality")
            or d.get("principalSubdivision") or "").strip()
    if not name:
        return None
    cc = (d.get("countryCode") or "").upper()
    admin1 = d.get("principalSubdivision") or ""
    return {
        "name": name,
        "lat": float(lat),
        "lon": float(lon),
        "country": d.get("countryName") or "",
        "country_code": cc,
        "admin1": admin1,
        "state": region_abbrev(cc, admin1) or admin1,
        "tz": "auto",
    }


# 记住地点并返回坐标 token
def remember_place(place):
    postal = encode_postal(place["lat"], place["lon"])
    with _cache_lock:
        _cache[postal] = place
        save_cache()
    return postal


CUPERTINO = {"name": "Cupertino", "lat": 37.323, "lon": -122.032,
             "country": "United States", "country_code": "US",
             "admin1": "California", "state": "CA",
             "tz": "America/Los_Angeles"}


# 把 widget 传来的 ZIP / WOEID / 坐标 token 统一解析成 place
def resolve_postal(postal):
    """Turn whatever the widget passes in into a place dict.

    AccuWeather (10.4/10.5) passes a US ZIP or our coordinate token;
    the dgw gateway (10.6/10.7) passes a WOEID / 'CODE|WOEID' compound
    (preset cities) or our coordinate token (after a city search).
    """
    postal = (postal or "").strip()
    # our own coordinate token
    coords = decode_postal(postal)
    if coords:
        lat, lon = coords
        with _cache_lock:
            p = _cache.get(postal)
        if p and p.get("name"):
            return p
        # cache lost (redeploy/restart): recover a real name and refill cache
        try:
            rp = reverse_geocode(lat, lon)
            if rp:
                remember_place(rp)
                return rp
        except Exception:
            pass
        # last resort: a non-empty name so the widget never sees an empty
        # <City> (its parser calls tag.firstChild.data with no null check)
        return {"name": "Location %.3f, %.3f" % (lat, lon),
                "lat": lat, "lon": lon, "country": "",
                "country_code": "", "admin1": "", "state": "", "tz": "auto"}
    # compound "USCA0273|2388327" -> WOEID lookup
    if "|" in postal:
        p = resolve_woeid(postal)
        if p:
            return p
    # bare numeric: 5-digit US ZIP first (AccuWeather), then WOEID
    if postal.isdigit():
        if len(postal) == 5:
            ps = lookup_us_zip(postal)
            if ps:
                return ps[0]
        p = resolve_woeid(postal)
        if p:
            return p
    # famous Cupertino first-launch default
    if postal in ("95014", "2388327", "USCA0273|2388327"):
        return CUPERTINO
    return None


# ---------------------------------------------------------------------------
# Open-Meteo forecast / 天气预报
# ---------------------------------------------------------------------------

# 取 Open-Meteo 预报：当前实况 + 未来 7 天，华氏/英里
def fetch_forecast(place, days=7):
    params = {
        "latitude": place["lat"],
        "longitude": place["lon"],
        "current": ("temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "is_day,weather_code,wind_speed_10m,wind_direction_10m,"
                    "pressure_msl,cloud_cover"),
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "sunrise,sunset,precipitation_probability_max,uv_index_max"),
        "timezone": place.get("tz") or "auto",
        "forecast_days": days,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return http_get_json(url)


# ---------------------------------------------------------------------------
# WMO weather code -> AccuWeather icon (1..44) + text / 天气码映射
# day icons: 1-32, night icons: 33-44
# ---------------------------------------------------------------------------

# (day_icon, night_icon, text)
WMO_MAP = {
    0:  (1,  33, "Sunny"),
    1:  (2,  34, "Mostly Sunny"),
    2:  (3,  35, "Partly Cloudy"),
    3:  (7,  38, "Cloudy"),
    45: (11, 11, "Fog"),
    48: (11, 11, "Fog"),
    51: (12, 39, "Light Showers"),
    53: (12, 40, "Showers"),
    55: (12, 40, "Showers"),
    56: (26, 26, "Freezing Rain"),
    57: (26, 26, "Freezing Rain"),
    61: (14, 39, "Light Rain"),
    63: (18, 40, "Rain"),
    65: (18, 40, "Heavy Rain"),
    66: (26, 26, "Freezing Rain"),
    67: (26, 26, "Freezing Rain"),
    71: (19, 43, "Flurries"),
    73: (22, 44, "Snow"),
    75: (22, 44, "Heavy Snow"),
    77: (24, 24, "Ice"),
    80: (14, 39, "Light Showers"),
    81: (12, 40, "Showers"),
    82: (18, 40, "Heavy Showers"),
    85: (20, 43, "Snow Showers"),
    86: (23, 44, "Snow Showers"),
    95: (15, 41, "Thunderstorms"),
    96: (16, 42, "Thunderstorms"),
    99: (16, 42, "Thunderstorms"),
}
WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# WMO 天气码 -> AccuWeather 图标(1..44)+文案，区分昼夜
def accu_for_wmo(code, is_day):
    d, n, text = WMO_MAP.get(int(code), (7, 38, "Cloudy"))
    return (d if is_day else n), text


# ---------------------------------------------------------------------------
# Moon phase (AccuWeather 1..27) / 月相计算
# ---------------------------------------------------------------------------

SYNODIC = 29.53058867
NEW_MOON_REF = 2451550.1  # 2000-01-06 18:14 UTC


# 儒略日，用于月相计算
def julian_day(dt_utc):
    y = dt_utc.year
    m = dt_utc.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = (math.floor(365.25 * (y + 4716))
          + math.floor(30.6001 * (m + 1))
          + dt_utc.day + b - 1524.5)
    jd += (dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0) / 24.0
    return jd


# 月相 1..27（10.4/10.5 AccuWeather 图标）
def moon_phase_index(local_date):
    dt = datetime(local_date.year, local_date.month, local_date.day,
                  12, 0, 0, tzinfo=timezone.utc)
    age = (julian_day(dt) - NEW_MOON_REF) % SYNODIC
    idx = int(round(age / SYNODIC * 26.0)) + 1
    return max(1, min(27, idx))


# ---------------------------------------------------------------------------
# Time formatting helpers / 时间格式转换
# ---------------------------------------------------------------------------

# "YYYY-MM-DDTHH:MM" -> "H:MM AM/PM"（12 小时制）
def iso_to_hhmm(iso):
    # Open-Meteo "2026-10-26T07:26"
    try:
        dt = datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M")
        return dt.strftime("%-I:%M %p") if sys.platform != "win32" else dt.strftime("%#I:%M %p")
    except Exception:
        return ""


# 24 小时 "HH:MM"；dgw parser 按字符串比较日出/日落
def iso_to_hhmm24(iso):
    # Zero-padded 24h "HH:MM" -- the dgw parser compares now/sunrise/sunset
    # as plain strings, so this must be lexicographically comparable.
    try:
        dt = datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M")
        return dt.strftime("%H:%M")
    except Exception:
        return ""


# 月相 1..24（10.6/10.7 Yahoo 图标）
def moon_phase_dgw(local_date):
    """dgw/iPhone widget uses a single moonphase 1..24 (icons moon-1..24)."""
    dt = datetime(local_date.year, local_date.month, local_date.day,
                  12, 0, 0, tzinfo=timezone.utc)
    age = (julian_day(dt) - NEW_MOON_REF) % SYNODIC
    idx = int(round(age / SYNODIC * 23.0)) + 1
    return max(1, min(24, idx))


# dgw 的 dayofweek：1=周日 … 7=周六
def yahoo_dayofweek(iso):
    """dgw dayofweek is numeric 1..7 with 1=Sunday."""
    dt = datetime.strptime(iso[:10], "%Y-%m-%d")
    return ((dt.weekday() + 1) % 7) + 1


# 日期 -> Mon..Sun
def iso_weekday(iso):
    try:
        dt = datetime.strptime(iso[:10], "%Y-%m-%d")
        return WEEKDAY[dt.weekday()]
    except Exception:
        return ""


# XML 文本转义（& < > "）
def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# AccuWeather XML (10.4/10.5) / 根标签 adc_Database 大小写不能错
# ---------------------------------------------------------------------------

# 10.4/10.5 AccuWeather 城市搜索结果 XML
def build_find_city_xml(places, refine=False):
    extra = "1" if refine else "0"
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<adc_Database>',
           '<CityList extra_cities="%s">' % extra]
    for p in places:
        postal = remember_place(p)
        # widget keeps a location only when city && state && postal are all
        # present, so never emit an empty city/state
        city = xml_escape(p.get("name") or "Unknown")
        state = xml_escape(p.get("state") or p.get("admin1")
                           or p.get("country") or "—")
        out.append('<location city="%s" state="%s" postal="%s"/>'
                   % (city, state, postal))
    out.append('</CityList>')
    out.append('</adc_Database>')
    return "\n".join(out)


# 10.4/10.5 AccuWeather 实况+预报 XML（字段名/大小写按 parser）
def build_weather_data_xml(place, data):
    cur = data.get("current", {})
    daily = data.get("daily", {})
    is_day = int(cur.get("is_day", 1)) == 1
    now_icon, now_text = accu_for_wmo(cur.get("weather_code", 1), is_day)

    cur_time = iso_to_hhmm(cur.get("time", ""))
    if not cur_time:
        # never emit an empty <Time>: the widget reads tag.firstChild.data
        cur_time = (datetime.now().strftime("%-I:%M %p")
                    if sys.platform != "win32" else "12:00 PM")
    # widget requires a non-empty <City> (firstChild.data, no null check)
    city = place.get("name") or "Unknown Location"
    temp = int(round(float(cur.get("temperature_2m", 0))))

    times = daily.get("time", [])
    codes = daily.get("weather_code", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    sunrises = daily.get("sunrise", [])
    sunsets = daily.get("sunset", [])

    sunrise = iso_to_hhmm(sunrises[0]) if sunrises else ""
    sunset = iso_to_hhmm(sunsets[0]) if sunsets else ""

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<adc_Database>',
           '<CurrentConditions>',
           '<Time>%s</Time>' % xml_escape(cur_time),
           '<City>%s</City>' % xml_escape(city),
           '<Temperature>%d</Temperature>' % temp,
           '<WeatherText>%s</WeatherText>' % xml_escape(now_text),
           '<WeatherIcon>%d</WeatherIcon>' % now_icon,
           '</CurrentConditions>',
           '<Planets><Sun rise="%s" set="%s"/></Planets>'
           % (xml_escape(sunrise), xml_escape(sunset)),
           '<Moon>']
    for i in range(7):
        try:
            d = datetime.strptime(times[0][:10], "%Y-%m-%d") + timedelta(days=i)
        except Exception:
            d = datetime.now(timezone.utc) + timedelta(days=i)
        out.append('<Phase>%d</Phase>' % moon_phase_index(d))
    out.append('</Moon>')
    out.append('<Forecast>')
    for i in range(min(7, len(times))):
        day_icon, day_text = accu_for_wmo(codes[i], True)
        hi = int(round(float(highs[i])))
        lo = int(round(float(lows[i])))
        wd = iso_weekday(times[i])
        if not wd:
            # empty <DayCode> would also throw on firstChild.data
            try:
                wd = WEEKDAY[datetime.strptime(times[i][:10],
                                               "%Y-%m-%d").weekday()]
            except Exception:
                wd = WEEKDAY[datetime.now().weekday()]
        out.append(
            '<day>'
            '<DayCode>%s</DayCode>'
            '<High_Temperature>%d</High_Temperature>'
            '<Low_Temperature>%d</Low_Temperature>'
            '<TXT_Long>%s</TXT_Long>'
            '<WeatherIcon>%d</WeatherIcon>'
            '</day>' % (wd, hi, lo,
                        xml_escape(day_text), day_icon))
    out.append('</Forecast>')
    out.append('</adc_Database>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Yahoo! Weather dgw (10.6/10.7) / dgw 预报与城市 XML
# ---------------------------------------------------------------------------

# Each value is (day_code, night_code, text). Codes are the classic Yahoo
# 0..47 set the dgw parser maps through yahooWeatherTypes[].
WMO_TO_YAHOO = {
    0:  (32, 31, "Sunny"),
    1:  (34, 33, "Fair"),
    2:  (30, 29, "Partly Cloudy"),
    3:  (28, 27, "Mostly Cloudy"),
    45: (20, 20, "Foggy"), 48: (20, 20, "Foggy"),
    51: (9, 9, "Drizzle"), 53: (9, 9, "Drizzle"), 55: (9, 9, "Drizzle"),
    56: (8, 8, "Freezing Drizzle"), 57: (8, 8, "Freezing Drizzle"),
    61: (11, 11, "Showers"), 63: (12, 12, "Rain"), 65: (12, 12, "Heavy Rain"),
    66: (10, 10, "Freezing Rain"), 67: (10, 10, "Freezing Rain"),
    71: (14, 14, "Light Snow"), 73: (16, 16, "Snow"), 75: (41, 41, "Heavy Snow"),
    77: (16, 16, "Snow"),
    80: (11, 11, "Showers"), 81: (40, 40, "Scattered Showers"),
    82: (12, 12, "Rain"),
    85: (46, 46, "Snow Showers"), 86: (41, 41, "Heavy Snow"),
    95: (4, 4, "Thunderstorms"), 96: (37, 37, "Isolated Thunderstorms"),
    99: (38, 38, "Scattered Thunderstorms"),
}
YAHOO_TEXT = {c: v[2] for c, v in WMO_TO_YAHOO.items()}


# WMO -> Yahoo 天气码(0..47)+文案
def yahoo_code_text(code, is_day):
    d, n, t = WMO_TO_YAHOO.get(int(code), (26, 26, "Cloudy"))
    return (d if is_day else n), t


# 取 ElementTree 节点的去空白文本
def _text(el):
    return (el.text or "").strip() if el is not None else ""


def parse_dgw_request(body_bytes):
    """解析 POST /dgw 的请求体。

    天气/股票/汇率共用一个 /dgw，但 query type 全局唯一（getquotes、
    convertcurrency、getforecastbylocationid…），所以按 type 路由，不依赖
    Host 头。返回 (kind, payload_dict)。
    """
    text = body_bytes.decode("utf-8", "replace")
    # 关键：Apple 的股票/汇率 parser 发出的 XML 声明是 encoding='utf−8'，
    # 中间是 U+2212（MINUS SIGN，字节 e2 88 92），不是 ASCII '-'。
    # ElementTree 会拒绝这个声明并抛异常，导致股票/汇率拿到空列表。
    # 这里先把 XML 声明整个剥掉；payload 本身是 UTF-8，不需要它。
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    try:
        root = ET.fromstring(text)
    except Exception:
        return (None, {})
    q = root.find("query")
    if q is None:
        return (None, {})
    t = q.get("type")
    if t == "getforecastbylocationid":
        return ("forecast", {"id": _text(q.find("list/id"))})
    if t == "getlocationid":
        return ("location", {"phrase": _text(q.find("phrase"))})
    if t == "getquotes":
        syms = [s.text.strip() for s in q.findall("list/symbol")
                if s.text and s.text.strip()]
        return ("quotes", {"symbols": syms})
    if t == "getsymbol":
        return ("symbol", {"phrase": _text(q.find("phrase"))})
    if t == "getchart":
        return ("chart", {"symbol": _text(q.find("symbol")),
                          "range": _text(q.find("range"))})
    if t == "convertcurrency":
        return ("converter", {})
    if t == "getnews":
        return ("news", {})
    return (None, {})


# 10.6/10.7 天气城市搜索 dgw XML
def build_dgw_locations_xml(places):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<response><result><list>']
    for p in places:
        city = p.get("name") or ""
        cc = p.get("country_code") or ""
        region = p.get("state") or ""
        regionname = p.get("admin1") or region
        countryname = p.get("country") or ""
        lid = remember_place(p)
        out.append(
            '<item><city>%s</city><region>%s</region><regionname>%s'
            '</regionname><country>%s</country><countryname>%s'
            '</countryname><id>%s</id></item>'
            % (xml_escape(city), xml_escape(region), xml_escape(regionname),
               xml_escape(cc), xml_escape(countryname), xml_escape(lid)))
    out.append('</list></result></response>')
    return "\n".join(out)


# 10.6/10.7 天气 dgw 预报 XML（Yahoo 码/月相 1..24）
def build_dgw_forecast_xml(place, data):
    cur = data.get("current", {})
    daily = data.get("daily", {})
    is_day = int(cur.get("is_day", 1)) == 1
    code, text = yahoo_code_text(cur.get("weather_code", 1), is_day)
    temp = int(round(float(cur.get("temperature_2m", 0))))
    now = iso_to_hhmm24(cur.get("time", ""))

    times = daily.get("time", [])
    codes = daily.get("weather_code", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    sunrises = daily.get("sunrise", [])
    sunsets = daily.get("sunset", [])
    sunrise = iso_to_hhmm24(sunrises[0]) if sunrises else ""
    sunset = iso_to_hhmm24(sunsets[0]) if sunsets else ""
    try:
        ldate = datetime.strptime(times[0][:10], "%Y-%m-%d")
    except Exception:
        ldate = datetime.now(timezone.utc)
    moon = moon_phase_dgw(ldate)

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<response><result><list><item>',
           '<location city="%s" countryname="%s"/>'
           % (xml_escape(place.get("name") or ""),
              xml_escape(place.get("country") or "")),
           '<condition time="%s" temp="%d" text="%s" code="%d"/>'
           % (xml_escape(now), temp, xml_escape(text), code),
           '<astronomy sunrise="%s" sunset="%s" moonphase="%d"/>'
           % (xml_escape(sunrise), xml_escape(sunset), moon),
           '<link>http://weather.yahoo.com/</link>']
    for i in range(min(6, len(times))):
        fc, ft = yahoo_code_text(codes[i], True)
        out.append(
            '<forecast text="%s" high="%d" low="%d" dayofweek="%d" '
            'code="%d"/>' % (
                xml_escape(ft), int(round(float(highs[i]))),
                int(round(float(lows[i]))), yahoo_dayofweek(times[i]), fc))
    out.append('</item></list></result></response>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Stocks dgw / 股票：实时 + 新浪日 K
# history from Sina. Rendered into getquotes / getsymbol / getchart XML.
# ---------------------------------------------------------------------------

INDEX_TO_TENCENT = {
    "^DJI": "usDJI", ".DJI": "usDJI", "INDU": "usDJI",
    "^IXIC": "usIXIC", ".IXIC": "usIXIC", "COMPX": "usIXIC",
    "^GSPC": "usINX", "^SPX": "usINX", ".INX": "usINX",
    "^NDX": "usNDX", ".NDX": "usNDX",
}
INDEX_TO_SINA = {
    "DJI": ".DJI", "INDU": ".DJI",
    "IXIC": ".IXIC", "COMPX": ".IXIC",
    "GSPC": ".INX", "INX": ".INX", "SPX": ".INX",
    "NDX": ".NDX",
}
TENCENT_EXCHANGE = {
    "OQ": "NASDAQ", "O": "NASDAQ", "NMS": "NASDAQ",
    "UN": "NYSE", "N": "NYSE", "NY": "NYSE",
    "A": "AMEX", "AQ": "AMEX",
}


def _raw_get(url, referer="https://finance.sina.com.cn"):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": referer})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()


# 股票代码 -> 代码（指数 usDJI…，个股 usAAPL）
def to_tencent_code(symbol):
    s = symbol.strip().upper()
    if s in INDEX_TO_TENCENT:
        return INDEX_TO_TENCENT[s]
    if s.startswith("^") or s.startswith("."):
        return "us" + s[1:]
    return "us" + s


def tencent_quotes(symbols):
    """一次接口批量取多个代码，返回 {原始代码: 字段数组}。

    指数用 usDJI/usIXIC/usINX，个股用 us<代码>（如 usAAPL）；返回体是 GBK。
    """
    want = []
    code_to_orig = {}
    for s in symbols:
        s = s.strip()
        if not s:
            continue
        tc = to_tencent_code(s)
        code_to_orig[tc] = s
        want.append(tc)
    if not want:
        return {}
    raw = _raw_get("https://qt.gtimg.cn/q=" + ",".join(want)).decode("gbk", "replace")
    out = {}
    for line in raw.replace("\n", "").split(";"):
        if "=\"" not in line:
            continue
        key = line.split("=")[0].strip()
        key = key.split("_")[-1]  # v_usAAPL -> usAAPL
        body = line.split("=\"", 1)[1].rstrip("\"")
        f = body.split("~")
        if len(f) < 35 or not f[3].strip():
            continue
        orig = code_to_orig.get(key, key)
        out[orig.upper()] = f
    return out


# 股票代码 -> 新浪代码（指数 .DJI/.IXIC/.INX）
def _sina_symbol(symbol):
    s = symbol.strip().upper()
    base = s.lstrip("^.")
    if base in INDEX_TO_SINA:
        return INDEX_TO_SINA[base]
    return base


# 新浪美股日 K（JSONP），返回 [{d,o,h,l,c,v}]
def sina_daily_k(symbol):
    """Return list of {d:YYYY-MM-DD, o,h,l,c,v} from Sina (US stocks)."""
    code = _sina_symbol(symbol)
    if code.startswith("$"):
        url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
               "var=/US_MinKService.getDailyK?symbol=%s" % code)
    else:
        url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
               "var=/US_MinKService.getDailyK?symbol=%s" % code)
    raw = _raw_get(url).decode("utf-8", "replace")
    i = raw.find("([")
    if i < 0:
        return []
    payload = raw[i + 1:].strip().rstrip(";")
    if payload.endswith(")"):
        payload = payload[:-1]
    return json.loads(payload)


# 判断美股是否开市（东部 9:30-16:00，自动处理夏令时）
def is_us_market_open():
    """True during US cash-session hours 9:30-16:00 Mon-Fri Eastern (DST aware)."""
    utc = datetime.now(timezone.utc)

    def nth_weekday(year, month, wd, n):
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        k = 0
        while True:
            if d.weekday() == wd:
                k += 1
                if k == n:
                    return d
            d += timedelta(days=1)

    y = utc.year
    dst_start = nth_weekday(y, 3, 6, 2).replace(hour=7)   # 2:00 EDT = 07:00 UTC
    dst_end = nth_weekday(y, 11, 6, 1).replace(hour=6)    # 2:00 EDT -> 1:00 EST
    offset = -4 if dst_start <= utc < dst_end else -5
    et = utc + timedelta(hours=offset)
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


# 给 dgw 响应套 <response><result> 外壳
def _dgw_wrap(body):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<response><result>'
            + body + '</result></response>')


def build_dgw_quotes_xml(symbols):
    """getquotes（wu-quotes.apple.com）：每个代码一个 <quote>。

    各版本 parser 的字段要求：
      * 10.4/10.5/10.6：symbol/price/change/changepercent/status/link；
      * 10.7：还必须有 name/timestamp/open/high/low/volume/marketcap/link，
        缺 name 会把整行标成 "unknown"，缺其它必需字段直接抛异常。
    所以统一输出完整字段；老版本 parser 用 getElementsByTagName 取自己
    要的标签，多余字段会被忽略。所有必需字段都保证非空，缺失填 "0"。
    """
    quotes = tencent_quotes(symbols)
    status = "1" if is_us_market_open() else "0"
    now_ts = int(datetime.now(timezone.utc).timestamp())
    out = ['<list>']
    for s in symbols:
        sym = s.strip().upper()
        f = quotes.get(sym)
        if not f:
            continue

        def fv(i, default="0"):
            return f[i].strip() if len(f) > i and f[i].strip() else default

        # 字段：1=名称 2=代码 3=现价 5=开盘 6=成交量
        # 31=涨跌额 32=涨跌幅 33=最高 34=最低 45=市值
        name = fv(1, sym)
        price = fv(3)
        change = fv(31)
        pct = fv(32)
        openp = fv(5, price)
        high = fv(33, price)
        low = fv(34, price)
        volume = fv(6)
        marketcap = fv(45)
        suffix = f[2].split(".")[-1] if len(f) > 2 and "." in f[2] else ""
        exchange = TENCENT_EXCHANGE.get(suffix, suffix or "NASDAQ")
        out.append(
            '<quote>'
            '<name>%s</name><symbol>%s</symbol><exchange>%s</exchange>'
            '<status>%s</status><timestamp>%d</timestamp>'
            '<price>%s</price><change>%s</change><changepercent>%s</changepercent>'
            '<open>%s</open><high>%s</high><low>%s</low>'
            '<volume>%s</volume><marketcap>%s</marketcap>'
            '<averagedailyvolume>0</averagedailyvolume><peratio>0</peratio>'
            '<yearrange>0</yearrange><dividendyield>0</dividendyield>'
            '<link>%s</link></quote>'
            % (xml_escape(name), xml_escape(sym), xml_escape(exchange),
               status, now_ts,
               xml_escape(price), xml_escape(change), xml_escape(pct),
               xml_escape(openp), xml_escape(high), xml_escape(low),
               xml_escape(volume), xml_escape(marketcap),
               xml_escape("http://finance.yahoo.com/q?s=%s" % sym)))
    out.append('</list>')
    return _dgw_wrap("".join(out))


# getsymbol：按关键词返回公司名/代码/交易所
def build_dgw_symbol_xml(query):
    """getsymbol on wu-stocks.apple.com: best-match <quote> entries."""
    quotes = tencent_quotes([query])
    out = ['<list>']
    if quotes:
        f = next(iter(quotes.values()))
        qsym = query.strip().upper()
        suffix = f[2].split(".")[-1] if "." in f[2] else ""
        exchange = TENCENT_EXCHANGE.get(suffix, suffix)
        name = f[46] if len(f) > 46 and f[46] else f[1]
        out.append(
            '<quote><name>%s</name><symbol>%s</symbol><exchange>%s</exchange>'
            '<link>%s</link></quote>'
            % (xml_escape(name), xml_escape(qsym), xml_escape(exchange),
               xml_escape("http://finance.yahoo.com/q?s=%s" % qsym)))
    out.append('</list>')
    return _dgw_wrap("".join(out))


# 日期 -> 当日美股开盘的 Unix 秒（图表 x 轴）
def chart_unix(datestr):
    """YYYY-MM-DD -> Unix seconds at the US market open (13:30/14:30 UTC)."""
    dt = datetime.strptime(datestr, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # US market open is 09:30 ET; use 14:30 UTC (EST), close enough for chart.
    return int(dt.replace(hour=14, minute=30).timestamp())


def build_dgw_chart_xml(symbol, rng):
    """getchart（wu-charts.apple.com）：走势图数据。

    10.4/10.5 只读 <point timestamp close>；
    10.6/10.7 会先找 <meta>，必须有 symbol/marketopen/marketclose
    （epoch 秒，且不能是 0/-1），否则直接抛 FeedError_MissingTag。
    这里统一带上 <meta>，用首尾 point 的 timestamp 作为开/收盘时间。
    """
    rows = sina_daily_k(symbol)
    n = {"5d": 5, "1d": 5, "1w": 5, "1m": 30, "3m": 90, "6m": 180,
         "1y": 252, "2y": 504}.get((rng or "").strip(), 180)
    rows = rows[-n:]
    ts = [chart_unix(r["d"]) for r in rows]
    meta = ""
    if ts:
        meta = ('<meta><symbol>%s</symbol>'
                '<marketopen>%d</marketopen><marketclose>%d</marketclose>'
                '</meta>' % (xml_escape(symbol.strip().upper()), ts[0], ts[-1]))
    out = []
    for r in rows:
        out.append('<point timestamp="%d" close="%s"/>'
                   % (chart_unix(r["d"]), xml_escape(str(r["c"]))))
    return _dgw_wrap(meta + "".join(out))


# ---------------------------------------------------------------------------
# 10.4/10.5 legacy GET endpoints / 老版 quote/ticker/chart/forex 接口
# The widget uses INDU / COMPX for the Dow / Nasdaq, not ^DJI / ^IXIC.
# ---------------------------------------------------------------------------

# 10.4/10.5 chart.asp 的 DATETIME 格式 "1430   MMDDYYYY"
def _legacy_datetime(datestr):
    # 2026-10-29 -> "1430   10292026", parseable by createDateFromDATETIME:
    # [0:2]hour [2:4]min, [7:9]month [9:11]day [11:15]year.
    y, m, d = datestr.split("-")
    return "1430   %s%s%s" % (m, d, y)


# 10.4/10.5 quote.asp：LASTTRADE/CHANGE/PCTCHANGE/MKT_STATUS
def build_legacy_quote_xml(symbols_csv):
    """GET quote.asp: <INDEXSET><QUOTE>SYMBOL/LASTTRADE/CHANGE/PCTCHANGE/
    MKT_STATUS</QUOTE>...</INDEXSET>."""
    syms = [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]
    quotes = tencent_quotes(syms)
    mkt = "1" if is_us_market_open() else "0"
    out = ["<INDEXSET>"]
    for s in syms:
        f = quotes.get(s)
        if not f:
            continue
        out.append(
            "<QUOTE><SYMBOL>%s</SYMBOL><LASTTRADE>%s</LASTTRADE>"
            "<CHANGE>%s</CHANGE><PCTCHANGE>%s</PCTCHANGE>"
            "<MKT_STATUS>%s</MKT_STATUS></QUOTE>"
            % (xml_escape(s), xml_escape(f[3]), xml_escape(f[31]),
               xml_escape(f[32]), mkt))
    out.append("</INDEXSET>")
    return "".join(out)


# 10.4/10.5 ticker.asp：公司名/代码/交易所
def build_legacy_ticker_xml(search):
    """GET ticker.asp: <INDEXSET><TICKER>COMPANYNAME/SYMBOL/EXCHANGE
    </TICKER></INDEXSET>."""
    qsym = search.strip().upper()
    quotes = tencent_quotes([qsym])
    out = ["<INDEXSET>"]
    f = quotes.get(qsym)
    if f:
        suffix = f[2].split(".")[-1] if "." in f[2] else ""
        exch = TENCENT_EXCHANGE.get(suffix, suffix)
        name = f[46] if len(f) > 46 and f[46] else f[1]
        out.append(
            "<TICKER><COMPANYNAME>%s</COMPANYNAME><SYMBOL>%s</SYMBOL>"
            "<EXCHANGE>%s</EXCHANGE></TICKER>"
            % (xml_escape(name), xml_escape(qsym), xml_escape(exch)))
    out.append("</INDEXSET>")
    return "".join(out)


# 10.4/10.5 chart.asp：DATETIME/CLOSE 序列
def build_legacy_chart_xml(symbol, interval, period):
    """GET chart.asp: <INDEXSET><CHART><QUOTES><QUOTE>DATETIME/CLOSE
    </QUOTE>...</QUOTES></CHART></INDEXSET>."""
    n = 180
    if interval == "15":
        n = 5                                  # 1d
    elif interval == "60":
        n = 5                                  # 1w
    elif period:
        n = {"1:M": 30, "3:M": 90, "6:M": 180,
             "12:M": 252, "24:M": 504}.get(period.strip(), 180)
    rows = sina_daily_k(symbol)[-n:]
    out = ["<INDEXSET><CHART><QUOTES>"]
    for r in rows:
        out.append("<QUOTE><DATETIME>%s</DATETIME><CLOSE>%s</CLOSE></QUOTE>"
                   % (xml_escape(_legacy_datetime(r["d"])),
                      xml_escape(str(r["c"]))))
    out.append("</QUOTES></CHART></INDEXSET>")
    return "".join(out)


def build_legacy_forex_xml():
    """GET forex.asp：<INDEXSET><FOREX>NEXT_UPDATE_DATE/TIME +
    RATELIST/RATE(DATE/TIME/ISO/IN_USD/US_UNIT/CURRENCY_UNIT)。

    注意 USD 也要返回（US_UNIT=CURRENCY_UNIT=1）；widget 不会自己补美元。
    """
    fx = fetch_forex()
    rates = fx["rates"]
    now = datetime.now(timezone.utc)
    next_date = now.strftime("%m/%d/%Y")
    next_time = now.strftime("%H:%M:%S") + " UTC"
    rate_date = now.strftime("%Y-%m-%d")
    rate_time = now.strftime("%H:%M:%S") + " UTC"
    out = ["<INDEXSET><FOREX>",
           "<NEXT_UPDATE_DATE>%s</NEXT_UPDATE_DATE>"
           "<NEXT_UPDATE_TIME>%s</NEXT_UPDATE_TIME>"
           "<RATELIST>" % (next_date, next_time)]
    for iso in sorted(rates.keys()):
        if iso not in FOREX_WHITELIST:
            continue
        per_usd = 1.0 if iso == "USD" else float(rates[iso])
        if per_usd <= 0:
            continue
        out.append(
            "<RATE><DATE>%s</DATE><TIME>%s</TIME><ISO>%s</ISO>"
            "<IN_USD>true</IN_USD><US_UNIT>%.6g</US_UNIT>"
            "<CURRENCY_UNIT>%.6g</CURRENCY_UNIT></RATE>"
            % (rate_date, rate_time, iso, per_usd, 1.0 / per_usd))
    out.append("</RATELIST></FOREX></INDEXSET>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Unit Converter currencies / 汇率：open.er-api.com 免费接口
# US_UNIT = value of 1 unit of currency in USD.
# ---------------------------------------------------------------------------

_FOREX_CACHE = {"at": 0.0, "rates": {}, "time": None}
_FOREX_TTL = 3600

# Only emit currencies that existed in Mac OS X 10.4 (2005) locale data and
# are still live today, so the widget's dropdown never shows blank names.
FOREX_WHITELIST = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK",
    "DKK", "CZK", "HUF", "PLN", "RON", "BGN", "HRK", "TRY", "RUB", "UAH",
    "ISK", "LTL", "LVL", "CNY", "HKD", "TWD", "KRW", "SGD", "MYR", "THB",
    "IDR", "PHP", "VND", "INR", "PKR", "BDT", "LKR", "ILS", "SAR", "AED",
    "KWD", "BHD", "QAR", "OMR", "JOD", "EGP", "ZAR", "MAD", "NGN", "KES",
    "MXN", "BRL", "ARS", "CLP", "COP", "PEN", "UYU", "KZT",
}


def fetch_forex():
    # open.er-api.com 免费免 key，返回以 USD 为基准的 rates（1 USD = X 单位）；
    # 缓存 1 小时，避免老 widget 每次重试都打一次上游。
    now = datetime.now(timezone.utc).timestamp()
    if _FOREX_CACHE["rates"] and now - _FOREX_CACHE["at"] < _FOREX_TTL:
        return _FOREX_CACHE
    data = http_get_json("https://open.er-api.com/v6/latest/USD")
    rates = data.get("rates") or {}
    if rates:
        _FOREX_CACHE["rates"] = rates
        _FOREX_CACHE["at"] = now
        _FOREX_CACHE["time"] = data.get("time_last_update_utc")
    return _FOREX_CACHE


def build_dgw_converter_xml():
    """convertcurrency（wu-converter.apple.com）：返回全部币种汇率。

    <convertedamount> 表示 1 美元能换多少目标币种；parser 自己取倒数得到
    rateToUSDollar。<result> 必须带 timestamp 属性。

    注意：USD 必须一起返回（convertedamount=1）。widget 的币种下拉完全由
    这些 <conversion> 生成，它不会自己补美元，只是在排序时把 USD 排第一。
    """
    fx = fetch_forex()
    rates = fx["rates"]
    ts = int(datetime.now(timezone.utc).timestamp())
    out = ['<result timestamp="%d">' % ts]
    for iso in sorted(rates.keys()):
        if iso not in FOREX_WHITELIST:
            continue
        per_usd = 1.0 if iso == "USD" else float(rates[iso])  # 1 USD 能换多少目标币种
        if per_usd <= 0:
            continue
        out.append('<conversion><to>%s</to><convertedamount>%.6g'
                   '</convertedamount></conversion>'
                   % (iso, per_usd))
    out.append('</result>')
    # strip the helper wrapper; the parser expects <result> itself.
    inner = "".join(out)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<response>' + inner + \
           '</response>'


# ---------------------------------------------------------------------------
# Translation widget / 翻译：POST /trans，后端百度翻译
# w4.systranlinks.com commented out). Backend is Baidu General Translation.
# Systran language codes differ from Baidu's own codes.
# ---------------------------------------------------------------------------

# Baidu uses its own non-BCP codes (jp/kor/fra/spa/ara/cht ...).
SYSTRAN_TO_BAIDU = {
    "zh": "zh", "zt": "cht", "en": "en", "nl": "nl",
    "fr": "fra", "de": "de", "el": "el", "it": "it",
    "ja": "jp", "ko": "kor", "pt": "pt", "ru": "ru", "es": "spa",
}
SYSTRAN_CODEC = {
    "utf-8": "utf-8",
    "euc-cn": "gb2312",
    "big5": "big5",
    "shift-jis": "shift_jis",
    "euc-kr": "cp949",
    "windows-1251": "cp1251",
    "iso-8859-7": "iso8859_7",
    "iso-8859-1": "latin-1",
}


# Baidu General Translation API, standard tier: free, no char cap, QPS=1, no
# developer verification. Configure via env vars; without them it stays off.
BAIDU_APPID = os.environ.get("BAIDU_APPID", "").strip()
BAIDU_KEY = os.environ.get("BAIDU_KEY", "").strip()
BAIDU_ENDPOINT = "https://fanyi-api.baidu.com/api/trans/vip/translate"


# 调百度通用翻译（免费标准版，QPS=1，需 BAIDU_APPID/BAIDU_KEY）
def baidu_translate(text, src, dst):
    """Real MT engine. Returns None when unconfigured or on any error."""
    if not BAIDU_APPID or not BAIDU_KEY:
        return None
    # sign = md5(appid + q + salt + key), signed over the RAW q (not URL-encoded)
    salt = str(int.from_bytes(os.urandom(8), "big"))
    sign = hashlib.md5(
        (BAIDU_APPID + text + salt + BAIDU_KEY).encode("utf-8")).hexdigest()
    query = urllib.parse.urlencode({"q": text, "from": src, "to": dst,
                                    "appid": BAIDU_APPID, "salt": salt,
                                    "sign": sign})
    req = urllib.request.Request(BAIDU_ENDPOINT + "?" + query,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error_code" in data:
        print("baidu translate error:", data.get("error_code"),
              data.get("error_msg"))
        return None
    # Multi-line input yields several trans_result segments; join them.
    return "".join(seg.get("dst", "") for seg in data.get("trans_result", []))


# 是否含汉字（判断中文译文是否有效）
def _has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


# 拒绝空译文、原文回显、中文目标却无汉字
def is_good_translation(original, translated, dst_systran):
    """Reject empty, echo-of-source, and a Chinese target that contains no Han
    characters (guards against an engine echoing the source language)."""
    if not translated:
        return False
    if translated.strip().lower() == original.strip().lower():
        return False
    if dst_systran in ("zh", "zt") and not _has_cjk(translated):
        return False
    return True


# Systran 语言对 -> 百度语言码；失败时回显原文（fail open）
def translate_text(text, lp):
    parts = (lp or "en_zh").split("_")
    src_sy = parts[0]
    dst_sy = parts[1] if len(parts) > 1 else "en"
    src_bd = SYSTRAN_TO_BAIDU.get(src_sy, "en")
    dst_bd = SYSTRAN_TO_BAIDU.get(dst_sy, "en")

    # Baidu is the only translation engine (MyMemory removed).
    try:
        result = baidu_translate(text, src_bd, dst_bd)
    except Exception as e:
        print("baidu translate error:", e)
        result = None
    if is_good_translation(text, result, dst_sy):
        return result
    return text  # fail open: echo source rather than show an empty widget


# 解析 systran 表单 POST，按老格式返回 "x!xxxxxxxx<译文>]"
def build_translation(body_bytes):
    """Parse the systran POST body and return a body in the old response shape."""
    body = body_bytes.replace(b"&amp;", b"&").decode("latin-1")
    fields = urllib.parse.parse_qs(body, encoding="latin-1",
                                  keep_blank_values=True)

    def fld(k):
        return fields.get(k, [""])[0]

    charset_raw = fld("charset")
    lp = fld("lp")
    # parse_qs decoded percent-sequences with latin-1: one char per byte.
    text_latin = fld("text")
    codec = SYSTRAN_CODEC.get(charset_raw.strip().lower(), "utf-8")
    text = text_latin.encode("latin-1").decode(codec, "replace")

    result = ""
    try:
        result = translate_text(text, lp)
    except Exception as e:
        print("translation error:", e)
        result = text  # fail open: echo the source instead of erroring

    # Parser: find first '!', skip 8 chars after it, then take up to ']'.
    return ("x!xxxxxxxx" + result + "]").encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server / HTTP 服务与路由
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DashboardRevival/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(),
                                        fmt % args))

    # 统一响应头：Content-Type / Content-Length / Connection: close
    def _send(self, body, ctype="text/xml; charset=utf-8", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    # GET：10.4/10.5 天气 + legacy 股票/汇率端点
    def do_GET(self):
        try:
            self.route()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send("<?xml version=\"1.0\"?><adc_Database><error>%s"
                       "</error></adc_Database>" % xml_escape(str(e)),
                       status=500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            path = urllib.parse.urlparse(self.path).path
            # 翻译走独立的 /trans（表单编码，不是 XML）
            if path.endswith("/trans"):
                self._send(build_translation(body),
                           ctype="text/xml; charset=utf-8")
                return
            if path.endswith("/dgw"):
                # 按请求体的 query type 分发：天气/股票/汇率共用一个入口
                kind, p = parse_dgw_request(body)
                if kind == "location":
                    places = lookup_city_text(p.get("phrase", ""))
                    self._send(build_dgw_locations_xml(places[:12]))
                    return
                if kind == "forecast":
                    place = resolve_postal(p.get("id", "")) or CUPERTINO
                    self._send(build_dgw_forecast_xml(
                        place, fetch_forecast(place)))
                    return
                if kind == "quotes":
                    self._send(build_dgw_quotes_xml(p.get("symbols", [])))
                    return
                if kind == "symbol":
                    self._send(build_dgw_symbol_xml(p.get("phrase", "")))
                    return
                if kind == "chart":
                    self._send(build_dgw_chart_xml(
                        p.get("symbol", ""), p.get("range", "")))
                    return
                if kind == "converter":
                    self._send(build_dgw_converter_xml())
                    return
                if kind == "news":
                    self._send('<?xml version="1.0"?><response><result>'
                               '<list/></result></response>')
                    return
                self._send('<?xml version="1.0"?><response><result><list/>'
                           '</result></response>', ctype="text/xml")
                return
            self._send("not found", ctype="text/plain", status=404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send("x!xxxxxxxx" + "]", ctype="text/xml; charset=utf-8")

    # GET 路由表：legacy .asp、AccuWeather、健康检查
    def route(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        def q(name):
            return (qs.get(name, [""])[0] or "").strip()

        # ---------- 10.4/10.5 legacy /fq/applewidgets GET endpoints ----------
        if path.endswith("/quote.asp"):
            self._send(build_legacy_quote_xml(q("symbols")))
            return
        if path.endswith("/ticker.asp"):
            self._send(build_legacy_ticker_xml(q("search")))
            return
        if path.endswith("/chart.asp"):
            self._send(build_legacy_chart_xml(
                q("symbols"), q("interval"), q("period")))
            return
        if path.endswith("/forex.asp"):
            self._send(build_legacy_forex_xml())
            return

        # ---------- AccuWeather (10.4 / 10.5) ----------
        if path.endswith("/Apple_find_city.asp"):
            location = q("location")
            places = lookup_city_text(location)
            # If the search is broad, Open-Meteo already caps results; mark
            # refine only when there are many hits.
            refine = len(places) > 12
            self._send(build_find_city_xml(places[:12], refine))
            return

        if path.endswith("/Apple_Weather_Data.asp"):
            postal = q("zipcode")
            place = resolve_postal(postal)
            if not place:
                self._send(build_weather_data_xml(
                    {"name": "Cupertino", "lat": 37.323, "lon": -122.032,
                     "state": "CA", "country_code": "US", "tz": "America/Los_Angeles"},
                    fetch_forecast({"name": "Cupertino", "lat": 37.323,
                                    "lon": -122.032, "tz": "America/Los_Angeles"})))
                return
            self._send(build_weather_data_xml(place, fetch_forecast(place)))
            return

        # Stocks, currency and 10.6/10.7 weather all use the POST /dgw
        # gateway, handled in do_POST (routed by the query type).

        # ---------- health / index ----------
        if path in ("/", "/index.html", "/health"):
            self._send("Dashboard revival server OK. Endpoints: "
                       "Apple_find_city.asp, Apple_Weather_Data.asp (10.4/10.5), "
                       "POST /dgw (weather 10.6/10.7, stocks, currency), "
                       "POST /trans (translation)\n",
                       ctype="text/plain; charset=utf-8")
            return

        self._send("not found", ctype="text/plain", status=404)


# 启动服务：默认 80，可用 argv[1] 或 PORT 覆盖
def main():
    port = LISTEN_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    elif os.environ.get("PORT"):
        port = int(os.environ["PORT"])
    load_cache()
    load_zipcodes()
    httpd = ThreadingHTTPServer((LISTEN_HOST, port), Handler)
    print("Serving classic Dashboard widgets on %s:%d" % (LISTEN_HOST, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()