import { readFileSync } from 'node:fs';

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api';
import { BotEditor, resolveGeneratedUrl, validateGeneratedUrl } from '../components/bots/BotEditor';
import type { Bot, BotSettingsSchemaField } from '../types';

const botEditorSource = readFileSync('src/components/bots/BotEditor.tsx', 'utf8');

const schema: BotSettingsSchemaField[] = [
  {
    key: 'endpoint',
    label: 'Endpoint URL',
    type: 'url',
    default: 'https://example.test/api',
  },
  { key: 'public_server', label: 'Public server', type: 'text' },
  { key: 'webhook_token', label: 'Webhook token', type: 'password' },
  {
    key: 'callback_url',
    label: 'Provider callback URL',
    type: 'generated_url',
    template: 'http://{public_server}:8000/api/hooks/provider?token={webhook_token}&to={TO}',
    help: 'Paste this URL into the provider configuration.',
    warning: 'This endpoint must be publicly reachable.',
    copy_label: 'Copy callback',
    testable: true,
    test_label: 'Test callback',
  },
];

function makeBot(): Bot {
  return {
    id: 'bot-1',
    name: 'provider',
    category: 'Test',
    description: 'Schema renderer test',
    long_description: 'Renders the settings schema fields, nothing more.',
    code: 'from remoteterm import bot',
    enabled: false,
    admin_only: false,
    respond_to_dms: true,
    scope: { channels: 'all' },
    cooldown_seconds: 0,
    per_user_cooldown_seconds: 0,
    queue_threshold_seconds: 0,
    settings_schema: schema,
    settings: {
      endpoint: 'https://api.example.test/incoming',
      public_server: 'mesh.example.test',
      webhook_token: 'secret-token',
    },
    ui_triggers: [],
    builtin_key: null,
    builtin_version: null,
    modified: false,
    last_error: null,
    sort_order: 0,
    created_at: 0,
    updated_at: 0,
    declared_keywords: [],
    declared_crons: [],
    declared_events: [],
    declared_webhooks: ['provider'],
    is_legacy: false,
    load_error: null,
    runs_24h: 0,
  };
}

describe('BotEditor settings schema URL fields', () => {
  afterEach(() => vi.restoreAllMocks());

  it('renders editable and generated URLs, preserving provider placeholders and copying', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    render(
      <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
    );

    const endpoint = await screen.findByDisplayValue('https://api.example.test/incoming');
    expect(endpoint).toHaveAttribute('type', 'url');

    const generated = 'http://mesh.example.test:8000/api/hooks/provider?token=secret-token&to={TO}';
    expect(screen.getByText(generated)).toBeInTheDocument();
    expect(screen.getByText('This endpoint must be publicly reachable.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Copy callback' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(generated));

    fireEvent.click(screen.getByRole('button', { name: 'Test callback' }));
    expect(writeText).toHaveBeenCalledTimes(1);
  });

  it('substitutes only declared settings and uses their defaults', () => {
    expect(resolveGeneratedUrl('{endpoint}/{EXTERNAL}', schema, {})).toBe(
      'https://example.test/api/{EXTERNAL}'
    );
  });

  it('URL-encodes setting values while preserving provider placeholders', () => {
    const token = 'a+b & c#d? space';
    expect(
      resolveGeneratedUrl(schema[3].type === 'generated_url' ? schema[3].template : '', schema, {
        public_server: 'mesh.example.test',
        webhook_token: token,
      })
    ).toBe(
      'http://mesh.example.test:8000/api/hooks/provider?token=a%2Bb%20%26%20c%23d%3F%20space&to={TO}'
    );
  });

  it('tests and copies the same encoded special-character URL', async () => {
    const bot = makeBot();
    bot.settings.webhook_token = 'a+b & c#d? space';
    vi.spyOn(api, 'getBot').mockResolvedValue(bot);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    render(
      <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Test callback' }));
    fireEvent.click(screen.getByRole('button', { name: 'Copy callback' }));

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        'http://mesh.example.test:8000/api/hooks/provider?token=a%2Bb%20%26%20c%23d%3F%20space&to={TO}'
      )
    );
  });

  it('copies through execCommand when the Clipboard API is unavailable', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand });

    render(
      <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Copy callback' }));

    await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'));
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('validates generated URLs locally without resolving external placeholders', () => {
    expect(
      validateGeneratedUrl(schema[3].type === 'generated_url' ? schema[3].template : '', schema, {
        public_server: 'mesh.example.test',
        webhook_token: 'secret-token',
      })
    ).toBeNull();
    expect(
      validateGeneratedUrl(schema[3].type === 'generated_url' ? schema[3].template : '', schema, {
        public_server: '',
        webhook_token: 'secret-token',
      })
    ).toBe('Public server is required');
  });

  it('contains no bot-specific SMS integration logic', () => {
    expect(botEditorSource).not.toContain('isSmsBot');
    expect(botEditorSource).not.toContain("builtin_key == 'sms'");
    expect(botEditorSource).not.toContain("declared_webhooks.includes('sms')");
    expect(botEditorSource).not.toContain('VoIP.ms');
  });

  it('conditionally shows provider-specific schema fields', async () => {
    const providerBot = makeBot();
    providerBot.settings_schema = [
      {
        key: 'provider',
        label: 'SMS provider',
        type: 'select',
        default: 'voipms',
        options: [
          { value: 'voipms', label: 'VoIP.ms' },
          { value: 'twilio', label: 'Twilio' },
        ],
      },
      {
        key: 'voip_username',
        label: 'VoIP username',
        type: 'text',
        show_when: { key: 'provider', value: 'voipms' },
      },
      {
        key: 'twilio_sid',
        label: 'Twilio SID',
        type: 'text',
        show_when: { key: 'provider', value: 'twilio' },
      },
    ];
    providerBot.settings = { provider: 'voipms' };
    vi.spyOn(api, 'getBot').mockResolvedValue(providerBot);

    render(
      <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
    );

    const provider = await screen.findByLabelText('SMS provider');
    expect(screen.getByText('VoIP username')).toBeInTheDocument();
    expect(screen.queryByText('Twilio SID')).not.toBeInTheDocument();

    fireEvent.change(provider, { target: { value: 'twilio' } });
    expect(screen.queryByText('VoIP username')).not.toBeInTheDocument();
    expect(screen.getByText('Twilio SID')).toBeInTheDocument();
  });
});

describe('BotEditor extra keywords', () => {
  afterEach(() => vi.restoreAllMocks());

  async function openTriggers(declared: string[]) {
    const bot = makeBot();
    bot.declared_keywords = declared;
    vi.spyOn(api, 'getBot').mockResolvedValue(bot);
    render(
      <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Triggers' }));
    return screen.getByPlaceholderText('add keyword…');
  }

  it('adds a keyword the code does not declare', async () => {
    const input = await openTriggers(['wx']);
    fireEvent.change(input, { target: { value: 'Forecast' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    // Lowercased, and offered with a remove button like any other chip.
    expect(screen.getByRole('button', { name: 'Remove keyword forecast' })).toBeInTheDocument();
  });

  it('refuses a keyword the code already declares', async () => {
    // The engine drops such a word, so saving the chip would promise an alias
    // that never answers — or worse, answers for the wrong command.
    const input = await openTriggers(['wx', 'wxalert']);
    fireEvent.change(input, { target: { value: 'WXALERT' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(
      screen.queryByRole('button', { name: 'Remove keyword wxalert' })
    ).not.toBeInTheDocument();
  });
});

describe('BotEditor about block', () => {
  afterEach(() => vi.restoreAllMocks());

  it('shows the short description and the long one under it on the Settings tab', async () => {
    vi.spyOn(api, 'getBot').mockResolvedValue(makeBot());
    render(
      <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
    );

    // Settings is the tab the editor opens on: what the bot does comes before
    // how it is configured.
    expect(await screen.findByText('Schema renderer test')).toBeInTheDocument();
    expect(
      screen.getByText('Renders the settings schema fields, nothing more.')
    ).toBeInTheDocument();
  });

  it('skips the block for a bot that describes itself nowhere', async () => {
    const bot = makeBot();
    bot.description = '';
    bot.long_description = '';
    vi.spyOn(api, 'getBot').mockResolvedValue(bot);
    render(
      <BotEditor botId="bot-1" channels={[]} contacts={[]} onBack={vi.fn()} onDeleted={vi.fn()} />
    );

    await screen.findByText('Where it runs');
    expect(screen.queryByText('What this bot does')).not.toBeInTheDocument();
  });
});
