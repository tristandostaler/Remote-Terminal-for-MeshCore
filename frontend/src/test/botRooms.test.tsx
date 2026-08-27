import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api';
import { BotEditor } from '../components/bots/BotEditor';
import { BotsView } from '../components/bots/BotsView';
import type { Bot, BotEngineStatus } from '../types';

function makeBot(overrides: Partial<Bot> = {}): Bot {
  return {
    id: 'bot-1',
    name: 'ping',
    category: 'Basic',
    description: 'Replies pong',
    long_description: '',
    code: 'from remoteterm import bot',
    enabled: true,
    admin_only: false,
    respond_to_dms: true,
    respond_to_rooms: true,
    scope: { channels: 'all' },
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
    declared_keywords: ['ping'],
    declared_crons: [],
    declared_events: [],
    declared_webhooks: [],
    is_legacy: false,
    load_error: null,
    runs_24h: 0,
    ...overrides,
  };
}

const engineStatus: BotEngineStatus = {
  settings: {
    command_prefix: '!',
    require_prefix: false,
    mention_mode: 'also',
    global_reply_seconds: 10,
    per_user_seconds: 30,
    tx_spacing_seconds: 2,
    max_response_hops: 64,
    default_language: 'en',
    auto_detect_language: true,
    banned_users: [],
    profanity_mode: 'off',
    admin_users: [],
  },
  disabled_until_restart: false,
  disabled_by_env: false,
  total_bots: 1,
  enabled_bots: 1,
  erroring_bots: 0,
  runs_24h: 0,
};

describe('bots in rooms', () => {
  afterEach(() => vi.restoreAllMocks());

  const renderList = (bot: Bot) => {
    vi.spyOn(api, 'getBots').mockResolvedValue([bot]);
    vi.spyOn(api, 'getBotEngine').mockResolvedValue(engineStatus);
    vi.spyOn(api, 'getBotRuns').mockResolvedValue([]);
    vi.spyOn(api, 'getBotStats').mockResolvedValue({
      runs: 0,
      replies: 0,
      reply_rate: 0,
      errors: 0,
      unique_users: 0,
      avg_duration_ms: 0,
      top_bots: [],
      top_channels: [],
      top_users: [],
      error_bots: [],
      runs_by_hour: [],
    });
    return render(
      <BotsView botId={null} channels={[]} contacts={[]} onOpenBot={vi.fn()} onCloseBot={vi.fn()} />
    );
  };

  it('names rooms in the scope summary when the bot answers there', async () => {
    renderList(makeBot({ respond_to_dms: true, respond_to_rooms: true }));
    expect(await screen.findByText('All channels + DMs + rooms')).toBeInTheDocument();
  });

  it('leaves rooms out of the scope summary when the bot does not', async () => {
    renderList(makeBot({ respond_to_dms: true, respond_to_rooms: false }));
    expect(await screen.findByText('All channels + DMs')).toBeInTheDocument();
  });

  it('saves the rooms toggle separately from the DM one', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    const updateBot = vi.spyOn(api, 'updateBot').mockResolvedValue(makeBot());

    render(<BotEditor botId="bot-1" channels={[]} onBack={vi.fn()} onDeleted={vi.fn()} />);

    const rooms = await screen.findByRole('checkbox', { name: /Respond in room servers/i });
    expect(rooms).toBeChecked();
    fireEvent.click(rooms);
    fireEvent.click(screen.getByRole('button', { name: /Save/i }));

    await waitFor(() => expect(updateBot).toHaveBeenCalled());
    const payload = updateBot.mock.calls[0][1];
    expect(payload.respond_to_rooms).toBe(false);
    // Turning a bot off in rooms must not also silence it in DMs.
    expect(payload.respond_to_dms).toBe(true);
  });

  it('simulates a room post from the Test tab', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    const testBot = vi.spyOn(api, 'testBot').mockResolvedValue({
      matched: true,
      trigger: 'kw ping',
      duration_ms: 4,
      replies: [],
      error: null,
      logs: [],
    });

    render(<BotEditor botId="bot-1" channels={[]} onBack={vi.fn()} onDeleted={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Test' }));
    fireEvent.change(screen.getByPlaceholderText('wx 98101'), { target: { value: 'ping' } });
    fireEvent.click(screen.getByRole('button', { name: 'Room' }));
    fireEvent.click(screen.getByRole('button', { name: /Run test/i }));

    await waitFor(() => expect(testBot).toHaveBeenCalled());
    expect(testBot.mock.calls[0][1]).toMatchObject({ is_room: true, is_dm: false });
  });
});
