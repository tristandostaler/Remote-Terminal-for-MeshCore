import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api';
import { EngineTab } from '../components/bots/EngineTab';
import type { BotEngineStatus } from '../types';

const engineStatus: BotEngineStatus = {
  settings: {
    command_prefix: '!',
    require_prefix: false,
    mention_mode: 'also',
    global_reply_seconds: 0,
    per_user_seconds: 0,
    tx_spacing_seconds: 0,
    max_response_hops: 3,
    default_language: 'en',
    auto_detect_language: false,
    banned_users: [],
    profanity_mode: 'off',
    admin_users: [],
    author_contact: 'old@example.com',
  },
  disabled_until_restart: false,
  disabled_by_env: false,
  total_bots: 1,
  enabled_bots: 1,
  erroring_bots: 0,
  runs_24h: 0,
};

describe('EngineTab author contact', () => {
  afterEach(() => vi.restoreAllMocks());

  it('shows the stored contact and saves an edit', async () => {
    vi.spyOn(api, 'getBotEngine').mockResolvedValue(engineStatus);
    const update = vi.spyOn(api, 'updateBotEngine').mockResolvedValue({
      ...engineStatus,
      settings: { ...engineStatus.settings, author_contact: 'VE2XYZ' },
    });

    render(<EngineTab contacts={[]} onChanged={vi.fn()} />);

    const field = await screen.findByLabelText('Author contact');
    expect(field).toHaveValue('old@example.com');
    fireEvent.change(field, { target: { value: 'VE2XYZ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(expect.objectContaining({ author_contact: 'VE2XYZ' }))
    );
  });
});
