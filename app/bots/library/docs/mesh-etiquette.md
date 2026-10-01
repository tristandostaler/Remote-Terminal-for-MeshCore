<!--
Reference notes for the tinyllm bot, shipped with RemoteTerm. Overwritten in
the tinyllm-docs folder on every restart: put your own notes in another .md
file. General good practice on a shared LoRa mesh; your local community's own
conventions come first: add them to your own notes.
-->

# Mesh etiquette (being a good neighbour on the mesh)

## Why airtime matters

Every message on a LoRa mesh uses shared airtime: while one node transmits,
nearby nodes cannot, and each flood message is repeated by every repeater
that hears it. Long, frequent or automated messages slow the mesh down for
everyone and cause collisions that lose other people's messages. Short,
purposeful messages keep it working, which matters most when the mesh is the
only network left.

## Keep messages short and useful

Write one short message rather than several, skip greetings chains and
repeated "test" messages on busy channels, and avoid emoji-only replies where
a reaction would do. Use a direct message for a conversation between two
people instead of a public channel. A test message belongs in a test channel
or a direct message to someone who agreed to help.

## Which channel to use

Public is for everyone in range: short announcements, questions and
introductions. Topic and hashtag channels (for example #bot for bot commands,
or a local area channel) keep traffic where it belongs. Send bot commands
only in the bot channels (#bot, #bots) or by direct message, never on Public.
Follow the channel names your local mesh community agreed on.

## Running bots responsibly

Bots answer automatically and can generate a lot of traffic. Keep them to the
bot channels and direct messages, give them cooldowns, keep answers short,
never have two bots answer each other, and turn off anything that posts on a
schedule more often than it needs to. Disclose that a node is a bot in its
name or answers.

## Adverts (how often to send them) and node names

An advert announces your node to the mesh; a flood advert is repeated across
the whole mesh, so send them only when needed (after changing name or
location, or occasionally), not every few minutes. Prefer a zero-hop advert
to reach only nearby nodes. Give your node a clear name, and sharing a
location is optional: set it only as precisely as you are comfortable with.

## Placing and configuring a repeater responsibly

Talk to the local mesh community before adding a repeater: a new repeater
right next to an existing one adds collisions without adding coverage. Use
the radio settings (frequency, bandwidth, spreading factor) the local mesh
uses, or your node cannot talk to anyone. Set the repeater's clock and
location, choose a strong admin password, and keep its firmware up to date.
Use regions and flood scopes where the community uses them to keep floods
local.

## Emergency traffic comes first

If someone sends an emergency message, stop other traffic on that channel,
relay it toward whoever can help, and acknowledge it so the sender knows it
got through. Do not clog the channel with questions; let the people handling
it talk. Practise emergency message formats in calm times, in a test channel.

## Privacy and respect

Messages on public channels are readable by anyone with the channel key, and
hashtag channel keys are public. Do not share other people's locations or
personal details without consent. Keep it civil and legal: radio rules forbid
some content and encryption rules vary by country.
