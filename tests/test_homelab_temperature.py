# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""The host's CPU temperature in /homelab, and its optional alert."""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from candygoogy import config, handlers, homelab, settings
from candygoogy.settings import load_settings
from tests.helpers import FakeHomelab, call, fake_app, make_update, replies, sent_texts

VALID = {
    "BOT_TOKEN": "123456:TEST-TOKEN-not-a-real-token-0000",
    "AUTHORIZED_USER_IDS": "1111",
    "WORKSTATION_MAC": "00:00:5e:00:53:01",
    "WORKSTATION_IP": "192.0.2.10",
    "SSH_USER": "tester",
}


def homelab_card() -> str:
    update = make_update()
    call(handlers.homelab, update)
    [text] = replies(update)
    return text


def run_check(app: MagicMock) -> None:
    asyncio.run(homelab.check_and_alert(app))


def open_alerts() -> dict[str, str]:
    return homelab._open_alerts  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def alert_at_85(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(homelab, "TEMP_ALERT_C", 85)


# --- Reading -----------------------------------------------------------------------------


def test_the_hottest_thermal_zone_wins_with_its_type(lab: FakeHomelab) -> None:
    _ = lab.zone("acpitz", "41000")
    _ = lab.zone("x86_pkg_temp", "52500")
    _ = lab.zone("iwlwifi_1", "47000")
    assert homelab.read_temperature() == ("x86_pkg_temp", 52.5)


@pytest.mark.parametrize("bad", [None, "-273200", "abc", "200000", "0", "", "4.2e4"])
def test_implausible_or_unreadable_zones_are_skipped(lab: FakeHomelab, bad: str | None) -> None:
    _ = lab.zone("broken", bad)
    _ = lab.zone("cpu-thermal", "48312")
    assert homelab.read_temperature() == ("cpu-thermal", 48.312)


def test_no_thermal_zones_means_no_temperature(lab: FakeHomelab) -> None:
    assert homelab.read_temperature() is None
    _ = lab.zone("broken", "-273200")  # only invalid zones
    assert homelab.read_temperature() is None


def test_no_sysfs_at_all_means_no_temperature(
    lab: FakeHomelab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(homelab, "SYS", lab.sys / "missing")
    assert homelab.read_temperature() is None


def test_hwmon_cpu_drivers_are_the_fallback(lab: FakeHomelab) -> None:
    _ = lab.hwmon("nvme", "61850")  # hotter, but not the CPU
    _ = lab.hwmon("iwlwifi_1", "70000")
    _ = lab.hwmon("k10temp", "44125", "55000")
    assert homelab.read_temperature() == ("k10temp", 55.0)


def test_hwmon_is_ignored_while_a_zone_has_a_reading(lab: FakeHomelab) -> None:
    _ = lab.zone("x86_pkg_temp", "40000")
    _ = lab.hwmon("coretemp", "90000")
    assert homelab.read_temperature() == ("x86_pkg_temp", 40.0)


def test_hwmon_is_used_when_every_zone_is_invalid(lab: FakeHomelab) -> None:
    _ = lab.zone("acpitz", None)
    _ = lab.hwmon("coretemp", "abc", "63000")
    assert homelab.read_temperature() == ("coretemp", 63.0)


@pytest.mark.parametrize("name", ["nvme", "iwlwifi_1", "amdgpu", "acpitz"])
def test_other_hwmon_drivers_are_never_read(lab: FakeHomelab, name: str) -> None:
    _ = lab.hwmon(name, "50000")
    assert homelab.read_temperature() is None


def test_long_labels_are_cut(lab: FakeHomelab) -> None:
    _ = lab.zone("x" * 100, "50000")
    reading = homelab.read_temperature()
    assert reading is not None and reading[0] == "x" * homelab.MAX_SENSOR_LABEL_CHARS


# --- The card ---------------------------------------------------------------------------


def test_card_shows_the_temperature_after_the_load(lab: FakeHomelab) -> None:
    _ = lab.zone("x86_pkg_temp", "52400")
    text = homelab_card()
    assert "Up 6d 4h · load 0.19 0.76 1.39 (" in text
    assert " cores) · 52 °C\n" in text
    assert "x86_pkg_temp" not in text  # the number only
    assert "⚠️" not in text


@pytest.mark.usefixtures("lab")
def test_card_without_a_sensor_says_nothing_about_it() -> None:
    text = homelab_card()
    assert "°C" not in text and "emperature" not in text
    assert " cores)\n" in text


@pytest.mark.usefixtures("alert_at_85")
def test_card_moves_an_alerting_temperature_to_the_warnings(lab: FakeHomelab) -> None:
    _ = lab.zone("x86_pkg_temp", "87000")
    text = homelab_card()
    assert "⚠️ Running hot. x86_pkg_temp at 87 °C (alert at 85 °C)." in text
    assert text.count("°C") == 2  # in the warning only, not on the load line
    assert " cores)\n" in text


@pytest.mark.usefixtures("alert_at_85")
def test_the_sensor_label_is_escaped(lab: FakeHomelab) -> None:
    _ = lab.zone("<b>zone</b>", "90000")
    text = homelab_card()
    assert "&lt;b&gt;zone&lt;/b&gt; at 90 °C" in text and "<b>zone</b>" not in text


# --- The setting -------------------------------------------------------------------------


def test_the_alert_is_off_unless_set() -> None:
    assert load_settings(VALID).homelab_temp_alert_c is None
    assert load_settings({**VALID, "HOMELAB_TEMP_ALERT_C": " "}).homelab_temp_alert_c is None
    assert config.HOMELAB_TEMP_ALERT_C is None  # tests/__init__.py removes it
    assert "HOMELAB_TEMP_ALERT_C" in settings.KNOWN_VARIABLES


@pytest.mark.parametrize(("text", "value"), [("40", 40), ("85", 85), (" 110 ", 110)])
def test_valid_alert_temperatures(text: str, value: int) -> None:
    assert load_settings({**VALID, "HOMELAB_TEMP_ALERT_C": text}).homelab_temp_alert_c == value


@pytest.mark.parametrize("text", ["39", "111", "0", "85.5", "-90", "eighty", "SECRET9q", "0085"])
def test_bad_alert_temperatures_are_named_not_echoed(text: str) -> None:
    with pytest.raises(settings.ConfigError) as error:
        _ = load_settings({**VALID, "HOMELAB_TEMP_ALERT_C": text})
    # The same fixed sentence for every bad value, so none is ever echoed.
    assert error.value.problems == [
        "HOMELAB_TEMP_ALERT_C must be a whole number of °C from 40 to 110, e.g. 85."
    ]
    assert "SECRET" not in str(error.value)


# --- The alert ---------------------------------------------------------------------------


def set_zone(lab: FakeHomelab, millidegrees: str) -> None:
    """The one CPU zone now reads `millidegrees`."""
    zone = lab.sys / "class" / "thermal" / "thermal_zone0"
    if not zone.exists():
        _ = lab.zone("x86_pkg_temp", millidegrees)
    else:
        _ = (zone / "temp").write_text(f"{millidegrees}\n")


@pytest.mark.usefixtures("alert_at_85")
def test_alert_opens_at_the_threshold_and_holds_in_the_band(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    set_zone(lab, "84900")
    run_check(app)
    assert sent_texts(app) == []  # just below: nothing yet
    set_zone(lab, "85000")
    run_check(app)
    [alert] = sent_texts(app)
    assert alert.startswith(
        "⚠️ <b>Homelab: Running hot</b>\nx86_pkg_temp at 85 °C (alert at 85 °C)."
    )
    for millidegrees in ("84000", "80000", "86000", "80500"):  # hovering: no repeat, no clear
        set_zone(lab, millidegrees)
        run_check(app)
    assert len(sent_texts(app)) == 1
    assert open_alerts() == {"temp": "Running hot"}
    set_zone(lab, "79900")  # below 85 - 5
    run_check(app)
    assert sent_texts(app)[1:] == ["✅ <b>Homelab: back to normal</b>\nWas: Running hot."]
    set_zone(lab, "82000")  # back in the band: doesn't reopen
    run_check(app)
    assert len(sent_texts(app)) == 2


@pytest.mark.usefixtures("alert_at_85")
def test_a_lost_sensor_leaves_an_open_alert_alone(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    set_zone(lab, "90000")
    run_check(app)
    _ = (lab.sys / "class" / "thermal" / "thermal_zone0" / "temp").write_text("-273200\n")
    run_check(app)
    run_check(app)
    assert len(sent_texts(app)) == 1  # neither repeated nor "back to normal"
    assert open_alerts() == {"temp": "Running hot"}


def test_no_alert_without_the_setting(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    set_zone(lab, "105000")
    run_check(app)
    assert sent_texts(app) == []
    assert " cores) · 105 °C\n" in homelab_card()  # still shown


@pytest.mark.usefixtures("lab")
def test_startup_drops_a_temperature_alert_once_the_setting_is_gone() -> None:
    open_alerts()["temp"] = "Running hot"
    homelab.drop_switched_off_alerts(time.time())
    assert open_alerts() == {}


@pytest.mark.usefixtures("lab", "alert_at_85")
def test_startup_keeps_a_temperature_alert_while_the_setting_is_on() -> None:
    open_alerts()["temp"] = "Running hot"
    homelab.drop_switched_off_alerts(time.time())
    assert open_alerts() == {"temp": "Running hot"}


@pytest.mark.usefixtures("alert_at_85")
def test_the_daily_summary_counts_it_as_a_problem(lab: FakeHomelab) -> None:
    set_zone(lab, "88000")
    health = asyncio.run(homelab.read_health())
    lines = homelab.summary_lines(health, time.time())
    assert lines[:2] == [
        "🖥 Homelab: 1 problem.",
        "⚠️ Running hot. x86_pkg_temp at 88 °C (alert at 85 °C).",
    ]
