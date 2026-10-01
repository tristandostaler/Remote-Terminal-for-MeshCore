<!--
Reference notes for the tinyllm bot, shipped with RemoteTerm. Overwritten in
the tinyllm-docs folder on every restart: put your own notes in another .md
file. Standard amateur radio and timekeeping references.
-->

# Radio reference tables

## Morse code letters

A .-  B -...  C -.-.  D -..  E .  F ..-.  G --.  H ....  I ..  J .---
K -.-  L .-..  M --  N -.  O ---  P .--.  Q --.-  R .-.  S ...  T -
U ..-  V ...-  W .--  X -..-  Y -.--  Z --..

## Morse code numbers and punctuation

1 .----  2 ..---  3 ...--  4 ....-  5 .....  6 -....  7 --...  8 ---..
9 ----.  0 -----  period .-.-.-  comma --..--  question mark ..--..
slash -..-.  equals (break) -...-  SOS ...---... (sent as one character).
Timing: a dash is 3 dots long, a gap inside a letter is 1 dot, between
letters 3 dots, between words 7 dots.

## Q-codes (what QTH, QSL, QRZ and others mean)

Q-codes are three-letter shorthand used on radio; as a question they end
with a question mark. QTH: my location is. QSL: I confirm receipt. QRZ: who
is calling me? QRM: man-made interference. QRN: natural noise (static). QSY:
change frequency. QRT: stop transmitting, closing down. QRV: I am ready. QSO:
a contact or conversation. QRP: low power (5 W or less). QRO: high power.
QSB: signal fading. QRX: wait, stand by. QTR: the correct time is.

## Signal reports (RST and 5-9)

RST rates Readability 1 to 5 (5 = perfectly readable), Strength 1 to 9 (9 =
very strong) and, for Morse only, Tone 1 to 9. "Five nine" (59) on voice
means loud and clear. On MeshCore, SNR and RSSI give a measured report
instead: SNR above 0 dB is a good link, RSSI around -120 dBm is weak.

## UTC and time zones

Radio logs and the mesh use UTC (Coordinated Universal Time, also called Zulu
or Z). Standard offsets: Newfoundland UTC-3:30, Atlantic UTC-4, Eastern
UTC-5, Central UTC-6, Mountain UTC-7, Pacific UTC-8, Alaska UTC-9, Hawaii
UTC-10; UK UTC+0, Central Europe UTC+1, Eastern Europe UTC+2, India
UTC+5:30, China UTC+8, Japan UTC+9, Australia Eastern UTC+10. Daylight saving
time adds one hour in summer where used (for example Eastern UTC-4). To
convert local time to UTC, subtract the offset: 14:00 Eastern standard time
is 19:00 UTC.

## 24-hour time

Radio and logs use the 24-hour clock: 00:00 is midnight, 12:00 is noon, and
afternoon hours add 12 (3 pm is 15:00, 11 pm is 23:00). Say it as "fifteen
hundred" or digit by digit.

## Maidenhead grid locators

Amateur radio shares rough positions as grid squares such as FN35fl. The
first two letters (field, A to R) split the world into 20 degree by 10
degree areas, the two digits (square) into 2 by 1 degree areas (about 160 by
110 km), and the last two letters (subsquare, a to x) into 5 by 2.5 minutes
(about 6 by 4 km). Four characters (FN35) are enough for most contacts. Most
mapping and logging apps convert GPS coordinates to a grid locator.

## Decibels quick reference

Decibels compare power: +3 dB doubles it, +10 dB is ten times, -3 dB halves
it. dBm is power relative to 1 milliwatt: 0 dBm = 1 mW, 10 dBm = 10 mW, 14
dBm = 25 mW, 20 dBm = 100 mW, 22 dBm = about 160 mW, 27 dBm = 500 mW, 30 dBm
= 1 W. Every 6 dB of extra link budget roughly doubles the range in open
terrain.

## Wavelength and antenna length

Wavelength in metres is 300 divided by the frequency in MHz. At 915 MHz a
wavelength is about 33 cm, so a quarter-wave antenna is about 8 cm; at 868
MHz about 35 cm and 8.6 cm; at 433 MHz about 69 cm and 17 cm; at 146 MHz
(2 m band) about 2 m and 51 cm.
