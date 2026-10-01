<!--
Reference notes for the tinyllm bot, shipped with RemoteTerm. Overwritten in
the tinyllm-docs folder on every restart: put your own notes in another .md
file. Antennas, solar and batteries for nodes are in meshcore-hardware.md.
-->

# Electronics and power basics

## Ohm's law (volts, amps, ohms)

Voltage (V, volts) = current (I, amps) × resistance (R, ohms). So I = V / R
and R = V / I. Example: 5 V across a 250 ohm resistor drives 0.02 A (20 mA).
Power (P, watts) = V × I = I² × R = V² / R. Example: 12 V at 2 A is 24 W.

## Battery capacity and runtime (how long a battery lasts)

Energy in watt-hours (Wh) = voltage × amp-hours (Ah). A 3.7 V 3,000 mAh
(3 Ah) cell holds about 11 Wh; a 12 V 100 Ah battery about 1,200 Wh. Runtime
in hours is roughly the usable energy divided by the load in watts: an 11 Wh
cell running a 0.5 W node lasts about 22 hours, less in practice (allow 20
percent for losses and never draining a lithium cell fully, or 50 percent for
lead-acid).

## Cells in series and in parallel

In series (plus to minus), voltages add and capacity stays the same: two
3.7 V 3 Ah cells make 7.4 V 3 Ah. In parallel (plus to plus), capacity adds
and voltage stays the same: 3.7 V 6 Ah. Only combine identical cells at the
same charge level, and use a battery management system (BMS) for lithium
packs.

## Battery voltages (when is it full or empty)

Lithium-ion / LiPo cell: 4.2 V full, 3.7 V nominal, about 3.3 V nearly empty,
never below about 3.0 V. LiFePO4 cell: 3.6 V full, 3.2 V nominal, about 2.8 V
empty. 12 V lead-acid (resting): 12.7 V full, 12.2 V half, 11.9 V and below
nearly empty (stop discharging to protect it). AA alkaline: 1.5 V new, 1.0 V
used up; NiMH rechargeable AA: 1.2 V.

## USB power limits

USB 2.0 ports give 5 V at 0.5 A (2.5 W); USB 3.0 ports 0.9 A (4.5 W). A
dedicated USB charger often gives 5 V at 2 to 3 A. USB-C can give 5 V at up to
3 A (15 W), and with USB Power Delivery it negotiates higher voltages (9, 15,
20 V) for up to 100 W or more. A device can only draw what both the charger
and the cable support.

## Wire gauge and current

Thicker wire carries more current safely. Rough limits for short low-voltage
runs: 22 AWG (0.33 mm²) about 3 A, 18 AWG (0.8 mm²) about 7 to 10 A, 14 AWG
(2 mm²) about 15 A, 12 AWG (3.3 mm²) about 20 A, 10 AWG (5.3 mm²) about 30 A.
Long runs at low voltage lose voltage: use thicker wire, and a fuse rated for
the wire near the battery.

## Fuses and short circuits

A fuse protects the wire, not the device: pick a fuse at or below what the
wire can carry, and put it as close to the battery's positive terminal as
possible. A short circuit on a lithium or car battery can melt wires and
start a fire within seconds: cover terminals, remove rings and watches, and
connect the negative last.

## How to use a multimeter

Voltage (V with a straight line for DC, wavy line for AC): black probe in COM,
red in V, touch across the two points. Continuity or resistance (ohms or the
beep symbol): only on unpowered circuits; it beeps when two points are
connected, useful for finding broken wires and checking fuses. Current (A):
move the red probe to the A or mA socket and put the meter in series with the
load; never measure current across a battery, it shorts it and blows the
meter's fuse.

## Resistor colour code

Bands read from the end with the bands closest together. Black 0, brown 1,
red 2, orange 3, yellow 4, green 5, blue 6, violet 7, grey 8, white 9. The
first two (or three) bands are digits, the next is the number of zeros, and
the last is tolerance (gold 5 percent, silver 10, brown 1). Example: brown,
black, red, gold = 1, 0, two zeros = 1,000 ohms (1 kΩ) ±5 percent.

## LED resistor

An LED needs a resistor in series: R = (supply voltage - LED voltage) / LED
current. Red LEDs drop about 2 V, white and blue about 3 V; 10 to 20 mA is
typical. For a red LED on 5 V at 15 mA: (5 - 2) / 0.015 = 200 ohms; use the
next standard value up (220 ohms).

## Soldering basics

Heat the joint (the pad and the wire together) with a clean, tinned iron at
about 320 to 350 °C, then feed solder into the joint, not onto the iron. A
good joint is shiny and cone-shaped, and takes 2 to 3 seconds. Tin wires
before joining them. Work in a ventilated place, never touch the tip, and put
the iron back in its stand. Heat-shrink tubing insulates splices: slide it on
before soldering.

## Solar panel and charging math

A solar panel's rated watts are under full sun; expect about 3 to 5 equivalent
full-sun hours a day in summer and 1 to 2 in winter at mid latitudes. Daily
energy is about panel watts × sun hours × 0.7 for losses: a 10 W panel gives
roughly 7 to 35 Wh a day. A small node using 0.5 W needs 12 Wh a day, so size
for the worst month and add battery for several cloudy days. Always charge
through a charge controller matched to the battery.

## Inverters and running mains devices from a battery

An inverter turns 12 V DC into mains AC. Size it above the device's watts
(motors and fridges need 3 to 6 times their running power to start). A
battery's runtime with an inverter is about Wh × 0.85 / watts. Prefer
devices that run on 12 V or USB directly: every conversion wastes power.
