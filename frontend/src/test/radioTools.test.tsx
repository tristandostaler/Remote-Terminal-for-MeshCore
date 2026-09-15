import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { RadioChannelSlotsPanel } from '../components/settings/RadioChannelSlotsPanel';
import { RadioCliConsole } from '../components/settings/RadioCliConsole';
import { api } from '../api';
import type { RadioChannelSlotsResponse } from '../types';

vi.mock('../api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api')>();
  return {
    ...original,
    api: {
      ...original.api,
      getRadioChannelSlots: vi.fn(),
      runRadioCli: vi.fn(),
    },
  };
});

const mockApi = api as unknown as {
  getRadioChannelSlots: ReturnType<typeof vi.fn>;
  runRadioCli: ReturnType<typeof vi.fn>;
};

const slots: RadioChannelSlotsResponse = {
  max_channels: 4,
  resident_enabled: true,
  slots: [
    {
      slot: 0,
      empty: false,
      name: 'Public',
      key: '8B3387E9C5CDEA6AC9E5EDBAA115CD72',
      known_name: 'Public',
      resident: true,
      send_cache: false,
    },
    {
      slot: 1,
      empty: false,
      name: '#stray',
      key: 'ABABABABABABABABABABABABABABABAB',
      known_name: null,
      resident: false,
      send_cache: false,
    },
    {
      slot: 2,
      empty: true,
      name: null,
      key: null,
      known_name: null,
      resident: false,
      send_cache: false,
    },
    {
      slot: 3,
      empty: true,
      name: null,
      key: null,
      known_name: null,
      resident: false,
      send_cache: false,
    },
  ],
};

beforeEach(() => {
  mockApi.getRadioChannelSlots.mockReset();
  mockApi.runRadioCli.mockReset();
});

describe('RadioChannelSlotsPanel', () => {
  it('reads the slots on refresh and labels residency', async () => {
    mockApi.getRadioChannelSlots.mockResolvedValue(slots);
    render(<RadioChannelSlotsPanel connected defaultOpen />);

    fireEvent.click(screen.getByRole('button', { name: /refresh channel slots/i }));

    await waitFor(() => expect(screen.getByText('Public')).toBeInTheDocument());
    expect(screen.getByText('Resident')).toBeInTheDocument();
    expect(screen.getByText('#stray')).toBeInTheDocument();
    expect(screen.getByText('Not joined')).toBeInTheDocument();
    expect(screen.getByText('2 of 4 slots in use')).toBeInTheDocument();
    // Empty slots are folded away until asked for.
    expect(screen.queryByText('Empty')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/show 2 empty slots/i));
    expect(screen.getAllByText('Empty')).toHaveLength(2);
  });

  it('shows the server error and keeps the button usable', async () => {
    mockApi.getRadioChannelSlots.mockRejectedValue(new Error('Radio is busy'));
    render(<RadioChannelSlotsPanel connected defaultOpen />);

    fireEvent.click(screen.getByRole('button', { name: /refresh channel slots/i }));

    await waitFor(() => expect(screen.getByText('Radio is busy')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /refresh channel slots/i })).not.toBeDisabled();
  });

  it('disables refresh while disconnected', () => {
    render(<RadioChannelSlotsPanel connected={false} defaultOpen />);
    expect(screen.getByRole('button', { name: /refresh channel slots/i })).toBeDisabled();
    expect(screen.getByText('Radio is not connected.')).toBeInTheDocument();
  });
});

describe('RadioCliConsole', () => {
  it('sends the command and prints the reply', async () => {
    mockApi.runRadioCli.mockResolvedValue({ command: 'ver', reply: 'v1.9.0', elapsed_ms: 42 });
    render(<RadioCliConsole connected defaultOpen />);

    const input = screen.getByLabelText('Radio CLI command');
    fireEvent.change(input, { target: { value: 'ver' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() => expect(screen.getByText('v1.9.0')).toBeInTheDocument());
    expect(mockApi.runRadioCli).toHaveBeenCalledWith('ver');
    expect(screen.getByText(/> ver/)).toBeInTheDocument();
    expect(screen.getByText('42 ms')).toBeInTheDocument();
    expect((input as HTMLInputElement).value).toBe('');
  });

  it('shows the server message when the firmware cannot run CLI commands', async () => {
    mockApi.runRadioCli.mockRejectedValue(new Error('needs 14 or newer'));
    render(<RadioCliConsole connected defaultOpen />);

    const input = screen.getByLabelText('Radio CLI command');
    fireEvent.change(input, { target: { value: 'ver' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() => expect(screen.getByText('needs 14 or newer')).toBeInTheDocument());
  });

  it('recalls the previous command with the up arrow', async () => {
    mockApi.runRadioCli.mockResolvedValue({ command: 'ver', reply: 'ok', elapsed_ms: 1 });
    render(<RadioCliConsole connected defaultOpen />);

    const input = screen.getByLabelText('Radio CLI command') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'ver' } });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(screen.getByText('ok')).toBeInTheDocument());

    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(input.value).toBe('ver');
  });

  it('is disabled while disconnected', () => {
    render(<RadioCliConsole connected={false} defaultOpen />);
    expect(screen.getByLabelText('Radio CLI command')).toBeDisabled();
  });
});
