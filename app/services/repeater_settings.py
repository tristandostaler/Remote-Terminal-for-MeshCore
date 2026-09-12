"""Catalog of repeater firmware settings the dashboard can read and write.

A repeater is configured over its text CLI: ``get <key>`` reads one value,
``set <key> <value>`` writes it, and both are admin-only (the firmware refuses
to route CLI text for a guest or read-only client). Everything here exists so an
operator never has to remember those keys — the dashboard renders this catalog
as a form, reads the current values, and writes back only the fields that
changed.

The catalog is deliberately a *superset* of what any one firmware build
supports. An unknown key is answered by the generic config handler with
``"??: <key>"`` (or an ``ERR...`` string), never with silence, so the fetch path
marks that setting unsupported and the UI disables its field instead of offering
an edit that cannot work. That keeps one catalog usable across firmware versions
without version-sniffing every key.

Nothing here talks to the radio: this module is pure validation, formatting and
reply parsing, so it is cheap to test. ``app/routers/repeaters.py`` owns the
batched CLI round trips.
"""

from dataclasses import dataclass

# Values are sent verbatim as the tail of a one-line CLI command, so anything
# that is not printable text would either be dropped in transit or split the
# command. Reject those characters rather than silently mangling them.
_FORBIDDEN_VALUE_CHARS = set(range(0x00, 0x20)) | {0x7F}

# Firmware answers an unrecognised config key with this prefix.
_UNSUPPORTED_PREFIX = "??"

# Spreading factor / coding rate bounds accepted by the LoRa radio drivers.
_SF_RANGE = (5, 12)
_CR_RANGE = (5, 8)
# Deliberately wide: the same firmware ships on 433/868/915 MHz hardware, and a
# tighter guess here would block a legitimate regional frequency.
_FREQ_RANGE_MHZ = (100.0, 1000.0)
_BW_RANGE_KHZ = (7.0, 1000.0)


class SettingValueError(ValueError):
    """A value the operator supplied cannot be written to the repeater."""


@dataclass(frozen=True)
class SettingGroup:
    """One titled section of the settings form."""

    key: str
    label: str
    description: str


@dataclass(frozen=True)
class RepeaterSetting:
    """One firmware setting: how to read it, how to write it, what is valid."""

    key: str
    """Stable identifier used by the API and the UI (not the firmware key)."""

    label: str
    group: str
    cli_key: str
    """The firmware's own key, as used in ``get``/``set``."""

    value_type: str
    """One of ``string``, ``int``, ``float``, ``bool``, ``enum``, ``radio``."""

    help: str = ""
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()
    max_length: int | None = None
    readable: bool = True
    writable: bool = True
    sensitive: bool = False
    """Password-like: the UI masks it and never puts it in the console log."""

    note: str | None = None
    """Extra warning shown next to the field (reboots, reachability, ...)."""

    @property
    def get_command(self) -> str:
        return f"get {self.cli_key}"

    def set_command(self, formatted_value: str) -> str:
        return f"set {self.cli_key} {formatted_value}"


SETTING_GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup("identity", "Identity & Location", "How the repeater names and places itself"),
    SettingGroup("radio", "Radio", "LoRa parameters and repeating behaviour"),
    SettingGroup("advert", "Advertising", "How often the repeater announces itself"),
    SettingGroup("access", "Access", "Passwords and what a non-admin client may do"),
    SettingGroup("telemetry", "Telemetry", "Who may read each class of telemetry"),
    SettingGroup("advanced", "Advanced", "Timing and airtime tuning; leave alone unless needed"),
)

REPEATER_SETTINGS: tuple[RepeaterSetting, ...] = (
    # --- Identity -----------------------------------------------------------
    RepeaterSetting(
        key="name",
        label="Name",
        group="identity",
        cli_key="name",
        value_type="string",
        max_length=32,
        help="Advertised node name.",
        note="Changing this re-advertises the repeater under the new name.",
    ),
    RepeaterSetting(
        key="lat",
        label="Latitude",
        group="identity",
        cli_key="lat",
        value_type="float",
        minimum=-90.0,
        maximum=90.0,
        step=0.000001,
        unit="°",
        help="Advertised latitude in decimal degrees.",
    ),
    RepeaterSetting(
        key="lon",
        label="Longitude",
        group="identity",
        cli_key="lon",
        value_type="float",
        minimum=-180.0,
        maximum=180.0,
        step=0.000001,
        unit="°",
        help="Advertised longitude in decimal degrees.",
    ),
    RepeaterSetting(
        key="owner_info",
        label="Owner Info",
        group="identity",
        cli_key="owner.info",
        value_type="string",
        max_length=128,
        help="Free-text owner/contact note other clients can read.",
    ),
    # --- Radio --------------------------------------------------------------
    RepeaterSetting(
        key="radio",
        label="Radio (freq, BW, SF, CR)",
        group="radio",
        cli_key="radio",
        value_type="radio",
        help="Frequency in MHz, bandwidth in kHz, spreading factor, coding rate.",
        note=(
            "The repeater answers on these parameters only. Getting them wrong takes "
            "it off the air until someone reaches it physically."
        ),
    ),
    RepeaterSetting(
        key="tx_power",
        label="TX Power",
        group="radio",
        cli_key="tx",
        value_type="int",
        unit="dBm",
        minimum=0,
        maximum=30,
        help="Transmit power. The hardware clamps values it cannot reach.",
    ),
    RepeaterSetting(
        key="airtime_factor",
        label="Airtime Factor",
        group="radio",
        cli_key="af",
        value_type="float",
        minimum=0,
        maximum=100,
        step=0.1,
        help="Airtime budget divisor; higher means the repeater transmits less.",
    ),
    RepeaterSetting(
        key="duty_cycle",
        label="Duty Cycle Limit",
        group="radio",
        cli_key="dutycycle",
        value_type="float",
        unit="%",
        minimum=0,
        maximum=100,
        step=0.1,
        help="Share of airtime the repeater may use (firmware 1.15 and newer).",
    ),
    RepeaterSetting(
        key="repeat",
        label="Repeat Mode",
        group="radio",
        cli_key="repeat",
        value_type="bool",
        help="Whether the node relays other nodes' packets at all.",
        note="Turning this off leaves the node reachable but stops it repeating.",
    ),
    RepeaterSetting(
        key="flood_max",
        label="Max Flood Hops",
        group="radio",
        cli_key="flood.max",
        value_type="int",
        unit="hops",
        minimum=0,
        maximum=64,
        help="Flood packets with more hops than this are not repeated.",
    ),
    # --- Advertising --------------------------------------------------------
    RepeaterSetting(
        key="advert_interval",
        label="Local Advert Interval",
        group="advert",
        cli_key="advert.interval",
        value_type="int",
        unit="minutes",
        minimum=0,
        maximum=240,
        help="Zero-hop advert period in minutes; 0 disables it.",
    ),
    RepeaterSetting(
        key="flood_advert_interval",
        label="Flood Advert Interval",
        group="advert",
        cli_key="flood.advert.interval",
        value_type="int",
        unit="hours",
        minimum=0,
        maximum=48,
        help="Flood advert period in hours; 0 disables it. Floods cost the whole mesh airtime.",
    ),
    # --- Access -------------------------------------------------------------
    RepeaterSetting(
        key="password",
        label="Admin Password",
        group="access",
        cli_key="password",
        value_type="string",
        max_length=15,
        readable=False,
        sensitive=True,
        help="Password for admin logins. Cannot be read back, only replaced.",
        note="Setting this wrong locks everyone out of admin until a physical reset.",
    ),
    RepeaterSetting(
        key="guest_password",
        label="Guest Password",
        group="access",
        cli_key="guest.password",
        value_type="string",
        max_length=15,
        sensitive=True,
        help="Password for guest logins.",
    ),
    RepeaterSetting(
        key="allow_read_only",
        label="Allow Read-Only Access",
        group="access",
        cli_key="allow.read.only",
        value_type="bool",
        help="Whether clients without admin rights may read status and telemetry.",
    ),
    # --- Telemetry ----------------------------------------------------------
    RepeaterSetting(
        key="telemetry_mode_base",
        label="Base Telemetry",
        group="telemetry",
        cli_key="telemetry.mode.base",
        value_type="enum",
        options=("always", "admin", "never"),
        help="Who may read battery/uptime telemetry.",
    ),
    RepeaterSetting(
        key="telemetry_mode_loc",
        label="Location Telemetry",
        group="telemetry",
        cli_key="telemetry.mode.loc",
        value_type="enum",
        options=("always", "admin", "never"),
        help="Who may read GPS/location telemetry.",
    ),
    RepeaterSetting(
        key="telemetry_mode_env",
        label="Environment Telemetry",
        group="telemetry",
        cli_key="telemetry.mode.env",
        value_type="enum",
        options=("always", "admin", "never"),
        help="Who may read attached environment sensors.",
    ),
    # --- Advanced -----------------------------------------------------------
    RepeaterSetting(
        key="rx_delay",
        label="RX Delay Base",
        group="advanced",
        cli_key="rxdelay",
        value_type="float",
        unit="ms",
        minimum=0,
        maximum=10000,
        step=1,
        help="Receive settling delay used before a repeat is scheduled.",
    ),
    RepeaterSetting(
        key="tx_delay",
        label="TX Delay Factor",
        group="advanced",
        cli_key="txdelay",
        value_type="float",
        minimum=0,
        maximum=10000,
        step=0.1,
        help="Spread of the random pre-transmit delay that keeps repeaters from colliding.",
    ),
    RepeaterSetting(
        key="direct_tx_delay",
        label="Direct TX Delay Factor",
        group="advanced",
        cli_key="direct.tx.delay",
        value_type="float",
        minimum=0,
        maximum=10000,
        step=0.1,
        help="Same, for directly-routed packets.",
    ),
    RepeaterSetting(
        key="agc_reset_interval",
        label="AGC Reset Interval",
        group="advanced",
        cli_key="agc.reset.interval",
        value_type="int",
        minimum=0,
        maximum=100000,
        help="How often the radio's automatic gain control is reset; 0 disables it.",
    ),
    RepeaterSetting(
        key="multi_acks",
        label="Multi ACKs",
        group="advanced",
        cli_key="multi.acks",
        value_type="bool",
        help="Whether the repeater sends multiple acknowledgements for a delivery.",
    ),
)

SETTINGS_BY_KEY: dict[str, RepeaterSetting] = {s.key: s for s in REPEATER_SETTINGS}

READABLE_SETTINGS: tuple[RepeaterSetting, ...] = tuple(s for s in REPEATER_SETTINGS if s.readable)

_TRUE_WORDS = {"on", "true", "yes", "1", "enabled"}
_FALSE_WORDS = {"off", "false", "no", "0", "disabled"}


def get_setting(key: str) -> RepeaterSetting:
    """Look up a setting by its catalog key, or raise ``SettingValueError``."""
    setting = SETTINGS_BY_KEY.get(key)
    if setting is None:
        raise SettingValueError(f"Unknown repeater setting '{key}'")
    return setting


def _reject_unprintable(text: str, label: str) -> None:
    if any(ord(char) in _FORBIDDEN_VALUE_CHARS for char in text):
        raise SettingValueError(f"{label} cannot contain control characters or line breaks")


def _as_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise SettingValueError(f"{label} must be a number")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise SettingValueError(f"{label} must be a number") from exc
    raise SettingValueError(f"{label} must be a number")


def _check_range(number: float, setting: RepeaterSetting) -> None:
    if setting.minimum is not None and number < setting.minimum:
        raise SettingValueError(f"{setting.label} must be at least {_trim_number(setting.minimum)}")
    if setting.maximum is not None and number > setting.maximum:
        raise SettingValueError(f"{setting.label} must be at most {_trim_number(setting.maximum)}")


def _trim_number(number: float) -> str:
    """Format a float without a trailing ``.0`` so commands stay firmware-plain."""
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _format_radio(value: object, setting: RepeaterSetting) -> str:
    """Validate and normalise a ``freq,bw,sf,cr`` tuple."""
    if not isinstance(value, str):
        raise SettingValueError(f"{setting.label} must be 'freq,bandwidth,sf,cr'")
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4 or any(part == "" for part in parts):
        raise SettingValueError(
            f"{setting.label} must have four comma-separated parts: freq,bandwidth,sf,cr"
        )
    try:
        freq = float(parts[0])
        bandwidth = float(parts[1])
        spreading = int(parts[2])
        coding = int(parts[3])
    except ValueError as exc:
        raise SettingValueError(f"{setting.label} parts must be numbers") from exc

    if not _FREQ_RANGE_MHZ[0] <= freq <= _FREQ_RANGE_MHZ[1]:
        raise SettingValueError(
            f"Frequency must be between {_trim_number(_FREQ_RANGE_MHZ[0])} and "
            f"{_trim_number(_FREQ_RANGE_MHZ[1])} MHz"
        )
    if not _BW_RANGE_KHZ[0] <= bandwidth <= _BW_RANGE_KHZ[1]:
        raise SettingValueError(
            f"Bandwidth must be between {_trim_number(_BW_RANGE_KHZ[0])} and "
            f"{_trim_number(_BW_RANGE_KHZ[1])} kHz"
        )
    if not _SF_RANGE[0] <= spreading <= _SF_RANGE[1]:
        raise SettingValueError(
            f"Spreading factor must be between {_SF_RANGE[0]} and {_SF_RANGE[1]}"
        )
    if not _CR_RANGE[0] <= coding <= _CR_RANGE[1]:
        raise SettingValueError(f"Coding rate must be between {_CR_RANGE[0]} and {_CR_RANGE[1]}")

    return f"{_trim_number(freq)},{_trim_number(bandwidth)},{spreading},{coding}"


def format_value(setting: RepeaterSetting, value: object) -> str:
    """Turn an API value into the exact text the firmware's ``set`` expects.

    Raises ``SettingValueError`` for anything out of range, of the wrong shape,
    or carrying characters that would break the one-line CLI command.
    """
    if not setting.writable:
        raise SettingValueError(f"{setting.label} cannot be changed")

    if setting.value_type == "radio":
        return _format_radio(value, setting)

    if setting.value_type == "bool":
        if isinstance(value, bool):
            return "on" if value else "off"
        text = str(value).strip().lower()
        if text in _TRUE_WORDS:
            return "on"
        if text in _FALSE_WORDS:
            return "off"
        raise SettingValueError(f"{setting.label} must be on or off")

    if setting.value_type == "enum":
        text = str(value).strip().lower()
        if text not in setting.options:
            allowed = ", ".join(setting.options)
            raise SettingValueError(f"{setting.label} must be one of: {allowed}")
        return text

    if setting.value_type == "int":
        number = _as_number(value, setting.label)
        if number != int(number):
            raise SettingValueError(f"{setting.label} must be a whole number")
        _check_range(number, setting)
        return str(int(number))

    if setting.value_type == "float":
        number = _as_number(value, setting.label)
        _check_range(number, setting)
        return _trim_number(number)

    # string
    text = str(value)
    _reject_unprintable(text, setting.label)
    text = text.strip()
    if text == "":
        raise SettingValueError(f"{setting.label} cannot be empty")
    if setting.max_length is not None and len(text) > setting.max_length:
        raise SettingValueError(f"{setting.label} must be {setting.max_length} characters or fewer")
    return text


def is_unsupported_reply(reply: str | None) -> bool:
    """True when the firmware answered "I don't know that key"."""
    if reply is None:
        return False
    text = reply.strip()
    return text.startswith(_UNSUPPORTED_PREFIX)


def is_error_reply(reply: str | None) -> bool:
    """True when the firmware answered with an explicit error."""
    if reply is None:
        return False
    text = reply.strip().lower()
    return text.startswith("err") or text.startswith("(err") or text.startswith("error")


def parse_get_reply(setting: RepeaterSetting, reply: str | None) -> tuple[str | None, str]:
    """Turn one ``get`` reply into ``(value, status)``.

    Status is ``ok``, ``unsupported`` (firmware does not know the key),
    ``error`` (firmware refused) or ``no_reply`` (nothing came back — out of
    range, or not logged in as admin, since the firmware routes no CLI text at
    all for a non-admin client).

    Some builds echo the key back (``"flood.max: 3"``); that prefix is stripped
    so the UI always sees a bare value.
    """
    if reply is None:
        return None, "no_reply"
    if is_unsupported_reply(reply):
        return None, "unsupported"
    if is_error_reply(reply):
        return None, "error"

    text = reply.strip()
    prefix = f"{setting.cli_key}:"
    if text.lower().startswith(prefix.lower()):
        text = text[len(prefix) :].strip()

    if text == "":
        # The firmware answered with an empty value: the setting exists and is
        # unset (an empty guest password, say), which is not the same as silence.
        return "", "ok"

    if setting.value_type == "bool":
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return "on", "ok"
        if lowered in _FALSE_WORDS:
            return "off", "ok"
    if setting.value_type == "enum":
        lowered = text.lower()
        if lowered in setting.options:
            return lowered, "ok"

    return text, "ok"


def classify_set_reply(reply: str | None) -> str:
    """Turn one ``set`` reply into ``ok`` / ``unsupported`` / ``error`` / ``no_reply``."""
    if reply is None:
        return "no_reply"
    if is_unsupported_reply(reply):
        return "unsupported"
    if is_error_reply(reply):
        return "error"
    return "ok"
