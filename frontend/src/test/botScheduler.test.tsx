import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SchedulerTab } from '../components/bots/SchedulerTab';
import { api } from '../api';
import type { BotSchedule, Channel } from '../types';

const channels = [
  { key: 'aa'.repeat(16), name: '#general' },
  { key: 'bb'.repeat(16), name: '#ops' },
] as Channel[];

function makeSchedule(overrides: Partial<BotSchedule> = {}): BotSchedule {
  return {
    id: 'sched-1',
    label: 'Morning digest',
    cron: '0 7 * * *',
    channel_key: channels[0].key,
    flood_scope: 'west',
    message: 'Good morning',
    enabled: true,
    last_run_at: null,
    last_result: null,
    created_at: 0,
    next_run_at: null,
    channel_name: '#general',
    ...overrides,
  };
}

function mockApi(schedule: BotSchedule) {
  vi.spyOn(api, 'getBotSchedules').mockResolvedValue([schedule]);
  vi.spyOn(api, 'validateCron').mockResolvedValue({ valid: true, error: null, next_runs: [] });
}

describe('SchedulerTab', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('edits an existing schedule and sends the changed fields', async () => {
    const schedule = makeSchedule();
    mockApi(schedule);
    const update = vi
      .spyOn(api, 'updateBotSchedule')
      .mockResolvedValue({ ...schedule, cron: '30 8 * * *', flood_scope: null });

    render(<SchedulerTab channels={channels} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Morning digest' }));

    // Form is prefilled from the schedule.
    expect(screen.getByDisplayValue('Morning digest')).toBeInTheDocument();
    expect(screen.getByDisplayValue('west')).toBeInTheDocument();

    fireEvent.change(screen.getByDisplayValue('0 7 * * *'), { target: { value: '30 8 * * *' } });
    fireEvent.change(screen.getByDisplayValue('west'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('sched-1', {
        label: 'Morning digest',
        cron: '30 8 * * *',
        channel_key: channels[0].key,
        message: 'Good morning',
        flood_scope: null,
      })
    );
    await waitFor(() => expect(screen.getByText('30 8 * * *')).toBeInTheDocument());
  });

  it('deletes a schedule after a confirming second click', async () => {
    const schedule = makeSchedule();
    mockApi(schedule);
    const del = vi.spyOn(api, 'deleteBotSchedule').mockResolvedValue({ status: 'ok' });

    render(<SchedulerTab channels={channels} />);
    const button = await screen.findByRole('button', { name: 'Delete' });
    fireEvent.click(button);
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    await waitFor(() => expect(del).toHaveBeenCalledWith('sched-1'));
    await waitFor(() => expect(screen.queryByText('Morning digest')).not.toBeInTheDocument());
  });
});
