import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SettingsAboutSection } from '../components/settings/SettingsAboutSection';

describe('SettingsAboutSection', () => {
  it('renders the debug support snapshot link', () => {
    render(
      <SettingsAboutSection
        health={{
          status: 'ok',
          radio_connected: true,
          radio_initializing: false,
          connection_info: 'Serial: /dev/ttyUSB0',
          app_info: {
            version: '3.2.0-test',
            commit_hash: 'deadbeef',
          },
          database_size_mb: 1.2,
          oldest_undecrypted_timestamp: null,
          fanout_statuses: {},
          bots_disabled: false,
        }}
      />
    );

    const link = screen.getByRole('link', { name: /Open debug support snapshot/i });
    expect(link).toHaveAttribute('href', './api/debug');
    expect(link).toHaveAttribute('target', '_blank');
  });

  it('explains how to enable the virtual companion node when it is off', () => {
    render(
      <SettingsAboutSection
        health={{
          status: 'ok',
          radio_connected: true,
          radio_initializing: false,
          connection_info: null,
          database_size_mb: 0,
          oldest_undecrypted_timestamp: null,
          fanout_statuses: {},
          bots_disabled: false,
          virtual_node: {
            enabled: false,
            listening: false,
            host: '0.0.0.0',
            port: 5000,
            read_only: false,
            client_count: 0,
            local_commands: 0,
            cached_commands: 0,
            forwarded_commands: 0,
          },
        }}
      />
    );

    const block = screen.getByTestId('virtual-node-status');
    expect(block).toHaveTextContent('Disabled');
    expect(block).toHaveTextContent('MESHCORE_VIRTUAL_NODE_ENABLED=true');
  });

  it('shows the listener, connected apps and where commands went when enabled', () => {
    render(
      <SettingsAboutSection
        health={{
          status: 'ok',
          radio_connected: true,
          radio_initializing: false,
          connection_info: null,
          database_size_mb: 0,
          oldest_undecrypted_timestamp: null,
          fanout_statuses: {},
          bots_disabled: false,
          virtual_node: {
            enabled: true,
            listening: true,
            host: '0.0.0.0',
            port: 5000,
            read_only: true,
            client_count: 2,
            local_commands: 40,
            cached_commands: 2,
            forwarded_commands: 3,
          },
        }}
      />
    );

    const block = screen.getByTestId('virtual-node-status');
    expect(block).toHaveTextContent('Listening on');
    expect(block).toHaveTextContent('0.0.0.0:5000');
    expect(block).toHaveTextContent('(read-only)');
    expect(block).toHaveTextContent('2 connected apps');
    expect(block).toHaveTextContent('42 answered locally');
    expect(block).toHaveTextContent('3 forwarded to the radio');
  });
});
