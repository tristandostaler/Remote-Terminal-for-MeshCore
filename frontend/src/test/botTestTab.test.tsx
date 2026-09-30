import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api';
import { BotEditor } from '../components/bots/BotEditor';
import type { Bot, BotTestResponse } from '../types';

function makeBot(): Bot {
  return {
    id: 'bot-1',
    name: 'tinyllm',
    category: 'Fun',
    description: '',
    long_description: '',
    code: 'from remoteterm import bot',
    enabled: true,
    admin_only: false,
    respond_to_dms: true,
    scope: { channels: 'all', rooms: { only: [] } },
    cooldown_seconds: 0,
    per_user_cooldown_seconds: 0,
    queue_threshold_seconds: 0,
    settings_schema: [],
    settings: {},
    ui_triggers: [],
    builtin_key: null,
    builtin_version: null,
    modified: false,
    last_error: null,
    sort_order: 0,
    created_at: 0,
    updated_at: 0,
    declared_keywords: ['ask'],
    declared_crons: [],
    declared_events: [],
    declared_webhooks: [],
    is_legacy: false,
    load_error: null,
    runs_24h: 0,
    deletable: true,
  };
}

function reply(text: string): BotTestResponse {
  return {
    matched: true,
    trigger: 'kw ask',
    duration_ms: 3,
    replies: [{ is_dm: true, destination: null, channel_key: null, text, region: null }],
    error: null,
    logs: [],
  };
}

async function openTestTab() {
  render(
    <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
  );
  fireEvent.click(await screen.findByRole('button', { name: 'Test' }));
}

function send(text: string) {
  fireEvent.change(screen.getByPlaceholderText('wx 98101'), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: /Run test/i }));
}

describe('bot Test tab', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('sends the earlier DM exchanges so a bot can remember the conversation', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    const testBot = vi
      .spyOn(api, 'testBot')
      .mockResolvedValueOnce(reply('Hi Ada.'))
      .mockResolvedValueOnce(reply('Ada.'));
    await openTestTab();
    fireEvent.click(screen.getByRole('button', { name: 'DM' }));

    send('ask my name is Ada');
    expect(await screen.findByText('Hi Ada.')).toBeInTheDocument();
    send('ask what is my name');
    await waitFor(() => expect(testBot).toHaveBeenCalledTimes(2));

    expect(testBot.mock.calls[0][1].transcript).toEqual([]);
    expect(testBot.mock.calls[1][1]).toMatchObject({
      is_dm: true,
      transcript: [
        { text: 'ask my name is Ada', outgoing: false },
        { text: 'Hi Ada.', outgoing: true },
      ],
    });
  });

  it('clears the transcript, which also starts the DM conversation over', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    const testBot = vi.spyOn(api, 'testBot').mockResolvedValue(reply('Hello.'));
    await openTestTab();
    fireEvent.click(screen.getByRole('button', { name: 'DM' }));

    const clear = screen.getByRole('button', { name: /Clear/i });
    expect(clear).toBeDisabled();
    send('ask hi');
    expect(await screen.findByText('Hello.')).toBeInTheDocument();

    fireEvent.click(clear);
    expect(screen.queryByText('Hello.')).not.toBeInTheDocument();
    expect(screen.getByText(/No test runs yet/)).toBeInTheDocument();

    send('ask again');
    await waitFor(() => expect(testBot).toHaveBeenCalledTimes(2));
    expect(testBot.mock.calls[1][1].transcript).toEqual([]);
  });

  it('labels each run with where it was sent, and sends no transcript outside DMs', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    const testBot = vi.spyOn(api, 'testBot').mockResolvedValue(reply('ok'));
    await openTestTab();

    send('ask in channel');
    await screen.findByText('ok');
    fireEvent.click(screen.getByRole('button', { name: 'DM' }));
    send('ask in dm');
    await waitFor(() => expect(testBot).toHaveBeenCalledTimes(2));

    expect(screen.getByText(/in #test/)).toBeInTheDocument();
    expect(screen.getByText(/\(DM\)/)).toBeInTheDocument();
    // The channel run is not part of the DM conversation.
    expect(testBot.mock.calls[1][1].transcript).toEqual([]);
  });
});
