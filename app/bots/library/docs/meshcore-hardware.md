<!--
Reference notes for the tinyllm bot, shipped with RemoteTerm. Overwritten in
the tinyllm-docs folder on every restart: put your own notes in another .md
file. Radio settings (BW, SF, CR) and regional presets are in meshcore-faq.md.
General radio practice, not tied to one board; check your hardware's own
documentation for its limits.
-->

# Antennas, repeater placement and power

## Never transmit without an antenna

Always connect the antenna before powering a LoRa radio. Transmitting with no
antenna, or a broken cable, reflects the power back into the radio chip and
can damage it permanently. Turn the device off before swapping antennas.

## Antenna connectors (SMA, RP-SMA, IPEX / U.FL)

SMA and RP-SMA look alike but do not mate properly: SMA male has a centre pin
inside the threaded nut, RP-SMA male has a hole there. Check both sides
before buying an antenna or pigtail. Tiny IPEX / U.FL connectors on boards
are fragile and rated for only a few dozen connections: pull straight up, and
glue or tape the cable so it cannot be yanked.

## Choosing an antenna

Use an antenna tuned for your band (for example 868 MHz in Europe, 915 MHz in
North America); a wrong-band antenna loses a lot of signal. A higher-gain
omnidirectional antenna flattens its pattern: more range toward the horizon,
less straight up and down, so very high gain can miss nearby nodes on hills or
in tall buildings. 3 to 6 dBi suits most repeaters. A directional (Yagi)
antenna links two fixed points far apart. Antennas are vertical
(polarised); keep them vertical on all nodes.

## How high to put the antenna (height and line of sight)

At these frequencies range is mostly line of sight: raising an antenna from
2 m to 10 m usually helps far more than more transmit power. Hills,
buildings, trees and even people block the signal; foliage and wet leaves
absorb it. The space around the direct line between two antennas (the
Fresnel zone) also needs to be clear, so a path that just grazes a rooftop
or hilltop loses signal. A repeater on a roof, tower or hilltop covers a
whole area.

## Where to put a repeater

High and clear: a rooftop, mast, attic window facing the area to cover, or a
hill. Away from metal and other transmitters (cell antennas, broadcast
towers), which can deafen a receiver. Somewhere you can reach for repairs,
with power or sun for a panel. Talk to the local mesh first: a new repeater
next to an existing one adds collisions without adding coverage.

## Coax cable loss

Long thin coax eats signal: at 900 MHz, RG-174 loses about 1 dB per metre,
RG-58 about 0.5 dB per metre, LMR-400 about 0.13 dB per metre. Keep cable
runs short and thick, or put the radio up at the antenna and run power or
USB down instead. Every connector adds a little loss.

## Weatherproofing an outdoor node

Use a sealed (IP65 or better) enclosure, cable glands for every cable, and a
small vent or breather plug so condensation can escape. Wrap outdoor
connectors in self-amalgamating tape, then electrical tape over it. Point
cable entries downward with a drip loop so water runs off. Avoid direct sun on
the enclosure in hot climates: heat kills batteries.

## Lightning and grounding

A mast or antenna on a roof is a lightning target. Ground the mast to a proper
earth ground, and use a coax lightning arrester where the cable enters the
building. Disconnect indoor gear during storms if the antenna is high and
exposed. Solar-powered nodes with no cable into the building are safer for
the house.

## Powering a node (battery life)

LoRa nodes use very little power while listening and more while transmitting.
Board choice matters most: nRF52-based boards (RAK4631, T114, T1000-E,
XIAO nRF52) use far less power than ESP32-based ones (Heltec V3, T-Beam),
often lasting days to weeks on a small battery, so they suit solar repeaters.
Screens, Wi-Fi, Bluetooth and GPS all drain batteries: turn off what you do
not need. Fewer adverts and lower TX power also help.

## Solar power for a repeater

Size the panel and battery for the worst season, not summer. A low-power
nRF52 repeater often runs on a panel of about 1 to 6 W with a single 18650 or
a few thousand mAh battery; an ESP32 board needs several times more. Tilt the
panel toward the equator at about your latitude (steeper in winter), keep it
free of shade, and plan for several days without sun. Use a charge
controller suited to the battery chemistry.

## Battery types and safety

Lithium-ion (18650, LiPo): small and light, but must not be charged below
about 0 °C or above about 45 °C, and must never be punctured or
short-circuited. LiFePO4: safer and longer-lived, tolerate heat better, but
also must not be charged below 0 °C; in cold climates use a charger with a
low-temperature cut-off or a heated enclosure. Use protected cells, keep
batteries out of direct sun, and stop using any that swell.

## Transmit power and duty cycle rules

Each region limits LoRa transmit power and how much time a device may spend
transmitting. In Europe's 868 MHz band many sub-bands allow only 1 percent
duty cycle (36 seconds per hour) at up to 25 mW (14 dBm); 869.4 to 869.65 MHz
allows 10 percent at up to 500 mW (27 dBm). In North America's 902 to 928 MHz
band (FCC Part 15, ISED RSS-247) the most any device may use is 1 W (30 dBm),
and the limit depends on how the signal uses the band, so it can be lower;
most LoRa boards top out around 22 dBm (160 mW) anyway. Check your country's
rules. More power rarely helps as much as a
higher antenna, and it costs battery.

## Testing range and coverage

Send messages or pings while moving around, and note the SNR (signal to noise
ratio) and RSSI (signal strength) the node reports: positive SNR is a strong
link, and LoRa still decodes down to roughly -7 dB at SF7 and -20 dB at SF12.
RSSI near -120 dBm or below is weak. Trace and path discovery show which
repeaters a message took.
