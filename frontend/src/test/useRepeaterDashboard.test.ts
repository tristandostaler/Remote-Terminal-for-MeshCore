import { StrictMode, createElement, type ReactNode } from 'react';
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import {
  resetRepeaterDashboardCacheForTests,
  useRepeaterDashboard,
} from '../hooks/useRepeaterDashboard';
import type { Conversation } from '../types';

// Mock the api module
vi.mock('../api', () => ({
  api: {
    repeaterLogin: vi.fn(),
    repeaterStatus: vi.fn(),
    repeaterNodeInfo: vi.fn(),
    repeaterNeighbors: vi.fn(),
    repeaterAcl: vi.fn(),
    repeaterOwnerInfo: vi.fn(),
    repeaterLppTelemetry: vi.fn(),
    repeaterRegions: vi.fn(),
    sendRepeaterCommand: vi.fn(),
    repeaterSyncClock: vi.fn(),
    repeaterFixClock: vi.fn(),
    repeaterSettingsSchema: vi.fn(async () => ({
      groups: [{ key: 'radio', label: 'Radio', description: 'LoRa parameters' }],
      settings: [
        {
          key: 'flood_max',
          label: 'Max Flood Hops',
          group: 'radio',
          cli_key: 'flood.max',
          value_type: 'int',
          help: '',
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
        },
        {
          key: 'guest_password',
          label: 'Guest Password',
          group: 'access',
          cli_key: 'guest.password',
          value_type: 'string',
          help: '',
          unit: null,
          minimum: null,
          maximum: null,
          step: null,
          options: [],
          max_length: 15,
          readable: true,
          writable: true,
          sensitive: true,
          note: null,
        },
      ],
    })),
    repeaterSettings: vi.fn(),
    repeaterApplySettings: vi.fn(),
    getHostClock: vi.fn(async () => ({
      checked_at: 0,
      trusted: true,
      verified: true,
      offset_seconds: 0.1,
      source: 'ntp',
      reference: 'pool.ntp.org',
      step_seconds: 0,
      threshold_seconds: 60,
      message: 'Server clock verified via NTP: 0.1s ahead of the reference.',
    })),
  },
}));

// Mock sonner toast
vi.mock('../components/ui/sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  },
}));

// Get mock reference — cast to Record<string, Mock> for type-safe mock method access
const { api: _rawApi } = await import('../api');
const mockApi = _rawApi as unknown as Record<string, Mock>;
const { toast } = await import('../components/ui/sonner');
const mockToast = toast as unknown as Record<string, Mock>;

const REPEATER_KEY = 'aa'.repeat(32);

const repeaterConversation: Conversation = {
  type: 'contact',
  id: REPEATER_KEY,
  name: 'TestRepeater',
};

describe('useRepeaterDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRepeaterDashboardCacheForTests();
  });

  it('starts with logged out state', () => {
    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));
    expect(result.current.loggedIn).toBe(false);
    expect(result.current.loginLoading).toBe(false);
    expect(result.current.loginError).toBe(null);
  });

  it('login sets loggedIn on success', async () => {
    mockApi.repeaterLogin.mockResolvedValueOnce({
      status: 'ok',
      authenticated: true,
      message: null,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.login('secret');
    });

    expect(result.current.loggedIn).toBe(true);
    expect(result.current.loginError).toBe(null);
    expect(result.current.lastLoginAttempt?.heardBack).toBe(true);
    expect(result.current.lastLoginAttempt?.outcome).toBe('confirmed');
    expect(mockApi.repeaterLogin).toHaveBeenCalledWith(REPEATER_KEY, 'secret');
  });

  it('login sets error on failure', async () => {
    mockApi.repeaterLogin.mockResolvedValueOnce({
      status: 'error',
      authenticated: false,
      message: 'Auth failed',
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.login('bad');
    });

    expect(result.current.loggedIn).toBe(true);
    expect(result.current.loginError).toBe('Auth failed');
    expect(result.current.lastLoginAttempt?.heardBack).toBe(true);
    expect(result.current.lastLoginAttempt?.outcome).toBe('not_confirmed');
    expect(mockToast.error).toHaveBeenCalledWith('Login not confirmed', {
      description: 'Auth failed',
    });
  });

  it('loginAsGuest calls login with empty password', async () => {
    mockApi.repeaterLogin.mockResolvedValueOnce({
      status: 'ok',
      authenticated: true,
      message: null,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.loginAsGuest();
    });

    expect(mockApi.repeaterLogin).toHaveBeenCalledWith(REPEATER_KEY, '');
    expect(result.current.loggedIn).toBe(true);
  });

  it('login still opens dashboard when request rejects', async () => {
    mockApi.repeaterLogin.mockRejectedValueOnce(new Error('Network error'));

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.login('secret');
    });

    expect(result.current.loggedIn).toBe(true);
    expect(result.current.loginError).toBe('Network error');
    expect(result.current.lastLoginAttempt?.heardBack).toBe(false);
    expect(result.current.lastLoginAttempt?.outcome).toBe('request_failed');
    expect(mockToast.error).toHaveBeenCalledWith('Login request failed', {
      description:
        'Network error. The dashboard is still available, but repeater operations may fail until a login succeeds.',
    });
  });

  it('refreshPane stores data on success', async () => {
    const statusData = {
      battery_volts: 4.2,
      tx_queue_len: 0,
      noise_floor_dbm: -120,
      last_rssi_dbm: -85,
      last_snr_db: 7.5,
      packets_received: 100,
      packets_sent: 50,
      airtime_seconds: 600,
      rx_airtime_seconds: 1200,
      uptime_seconds: 86400,
      sent_flood: 10,
      sent_direct: 40,
      recv_flood: 30,
      recv_direct: 70,
      flood_dups: 1,
      direct_dups: 0,
      full_events: 0,
    };
    mockApi.repeaterStatus.mockResolvedValueOnce(statusData);

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.refreshPane('status');
    });

    expect(result.current.paneData.status).toEqual(statusData);
    expect(result.current.paneStates.status.loading).toBe(false);
    expect(result.current.paneStates.status.error).toBe(null);
    expect(result.current.paneStates.status.fetched_at).toEqual(expect.any(Number));
  });

  it('refreshPane still issues requests under StrictMode remount probing', async () => {
    const statusData = { battery_volts: 4.2 };
    mockApi.repeaterStatus.mockResolvedValueOnce(statusData);

    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(StrictMode, null, children);

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation), { wrapper });

    await act(async () => {
      await result.current.refreshPane('status');
    });

    expect(mockApi.repeaterStatus).toHaveBeenCalledTimes(1);
    expect(result.current.paneData.status).toEqual(statusData);
  });

  it('refreshPane retries up to 3 times', async () => {
    mockApi.repeaterStatus.mockRejectedValueOnce(new Error('fail1'));
    mockApi.repeaterStatus.mockRejectedValueOnce(new Error('fail2'));
    mockApi.repeaterStatus.mockRejectedValueOnce(new Error('fail3'));

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.refreshPane('status');
    });

    expect(mockApi.repeaterStatus).toHaveBeenCalledTimes(3);
    expect(result.current.paneStates.status.error).toBe('fail3');
    expect(result.current.paneData.status).toBe(null);
  });

  it('refreshPane succeeds on second attempt', async () => {
    const statusData = { battery_volts: 3.7 };
    mockApi.repeaterStatus.mockRejectedValueOnce(new Error('fail1'));
    mockApi.repeaterStatus.mockResolvedValueOnce(statusData);

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.refreshPane('status');
    });

    expect(mockApi.repeaterStatus).toHaveBeenCalledTimes(2);
    expect(result.current.paneData.status).toEqual(statusData);
    expect(result.current.paneStates.status.error).toBe(null);
  });

  it('sendConsoleCommand adds entries to console history', async () => {
    mockApi.sendRepeaterCommand.mockResolvedValueOnce({
      command: 'ver',
      response: 'v2.1.0',
      sender_timestamp: 1000,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.sendConsoleCommand('ver');
    });

    expect(result.current.consoleHistory).toHaveLength(2);
    expect(result.current.consoleHistory[0].outgoing).toBe(true);
    expect(result.current.consoleHistory[0].command).toBe('ver');
    expect(result.current.consoleHistory[1].outgoing).toBe(false);
    expect(result.current.consoleHistory[1].response).toBe('v2.1.0');
  });

  it('sendConsoleCommand adds error entry on failure', async () => {
    mockApi.sendRepeaterCommand.mockRejectedValueOnce(new Error('Network error'));

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.sendConsoleCommand('ver');
    });

    expect(result.current.consoleHistory).toHaveLength(2);
    expect(result.current.consoleHistory[0].outgoing).toBe(true);
    expect(result.current.consoleHistory[0].command).toBe('ver');
    expect(result.current.consoleHistory[1].outgoing).toBe(false);
    expect(result.current.consoleHistory[1].response).toBe('Error: Network error');
    expect(result.current.consoleLoading).toBe(false);
  });

  it('sendZeroHopAdvert sends "advert.zerohop" command', async () => {
    mockApi.sendRepeaterCommand.mockResolvedValueOnce({
      command: 'advert.zerohop',
      response: 'ok',
      sender_timestamp: 1000,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.sendZeroHopAdvert();
    });

    expect(mockApi.sendRepeaterCommand).toHaveBeenCalledWith(REPEATER_KEY, 'advert.zerohop');
  });

  it('sendFloodAdvert sends "advert" command', async () => {
    mockApi.sendRepeaterCommand.mockResolvedValueOnce({
      command: 'advert',
      response: 'ok',
      sender_timestamp: 1000,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.sendFloodAdvert();
    });

    expect(mockApi.sendRepeaterCommand).toHaveBeenCalledWith(REPEATER_KEY, 'advert');
  });

  it('rebootRepeater sends "reboot" command', async () => {
    mockApi.sendRepeaterCommand.mockResolvedValueOnce({
      command: 'reboot',
      response: 'ok',
      sender_timestamp: 1000,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.rebootRepeater();
    });

    expect(mockApi.sendRepeaterCommand).toHaveBeenCalledWith(REPEATER_KEY, 'reboot');
  });

  const hostClockFixture = {
    checked_at: 0,
    trusted: true,
    verified: true,
    offset_seconds: 0.1,
    source: 'ntp' as const,
    reference: 'pool.ntp.org',
    step_seconds: 0,
    threshold_seconds: 60,
    message: 'Server clock verified via NTP: 0.1s ahead of the reference.',
  };

  it('syncClock uses the server-side sync and records the verdict in the console', async () => {
    mockApi.repeaterSyncClock.mockResolvedValueOnce({
      status: 'set',
      command: 'time 1700000000',
      reply: 'OK - clock set: 12:00 - 3/9/2026 UTC',
      repeater_clock: null,
      offset_seconds: null,
      message: 'Clock set: OK - clock set: 12:00 - 3/9/2026 UTC',
      host_clock: hostClockFixture,
    });
    mockApi.repeaterNodeInfo.mockResolvedValue({
      name: 'R',
      lat: null,
      lon: null,
      clock_utc: '12:00 - 3/9/2026 UTC',
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.syncClock();
    });

    // The browser's clock is never pushed: the server decides what time it is.
    expect(mockApi.repeaterSyncClock).toHaveBeenCalledWith(REPEATER_KEY);
    expect(mockApi.sendRepeaterCommand).not.toHaveBeenCalled();
    const last = result.current.consoleHistory[result.current.consoleHistory.length - 1];
    expect(last.outgoing).toBe(false);
    expect(last.response).toContain('time 1700000000');
    expect(last.response).toContain('Clock set');
    expect(result.current.hostClock?.trusted).toBe(true);
  });

  it('fixForwardClock passes the last login password and records every step', async () => {
    mockApi.repeaterLogin.mockResolvedValueOnce({
      status: 'ok',
      authenticated: true,
      message: null,
    });
    mockApi.repeaterFixClock.mockResolvedValueOnce({
      status: 'fixed',
      message: 'Rebooted and re-synced.',
      steps: ['clock reads 12:00 - 5/9/2026 UTC (+172800s vs this server)', 'sent clkreboot'],
      before_clock: '12:00 - 5/9/2026 UTC',
      before_offset_seconds: 172800,
      after_clock: '12:00 - 3/9/2026 UTC',
      after_offset_seconds: 0,
      host_clock: hostClockFixture,
    });
    mockApi.repeaterNodeInfo.mockResolvedValue({
      name: 'R',
      lat: null,
      lon: null,
      clock_utc: '12:00 - 3/9/2026 UTC',
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.login('secret');
    });
    await act(async () => {
      await result.current.fixForwardClock();
    });

    expect(mockApi.repeaterFixClock).toHaveBeenCalledWith(REPEATER_KEY, 'secret');
    const last = result.current.consoleHistory[result.current.consoleHistory.length - 1];
    expect(last.response).toContain('[fixed]');
    expect(last.response).toContain('sent clkreboot');
  });

  it('a refused sync surfaces the host clock verdict', async () => {
    mockApi.repeaterSyncClock.mockResolvedValueOnce({
      status: 'server_clock_untrusted',
      command: '',
      reply: null,
      repeater_clock: null,
      offset_seconds: null,
      message: 'Server clock is 172800s ahead of the reference.',
      host_clock: { ...hostClockFixture, trusted: false, offset_seconds: 172800 },
    });
    mockApi.repeaterNodeInfo.mockResolvedValue({
      name: 'R',
      lat: null,
      lon: null,
      clock_utc: null,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.syncClock();
    });

    expect(result.current.hostClock?.trusted).toBe(false);
    const last = result.current.consoleHistory[result.current.consoleHistory.length - 1];
    expect(last.response).toContain('ahead of the reference');
  });

  it('loadAll refreshes every pane, then reads only the settings no pane covered', async () => {
    mockApi.repeaterStatus.mockResolvedValueOnce({ battery_volts: 4.0 });
    mockApi.repeaterNodeInfo.mockResolvedValueOnce({
      name: 'Hilltop',
      lat: '45.5',
      lon: null,
      clock_utc: null,
      settings: [
        { key: 'name', value: 'Hilltop', raw: 'Hilltop', status: 'ok' },
        { key: 'lat', value: '45.5', raw: '45.5', status: 'ok' },
        { key: 'lon', value: null, raw: null, status: 'no_reply' },
      ],
    });
    mockApi.repeaterNeighbors.mockResolvedValueOnce({ neighbors: [] });
    mockApi.repeaterAcl.mockResolvedValueOnce({ acl: [] });
    mockApi.repeaterOwnerInfo.mockResolvedValueOnce({
      owner_info: 'Tristan',
      firmware_version: 'v1.15.0',
      name: 'Hilltop',
      guest_password: 'hunter2',
      settings: [
        { key: 'owner_info', value: 'Tristan', raw: null, status: 'ok' },
        { key: 'guest_password', value: 'hunter2', raw: null, status: 'ok' },
      ],
    });
    mockApi.repeaterLppTelemetry.mockResolvedValueOnce({ sensors: [] });
    mockApi.repeaterRegions.mockResolvedValueOnce({
      regions: [],
      raw: null,
      truncated: false,
      source: 'cli',
    });
    mockApi.repeaterSettings.mockResolvedValueOnce({
      values: [{ key: 'flood_max', value: '3', raw: '3', status: 'ok' }],
      cli_responsive: true,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.loadAll();
    });

    expect(mockApi.repeaterStatus).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterNodeInfo).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterNeighbors).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterAcl).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterOwnerInfo).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterLppTelemetry).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterRegions).toHaveBeenCalledTimes(1);
    // Node info and owner info already asked the repeater for these five, so
    // the settings read that follows must not ask again -- even for the one
    // that went unanswered, since asking twice would not change that.
    expect(mockApi.repeaterSettings).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterSettings).toHaveBeenCalledWith(REPEATER_KEY, {
      excludeKeys: ['name', 'lat', 'lon', 'owner_info', 'guest_password'],
    });
    // ...and what the panes read is in the editor as if it had been read there.
    expect(result.current.settingsValues.name.value).toBe('Hilltop');
    expect(result.current.settingsValues.lon.status).toBe('no_reply');
    expect(result.current.settingsValues.guest_password.value).toBe('hunter2');
    expect(result.current.settingsValues.flood_max.value).toBe('3');
  });

  it('loadAll reads everything when the panes that share settings failed', async () => {
    mockApi.repeaterStatus.mockResolvedValueOnce({ battery_volts: 4.0 });
    mockApi.repeaterNodeInfo.mockRejectedValue(new Error('timeout'));
    mockApi.repeaterNeighbors.mockResolvedValueOnce({ neighbors: [] });
    mockApi.repeaterAcl.mockResolvedValueOnce({ acl: [] });
    mockApi.repeaterOwnerInfo.mockRejectedValue(new Error('timeout'));
    mockApi.repeaterLppTelemetry.mockResolvedValueOnce({ sensors: [] });
    mockApi.repeaterRegions.mockResolvedValueOnce({
      regions: [],
      raw: null,
      truncated: false,
      source: 'cli',
    });
    mockApi.repeaterSettings.mockResolvedValueOnce({ values: [], cli_responsive: true });

    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

      let loading: Promise<void> = Promise.resolve();
      act(() => {
        loading = result.current.loadAll();
      });
      // Two failing panes, three attempts each, two seconds between attempts.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
        await loading;
      });

      // Three attempts, then three more when Neighbors tried to prefetch it.
      expect(mockApi.repeaterNodeInfo).toHaveBeenCalledTimes(6);
      // Nothing was read for the editor, so nothing is left out of its read.
      expect(mockApi.repeaterSettings).toHaveBeenCalledWith(REPEATER_KEY, {});
    } finally {
      vi.useRealTimers();
    }
  });

  it('a pane refresh fills the editor fields it read, and an editor read updates the pane', async () => {
    mockApi.repeaterNodeInfo.mockResolvedValueOnce({
      name: 'Hilltop',
      lat: '45.5',
      lon: '-73.5',
      clock_utc: '12:00:00 - 1/1/2024 UTC',
      settings: [
        { key: 'name', value: 'Hilltop', raw: 'Hilltop', status: 'ok' },
        { key: 'lat', value: '45.5', raw: '45.5', status: 'ok' },
        { key: 'lon', value: '-73.5', raw: '-73.5', status: 'ok' },
      ],
    });
    mockApi.repeaterSettings.mockResolvedValueOnce({
      values: [
        { key: 'name', value: 'Valley', raw: 'Valley', status: 'ok' },
        // A failed read must not blank the pane's copy.
        { key: 'lat', value: null, raw: null, status: 'no_reply' },
        // Owner info was never fetched as a pane, so there is nothing to update.
        { key: 'owner_info', value: 'Tristan', raw: 'Tristan', status: 'ok' },
      ],
      cli_responsive: true,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.refreshPane('nodeInfo');
    });
    expect(result.current.settingsValues.name.value).toBe('Hilltop');
    expect(result.current.settingsValues.lon.value).toBe('-73.5');

    await act(async () => {
      await result.current.fetchSettings({ group: 'identity' });
    });
    expect(result.current.paneData.nodeInfo?.name).toBe('Valley');
    expect(result.current.paneData.nodeInfo?.lat).toBe('45.5');
    expect(result.current.paneData.nodeInfo?.clock_utc).toBe('12:00:00 - 1/1/2024 UTC');
    expect(result.current.paneData.ownerInfo).toBeNull();
  });

  it('applySettings updates the pane showing the value, including a new guest password', async () => {
    mockApi.repeaterOwnerInfo.mockResolvedValueOnce({
      owner_info: 'Tristan',
      firmware_version: 'v1.15.0',
      name: 'Hilltop',
      guest_password: 'hunter2',
      settings: [
        { key: 'owner_info', value: 'Tristan', raw: null, status: 'ok' },
        { key: 'guest_password', value: 'hunter2', raw: null, status: 'ok' },
      ],
    });
    mockApi.repeaterApplySettings.mockResolvedValueOnce({
      results: [
        {
          key: 'guest_password',
          command: 'set guest.password ********',
          value: '********',
          reply: 'OK',
          status: 'ok',
        },
        {
          key: 'owner_info',
          command: 'set owner.info Someone else',
          value: 'Someone else',
          reply: 'ERR: nope',
          status: 'error',
        },
        { key: 'name', command: 'set name Valley', value: 'Valley', reply: 'OK', status: 'ok' },
      ],
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    // The schema load is what marks the setting sensitive, so wait for it.
    mockApi.repeaterLogin.mockResolvedValueOnce({ status: 'ok', authenticated: true });
    await act(async () => {
      await result.current.login('pw');
    });
    await waitFor(() => expect(result.current.settingsSchema).not.toBe(null));

    await act(async () => {
      await result.current.refreshPane('ownerInfo');
      await result.current.applySettings([
        { key: 'guest_password', value: 'letmein' },
        { key: 'owner_info', value: 'Someone else' },
        { key: 'name', value: 'Valley' },
      ]);
    });

    // The pane prints the guest password it read back, so it shows the one
    // that was just set rather than the stale one -- while the editor itself
    // still never holds a password.
    expect(result.current.paneData.ownerInfo?.guest_password).toBe('letmein');
    expect(result.current.settingsValues.guest_password.value).toBeNull();
    // A refused write leaves the pane as it was.
    expect(result.current.paneData.ownerInfo?.owner_info).toBe('Tristan');
    // Owner Info prints the name too, so a rename reaches it as well.
    expect(result.current.paneData.ownerInfo?.name).toBe('Valley');
  });

  it('refreshing neighbors fetches node info first', async () => {
    mockApi.repeaterNodeInfo.mockResolvedValueOnce({
      name: 'Repeater',
      lat: '-31.9523',
      lon: '115.8613',
      clock_utc: null,
    });
    mockApi.repeaterNeighbors.mockResolvedValueOnce({ neighbors: [] });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.refreshPane('neighbors');
    });

    expect(mockApi.repeaterNodeInfo).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterNeighbors).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterNodeInfo.mock.invocationCallOrder[0]).toBeLessThan(
      mockApi.repeaterNeighbors.mock.invocationCallOrder[0]
    );
    expect(result.current.paneData.nodeInfo?.lat).toBe('-31.9523');
    expect(result.current.paneData.neighbors).toEqual({ neighbors: [] });
  });

  it('refreshing neighbors reuses already-fetched node info', async () => {
    mockApi.repeaterNodeInfo.mockResolvedValueOnce({
      name: 'Repeater',
      lat: '-31.9523',
      lon: '115.8613',
      clock_utc: null,
    });
    mockApi.repeaterNeighbors.mockResolvedValueOnce({ neighbors: [] });
    mockApi.repeaterNeighbors.mockResolvedValueOnce({ neighbors: [] });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.refreshPane('neighbors');
    });
    await act(async () => {
      await result.current.refreshPane('neighbors');
    });

    expect(mockApi.repeaterNodeInfo).toHaveBeenCalledTimes(1);
    expect(mockApi.repeaterNeighbors).toHaveBeenCalledTimes(2);
  });

  it('refreshing neighbors skips node info prefetch when advert location already exists', async () => {
    mockApi.repeaterNeighbors.mockResolvedValueOnce({ neighbors: [] });

    const { result } = renderHook(() =>
      useRepeaterDashboard(repeaterConversation, { hasAdvertLocation: true })
    );

    await act(async () => {
      await result.current.refreshPane('neighbors');
    });

    expect(mockApi.repeaterNodeInfo).not.toHaveBeenCalled();
    expect(mockApi.repeaterNeighbors).toHaveBeenCalledTimes(1);
    expect(result.current.paneData.neighbors).toEqual({ neighbors: [] });
  });

  it('restores dashboard state when navigating away and back to the same repeater', async () => {
    const statusData = { battery_volts: 4.2 };
    mockApi.repeaterLogin.mockResolvedValueOnce({
      status: 'ok',
      authenticated: true,
      message: null,
    });
    mockApi.repeaterStatus.mockResolvedValueOnce(statusData);
    mockApi.sendRepeaterCommand.mockResolvedValueOnce({
      command: 'ver',
      response: 'v2.1.0',
      sender_timestamp: 1000,
    });

    const firstMount = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await firstMount.result.current.login('secret');
      await firstMount.result.current.refreshPane('status');
      await firstMount.result.current.sendConsoleCommand('ver');
    });

    expect(firstMount.result.current.loggedIn).toBe(true);
    expect(firstMount.result.current.paneData.status).toEqual(statusData);
    expect(firstMount.result.current.consoleHistory).toHaveLength(2);

    firstMount.unmount();

    const secondMount = renderHook(() => useRepeaterDashboard(repeaterConversation));

    expect(secondMount.result.current.loggedIn).toBe(true);
    expect(secondMount.result.current.loginError).toBe(null);
    expect(secondMount.result.current.paneData.status).toEqual(statusData);
    expect(secondMount.result.current.paneStates.status.loading).toBe(false);
    expect(secondMount.result.current.consoleHistory).toHaveLength(2);
    expect(secondMount.result.current.consoleHistory[1].response).toBe('v2.1.0');
  });

  it('fetchSettings merges values per group and records CLI responsiveness', async () => {
    mockApi.repeaterSettings.mockResolvedValueOnce({
      values: [{ key: 'flood_max', value: '3', raw: '3', status: 'ok' }],
      cli_responsive: true,
    });
    mockApi.repeaterSettings.mockResolvedValueOnce({
      values: [{ key: 'guest_password', value: 'hunter2', raw: null, status: 'ok' }],
      cli_responsive: true,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.fetchSettings({ group: 'radio' });
      await result.current.fetchSettings({ group: 'access' });
    });

    expect(mockApi.repeaterSettings).toHaveBeenNthCalledWith(1, REPEATER_KEY, { group: 'radio' });
    // A second group read adds to what the first read, rather than replacing it.
    expect(result.current.settingsValues.flood_max.value).toBe('3');
    expect(result.current.settingsValues.guest_password.value).toBe('hunter2');
    expect(result.current.settingsCliResponsive).toBe(true);
  });

  it('fetchSettings reports a guest session as not CLI-responsive', async () => {
    mockApi.repeaterSettings.mockResolvedValueOnce({
      values: [{ key: 'flood_max', value: null, raw: null, status: 'no_reply' }],
      cli_responsive: false,
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.fetchSettings({ group: 'radio' });
    });

    expect(result.current.settingsCliResponsive).toBe(false);
    expect(result.current.settingsValues.flood_max.status).toBe('no_reply');
  });

  it('applySettings mirrors each write into the console and keeps confirmed values', async () => {
    mockApi.repeaterApplySettings.mockResolvedValueOnce({
      results: [
        {
          key: 'flood_max',
          command: 'set flood.max 5',
          value: '5',
          status: 'ok',
          reply: 'OK',
        },
      ],
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.applySettings([{ key: 'flood_max', value: '5' }]);
    });

    expect(result.current.consoleHistory).toHaveLength(2);
    expect(result.current.consoleHistory[0].command).toBe('set flood.max 5');
    expect(result.current.consoleHistory[1].response).toBe('OK');
    expect(result.current.settingsValues.flood_max.value).toBe('5');
    expect(mockToast.success).toHaveBeenCalled();
  });

  it('applySettings leaves a failed write showing the previous value', async () => {
    mockApi.repeaterSettings.mockResolvedValueOnce({
      values: [{ key: 'flood_max', value: '3', raw: '3', status: 'ok' }],
      cli_responsive: true,
    });
    mockApi.repeaterApplySettings.mockResolvedValueOnce({
      results: [
        {
          key: 'flood_max',
          command: 'set flood.max 99',
          value: '99',
          status: 'error',
          reply: 'ERR: out of range',
        },
      ],
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    await act(async () => {
      await result.current.fetchSettings({ group: 'radio' });
      await result.current.applySettings([{ key: 'flood_max', value: '99' }]);
    });

    expect(result.current.settingsValues.flood_max.value).toBe('3');
    expect(mockToast.error).toHaveBeenCalled();
  });

  it('applySettings never stores a password as a readable value', async () => {
    mockApi.repeaterApplySettings.mockResolvedValueOnce({
      results: [
        {
          key: 'guest_password',
          command: 'set guest.password ********',
          value: '********',
          status: 'ok',
          reply: 'OK',
        },
      ],
    });

    const { result } = renderHook(() => useRepeaterDashboard(repeaterConversation));

    // The schema load is what marks the setting sensitive, so wait for it.
    mockApi.repeaterLogin.mockResolvedValueOnce({ status: 'ok', authenticated: true });
    await act(async () => {
      await result.current.login('pw');
    });
    await waitFor(() => expect(result.current.settingsSchema).not.toBe(null));

    await act(async () => {
      await result.current.applySettings([{ key: 'guest_password', value: 'hunter2' }]);
    });

    expect(result.current.settingsValues.guest_password.value).toBe(null);
    expect(result.current.consoleHistory.some((e) => e.command.includes('hunter2'))).toBe(false);
  });
});
