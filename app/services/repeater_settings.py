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

# Firmware answers an unrecognised ``get`` key with ``"??: <key>"`` and an
# unrecognised ``set`` key with ``"unknown config: <key>"``. A key the build
# knows but the board cannot honour (FEM gain on a board without a front-end
# module, power management outside nRF52) answers ``"Error: unsupported"`` or
# ``"ERROR: ... not supported"``; those lock the field the same way, since no
# value the operator types will ever take.
_UNSUPPORTED_PREFIXES = ("??", "unknown config")
_UNSUPPORTED_WORDS = ("unsupported", "not supported")

# The firmware's own name validation: ``isValidName`` in CommonCLI.cpp refuses
# these because they would break the advert/contact text formats.
_NAME_FORBIDDEN_CHARS = "[]\\:,?*"

# Bounds mirrored from the firmware's ``set radio`` check (CommonCLI.cpp). The
# frequency range covers every band MeshCore ships on, including 2.4 GHz LR2021
# boards, and the firmware refuses anything outside it anyway.
_SF_RANGE = (5, 12)
_CR_RANGE = (5, 8)
_FREQ_RANGE_MHZ = (150.0, 2500.0)
_BW_RANGE_KHZ = (7.0, 500.0)


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
    """One of ``string``, ``int``, ``float``, ``bool``, ``enum``, ``radio``, ``int_list``.

    ``int_list`` is a comma-separated list of whole numbers, each bounded by
    ``minimum``/``maximum`` and at most ``max_items`` long (``extra.sf``).
    """

    help: str = ""
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()
    max_length: int | None = None
    max_items: int | None = None
    readable: bool = True
    writable: bool = True
    sensitive: bool = False
    """Password-like: the UI masks it and never puts it in the console log."""

    note: str | None = None
    """Extra warning shown next to the field (reboots, reachability, ...)."""

    set_verb: str | None = "set"
    """The word in front of the key when writing. ``None`` means the firmware
    takes the key as a top-level command (``password <value>`` has no ``set``)."""

    bool_style: str = "word"
    """How a bool is written: ``word`` sends ``on``/``off``; ``digit`` sends
    ``1``/``0`` for keys the firmware parses with ``atoi`` (``multi.acks``),
    where the word ``on`` would silently read as zero."""

    reply_aliases: tuple[tuple[str, str], ...] = ()
    """``(reply_text_lowercased, value)`` pairs: how a ``get`` reply that does
    not spell the value the way ``set`` wants it is normalised for the form."""

    forbidden_chars: str = ""
    """Characters the firmware rejects in this string beyond control characters."""

    @property
    def get_command(self) -> str:
        return f"get {self.cli_key}"

    def set_command(self, formatted_value: str) -> str:
        if self.set_verb is None:
            return f"{self.cli_key} {formatted_value}"
        return f"{self.set_verb} {self.cli_key} {formatted_value}"

    @property
    def command_hint(self) -> str:
        """The command the UI shows beside the field, without its value."""
        if not self.writable:
            return self.get_command
        if self.set_verb is None:
            return self.cli_key
        return f"{self.set_verb} {self.cli_key}"


SETTING_GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup("identity", "Identity & Location", "How the repeater names and places itself"),
    SettingGroup("radio", "Radio", "LoRa parameters, receiver gain and repeating behaviour"),
    SettingGroup("advert", "Advertising", "How often the repeater announces itself"),
    SettingGroup("access", "Access", "Passwords and what a non-admin client may do"),
    SettingGroup("telemetry", "Telemetry", "Who may read each class of telemetry"),
    SettingGroup(
        "bridge",
        "Bridge",
        "RS232 / ESP-NOW packet bridge; only on firmware built with a bridge",
    ),
    SettingGroup(
        "advanced", "Advanced", "Timing, airtime and routing tuning; leave alone unless needed"
    ),
    SettingGroup("info", "Device Info", "Read-only facts the firmware reports about itself"),
)

REPEATER_SETTINGS: tuple[RepeaterSetting, ...] = (
    # --- Identity -----------------------------------------------------------
    RepeaterSetting(
        key="name",
        label="Name",
        group="identity",
        cli_key="name",
        value_type="string",
        max_length=31,
        forbidden_chars=_NAME_FORBIDDEN_CHARS,
        help="Advertised node name. The firmware refuses [ ] \\ : , ? and *.",
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
        max_length=119,
        help="Free-text owner/contact note other clients can read. A | becomes a line break.",
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
            "Takes effect after a reboot. The repeater answers on these parameters only: "
            "getting them wrong takes it off the air until someone reaches it physically."
        ),
    ),
    RepeaterSetting(
        key="tx_power",
        label="TX Power",
        group="radio",
        cli_key="tx",
        value_type="int",
        unit="dBm",
        minimum=-9,
        maximum=30,
        help="Transmit power. The hardware clamps values it cannot reach.",
    ),
    RepeaterSetting(
        key="rx_boosted_gain",
        label="RX Boosted Gain",
        group="radio",
        cli_key="radio.rxgain",
        value_type="bool",
        help=(
            "Runs the LoRa receiver in its high-sensitivity mode for a little more current. "
            "Radios without that mode answer unsupported."
        ),
    ),
    RepeaterSetting(
        key="fem_rx_gain",
        label="Front-End RX Gain (LNA)",
        group="radio",
        cli_key="radio.fem.rxgain",
        value_type="bool",
        help="Enables the external low-noise amplifier on boards fitted with a front-end module.",
    ),
    RepeaterSetting(
        key="fem_tx_gain",
        label="Front-End TX Gain (PA)",
        group="radio",
        cli_key="radio.fem.txgain",
        value_type="bool",
        help="Enables the external power amplifier on boards fitted with a front-end module.",
    ),
    RepeaterSetting(
        key="cad",
        label="Channel Activity Detection",
        group="radio",
        cli_key="cad",
        value_type="bool",
        help="Listen for another transmission before sending, to avoid talking over it.",
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
        minimum=1,
        maximum=100,
        step=0.1,
        help=(
            "Share of airtime the repeater may use (firmware 1.15 and newer). "
            "The same budget as Airtime Factor, expressed the other way round."
        ),
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
    RepeaterSetting(
        key="flood_max_advert",
        label="Max Flood Hops (adverts)",
        group="radio",
        cli_key="flood.max.advert",
        value_type="int",
        unit="hops",
        minimum=0,
        maximum=64,
        help="Hop limit applied to flooded adverts specifically.",
    ),
    RepeaterSetting(
        key="flood_max_unscoped",
        label="Max Flood Hops (unscoped)",
        group="radio",
        cli_key="flood.max.unscoped",
        value_type="int",
        unit="hops",
        minimum=0,
        maximum=64,
        help="Hop limit applied to flood packets that carry no region scope.",
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
        step=2,
        help=(
            "Zero-hop advert period in minutes; 0 disables it. Stored in two-minute steps, "
            "and the firmware refuses anything under its minimum (an hour on current builds)."
        ),
    ),
    RepeaterSetting(
        key="flood_advert_interval",
        label="Flood Advert Interval",
        group="advert",
        cli_key="flood.advert.interval",
        value_type="int",
        unit="hours",
        minimum=0,
        maximum=168,
        help=(
            "Flood advert period in hours: 0 disables it, otherwise 3 to 168. "
            "Floods cost the whole mesh airtime."
        ),
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
        # The firmware takes this as a top-level `password <new>` command; there
        # is no `set password`, which answers "unknown config".
        set_verb=None,
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
    # --- Bridge -------------------------------------------------------------
    # Only firmware built with WITH_BRIDGE (and the RS232 or ESP-NOW flavour)
    # knows these; everything else answers "??" and the fields lock themselves.
    RepeaterSetting(
        key="bridge_type",
        label="Bridge Type",
        group="bridge",
        cli_key="bridge.type",
        value_type="string",
        writable=False,
        help="Which bridge this firmware was built with: rs232, espnow or none.",
    ),
    RepeaterSetting(
        key="bridge_enabled",
        label="Bridge Enabled",
        group="bridge",
        cli_key="bridge.enabled",
        value_type="bool",
        help="Whether packets are passed to and from the bridge link.",
    ),
    RepeaterSetting(
        key="bridge_source",
        label="Bridge Source",
        group="bridge",
        cli_key="bridge.source",
        value_type="enum",
        options=("rx", "tx"),
        # The firmware reads back "logRx"/"logTx" but wants "rx"/"tx" written.
        reply_aliases=(("logrx", "rx"), ("logtx", "tx")),
        help="Which packets cross the bridge: those received over the air (rx) or those this node transmits (tx).",
    ),
    RepeaterSetting(
        key="bridge_delay",
        label="Bridge Delay",
        group="bridge",
        cli_key="bridge.delay",
        value_type="int",
        unit="ms",
        minimum=0,
        maximum=10000,
        help="Delay before a bridged packet is re-sent over the air.",
    ),
    RepeaterSetting(
        key="bridge_baud",
        label="Bridge Baud Rate (RS232)",
        group="bridge",
        cli_key="bridge.baud",
        value_type="int",
        unit="baud",
        minimum=9600,
        maximum=115200,
        help="Serial speed of the RS232 bridge link. Changing it restarts the bridge.",
    ),
    RepeaterSetting(
        key="bridge_channel",
        label="Bridge Wi-Fi Channel (ESP-NOW)",
        group="bridge",
        cli_key="bridge.channel",
        value_type="int",
        minimum=1,
        maximum=14,
        help="Wi-Fi channel the ESP-NOW bridge uses; both ends must match.",
    ),
    RepeaterSetting(
        key="bridge_secret",
        label="Bridge Secret (ESP-NOW)",
        group="bridge",
        cli_key="bridge.secret",
        value_type="string",
        max_length=15,
        sensitive=True,
        help="Shared key that scrambles ESP-NOW bridge packets; both ends must match.",
    ),
    # --- Advanced -----------------------------------------------------------
    RepeaterSetting(
        key="rx_delay",
        label="RX Delay Base",
        group="advanced",
        cli_key="rxdelay",
        value_type="float",
        minimum=0,
        maximum=20,
        step=0.1,
        help=(
            "Base of the SNR-weighted delay before a heard packet is repeated, so the "
            "repeater that heard it best goes first; 0 disables it. The firmware accepts 0 to 20."
        ),
    ),
    RepeaterSetting(
        key="tx_delay",
        label="TX Delay Factor",
        group="advanced",
        cli_key="txdelay",
        value_type="float",
        minimum=0,
        maximum=2,
        step=0.1,
        help=(
            "Spread of the random pre-transmit delay that keeps repeaters from colliding (0 to 2)."
        ),
    ),
    RepeaterSetting(
        key="direct_tx_delay",
        label="Direct TX Delay Factor",
        group="advanced",
        cli_key="direct.txdelay",
        value_type="float",
        minimum=0,
        maximum=2,
        step=0.1,
        help="Same, for directly-routed packets (0 to 2).",
    ),
    RepeaterSetting(
        key="interference_threshold",
        label="Interference Threshold",
        group="advanced",
        cli_key="int.thresh",
        value_type="int",
        minimum=0,
        maximum=255,
        help=(
            "Noise-floor margin the radio must see clear before it transmits; 0 leaves the "
            "check off."
        ),
    ),
    RepeaterSetting(
        key="agc_reset_interval",
        label="AGC Reset Interval",
        group="advanced",
        cli_key="agc.reset.interval",
        value_type="int",
        unit="seconds",
        minimum=0,
        maximum=1020,
        step=4,
        help=(
            "How often the radio's automatic gain control is reset; 0 disables it. "
            "Rounded down to a multiple of four."
        ),
    ),
    RepeaterSetting(
        key="multi_acks",
        label="Multi ACKs",
        group="advanced",
        cli_key="multi.acks",
        value_type="bool",
        # The firmware parses this one with atoi(), so it must be written 1/0:
        # the word "on" would read as zero and quietly turn the feature off.
        bool_style="digit",
        help="Whether the repeater sends multiple acknowledgements for a delivery.",
    ),
    RepeaterSetting(
        key="path_hash_mode",
        label="Path Hash Mode",
        group="advanced",
        cli_key="path.hash.mode",
        value_type="enum",
        options=("0", "1", "2"),
        help=(
            "How many bytes of each hop's identity a path records: 0 is the default one-byte "
            "hash; 1 and 2 record more to tell similar repeaters apart."
        ),
    ),
    RepeaterSetting(
        key="loop_detect",
        label="Loop Detection",
        group="advanced",
        cli_key="loop.detect",
        value_type="enum",
        options=("off", "minimal", "moderate", "strict"),
        help="How aggressively a packet that has already passed through this node is dropped.",
    ),
    RepeaterSetting(
        key="adc_multiplier",
        label="Battery ADC Multiplier",
        group="advanced",
        cli_key="adc.multiplier",
        value_type="float",
        minimum=0,
        maximum=10,
        step=0.001,
        help=(
            "Correction applied to the battery voltage reading; 0 restores the board's "
            "default. Boards without a battery divider answer unsupported."
        ),
    ),
    RepeaterSetting(
        key="extra_sf",
        label="Extra Spreading Factors",
        group="advanced",
        cli_key="extra.sf",
        value_type="int_list",
        minimum=_SF_RANGE[0],
        maximum=_SF_RANGE[1],
        max_items=3,
        reply_aliases=(("no extra sf configured", ""),),
        help=(
            "Up to three additional spreading factors the radio also listens on "
            "(LR2021 radios only), comma-separated."
        ),
    ),
    # --- Device info (read-only) ---------------------------------------------
    RepeaterSetting(
        key="role",
        label="Role",
        group="info",
        cli_key="role",
        value_type="string",
        writable=False,
        help="What this firmware is: repeater, room server, and so on.",
    ),
    RepeaterSetting(
        key="public_key",
        label="Public Key",
        group="info",
        cli_key="public.key",
        value_type="string",
        writable=False,
        help="The node's identity, as it appears in adverts.",
    ),
    RepeaterSetting(
        key="bootloader_version",
        label="Bootloader Version",
        group="info",
        cli_key="bootloader.ver",
        value_type="string",
        writable=False,
        help="Bootloader the board runs (nRF52 boards only).",
    ),
    RepeaterSetting(
        key="power_source",
        label="Power Source",
        group="info",
        cli_key="pwrmgt.source",
        value_type="string",
        writable=False,
        help="Whether the board is running from external power or its battery (nRF52 power management only).",
    ),
    RepeaterSetting(
        key="boot_reason",
        label="Last Boot Reason",
        group="info",
        cli_key="pwrmgt.bootreason",
        value_type="string",
        writable=False,
        help="Why the board last reset and how it last shut down.",
    ),
    RepeaterSetting(
        key="boot_voltage",
        label="Voltage At Boot",
        group="info",
        cli_key="pwrmgt.bootmv",
        value_type="string",
        writable=False,
        help="Battery voltage measured when the board last started (nRF52 power management only).",
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
        on, off = ("1", "0") if setting.bool_style == "digit" else ("on", "off")
        if isinstance(value, bool):
            return on if value else off
        text = str(value).strip().lower()
        if text in _TRUE_WORDS:
            return on
        if text in _FALSE_WORDS:
            return off
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

    if setting.value_type == "int_list":
        return _format_int_list(value, setting)

    # string
    text = str(value)
    _reject_unprintable(text, setting.label)
    text = text.strip()
    if text == "":
        raise SettingValueError(f"{setting.label} cannot be empty")
    if setting.max_length is not None and len(text) > setting.max_length:
        raise SettingValueError(f"{setting.label} must be {setting.max_length} characters or fewer")
    bad = sorted({char for char in text if char in setting.forbidden_chars})
    if bad:
        raise SettingValueError(f"{setting.label} cannot contain {' '.join(bad)}")
    return text


def _format_int_list(value: object, setting: RepeaterSetting) -> str:
    """Validate a comma-separated list of whole numbers (``extra.sf``)."""
    if isinstance(value, list | tuple):
        parts = [str(part).strip() for part in value]
    else:
        text = str(value)
        _reject_unprintable(text, setting.label)
        parts = [part.strip() for part in text.split(",")]
    parts = [part for part in parts if part != ""]
    if not parts:
        raise SettingValueError(f"{setting.label} cannot be empty")
    if setting.max_items is not None and len(parts) > setting.max_items:
        raise SettingValueError(f"{setting.label} takes at most {setting.max_items} values")
    numbers: list[int] = []
    for part in parts:
        number = _as_number(part, setting.label)
        if number != int(number):
            raise SettingValueError(f"{setting.label} must be whole numbers")
        _check_range(number, setting)
        numbers.append(int(number))
    return ",".join(str(number) for number in numbers)


def is_unsupported_reply(reply: str | None) -> bool:
    """True when the firmware answered "I don't know that key" or "this board can't".

    ``get`` answers an unknown key ``"??: <key>"`` and ``set`` answers
    ``"unknown config: <key>"``. A key the build knows but the hardware cannot
    honour answers ``"Error: unsupported"`` (or ``"... not supported"``); no
    value will ever take on that board, so it locks the field the same way.
    """
    if reply is None:
        return False
    text = reply.strip().lower()
    if text.startswith(_UNSUPPORTED_PREFIXES):
        return True
    return any(word in text for word in _UNSUPPORTED_WORDS)


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

    # Some keys read back in a different spelling from the one `set` wants
    # (`bridge.source` answers "logRx" for "rx"), or answer a sentence where an
    # empty value is meant ("No extra SF configured").
    for reply_text, value in setting.reply_aliases:
        if text.lower() == reply_text:
            return value, "ok"

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
