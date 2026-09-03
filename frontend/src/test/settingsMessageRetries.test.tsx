import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { SettingsRadioSection } from '../components/settings/SettingsRadioSection';
import {
  MAX_MESSAGE_RETRIES,
  MIN_MESSAGE_RETRIES,
  type AppSettings,
  type RadioConfig,
} from '../types';

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
  telemetry_routed_hourly: false,
  virtual_node_allow_admin_commands: false,
};

function renderSection(overrides: Partial<AppSettings> = {}) {
  const onSaveAppSettings = vi.fn().mockResolvedValue(undefined);
  render(
    <SettingsRadioSection
      config={baseConfig}
      health={null}
      appSettings={{ ...baseSettings, ...overrides }}
      pageMode={false}
      onSave={vi.fn().mockResolvedValue(undefined)}
      onSaveAppSettings={onSaveAppSettings}
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
      onClose={vi.fn()}
    />
  );
  return { onSaveAppSettings };
}

function retriesField(): HTMLInputElement {
  return screen.getByLabelText('Direct Message Send Attempts') as HTMLInputElement;
}

describe('direct message attempt cap setting', () => {
  it('shows the stored value and the legal range', () => {
    renderSection({ max_message_retries: 5 });

    const field = retriesField();
    expect(field.value).toBe('5');
    expect(field.min).toBe(String(MIN_MESSAGE_RETRIES));
    expect(field.max).toBe(String(MAX_MESSAGE_RETRIES));
  });

  it('saves a new value on blur', async () => {
    const { onSaveAppSettings } = renderSection();

    await userEvent.clear(retriesField());
    await userEvent.type(retriesField(), '7');
    await userEvent.tab();

    expect(onSaveAppSettings).toHaveBeenCalledWith({ max_message_retries: 7 });
  });

  it('clamps an over-range entry rather than sending it', async () => {
    const { onSaveAppSettings } = renderSection();

    await userEvent.clear(retriesField());
    await userEvent.type(retriesField(), '99');
    await userEvent.tab();

    expect(onSaveAppSettings).toHaveBeenCalledWith({ max_message_retries: MAX_MESSAGE_RETRIES });
    expect(retriesField().value).toBe(String(MAX_MESSAGE_RETRIES));
  });

  it('snaps an emptied field back to what is stored, saving nothing', async () => {
    const { onSaveAppSettings } = renderSection({ max_message_retries: 4 });

    await userEvent.clear(retriesField());
    await userEvent.tab();

    expect(onSaveAppSettings).not.toHaveBeenCalled();
    expect(retriesField().value).toBe('4');
  });

  it('saves nothing when the value has not changed', async () => {
    const { onSaveAppSettings } = renderSection({ max_message_retries: 3 });

    await userEvent.click(retriesField());
    await userEvent.tab();

    expect(onSaveAppSettings).not.toHaveBeenCalled();
  });

  it('explains that channel messages are unaffected', () => {
    renderSection();

    expect(screen.getByText(/Channel messages are unaffected/)).toBeInTheDocument();
  });
});
