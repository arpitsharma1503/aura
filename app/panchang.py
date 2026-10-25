"""Hindu Panchang for the Calendar: Tithi (with Paksha and its start and end), Nakshatra, Yoga,
Karana, lunar month, Vikram and Shaka Samvat, sunrise and sunset, for a date and place.

Calculated on the Pi with the Swiss Ephemeris (``pyswisseph``, built-in Moshier model: no
data files, arcsecond-level positions), not looked up from a table or a 29.5-day cycle:

* **Tithi** = the Moon's apparent geocentric longitude minus the Sun's, in 12° steps (30 per
  lunar month). Shukla Paksha is 0-180°, Krishna Paksha 180-360°. Its start and end are the
  moments that angle crosses a multiple of 12°, found by iteration to under a second.
* **Karana** = half a Tithi (6° steps), with the fixed and the repeating sequence.
* **Nakshatra** = the Moon's sidereal longitude in 13°20' steps; **Yoga** = the Sun's plus the
  Moon's sidereal longitude in 13°20' steps. Sidereal = Lahiri (Chitrapaksha) ayanamsa,
  the Government of India's standard.
* **Day's Tithi** (udaya tithi) is the one at local sunrise, as almanacs list it; the current
  Tithi is the one at this moment. Sunrise and sunset are for the upper limb of the Sun with
  atmospheric refraction, at the home location's coordinates.
* **Lunar month:** an amanta month runs from new moon to new moon and is named after the
  Sun's sidereal sign at its starting new moon (Sun in Meena -> Chaitra). A month whose two
  new moons fall in the same sign is Adhik (an extra month). North India counts months
  purnimanta (from full moon), so during Krishna Paksha the purnimanta month is the next one.
* **Vikram Samvat** starts at Chaitra Shukla Pratipada (Gregorian year + 57 after it);
  Shaka Samvat = Gregorian year - 78 after it.

Nothing here needs the internet. Festivals beyond what the Tithi itself defines (Ekadashi,
Purnima, Amavasya) aren't calculated: they follow regional rules; the household's holiday
calendar feed lists them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import swisseph as swe

swe.set_ephe_path(None)
_FLAGS = swe.FLG_MOSEPH | swe.FLG_SPEED
_SID = _FLAGS | swe.FLG_SIDEREAL
swe.set_sid_mode(swe.SIDM_LAHIRI)
NAK = 360 / 27

TITHIS = [("प्रतिपदा", "Pratipada"), ("द्वितीया", "Dwitiya"), ("तृतीया", "Tritiya"), ("चतुर्थी", "Chaturthi"),
          ("पंचमी", "Panchami"), ("षष्ठी", "Shashthi"), ("सप्तमी", "Saptami"), ("अष्टमी", "Ashtami"),
          ("नवमी", "Navami"), ("दशमी", "Dashami"), ("एकादशी", "Ekadashi"), ("द्वादशी", "Dwadashi"),
          ("त्रयोदशी", "Trayodashi"), ("चतुर्दशी", "Chaturdashi")]
PURNIMA, AMAVASYA = ("पूर्णिमा", "Purnima"), ("अमावस्या", "Amavasya")
PAKSHA = {"shukla": ("शुक्ल पक्ष", "Shukla Paksha"), "krishna": ("कृष्ण पक्ष", "Krishna Paksha")}
NAKSHATRAS = [("अश्विनी", "Ashwini"), ("भरणी", "Bharani"), ("कृत्तिका", "Krittika"), ("रोहिणी", "Rohini"),
              ("मृगशिरा", "Mrigashira"), ("आर्द्रा", "Ardra"), ("पुनर्वसु", "Punarvasu"), ("पुष्य", "Pushya"),
              ("आश्लेषा", "Ashlesha"), ("मघा", "Magha"), ("पूर्वा फाल्गुनी", "Purva Phalguni"),
              ("उत्तरा फाल्गुनी", "Uttara Phalguni"), ("हस्त", "Hasta"), ("चित्रा", "Chitra"), ("स्वाति", "Swati"),
              ("विशाखा", "Vishakha"), ("अनुराधा", "Anuradha"), ("ज्येष्ठा", "Jyeshtha"), ("मूल", "Mula"),
              ("पूर्वाषाढ़ा", "Purva Ashadha"), ("उत्तराषाढ़ा", "Uttara Ashadha"), ("श्रवण", "Shravana"),
              ("धनिष्ठा", "Dhanishtha"), ("शतभिषा", "Shatabhisha"), ("पूर्वा भाद्रपद", "Purva Bhadrapada"),
              ("उत्तरा भाद्रपद", "Uttara Bhadrapada"), ("रेवती", "Revati")]
YOGAS = [("विष्कम्भ", "Vishkambha"), ("प्रीति", "Priti"), ("आयुष्मान्", "Ayushman"), ("सौभाग्य", "Saubhagya"),
         ("शोभन", "Shobhana"), ("अतिगण्ड", "Atiganda"), ("सुकर्मा", "Sukarma"), ("धृति", "Dhriti"), ("शूल", "Shula"),
         ("गण्ड", "Ganda"), ("वृद्धि", "Vriddhi"), ("ध्रुव", "Dhruva"), ("व्याघात", "Vyaghata"), ("हर्षण", "Harshana"),
         ("वज्र", "Vajra"), ("सिद्धि", "Siddhi"), ("व्यतीपात", "Vyatipata"), ("वरीयान्", "Variyana"), ("परिघ", "Parigha"),
         ("शिव", "Shiva"), ("सिद्ध", "Siddha"), ("साध्य", "Sadhya"), ("शुभ", "Shubha"), ("शुक्ल", "Shukla"),
         ("ब्रह्म", "Brahma"), ("इन्द्र", "Indra"), ("वैधृति", "Vaidhriti")]
KARANA_CYCLE = [("बव", "Bava"), ("बालव", "Balava"), ("कौलव", "Kaulava"), ("तैतिल", "Taitila"), ("गर", "Gara"),
                ("वणिज", "Vanija"), ("विष्टि", "Vishti")]
KARANA_FIXED = {0: ("किंस्तुघ्न", "Kimstughna"), 57: ("शकुनि", "Shakuni"), 58: ("चतुष्पद", "Chatushpada"), 59: ("नाग", "Naga")}
MONTHS = [("चैत्र", "Chaitra"), ("वैशाख", "Vaishakha"), ("ज्येष्ठ", "Jyeshtha"), ("आषाढ़", "Ashadha"),
          ("श्रावण", "Shravana"), ("भाद्रपद", "Bhadrapada"), ("आश्विन", "Ashwin"), ("कार्तिक", "Kartika"),
          ("मार्गशीर्ष", "Margashirsha"), ("पौष", "Pausha"), ("माघ", "Magha"), ("फाल्गुन", "Phalguna")]


# --------------------------------------------------------------------------- positions

def _jd(t: datetime) -> float:
    t = t.astimezone(UTC)
    return swe.julday(t.year, t.month, t.day, t.hour + t.minute / 60 + (t.second + t.microsecond / 1e6) / 3600)


def _dt(jd: float) -> datetime:
    y, m, d, h = swe.revjul(jd)
    return datetime(y, m, d, tzinfo=UTC) + timedelta(hours=h)


def _lon(jd: float, body: int, flags: int) -> tuple[float, float]:
    """(longitude, speed in degrees/day)."""
    x = swe.calc_ut(jd, body, flags)[0]
    return x[0], x[3]


def elongation(jd: float) -> tuple[float, float]:
    """Moon minus Sun, apparent geocentric, 0-360 degrees, and its rate (degrees/day)."""
    m, ms = _lon(jd, swe.MOON, _FLAGS)
    s, ss = _lon(jd, swe.SUN, _FLAGS)
    return (m - s) % 360, ms - ss


def moon_sidereal(jd: float) -> tuple[float, float]:
    return _lon(jd, swe.MOON, _SID)


def yoga_angle(jd: float) -> tuple[float, float]:
    m, ms = _lon(jd, swe.MOON, _SID)
    s, ss = _lon(jd, swe.SUN, _SID)
    return (m + s) % 360, ms + ss


def sun_sidereal(jd: float) -> float:
    return _lon(jd, swe.SUN, _SID)[0]


def _crossing(f, jd: float, target: float) -> float:
    """When the angle f (a function returning (angle, rate)) next reaches ``target`` near jd.
    Newton steps on the wrapped difference; converges to well under a second."""
    for _ in range(50):
        a, rate = f(jd)
        diff = ((target - a + 180) % 360) - 180
        if abs(diff) < 1e-7:
            break
        jd += diff / rate
    return jd


def _span(f, jd: float, step: float) -> tuple[int, float, float]:
    """(index, start jd, end jd) of the step-sized segment of angle f that contains jd."""
    a, _ = f(jd)
    i = int(a // step)
    start = _crossing(f, jd - ((a - i * step) / 13.0), (i * step) % 360)
    end = _crossing(f, jd + (((i + 1) * step - a) / 13.0), ((i + 1) * step) % 360)
    if start > jd:                       # numerical edge right at a boundary
        start = _crossing(f, jd - 0.5, (i * step) % 360)
    return i % int(round(360 / step)), start, end


# --------------------------------------------------------------------------- names

def tithi_names(i: int) -> dict:
    """i = 0..29 (0 = Shukla Pratipada, 14 = Purnima, 29 = Amavasya)."""
    paksha = "shukla" if i < 15 else "krishna"
    n = i % 15
    hi, en = (PURNIMA if i == 14 else AMAVASYA) if n == 14 else TITHIS[n]
    return {"index": i, "number": n + 1, "paksha": paksha, "paksha_hi": PAKSHA[paksha][0], "paksha_en": PAKSHA[paksha][1],
            "name_hi": hi, "name_en": en}


def karana_names(k: int) -> tuple[str, str]:
    return KARANA_FIXED.get(k) or KARANA_CYCLE[(k - 1) % 7]


def _iso(jd: float, tz: ZoneInfo) -> str:
    return _dt(jd).astimezone(tz).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- months and years

def _new_moon_before(jd: float) -> float:
    a, _ = elongation(jd)
    return _crossing(elongation, jd - a / 12.2, 0.0)


def _new_moon_after(jd: float) -> float:
    a, _ = elongation(jd)
    return _crossing(elongation, jd + (360 - a) / 12.2, 0.0)


@lru_cache(maxsize=256)
def lunar_month(start_new_moon_jd: float) -> dict:
    """Amanta month that starts at this new moon: index (Chaitra = 0) and whether it's Adhik."""
    rashi = int(sun_sidereal(start_new_moon_jd) // 30)
    next_nm = _new_moon_after(start_new_moon_jd + 1)
    adhik = int(sun_sidereal(next_nm) // 30) == rashi          # no sankranti in this month
    return {"index": (rashi + 1) % 12, "adhik": adhik, "start": start_new_moon_jd, "end": next_nm}


def month_at(jd: float) -> dict:
    return lunar_month(round(_new_moon_before(jd), 6))


def samvat(jd: float) -> dict:
    """Vikram and Shaka Samvat from the most recent Chaitra Shukla Pratipada (non-Adhik Chaitra start)."""
    nm = _new_moon_before(jd)
    for _ in range(14):
        m = lunar_month(round(nm, 6))
        if m["index"] == 0 and not m["adhik"]:
            y = _dt(nm).year
            return {"vikram": y + 57, "shaka": y - 78}
        nm = _new_moon_before(nm - 1)
    raise RuntimeError("no Chaitra found in 14 lunar months")


# --------------------------------------------------------------------------- sun

def _sun_event(jd_start: float, lat: float, lon: float, rising: bool) -> float | None:
    flag = swe.CALC_RISE if rising else swe.CALC_SET
    res, tret = swe.rise_trans(jd_start, swe.SUN, flag, (lon, lat, 0.0), 0.0, 0.0, swe.FLG_MOSEPH)
    return tret[0] if res == 0 else None


# --------------------------------------------------------------------------- the panchang

def _segments(f, step: float, start: float, end: float, names, tz: ZoneInfo) -> list[dict]:
    """Every segment of angle f between two moments (e.g. all Tithis between sunrise and the
    next sunrise: usually one, sometimes two, rarely none changes)."""
    out, jd = [], start
    while jd < end and len(out) < 6:
        i, s, e = _span(f, jd + 1e-6, step)
        out.append({**names(i), "start": _iso(s, tz), "end": _iso(e, tz)})
        jd = e
    return out


def panchang(d: date, tz_name: str, lat: float, lon: float, now: datetime | None = None) -> dict:
    tz = ZoneInfo(tz_name)
    midnight = datetime.combine(d, time(0), tz)
    jd0 = _jd(midnight)
    rise = _sun_event(jd0, lat, lon, True)
    set_ = _sun_event(rise or jd0, lat, lon, False)
    next_rise = _sun_event((rise or jd0) + 0.5, lat, lon, True)
    if rise is None or next_rise is None:
        raise ValueError("no sunrise at this place and date")
    tithi_names_ = tithi_names
    nak_names = lambda i: {"index": i, "name_hi": NAKSHATRAS[i][0], "name_en": NAKSHATRAS[i][1]}  # noqa: E731
    yoga_names = lambda i: {"index": i, "name_hi": YOGAS[i][0], "name_en": YOGAS[i][1]}            # noqa: E731
    kar_names = lambda k: {"index": k, "name_hi": karana_names(k)[0], "name_en": karana_names(k)[1]}  # noqa: E731

    tithis = _segments(elongation, 12.0, rise, next_rise, tithi_names_, tz)
    month = month_at(rise)
    m_hi, m_en = MONTHS[month["index"]]
    sunrise_tithi = tithis[0]
    pur_index = (month["index"] + 1) % 12 if sunrise_tithi["paksha"] == "krishna" else month["index"]
    observance = {14: ("पूर्णिमा", "Purnima"), 29: ("अमावस्या", "Amavasya"),
                  10: ("एकादशी", "Ekadashi"), 25: ("एकादशी", "Ekadashi")}.get(sunrise_tithi["index"])
    out = {
        "date": d.isoformat(),
        "timezone": tz_name,
        "sunrise": _iso(rise, tz), "sunset": _iso(set_, tz) if set_ else None, "next_sunrise": _iso(next_rise, tz),
        "tithi": sunrise_tithi,                      # the day's Tithi (at sunrise)
        "tithis": tithis,                            # every Tithi from this sunrise to the next
        "nakshatras": _segments(moon_sidereal, NAK, rise, next_rise, nak_names, tz),
        "yogas": _segments(yoga_angle, NAK, rise, next_rise, yoga_names, tz),
        "karanas": _segments(elongation, 6.0, rise, next_rise, kar_names, tz),
        "month": {"amanta_hi": ("अधिक " if month["adhik"] else "") + m_hi, "amanta_en": ("Adhik " if month["adhik"] else "") + m_en,
                  "purnimanta_hi": ("अधिक " if month["adhik"] else "") + MONTHS[pur_index][0],
                  "purnimanta_en": ("Adhik " if month["adhik"] else "") + MONTHS[pur_index][1], "adhik": month["adhik"]},
        "samvat": samvat(rise),
        "observance": {"name_hi": observance[0], "name_en": observance[1]} if observance else None,
        "conventions": {"ephemeris": "Swiss Ephemeris (Moshier)", "ayanamsa": "Lahiri (Chitrapaksha)",
                        "day_tithi": "at local sunrise", "sunrise": "upper limb, with refraction",
                        "months": "amanta and purnimanta", "location": [round(lat, 4), round(lon, 4)]},
    }
    if now is not None:
        jn = _jd(now)
        i, s, e = _span(elongation, jn, 12.0)
        nxt = tithi_names_((i + 1) % 30)
        out["now"] = {"at": now.astimezone(tz).isoformat(timespec="seconds"),
                      "tithi": {**tithi_names_(i), "start": _iso(s, tz), "end": _iso(e, tz)},
                      "next": {**nxt, "start": _iso(e, tz)},
                      "month_en": out["month"]["purnimanta_en"]}
        ni, ns, ne = _span(moon_sidereal, jn, NAK)
        out["now"]["nakshatra"] = {**nak_names(ni), "start": _iso(ns, tz), "end": _iso(ne, tz)}
    return out


def _check() -> None:      # pragma: no cover - quick manual sanity check
    p = panchang(date.today(), "Asia/Kolkata", 27.18, 78.01, datetime.now(UTC))
    print(p["tithi"], p["now"]["tithi"]["end"], p["month"], p["samvat"])


if __name__ == "__main__":  # pragma: no cover
    _check()
