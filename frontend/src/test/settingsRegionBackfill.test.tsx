import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api';

import { SettingsRadioSection } from '../components/settings/SettingsRadioSection';
import type { AppSettings, RadioConfig } from '../types';

const baseConfig: RadioConfig = {
  public_key: 'aa'.repeat(32),
  name: 'TestNode',
  lat: 1,
  lon: 2,
  tx_power: 17,
  max_tx_power: 22,
  radio: { freq: 910.525, bw: 62.5, sf: 7, cr: 5 },
  path_hash_mode: 0,
  path_hash_mode_supported: false,
  advert_location_source: 'current',
  multi_acks_enabled: false,
};

const baseSettings: AppSettings = {
  max_radio_contacts: 200,
  auto_decrypt_dm_on_advert: false,
  last_message_times: {},
  advert_interval: 0,
  last_advert_time: 0,
  flood_scope: '',
  known_regions: [],
  blocked_keys: [],
  blocked_names: [],
  discovery_blocked_types: [],
  tracked_telemetry_repeaters: [],
  tracked_telemetry_contacts: [],
  clock_sync_repeaters: [],
  clock_autofix_repeaters: [],
  auto_resend_channel: false,
  max_message_retries: 3,
  telemetry_interval_hours: 8,
  auto_discover_regions_hours: 0,
  owner_info_refresh_days: 7,
  telemetry_routed_hourly: false,
  virtual_node_allow_admin_commands: false,
  live_feed_enabled: false,
  live_feed_url: 'https://live.meshcore.ca',
  live_feed_region: '',
  live_feed_channels: ['*'],
  live_feed_poll_interval: 300,
};

function renderSection() {
  const onMessagesRetagged = vi.fn();
  render(
    <SettingsRadioSection
      config={baseConfig}
      health={null}
      appSettings={baseSettings}
      pageMode={false}
      onSave={vi.fn().mockResolvedValue(undefined)}
      onSaveAppSettings={vi.fn().mockResolvedValue(undefined)}
      onSetPrivateKey={vi.fn().mockResolvedValue(undefined)}
      onReboot={vi.fn().mockResolvedValue(undefined)}
      onDisconnect={vi.fn().mockResolvedValue(undefined)}
      onReconnect={vi.fn().mockResolvedValue(undefined)}
      onAdvertise={vi.fn().mockResolvedValue(undefined)}
      meshDiscovery={null}
      meshDiscoveryLoadingTarget={null}
      onDiscoverMesh={vi.fn().mockResolvedValue(undefined)}
      regionDiscovery={null}
      regionDiscoveryLoading={false}
      onDiscoverRegions={vi.fn().mockResolvedValue(undefined)}
      onMessagesRetagged={onMessagesRetagged}
      onClose={vi.fn()}
    />
  );
  return { onMessagesRetagged };
}

const retagButton = () => screen.getByRole('button', { name: 'Re-tag Messages' });

describe('region re-tag button', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('runs the backfill and asks loaded conversations to refetch', async () => {
    const backfill = vi
      .spyOn(api, 'backfillRegions')
      .mockResolvedValue({ scanned: 10, scoped: 3, named: 2 });
    const { onMessagesRetagged } = renderSection();

    await userEvent.click(retagButton());

    expect(backfill).toHaveBeenCalledTimes(1);
    expect(onMessagesRetagged).toHaveBeenCalledTimes(1);
    expect(retagButton()).toBeEnabled();
  });

  it('does not refetch conversations when the backfill fails', async () => {
    vi.spyOn(api, 'backfillRegions').mockRejectedValue(new Error('boom'));
    const { onMessagesRetagged } = renderSection();

    await userEvent.click(retagButton());

    expect(onMessagesRetagged).not.toHaveBeenCalled();
    expect(retagButton()).toBeEnabled();
  });
});
