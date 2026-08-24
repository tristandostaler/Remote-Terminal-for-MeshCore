import { describe, expect, it } from 'vitest';

import type { Channel } from '../types';
import {
  DEFAULT_BOT_CHANNELS,
  defaultBotChannelName,
  isUnjoinedChannel,
  scopeChannelLabel,
} from '../utils/botScope';

function channel(key: string, name: string): Channel {
  return {
    key,
    name,
    is_hashtag: name.startsWith('#'),
    on_radio: true,
    last_read_at: null,
    favorite: false,
    muted: false,
  };
}

const BOT_KEY = DEFAULT_BOT_CHANNELS[0].key;
const BOTS_KEY = DEFAULT_BOT_CHANNELS[1].key;

describe('default bot channels', () => {
  it('mirrors the keys the backend derives from #bot and #bots', () => {
    // SHA256 of the verbatim name, first 16 bytes — same on every node. Keep in
    // step with BOT_CHANNEL_KEYS in app/channel_constants.py.
    expect(DEFAULT_BOT_CHANNELS).toEqual([
      { key: 'EB50A1BCB3E4E5D7BF69A57C9DADA211', name: '#bot' },
      { key: '0D24F5830B449668B8C221759B6C50D2', name: '#bots' },
    ]);
  });

  it('names a default bot channel from its key alone', () => {
    expect(defaultBotChannelName(BOT_KEY)).toBe('#bot');
    expect(defaultBotChannelName(BOTS_KEY.toLowerCase())).toBe('#bots');
    expect(defaultBotChannelName('A'.repeat(32))).toBeUndefined();
  });
});

describe('scopeChannelLabel', () => {
  it('prefers the joined channel name', () => {
    expect(scopeChannelLabel(BOT_KEY, [channel(BOT_KEY, '#bot')])).toBe('#bot');
  });

  it('falls back to the well-known bot channel name when not joined', () => {
    // The whole point of the default: it names a channel we may not have yet,
    // so the chip must not read as a raw hex key.
    expect(scopeChannelLabel(BOT_KEY, [])).toBe('#bot');
  });

  it('falls back to a truncated key for anything else', () => {
    expect(scopeChannelLabel('A'.repeat(32), [])).toBe('AAAAAAAA…');
  });
});

describe('isUnjoinedChannel', () => {
  it('is case-insensitive about the key', () => {
    expect(isUnjoinedChannel(BOT_KEY.toLowerCase(), [channel(BOT_KEY, '#bot')])).toBe(false);
  });

  it('reports a scoped channel this node does not carry', () => {
    expect(isUnjoinedChannel(BOTS_KEY, [channel(BOT_KEY, '#bot')])).toBe(true);
  });
});
