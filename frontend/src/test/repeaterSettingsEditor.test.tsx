import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import { SettingsEditorPane } from '../components/repeater/RepeaterSettingsEditorPane';
import type {
  RepeaterSettingDefinition,
  RepeaterSettingsSchemaResponse,
  RepeaterSettingValue,
} from '../types';

function definition(overrides: Partial<RepeaterSettingDefinition>): RepeaterSettingDefinition {
  return {
    key: 'flood_max',
    label: 'Max Flood Hops',
    group: 'radio',
    cli_key: 'flood.max',
    value_type: 'int',
    help: 'Flood packets with more hops than this are not repeated.',
    unit: 'hops',
    minimum: 0,
    maximum: 64,
    step: null,
    options: [],
    max_length: null,
    readable: true,
    writable: true,
    sensitive: false,
    note: null,
    ...overrides,
  };
}

const schema: RepeaterSettingsSchemaResponse = {
  groups: [
    { key: 'radio', label: 'Radio', description: 'LoRa parameters' },
    { key: 'access', label: 'Access', description: 'Passwords' },
  ],
  settings: [
    definition({}),
    definition({
      key: 'repeat',
      label: 'Repeat Mode',
      cli_key: 'repeat',
      value_type: 'bool',
      unit: null,
      minimum: null,
      maximum: null,
    }),
    definition({
      key: 'duty_cycle',
      label: 'Duty Cycle Limit',
      cli_key: 'dutycycle',
      value_type: 'float',
      unit: '%',
    }),
    definition({
      key: 'guest_password',
      label: 'Guest Password',
      group: 'access',
      cli_key: 'guest.password',
      value_type: 'string',
      unit: null,
      minimum: null,
      maximum: null,
      max_length: 15,
      sensitive: true,
    }),
  ],
};

function renderPane(
  overrides: Partial<Parameters<typeof SettingsEditorPane>[0]> = {},
  values: Record<string, RepeaterSettingValue> = {}
) {
  const onFetch = vi.fn(async () => {});
  const onApply = vi.fn(async () => []);
  render(
    <SettingsEditorPane
      schema={schema}
      values={values}
      loading={false}
      error={null}
      cliResponsive={true}
      onFetch={onFetch}
      onApply={onApply}
      {...overrides}
    />
  );
  return { onFetch, onApply };
}

describe('SettingsEditorPane', () => {
  it('reads one group at a time, and everything only on request', () => {
    const { onFetch } = renderPane();

    fireEvent.click(screen.getAllByRole('button', { name: 'Read' })[0]);
    expect(onFetch).toHaveBeenCalledWith({ group: 'radio' });

    fireEvent.click(screen.getByRole('button', { name: /Read All/ }));
    expect(onFetch).toHaveBeenLastCalledWith();
  });

  it('edits a field and applies it as one change, behind a confirmation', async () => {
    const { onApply } = renderPane(
      {},
      {
        flood_max: { key: 'flood_max', value: '3', raw: '3', status: 'ok' },
      }
    );

    // The group opens itself because it has values to show.
    const input = await screen.findByLabelText(/Max Flood Hops/);
    expect(input).toHaveValue(3);

    fireEvent.change(input, { target: { value: '5' } });

    const apply = screen.getByRole('button', { name: /Apply 1 change/ });
    // First click only arms: a write to a repeater is not one misclick away.
    fireEvent.click(apply);
    expect(onApply).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /Confirm 1 change/ }));
    await waitFor(() => expect(onApply).toHaveBeenCalledWith([{ key: 'flood_max', value: '5' }]));
  });

  it('locks a setting the firmware does not know', async () => {
    renderPane(
      {},
      {
        duty_cycle: { key: 'duty_cycle', value: null, raw: '??: dutycycle', status: 'unsupported' },
      }
    );

    expect(await screen.findByLabelText(/Duty Cycle Limit/)).toBeDisabled();
    expect(screen.getByText('Not supported by this firmware')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Apply/ })).toBeDisabled();
  });

  it('says so when nothing answered, which is what a guest session looks like', () => {
    renderPane({ cliResponsive: false });
    expect(screen.getByText(/log in with the admin password/i)).toBeInTheDocument();
  });

  it('masks a password field, and shows nothing when there is nothing to read', async () => {
    renderPane(
      {},
      {
        guest_password: { key: 'guest_password', value: null, raw: null, status: 'ok' },
      }
    );

    const input = await screen.findByLabelText(/Guest Password/);
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveValue('');
  });

  it('reverts pending edits without touching the repeater', async () => {
    const { onApply } = renderPane(
      {},
      {
        flood_max: { key: 'flood_max', value: '3', raw: '3', status: 'ok' },
      }
    );

    fireEvent.change(await screen.findByLabelText(/Max Flood Hops/), { target: { value: '7' } });
    expect(screen.getByRole('button', { name: /Apply 1 change/ })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Revert' }));

    expect(await screen.findByLabelText(/Max Flood Hops/)).toHaveValue(3);
    expect(onApply).not.toHaveBeenCalled();
  });
});
