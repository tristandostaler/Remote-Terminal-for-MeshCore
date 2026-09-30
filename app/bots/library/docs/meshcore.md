<!--
Starter reference notes for the tinyllm bot. Copied once into the bot's docs
folder (data/tinyllm-docs by default) and never overwritten there, so edit that
copy freely, add your own .md files next to it, or delete what you don't want.
Every heading starts a section; the bot searches all sections and hands the
best matches to the model.
The repeater settings part is generated from app/services/repeater_settings.py.
-->

# MeshCore basics

## What MeshCore is

MeshCore is firmware for small LoRa radios that pass text messages across a mesh
without internet or phone service. Messages hop from radio to radio through
repeaters until they reach the recipient.

## Node types

A contact is one of: a client (a person's companion radio, used from a phone
app or a computer), a repeater (a fixed node that relays traffic), a room server
(a shared message board people log in to), or a sensor (a node that reports
telemetry).

## Direct messages and acknowledgements

A direct message (DM) goes to one contact. The recipient's radio answers with an
ACK, which is how the sender knows it arrived. If no ACK comes back, apps
usually retry, and a last retry sent as a flood can find a new route.

## Flood and direct routing

A flood message is rebroadcast by every repeater that hears it, so it spreads
across the whole mesh. A direct message follows a known path of repeaters
instead, which is quieter. A path is learned from earlier traffic; a route can
also be set by hand as an override. Direct sends use the override first, then
the learned path, then fall back to flood.

## Hops and paths

Each repeater a message passes through is one hop. A path lists the repeaters by
short identifiers taken from their public keys: 1, 2 or 3 bytes per hop
depending on the path hash mode. The hop count is the number of repeaters, not
the number of bytes.

## Channels

Channel messages go to everyone who has the channel's key. Public is the shared
default channel. A hashtag channel such as #bots needs no key exchange: its key
is derived from its name (the first 16 bytes of the SHA-256 of the name,
including the #), so anyone who knows the name can join. A channel name can be
at most 32 bytes including the #. Private channels use a random secret key that
has to be shared.

## Adverts

An advert is a node announcing itself: its name, public key, optional location,
and its clock time, all signed. A flood advert travels through repeaters across
the mesh; a zero-hop advert only reaches radios in direct range. Contacts are
learned from adverts.

## Repeater and room logins

Repeaters and room servers take a login. The admin password gives full control,
including the text CLI (get/set settings, reboot). The guest password allows
read-only access such as status.

# Repeater console commands

## Firmware version (ver)

`ver` answers with the repeater's firmware version.

## Clock (clock, time, clkreboot)

`clock` shows the repeater's clock. `time <unix seconds>` sets it, but only
forward: a repeater whose clock is ahead answers "clock cannot go backwards".
`clkreboot` resets the clock to a fixed date in May 2024 and reboots, after
which `time` can move it forward to the right time.

## Neighbours (neighbors)

`neighbors` lists the nodes the repeater hears directly (zero hops away).

## Advertise now (advert)

`advert` makes the repeater send an advert now.

## Admin password (password)

`password <new password>` replaces the admin password. It cannot be read back.
Setting it wrong locks everyone out of admin until a physical reset.

# Repeater settings (CLI get / set)

A repeater is configured over its text CLI after an admin login: `get <key>` reads a value, `set <key> <value>` writes it. Guests cannot read or change settings.

## Identity & Location

How the repeater names and places itself

### Name (`get name` / `set name <value>`)

Advertised node name. The firmware refuses [ ] \ : , ? and *. Changing this re-advertises the repeater under the new name.

### Latitude (`get lat` / `set lat <value>`)

Advertised latitude in decimal degrees. Range -90 to 90 °.

### Longitude (`get lon` / `set lon <value>`)

Advertised longitude in decimal degrees. Range -180 to 180 °.

### Owner Info (`get owner.info` / `set owner.info <value>`)

Free-text owner/contact note other clients can read. A | becomes a line break.

## Radio

LoRa parameters, receiver gain and repeating behaviour

### Radio (freq, BW, SF, CR) (`get radio` / `set radio <value>`)

Frequency in MHz, bandwidth in kHz, spreading factor, coding rate. Takes effect after a reboot. The repeater answers on these parameters only: getting them wrong takes it off the air until someone reaches it physically.

### TX Power (`get tx` / `set tx <value>`)

Transmit power. The hardware clamps values it cannot reach. Range -9 to 30 dBm.

### RX Boosted Gain (`get radio.rxgain` / `set radio.rxgain <value>`)

Runs the LoRa receiver in its high-sensitivity mode for a little more current. Radios without that mode answer unsupported. On or off.

### Front-End RX Gain (LNA) (`get radio.fem.rxgain` / `set radio.fem.rxgain <value>`)

Enables the external low-noise amplifier on boards fitted with a front-end module. On or off.

### Front-End TX Gain (PA) (`get radio.fem.txgain` / `set radio.fem.txgain <value>`)

Enables the external power amplifier on boards fitted with a front-end module. On or off.

### Channel Activity Detection (`get cad` / `set cad <value>`)

Listen for another transmission before sending, to avoid talking over it. On or off.

### Airtime Factor (`get af` / `set af <value>`)

Airtime budget divisor; higher means the repeater transmits less. Range 0 to 100.

### Duty Cycle Limit (`get dutycycle` / `set dutycycle <value>`)

Share of airtime the repeater may use (firmware 1.15 and newer). The same budget as Airtime Factor, expressed the other way round. Range 1 to 100 %.

### Repeat Mode (`get repeat` / `set repeat <value>`)

Whether the node relays other nodes' packets at all. On or off. Turning this off leaves the node reachable but stops it repeating.

### Max Flood Hops (`get flood.max` / `set flood.max <value>`)

Flood packets with more hops than this are not repeated. Range 0 to 64 hops.

### Max Flood Hops (adverts) (`get flood.max.advert` / `set flood.max.advert <value>`)

Hop limit applied to flooded adverts specifically. Range 0 to 64 hops.

### Max Flood Hops (unscoped) (`get flood.max.unscoped` / `set flood.max.unscoped <value>`)

Hop limit applied to flood packets that carry no region scope. Range 0 to 64 hops.

## Advertising

How often the repeater announces itself

### Local Advert Interval (`get advert.interval` / `set advert.interval <value>`)

Zero-hop advert period in minutes; 0 disables it. Stored in two-minute steps, and the firmware refuses anything under its minimum (an hour on current builds). Range 0 to 240 minutes.

### Flood Advert Interval (`get flood.advert.interval` / `set flood.advert.interval <value>`)

Flood advert period in hours: 0 disables it, otherwise 3 to 168. Floods cost the whole mesh airtime. Range 0 to 168 hours.

## Access

Passwords and what a non-admin client may do

### Admin Password (`password <new password>`)

Password for admin logins. Cannot be read back, only replaced. Setting this wrong locks everyone out of admin until a physical reset.

### Guest Password (`get guest.password` / `set guest.password <value>`)

Password for guest logins.

### Allow Read-Only Access (`get allow.read.only` / `set allow.read.only <value>`)

Whether clients without admin rights may read status and telemetry. On or off.

## Telemetry

Who may read each class of telemetry

### Base Telemetry (`get telemetry.mode.base` / `set telemetry.mode.base <value>`)

Who may read battery/uptime telemetry. Values: always, admin, never.

### Location Telemetry (`get telemetry.mode.loc` / `set telemetry.mode.loc <value>`)

Who may read GPS/location telemetry. Values: always, admin, never.

### Environment Telemetry (`get telemetry.mode.env` / `set telemetry.mode.env <value>`)

Who may read attached environment sensors. Values: always, admin, never.

## Bridge

RS232 / ESP-NOW packet bridge; only on firmware built with a bridge

### Bridge Type (`get bridge.type`)

Which bridge this firmware was built with: rs232, espnow or none. Read-only.

### Bridge Enabled (`get bridge.enabled` / `set bridge.enabled <value>`)

Whether packets are passed to and from the bridge link. On or off.

### Bridge Source (`get bridge.source` / `set bridge.source <value>`)

Which packets cross the bridge: those received over the air (rx) or those this node transmits (tx). Values: rx, tx.

### Bridge Delay (`get bridge.delay` / `set bridge.delay <value>`)

Delay before a bridged packet is re-sent over the air. Range 0 to 10000 ms.

### Bridge Baud Rate (RS232) (`get bridge.baud` / `set bridge.baud <value>`)

Serial speed of the RS232 bridge link. Changing it restarts the bridge. Range 9600 to 115200 baud.

### Bridge Wi-Fi Channel (ESP-NOW) (`get bridge.channel` / `set bridge.channel <value>`)

Wi-Fi channel the ESP-NOW bridge uses; both ends must match. Range 1 to 14.

### Bridge Secret (ESP-NOW) (`get bridge.secret` / `set bridge.secret <value>`)

Shared key that scrambles ESP-NOW bridge packets; both ends must match.

## Advanced

Timing, airtime and routing tuning; leave alone unless needed

### RX Delay Base (`get rxdelay` / `set rxdelay <value>`)

Base of the SNR-weighted delay before a heard packet is repeated, so the repeater that heard it best goes first; 0 disables it. The firmware accepts 0 to 20. Range 0 to 20.

### TX Delay Factor (`get txdelay` / `set txdelay <value>`)

Spread of the random pre-transmit delay that keeps repeaters from colliding (0 to 2). Range 0 to 2.

### Direct TX Delay Factor (`get direct.txdelay` / `set direct.txdelay <value>`)

Same, for directly-routed packets (0 to 2). Range 0 to 2.

### Interference Threshold (`get int.thresh` / `set int.thresh <value>`)

Noise-floor margin the radio must see clear before it transmits; 0 leaves the check off. Range 0 to 255.

### AGC Reset Interval (`get agc.reset.interval` / `set agc.reset.interval <value>`)

How often the radio's automatic gain control is reset; 0 disables it. Rounded down to a multiple of four. Range 0 to 1020 seconds.

### Multi ACKs (`get multi.acks` / `set multi.acks <value>`)

Whether the repeater sends multiple acknowledgements for a delivery. On or off.

### Path Hash Mode (`get path.hash.mode` / `set path.hash.mode <value>`)

How many bytes of each hop's identity a path records: 0 is the default one-byte hash; 1 and 2 record more to tell similar repeaters apart. Values: 0, 1, 2.

### Loop Detection (`get loop.detect` / `set loop.detect <value>`)

How aggressively a packet that has already passed through this node is dropped. Values: off, minimal, moderate, strict.

### Battery ADC Multiplier (`get adc.multiplier` / `set adc.multiplier <value>`)

Correction applied to the battery voltage reading; 0 restores the board's default. Boards without a battery divider answer unsupported. Range 0 to 10.

### Extra Spreading Factors (`get extra.sf` / `set extra.sf <value>`)

Up to three additional spreading factors the radio also listens on (LR2021 radios only), comma-separated. Range 5 to 12.

## Device Info

Read-only facts the firmware reports about itself

### Role (`get role`)

What this firmware is: repeater, room server, and so on. Read-only.

### Public Key (`get public.key`)

The node's identity, as it appears in adverts. Read-only.

### Bootloader Version (`get bootloader.ver`)

Bootloader the board runs (nRF52 boards only). Read-only.

### Power Source (`get pwrmgt.source`)

Whether the board is running from external power or its battery (nRF52 power management only). Read-only.

### Last Boot Reason (`get pwrmgt.bootreason`)

Why the board last reset and how it last shut down. Read-only.

### Voltage At Boot (`get pwrmgt.bootmv`)

Battery voltage measured when the board last started (nRF52 power management only). Read-only.

