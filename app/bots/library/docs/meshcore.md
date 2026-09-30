<!--
Starter reference notes for the tinyllm bot, shipped with RemoteTerm. Copied
into the bot's docs folder (data/tinyllm-docs by default) and overwritten there
on every restart, so changes to this copy do not last: put your own notes in
another .md file next to it, which is never touched.
Every heading starts a section; the bot searches all sections and hands the
best matches to the model.
The regions guide is written from MeshCore's CLI documentation; the full
command reference and FAQ are in meshcore-cli.md and meshcore-faq.md.
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

# Regions (quick guide)

## What regions are

A region is a named area, such as #Europe or #UK, that flood messages can be
scoped to. Each repeater keeps a tree of regions (parents and children) and,
for each region, whether it floods messages scoped to that region. The
wildcard region `*` stands for messages that carry no region at all. Region
commands are repeater CLI commands: they need an admin login and firmware 1.10
or later.

## Save region changes (region save)

`region save` saves the region changes made since the last reboot. Run it
after adding, removing, allowing or blocking regions, or the changes are not
kept.

## Add a region (region put)

`region put <name> [parent_name]` creates a region. Without a parent it goes
under the wildcard `*`. A new region is allowed to flood. For example,
`region put #UK #Europe` adds #UK under #Europe. Then `region save`.

## Add several regions at once (region def, region load)

`region def a b c` creates a chain in one line: each name becomes a child of
the one before. `name|jump` creates `name` and then moves back to `jump` to
start another branch; `region def a|* b|* c` creates a flat list under `*`.
One line holds at most 160 characters. `region load` is the interactive way:
type one region per line, indent children under their parent (up to 8 levels),
add `F` after a name to allow flooding, and end with a blank line. The
firmware notes `region load` with no name does not work remotely. Finish with
`region save`.

## Remove (delete) a region (region remove)

`region remove <name>` deletes a region. Its child regions must be removed
first. Then `region save`.

## Allow flooding for a region (region allowf)

`region allowf <name>` lets the repeater flood (repeat) messages scoped to that
region. `region allowf *` allows messages that carry no region. Then `region
save`.

## Block flooding for a region (region denyf)

`region denyf <name>` stops the repeater flooding messages scoped to that
region. `region denyf *` drops messages that carry no region. Then `region
save`.

## Set the home region (region home)

`region home <name>` sets this node's home region; `region home` alone shows
it.

## Set the default scope region (region default)

`region default <name>` sets this node's default scope region; `region
default` alone shows it, and `region default <null>` clears it.

## List and inspect regions (region, region get, region list)

`region` alone shows the whole region tree with each region's flood
permission. `region get <name>` shows one region. `region list allowed` or
`region list denied` lists regions by permission (firmware 1.12 or later, over
the serial port only).
