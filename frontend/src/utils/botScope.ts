import { CONTACT_TYPE_ROOM, type Channel, type Contact } from '../types';

/**
 * The channels a bot listens to out of the box: the two conventional bot
 * channels plus DMs. Bots answer commands, so an unscoped bot replies on Public
 * and on every other channel the node carries — noise for everyone else on the
 * mesh. The operator widens the scope from the bot's Settings tab.
 *
 * Hashtag keys are SHA256(name)[:16] of the verbatim name, so these are the
 * same keys on every node and stay meaningful even before this node joins the
 * channel. Mirrors `BOT_CHANNEL_KEYS` in `app/channel_constants.py`.
 */
export const DEFAULT_BOT_CHANNELS: { key: string; name: string }[] = [
  { key: 'EB50A1BCB3E4E5D7BF69A57C9DADA211', name: '#bot' },
  { key: '0D24F5830B449668B8C221759B6C50D2', name: '#bots' },
];

/** The name of a well-known default bot channel, even if we have not joined it. */
export function defaultBotChannelName(key: string): string | undefined {
  const upper = key.toUpperCase();
  return DEFAULT_BOT_CHANNELS.find((channel) => channel.key === upper)?.name;
}

/**
 * How to label a scoped channel key: the joined channel's name, else the
 * well-known bot-channel name, else a truncated key.
 */
export function scopeChannelLabel(key: string, channels: Channel[]): string {
  const joined = channels.find((channel) => channel.key.toUpperCase() === key.toUpperCase());
  return joined?.name ?? defaultBotChannelName(key) ?? `${key.slice(0, 8)}…`;
}

/** True when the key is not a channel this node has joined — the bot is deaf there. */
export function isUnjoinedChannel(key: string, channels: Channel[]): boolean {
  return !channels.some((channel) => channel.key.toUpperCase() === key.toUpperCase());
}

/** The room servers this node knows about, the order the pickers show them in. */
export function roomContacts(contacts: Contact[]): Contact[] {
  return contacts
    .filter((contact) => contact.type === CONTACT_TYPE_ROOM)
    .sort((a, b) => (a.name || a.public_key).localeCompare(b.name || b.public_key));
}

/**
 * How to label a scoped room key: the known room's name, else a truncated key.
 *
 * Unlike a hashtag channel there is no name to derive — a room is a contact, so
 * a key we have never heard an advert from can only be shown as itself.
 */
export function scopeRoomLabel(key: string, contacts: Contact[]): string {
  const known = contacts.find((contact) => contact.public_key.toLowerCase() === key.toLowerCase());
  return known?.name || `${key.slice(0, 8)}…`;
}

/** True when the key is not a room contact this node knows — the bot is deaf there. */
export function isUnknownRoom(key: string, contacts: Contact[]): boolean {
  return !roomContacts(contacts).some(
    (contact) => contact.public_key.toLowerCase() === key.toLowerCase()
  );
}
