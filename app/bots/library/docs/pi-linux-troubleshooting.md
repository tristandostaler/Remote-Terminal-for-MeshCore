<!--
Reference notes for the tinyllm bot, shipped with RemoteTerm. Overwritten in
the tinyllm-docs folder on every restart: put your own notes in another .md
file. Commands are for Raspberry Pi OS / Debian / Ubuntu; RemoteTerm's own
setup and troubleshooting are in the remoteterm-*.md notes.
-->

# Raspberry Pi and Linux troubleshooting

## Check that RemoteTerm is running (or not starting)

If RemoteTerm does not start or the page does not load, check it is running and read its logs. Installed as a service: sudo systemctl status remoteterm shows whether it is
running; sudo systemctl restart remoteterm restarts it. With Docker: docker
compose ps (in the folder with docker-compose.yml) lists the container, and
docker compose restart remoteterm restarts it. The web interface listens on
port 8000: open http://<pi-address>:8000 from another device on the network.

## Read RemoteTerm's logs

Service: journalctl -u remoteterm -f follows the log live; add -n 200 for the
last 200 lines, or --since "1 hour ago". Docker: docker compose logs -f
remoteterm, or docker compose logs --tail 200 remoteterm. Set
MESHCORE_LOG_LEVEL=DEBUG for more detail, then restart.

## Find the Pi's IP address

On the Pi: hostname -I prints its addresses. From another computer on the
same network, try ping raspberrypi.local (or the hostname you set), or look in
the router's list of connected devices. ip a shows every interface; wlan0 is
Wi-Fi and eth0 is wired.

## Wi-Fi not connecting

Check the signal and network: nmcli device wifi list shows the networks the
Pi can see; sudo nmcli device wifi connect "NAME" password "PASSWORD"
connects (Raspberry Pi OS Bookworm and newer use NetworkManager). sudo
raspi-config, then System Options, Wireless LAN, also sets it, as does the
Wi-Fi country under Localisation Options, which must be set or Wi-Fi stays
off. rfkill list shows whether Wi-Fi is blocked; sudo rfkill unblock wifi
unblocks it. A wired Ethernet cable is the fallback.

## Radio not detected over USB

ls /dev/ttyUSB* /dev/ttyACM* lists serial devices; unplug and replug the
radio and run dmesg | tail -20 to see what the kernel detected. Try another
cable: many USB cables are charge-only with no data wires. The user running
RemoteTerm needs serial access: sudo usermod -aG dialout $USER, then log out
and back in. With Docker, the device must be passed through in
docker-compose.yml (devices). Only one program can hold the port at a time:
close the MeshCore web flasher, meshcore-cli or anything else using it.

## Disk full

df -h shows free space per disk; the line for / is the SD card. Find what is
big: sudo du -xh / --max-depth=2 | sort -h | tail -20. Common fixes: sudo
apt clean, sudo journalctl --vacuum-size=100M, docker system prune (removes
stopped containers and unused images), and in RemoteTerm, Settings, delete old
raw packets and vacuum the database.

## Out of memory and swap

free -h shows memory and swap in use. A Pi with 1 GB or less benefits from
swap: on Raspberry Pi OS, edit /etc/dphys-swapfile, set CONF_SWAPSIZE=1024,
then sudo systemctl restart dphys-swapfile. dmesg | grep -i oom shows whether
the kernel killed a process for lack of memory. Close unused programs, and
use the smallest tinyllm model on small boards.

## Under-voltage warning (lightning bolt)

A lightning bolt icon, or "Under-voltage detected" in dmesg, means the power
supply cannot keep up: the Pi slows down, and USB devices and the SD card can
fail. vcgencmd get_throttled prints throttled=0x0 when all is well. Use the
official power supply (5.1 V 3 A for a Pi 4, 5 V 5 A for a Pi 5) and a short,
thick cable; power hungry USB devices from a powered hub.

## SD card failing or read-only

Signs: random crashes, files that change back after a reboot, "read-only file
system" errors, or a Pi that will not boot. Back up the data folder right
away (it holds the database). Prevent it with a good-quality card, a proper
power supply, always shutting down cleanly (sudo shutdown -h now) rather than
pulling the power, or by booting from a USB SSD instead.

## Overheating

vcgencmd measure_temp shows the CPU temperature. Above about 80 °C the Pi
slows itself down. Add a heatsink or fan, give the case ventilation, and keep
it out of direct sun.

## Set the time without internet

timedatectl shows the clock and whether it is synchronised. The Pi has no
battery-backed clock (except the Pi 5 with a battery), so without internet it
starts at the last saved time. Set it by hand: sudo date -s "2026-10-01
14:30:00". A GPS module or a real-time clock (RTC) module keeps time
offline. MeshCore radios and repeaters rely on correct time for adverts.

## Useful Linux commands

ls lists files, cd changes directory, cat shows a file, nano edits one (save
with Ctrl+O, exit with Ctrl+X). sudo runs a command as administrator. top or
htop shows what uses CPU and memory (q quits). sudo reboot restarts; sudo
shutdown -h now powers off safely. uptime shows how long since boot. Ctrl+C
stops a running command.

## Stop a frozen program (kill a process)

Ctrl+C stops the command running in the terminal. For a program running in
the background, find its process number (PID) with ps aux | grep name or in
top, then kill PID; if it ignores that, kill -9 PID. pkill name kills by
name. Restart a stuck service with sudo systemctl restart name rather than
killing it.

## Update the system and RemoteTerm

sudo apt update && sudo apt upgrade updates the system (needs internet).
RemoteTerm with Docker: docker compose pull then docker compose up -d.
Installed from a git clone: git pull, then rebuild the frontend and restart
the service as the README describes. Back up the data folder first.

## Back up and restore RemoteTerm's data (database)

Everything RemoteTerm stores is in its data folder (data/ by default,
including meshcore.db). Stop RemoteTerm, copy the folder to a USB stick or
another computer (cp -r data /media/usb/), and start it again. To restore,
stop it, put the folder back, and start it.
