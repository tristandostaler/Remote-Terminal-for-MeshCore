import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsVirtualNodeSection } from '../components/settings/SettingsVirtualNodeSection';
import { api } from '../api';
import type { AppSettings, VirtualNodeOverview } from '../types';

vi.mock('../api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api')>();
  return {
    ...original,
    api: {
      ...original.api,
      getVirtualNode: vi.fn(),
      forgetVirtualNodeClient: vi.fn(),
      disconnectVirtualNodeClient: vi.fn(),
    },
  };
});

const mockApi = api as unknown as {
  getVirtualNode: ReturnType<typeof vi.fn>;
  forgetVirtualNodeClient: ReturnType<typeof vi.fn>;
  disconnectVirtualNodeClient: ReturnType<typeof vi.fn>;
};

const appSettings = {
  virtual_node_allow_admin_commands: false,
} as unknown as AppSettings;

function overview(partial: Partial<VirtualNodeOverview> = {}): VirtualNodeOverview {
  return {
    enabled: true,
    listening: true,
    host: '0.0.0.0',
    port: 5000,
    read_only: false,
    replay_limit: 1000,
    admin_commands_allowed: false,
    client_count: 1,
    local_commands: 12,
    cached_commands: 3,
    forwarded_commands: 2,
    connected: [
      {
        peer: '192.168.1.20:51234',
        client_id: 'MeshCore@192.168.1.20',
        app_name: 'MeshCore',
        connected_at: 1_700_000_000,
        commands: 12,
        queued_messages: 0,
        replayed_messages: 4,
      },
    ],
    known_clients: [
      {
        client_id: 'MeshCore@192.168.1.20',
        app_name: 'MeshCore',
        peer_host: '192.168.1.20',
        last_message_id: 4242,
        first_seen: 1_699_000_000,
        last_seen: 1_700_000_000,
        connections: 3,
        connected: true,
      },
      {
        client_id: 'mccli@192.168.1.7',
        app_name: 'mccli',
        peer_host: '192.168.1.7',
        last_message_id: 4100,
        first_seen: 1_699_000_000,
        last_seen: 1_699_500_000,
        connections: 1,
        connected: false,
      },
    ],
    ...partial,
  };
}

describe('SettingsVirtualNodeSection', () => {
  beforeEach(() => {
    mockApi.getVirtualNode.mockReset();
    mockApi.forgetVirtualNodeClient.mockReset();
    mockApi.disconnectVirtualNodeClient.mockReset();
  });

  it('lists connected and remembered apps with the listener address', async () => {
    mockApi.getVirtualNode.mockResolvedValue(overview());
    render(<SettingsVirtualNodeSection appSettings={appSettings} onSaveAppSettings={vi.fn()} />);

    await waitFor(() => expect(screen.getByTestId('virtual-node-connected')).toBeInTheDocument());
    expect(screen.getByTestId('virtual-node-listener')).toHaveTextContent('0.0.0.0:5000');
    expect(screen.getByTestId('virtual-node-connected')).toHaveTextContent('192.168.1.20:51234');
    const known = screen.getByTestId('virtual-node-known');
    expect(known).toHaveTextContent('mccli');
    expect(known).toHaveTextContent('message #4100');
    expect(known).toHaveTextContent('online');
  });

  it('saves the admin-commands switch through app settings', async () => {
    mockApi.getVirtualNode.mockResolvedValue(overview());
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<SettingsVirtualNodeSection appSettings={appSettings} onSaveAppSettings={onSave} />);

    const checkbox = await screen.findByRole('checkbox', {
      name: /Allow connected apps to change radio settings/i,
    });
    expect(checkbox).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(checkbox);
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({ virtual_node_allow_admin_commands: true })
    );
  });

  it('disables the admin switch when the node is read-only', async () => {
    mockApi.getVirtualNode.mockResolvedValue(overview({ read_only: true }));
    render(<SettingsVirtualNodeSection appSettings={appSettings} onSaveAppSettings={vi.fn()} />);
    const checkbox = await screen.findByRole('checkbox', {
      name: /Allow connected apps to change radio settings/i,
    });
    await waitFor(() => expect(checkbox).toBeDisabled());
    expect(screen.getByText(/refuses these commands regardless/i)).toBeInTheDocument();
  });

  it('forgets and disconnects apps through the api', async () => {
    mockApi.getVirtualNode.mockResolvedValue(overview());
    mockApi.forgetVirtualNodeClient.mockResolvedValue({ status: 'ok', client_id: 'x' });
    mockApi.disconnectVirtualNodeClient.mockResolvedValue({ status: 'ok', peer: 'x' });
    render(<SettingsVirtualNodeSection appSettings={appSettings} onSaveAppSettings={vi.fn()} />);

    const forgetButtons = await screen.findAllByRole('button', { name: 'Forget' });
    fireEvent.click(forgetButtons[1]);
    await waitFor(() =>
      expect(mockApi.forgetVirtualNodeClient).toHaveBeenCalledWith('mccli@192.168.1.7')
    );

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    await waitFor(() =>
      expect(mockApi.disconnectVirtualNodeClient).toHaveBeenCalledWith('192.168.1.20:51234')
    );
  });

  it('explains how to enable the node when it is off', async () => {
    mockApi.getVirtualNode.mockResolvedValue(
      overview({
        enabled: false,
        listening: false,
        client_count: 0,
        connected: [],
        known_clients: [],
      })
    );
    render(<SettingsVirtualNodeSection appSettings={appSettings} onSaveAppSettings={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId('virtual-node-listener')).toHaveTextContent(
        'MESHCORE_VIRTUAL_NODE_ENABLED=true'
      )
    );
    expect(screen.getByText('No app is connected right now.')).toBeInTheDocument();
  });
});
