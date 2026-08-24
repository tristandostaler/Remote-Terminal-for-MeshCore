"""Fake "hacking" gag: Linux commands get supervillain-mainframe errors.

Harmless and clearly a joke — nothing is executed, ever.
"""

import random

from remoteterm import bot

BOT_META = {
    "key": "hacker",
    "name": "hacker",
    "category": "Fun",
    "description": "Linux commands get joke supervillain-mainframe errors",
    "long_description": (
        "A gag. Type a Linux command such as `sudo`, `rm -rf` or `whoami` in one of this bot's "
        "channels and the node answers with a straight-faced supervillain-mainframe error. Nothing "
        "is ever executed — the keywords only pick a joke line. It answers to a long list of shell "
        "words, so keep it away from channels where those get typed in earnest. Offline."
    ),
    "version": "1.2.1",
}

KEYWORDS = (
    "sudo",
    "ps aux",
    "grep",
    "ls -l",
    "ls -la",
    "echo $path",
    "rm",
    "rm -rf",
    "cat",
    "whoami",
    "top",
    "htop",
    "netstat",
    "ss",
    "kill",
    "killall",
    "chmod",
    "find",
    "history",
    "passwd",
    "su",
    "ssh",
    "wget",
    "curl",
    "df -h",
    "free",
    "ifconfig",
    "ip addr",
    "uname -a",
)

RESPONSES = (
    "ACCESS DENIED: '{cmd}' needs level 11 clearance. You have level 'guest', {name}.",
    "INTRUSION DETECTED: releasing attack drones... drones are out of battery. You live.",
    "ERROR 418: this mainframe is a teapot and refuses to brew '{cmd}'.",
    "TRACE STARTED: triangulating {name}... you appear to be on a mesh radio. Bold move.",
    "kernel panic averted: '{cmd}' quarantined in the evil sandbox.",
    "AUDIT LOG: '{cmd}' by {name} recorded, printed, and framed in the villain's lair.",
    "COUNTERMEASURES ONLINE: deploying infinite progress bar. Please wait forever.",
    "DENIED: root access is reserved for the Dark Overlord and their houseplants.",
    "SECURITY ALERT: nice try, {name}. The self-destruct button is decorative.",
    "SEGFAULT (core dumped): the core rolled off the desk. It is said to still be rolling.",
    "FIREWALL: your '{cmd}' was extinguished by the literal wall of fire. Impressive aim.",
    "PERMISSION DENIED: password must contain at least one evil laugh (mwahaha).",
)


@bot.on_keyword()
@bot.on_keyword(*KEYWORDS)
async def mainframe(ctx, msg):
    cmd = msg.keyword or "that"
    # @[name] is the mention syntax mesh clients recognize and highlight.
    name = f"@[{msg.sender_name}]" if msg.sender_name else "intruder"
    await ctx.reply(random.choice(RESPONSES).format(cmd=cmd, name=name))
