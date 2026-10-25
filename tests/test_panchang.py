"""Panchang: validated against Drik Panchang (New Delhi) for three days covering Shukla and
Krishna Paksha, a Tithi that ends after midnight, and an Adhik (extra) month; plus the
structure, boundaries and time zones of the calculation."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.panchang import karana_names, panchang, tithi_names

DELHI = (28.6139, 77.2090)
IST = ZoneInfo("Asia/Kolkata")


def at(s: str) -> datetime:
    return datetime.fromisoformat(s)


def near(iso: str, expected: str, minutes: float = 2.0) -> bool:
    return abs((at(iso) - datetime.fromisoformat(expected).replace(tzinfo=IST)).total_seconds()) <= minutes * 60


def test_shukla_day_matches_drik_panchang():
    """25 Oct 2026, New Delhi (drikpanchang.com): sunrise 06:28, sunset 17:42, Shukla Chaturdashi
    upto 11:55, Revati upto 19:22, Harshana upto 17:22, Vanija upto 11:55, Vishti upto 22:52,
    Ashwin (amanta and purnimanta), Vikram Samvat 2083, Shaka 1948."""
    p = panchang(date(2026, 10, 25), "Asia/Kolkata", *DELHI)
    assert near(p["sunrise"], "2026-10-25T06:28") and near(p["sunset"], "2026-10-25T17:42")
    t = p["tithis"]
    assert (t[0]["paksha_en"], t[0]["name_en"]) == ("Shukla Paksha", "Chaturdashi") and near(t[0]["end"], "2026-10-25T11:55")
    assert t[1]["name_en"] == "Purnima"
    assert p["nakshatras"][0]["name_en"] == "Revati" and near(p["nakshatras"][0]["end"], "2026-10-25T19:22")
    assert p["yogas"][0]["name_en"] == "Harshana" and near(p["yogas"][0]["end"], "2026-10-25T17:22")
    k = p["karanas"]
    assert (k[0]["name_en"], k[1]["name_en"]) == ("Vanija", "Vishti")
    assert near(k[0]["end"], "2026-10-25T11:55") and near(k[1]["end"], "2026-10-25T22:52")
    assert p["month"]["amanta_en"] == "Ashwin" and p["month"]["purnimanta_en"] == "Ashwin"
    assert p["samvat"] == {"vikram": 2083, "shaka": 1948}


def test_krishna_day_and_purnimanta_month_match_drik_panchang():
    """3 Nov 2026: Krishna Navami upto 11:54, Magha upto 03:26 (Nov 4), Shukla yoga upto 09:54,
    Gara upto 11:54, Vanija upto 23:25; amanta Ashwin, purnimanta Kartika."""
    p = panchang(date(2026, 11, 3), "Asia/Kolkata", *DELHI)
    assert near(p["sunrise"], "2026-11-03T06:34")
    t = p["tithis"][0]
    assert (t["paksha_en"], t["name_en"]) == ("Krishna Paksha", "Navami") and near(t["end"], "2026-11-03T11:54")
    assert p["nakshatras"][0]["name_en"] == "Magha" and near(p["nakshatras"][0]["end"], "2026-11-04T03:26")
    assert p["yogas"][0]["name_en"] == "Shukla" and near(p["yogas"][0]["end"], "2026-11-03T09:54")
    assert [k["name_en"] for k in p["karanas"][:2]] == ["Gara", "Vanija"] and near(p["karanas"][1]["end"], "2026-11-03T23:25")
    assert (p["month"]["amanta_en"], p["month"]["purnimanta_en"]) == ("Ashwin", "Kartika")


def test_adhik_month_and_tithi_past_midnight_match_drik_panchang():
    """25 May 2026: Shukla Dashami upto 05:10 May 26 (after midnight, before the next sunrise at
    05:26, so Ekadashi also begins within this day),
    Uttara Phalguni upto 04:08 May 26, Vajra upto 03:15 May 26, Adhik Jyeshtha."""
    p = panchang(date(2026, 5, 25), "Asia/Kolkata", *DELHI)
    assert near(p["sunrise"], "2026-05-25T05:26")
    t = p["tithis"][0]
    assert t["name_en"] == "Dashami" and near(t["end"], "2026-05-26T05:10")
    assert p["tithis"][1]["name_en"] == "Ekadashi" and near(p["tithis"][1]["start"], "2026-05-26T05:10")   # starts before the next sunrise
    assert near(p["nakshatras"][0]["end"], "2026-05-26T04:08") and near(p["yogas"][0]["end"], "2026-05-26T03:15")
    assert p["month"]["adhik"] and p["month"]["amanta_en"] == "Adhik Jyeshtha" and p["month"]["amanta_hi"] == "अधिक ज्येष्ठ"
    assert p["samvat"]["vikram"] == 2083


def test_names_cover_both_pakshas_and_karanas():
    assert tithi_names(0)["name_en"] == "Pratipada" and tithi_names(14)["name_en"] == "Purnima"
    assert tithi_names(15)["paksha"] == "krishna" and tithi_names(15)["name_en"] == "Pratipada"
    assert tithi_names(29)["name_hi"] == "अमावस्या" and tithi_names(4)["name_hi"] == "पंचमी"
    assert tithi_names(19)["number"] == 5 and tithi_names(19)["paksha_hi"] == "कृष्ण पक्ष"
    assert [karana_names(k)[1] for k in (0, 1, 7, 8, 56, 57, 58, 59)] == \
        ["Kimstughna", "Bava", "Vishti", "Bava", "Vishti", "Shakuni", "Chatushpada", "Naga"]


def test_now_tithi_and_its_change():
    now = datetime(2026, 10, 25, 9, 0, tzinfo=IST)
    p = panchang(date(2026, 10, 25), "Asia/Kolkata", *DELHI, now=now)
    n = p["now"]
    assert n["tithi"]["name_en"] == "Chaturdashi" and near(n["tithi"]["end"], "2026-10-25T11:55")
    assert n["next"]["name_en"] == "Purnima" and n["next"]["start"] == n["tithi"]["end"]
    later = panchang(date(2026, 10, 25), "Asia/Kolkata", *DELHI, now=now + timedelta(hours=4))["now"]
    assert later["tithi"]["name_en"] == "Purnima"                  # the boundary passed


def test_full_and_new_moon_are_tithi_boundaries():
    """Purnima ends at the full moon and Amavasya at the new moon (USNO: full moon 26 Oct 2026
    04:12 UTC, new moon 9 Nov 2026 07:02 UTC)."""
    p = panchang(date(2026, 10, 25), "Asia/Kolkata", *DELHI, now=datetime(2026, 10, 25, 15, tzinfo=IST))
    assert abs(at(p["now"]["tithi"]["end"]) - datetime(2026, 10, 26, 4, 12, tzinfo=UTC)) < timedelta(minutes=2)
    q = panchang(date(2026, 11, 9), "Asia/Kolkata", *DELHI, now=datetime(2026, 11, 9, 7, tzinfo=IST))
    assert q["now"]["tithi"]["name_en"] == "Amavasya"
    assert abs(at(q["now"]["tithi"]["end"]) - datetime(2026, 11, 9, 7, 2, tzinfo=UTC)) < timedelta(minutes=2)


@pytest.mark.parametrize("d", [date(2026, 12, 31), date(2027, 1, 1), date(2026, 2, 28), date(2028, 2, 29)])
def test_year_and_month_boundaries_are_continuous(d):
    p = panchang(d, "Asia/Kolkata", *DELHI)
    for seq in ("tithis", "nakshatras", "yogas", "karanas"):
        s = p[seq]
        assert at(s[0]["start"]) <= at(p["sunrise"]) < at(s[0]["end"])
        for a, b in zip(s, s[1:]):
            assert a["end"] == b["start"]
        assert at(s[-1]["end"]) >= at(p["next_sunrise"])


def test_times_are_in_the_requested_time_zone():
    p = panchang(date(2026, 10, 25), "Asia/Kolkata", *DELHI)
    assert p["sunrise"].endswith("+05:30")
    q = panchang(date(2026, 10, 25), "Europe/London", 51.5, -0.12)
    assert q["sunrise"].endswith("+00:00")                     # British Summer Time ended that morning
    assert panchang(date(2026, 10, 24), "Europe/London", 51.5, -0.12)["sunrise"].endswith("+01:00")
    # the same place shown in another time zone: the same moments, different clock times
    delhi_in_london = panchang(date(2026, 10, 25), "Europe/London", *DELHI)
    assert at(delhi_in_london["tithis"][0]["end"]) == at(p["tithis"][0]["end"])
    assert delhi_in_london["tithis"][0]["end"].endswith("+00:00")


# --------------------------------------------------------------------------- API

from conftest import make_client, save_location  # noqa: E402


def test_api_uses_the_home_location_and_any_date(tmp_path):
    save_location(tmp_path)                                              # Pune, Asia/Kolkata
    c = make_client(tmp_path)
    today = c.get("/api/panchang").json()
    assert today["place"] == "Pune" and today["date"] == today["today"] and today["now"]["tithi"]["name_hi"]
    assert today["conventions"]["location"] == [18.52, 73.86]
    other = c.get("/api/panchang?date=2026-11-03").json()
    assert other["tithis"][0]["name_en"] == "Navami" and other["date"] == "2026-11-03"
    assert other["now"] == today["now"]                                  # "now" is always the present moment
    assert c.get("/api/panchang?date=2026-13-01").status_code == 422
    assert c.get("/api/panchang?date=1800-01-01").status_code == 422


def test_api_refuses_without_a_location(tmp_path):
    r = make_client(tmp_path).get("/api/panchang")
    assert r.status_code == 200 and r.json()["status"] == "no_location" and "home location" in r.json()["message"]


def test_api_failure_does_not_break_the_calendar(tmp_path, monkeypatch):
    save_location(tmp_path)
    c = make_client(tmp_path)
    import app.panchang as P
    monkeypatch.setattr(P, "panchang", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert c.get("/api/panchang").status_code == 503
    assert c.get("/api/calendar").status_code == 200
