import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';
import { api } from '../../api';
import type { AppSettings, AppSettingsUpdate, VirtualNodeOverview } from '../../types';
import { Button } from '../ui/button';
import { Checkbox } from '../ui/checkbox';
import { Label } from '../ui/label';

const POLL_INTERVAL_MS = 5000;

function formatTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

export function SettingsVirtualNodeSection({
  appSettings,
  onSaveAppSettings,
  className,
}: {
  appSettings: AppSettings | null | undefined;
  onSaveAppSettings: (update: AppSettingsUpdate) => Promise<void>;
  className?: string;
}) {
  const [overview, setOverview] = useState<VirtualNodeOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setOverview(await api.getVirtualNode());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load virtual node status');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const adminAllowed =
    appSettings?.virtual_node_allow_admin_commands ?? overview?.admin_commands_allowed ?? false;

  const handleAdminToggle = async (checked: boolean) => {
    setBusy('admin');
    try {
      await onSaveAppSettings({ virtual_node_allow_admin_commands: checked });
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  const handleDisconnect = async (peer: string) => {
    setBusy(`disconnect:${peer}`);
    try {
      await api.disconnectVirtualNodeClient(peer);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to disconnect the app');
    } finally {
      setBusy(null);
    }
  };

  const handleForget = async (clientId: string) => {
    setBusy(`forget:${clientId}`);
    try {
      await api.forgetVirtualNodeClient(clientId);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to forget the app');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={cn('space-y-6', className)} data-testid="virtual-node-section">
      <div>
        <h3 className="text-lg font-semibold">Virtual Companion Node</h3>
        <p className="text-sm text-muted-foreground">
          Lets other MeshCore apps (the mobile app over TCP/WiFi, meshcore-cli, Home Assistant) use
          this radio through RemoteTerm, as if the server were the radio. Contacts, channels,
          identity and messages are answered from the server; only what must reach the radio does.
        </p>
      </div>

      {error ? (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm">
          {error}
        </div>
      ) : null}

      {/* Listener status */}
      <div
        className="rounded-md border border-input p-4 text-sm space-y-2"
        data-testid="virtual-node-listener"
      >
        {overview === null ? (
          <div className="text-muted-foreground">Loading…</div>
        ) : !overview.enabled ? (
          <div className="space-y-1">
            <div className="font-medium">Disabled</div>
            <p className="text-muted-foreground">
              Start the server with{' '}
              <span className="font-mono text-xs">MESHCORE_VIRTUAL_NODE_ENABLED=true</span> (or
              switch it on in the Home Assistant add-on options) to let other MeshCore apps connect
              through this server.
            </p>
          </div>
        ) : (
          <>
            <div>
              <span className="text-muted-foreground">Status:</span>{' '}
              {overview.listening ? 'Listening' : 'Enabled but not listening (failed to bind)'}
              {overview.read_only ? ' · read-only' : ''}
            </div>
            {overview.listening ? (
              <div>
                <span className="text-muted-foreground">Address:</span>{' '}
                <span className="font-mono text-xs">
                  {overview.host}:{overview.port}
                </span>{' '}
                <span className="text-muted-foreground">
                  (use this server&apos;s IP and port {overview.port} in the app)
                </span>
              </div>
            ) : null}
            <div>
              <span className="text-muted-foreground">Connected apps:</span> {overview.client_count}
            </div>
            <div>
              <span className="text-muted-foreground">Commands:</span> {overview.local_commands}{' '}
              answered locally · {overview.cached_commands} from cache ·{' '}
              {overview.forwarded_commands} forwarded to the radio
            </div>
            <div>
              <span className="text-muted-foreground">History replay:</span>{' '}
              {overview.replay_limit > 0
                ? `up to ${overview.replay_limit} missed messages on reconnect`
                : 'off'}
            </div>
          </>
        )}
      </div>

      {/* Admin commands switch */}
      <div className="flex items-start gap-3 rounded-md border border-border/60 p-3">
        <Checkbox
          id="virtual-node-admin-commands"
          checked={adminAllowed}
          disabled={busy === 'admin' || overview?.read_only === true}
          onCheckedChange={(checked) => void handleAdminToggle(checked === true)}
          className="mt-0.5"
        />
        <div className="space-y-1">
          <Label htmlFor="virtual-node-admin-commands">
            Allow connected apps to change radio settings
          </Label>
          <p className="text-[0.8125rem] text-muted-foreground">
            Off by default. When off, apps can chat, browse contacts and channels, log in to
            repeaters and request telemetry, but any command that changes the radio itself (name,
            location, frequency, TX power, tuning, flood scope, path hash mode, signing, contact
            import) is refused. Turn it on to configure the radio from a phone; it applies to every
            connected app at once.
            {overview?.read_only ? (
              <>
                {' '}
                The node is running read-only (
                <span className="font-mono text-xs">MESHCORE_VIRTUAL_NODE_READ_ONLY</span>), which
                refuses these commands regardless of this switch.
              </>
            ) : null}
          </p>
        </div>
      </div>

      {/* Connected apps */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h4 className="font-medium">Connected apps</h4>
          <Button variant="ghost" size="sm" onClick={() => void refresh()} aria-label="Refresh">
            <RefreshCw size={14} />
          </Button>
        </div>
        {overview && overview.connected.length > 0 ? (
          <div className="overflow-x-auto rounded-md border border-input">
            <table className="w-full text-sm" data-testid="virtual-node-connected">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">App</th>
                  <th className="px-3 py-2 font-medium">Address</th>
                  <th className="px-3 py-2 font-medium">Connected</th>
                  <th className="px-3 py-2 font-medium">Commands</th>
                  <th className="px-3 py-2 font-medium">Queued</th>
                  <th className="px-3 py-2 font-medium">Replayed</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {overview.connected.map((client) => (
                  <tr key={client.peer} className="border-t border-border/60">
                    <td className="px-3 py-2">{client.app_name || 'unidentified'}</td>
                    <td className="px-3 py-2 font-mono text-xs">{client.peer}</td>
                    <td className="px-3 py-2">{formatTime(client.connected_at)}</td>
                    <td className="px-3 py-2">{client.commands}</td>
                    <td className="px-3 py-2">{client.queued_messages}</td>
                    <td className="px-3 py-2">{client.replayed_messages}</td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy === `disconnect:${client.peer}`}
                        onClick={() => void handleDisconnect(client.peer)}
                      >
                        Disconnect
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No app is connected right now.</p>
        )}
      </div>

      {/* Channel slots */}
      {overview && (overview.channel_slots?.length ?? 0) > 0 ? (
        <div className="space-y-2">
          <h4 className="font-medium">Channel slots</h4>
          <p className="text-[0.8125rem] text-muted-foreground">
            Apps address channels by slot number, and every MeshCore client treats slot 0 as the
            public channel. If a channel is missing from an app, or a message lands in the wrong
            one, compare this against the channel list in the app.
          </p>
          <div className="overflow-x-auto rounded-md border border-input">
            <table className="w-full text-sm" data-testid="virtual-node-slots">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">Slot</th>
                  <th className="px-3 py-2 font-medium">Channel</th>
                </tr>
              </thead>
              <tbody>
                {overview.channel_slots?.map((slot) => (
                  <tr key={slot.index} className="border-t border-border/60">
                    <td className="px-3 py-2">{slot.index}</td>
                    <td className="px-3 py-2">{slot.name ?? slot.key}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {/* Recent refusals */}
      {overview && (overview.recent_refusals?.length ?? 0) > 0 ? (
        <div className="space-y-2">
          <h4 className="font-medium">Recent refusals</h4>
          <p className="text-[0.8125rem] text-muted-foreground">
            Commands the node answered with an error. An app usually just says the message could not
            be sent, so this is where the reason shows up.
          </p>
          <div className="overflow-x-auto rounded-md border border-input">
            <table className="w-full text-sm" data-testid="virtual-node-refusals">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">When</th>
                  <th className="px-3 py-2 font-medium">App</th>
                  <th className="px-3 py-2 font-medium">Command</th>
                  <th className="px-3 py-2 font-medium">Error</th>
                  <th className="px-3 py-2 font-medium">Detail</th>
                </tr>
              </thead>
              <tbody>
                {overview.recent_refusals
                  ?.slice()
                  .reverse()
                  .map((refusal, index) => (
                    <tr key={`${refusal.at}-${index}`} className="border-t border-border/60">
                      <td className="px-3 py-2 whitespace-nowrap">{formatTime(refusal.at)}</td>
                      <td className="px-3 py-2">{refusal.app_name || refusal.peer}</td>
                      <td className="px-3 py-2 font-mono text-xs">{refusal.command}</td>
                      <td className="px-3 py-2 font-mono text-xs">{refusal.error}</td>
                      <td className="px-3 py-2 text-muted-foreground">{refusal.detail}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {/* Remembered apps */}
      <div className="space-y-2">
        <h4 className="font-medium">Remembered apps</h4>
        <p className="text-[0.8125rem] text-muted-foreground">
          Apps are told apart by the name they announce and the address they connect from. Each one
          keeps a cursor into the message history so it can catch up on what it missed when it
          reconnects. Forgetting an app makes its next connection start at the present.
        </p>
        {overview && overview.known_clients.length > 0 ? (
          <div className="overflow-x-auto rounded-md border border-input">
            <table className="w-full text-sm" data-testid="virtual-node-known">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">App</th>
                  <th className="px-3 py-2 font-medium">From</th>
                  <th className="px-3 py-2 font-medium">Last seen</th>
                  <th className="px-3 py-2 font-medium">Connections</th>
                  <th className="px-3 py-2 font-medium">Caught up to</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {overview.known_clients.map((client) => (
                  <tr key={client.client_id} className="border-t border-border/60">
                    <td className="px-3 py-2">
                      {client.app_name || 'unknown'}
                      {client.connected ? (
                        <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-xs text-primary">
                          online
                        </span>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{client.peer_host}</td>
                    <td className="px-3 py-2">{formatTime(client.last_seen)}</td>
                    <td className="px-3 py-2">{client.connections}</td>
                    <td className="px-3 py-2">message #{client.last_message_id}</td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy === `forget:${client.client_id}`}
                        onClick={() => void handleForget(client.client_id)}
                      >
                        Forget
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No app has connected yet.</p>
        )}
      </div>
    </div>
  );
}
