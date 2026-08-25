/**
 * Node stats: one page, one node, one window selector.
 *
 * The shell owns the header, the window, the fetch, and the empty/error states.
 * Everything below the header is a section component that takes its slice of
 * `NodeStatsResponse` and renders nothing when it has nothing to say.
 *
 * **Adding a section** — three steps, none of which touch an existing section:
 *   1. Add the field to `NodeStatsResponse` (backend `app/models.py` and
 *      `types.ts`) and populate it in `GET /contacts/{key}/stats`.
 *   2. Write a component under `components/nodeStats/` that takes that field
 *      plus `windowKey`, wrapped in `StatSection` from `nodeStatsShared`.
 *   3. Render it in the section list below, guarded on its field being present.
 *
 * Sections must honour the page's window rather than fetching their own. One
 * request, one period, so nothing on the page disagrees with anything else.
 */

import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';

import { api, isAbortError } from '../../api';
import { ContactAvatar } from '../ContactAvatar';
import { Separator } from '../ui/separator';
import { getContactDisplayName } from '../../utils/pubkey';
import { CONTACT_TYPE_REPEATER, NODE_STATS_DEFAULT_WINDOW, STATS_WINDOWS } from '../../types';
import type { Contact, NodeStatsResponse, StatsWindow } from '../../types';
import { ClockDriftStats } from './ClockDriftStats';
import { formatDateTime, windowPhrase } from './nodeStatsShared';

const CONTACT_TYPE_LABELS: Record<number, string> = {
  0: 'Unknown',
  1: 'Client',
  2: 'Repeater',
  3: 'Room',
  4: 'Sensor',
};

function WindowSelector({
  value,
  onChange,
  disabled,
}: {
  value: StatsWindow;
  onChange: (next: StatsWindow) => void;
  disabled?: boolean;
}) {
  return (
    <div
      className="inline-flex shrink-0 overflow-hidden rounded-md border border-border"
      role="group"
      aria-label="Statistics window"
    >
      {STATS_WINDOWS.map((option) => (
        <button
          key={option.key}
          type="button"
          title={option.title}
          disabled={disabled}
          aria-pressed={value === option.key}
          onClick={() => onChange(option.key)}
          className={`px-2.5 py-1 text-xs transition-colors disabled:opacity-60 ${
            value === option.key
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-muted hover:text-foreground'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function NodeStatsView({
  publicKey,
  contacts,
  onBack,
}: {
  publicKey: string;
  /** Live contact list, for the header while the payload is still in flight. */
  contacts: Contact[];
  onBack?: () => void;
}) {
  const [stats, setStats] = useState<NodeStatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedWindow, setSelectedWindow] = useState<StatsWindow>(NODE_STATS_DEFAULT_WINDOW);
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api.getNodeStats(publicKey, selectedWindow, controller.signal).then(
      (data) => {
        setStats(data);
        setLoading(false);
      },
      (err) => {
        if (isAbortError(err)) return;
        setError(err instanceof Error ? err.message : 'Failed to load node stats');
        setLoading(false);
      }
    );
    return () => controller.abort();
  }, [publicKey, selectedWindow, reloadNonce]);

  const handleRefresh = useCallback(() => setReloadNonce((n) => n + 1), []);

  const contact = contacts.find((c) => c.public_key.toLowerCase() === publicKey.toLowerCase());
  // Prefer the live contact name: it tracks WebSocket renames, while the payload
  // is a snapshot from whenever the request went out.
  const displayName =
    (contact && getContactDisplayName(contact.name, contact.public_key, contact.last_advert)) ||
    stats?.name ||
    publicKey.slice(0, 12);
  const nodeType = contact?.type ?? stats?.type ?? 0;
  // While a wider window loads the previous snapshot stays on screen, so
  // headings must follow the data rather than the pending selection.
  const shownWindow = stats?.window ?? selectedWindow;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            {onBack && (
              <button
                type="button"
                onClick={onBack}
                className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                title="Back"
                aria-label="Back"
              >
                <ArrowLeft className="h-4 w-4" />
              </button>
            )}
            <ContactAvatar
              name={contact?.name ?? stats?.name ?? null}
              publicKey={publicKey}
              contactType={nodeType}
              size={36}
            />
            <div className="min-w-0">
              <h2 className="truncate text-base font-semibold">{displayName}</h2>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                <span className="rounded bg-muted px-1.5 py-0.5 text-[0.625rem] uppercase tracking-wider">
                  {CONTACT_TYPE_LABELS[nodeType] ?? 'Unknown'}
                </span>
                <span className="truncate font-mono">{publicKey.slice(0, 16)}…</span>
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <WindowSelector
              value={selectedWindow}
              onChange={setSelectedWindow}
              disabled={loading}
            />
            <button
              type="button"
              onClick={handleRefresh}
              disabled={loading}
              className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-60"
              title="Refresh"
              aria-label="Refresh node stats"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          Everything this server knows about this node over {windowPhrase(shownWindow)}.
          {stats && ` Snapshot taken ${formatDateTime(stats.generated_at)}.`}
        </p>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[800px] p-4">
          {loading && !stats ? (
            <p className="py-8 text-center text-muted-foreground">Loading node stats…</p>
          ) : error && !stats ? (
            <p className="py-8 text-center text-muted-foreground">{error}</p>
          ) : stats ? (
            <div className="space-y-6">
              {/* ---- Section list. Add new sections here; see the file header. ---- */}
              {stats.clock_drift && (
                <ClockDriftStats drift={stats.clock_drift} windowKey={shownWindow} />
              )}

              {!stats.clock_drift && (
                <div className="py-8 text-center">
                  <p className="text-muted-foreground">
                    Nothing measured for this node in {windowPhrase(shownWindow)}.
                  </p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Clock drift is read from advertisements, so this node has to advertise within
                    the window to appear here. Try a wider one.
                  </p>
                </div>
              )}

              {stats.clock_drift && nodeType !== CONTACT_TYPE_REPEATER && (
                <>
                  <Separator />
                  <p className="text-xs text-muted-foreground">
                    This node is a {(CONTACT_TYPE_LABELS[nodeType] ?? 'unknown node').toLowerCase()}
                    , not a repeater. Everything above still applies — every node that advertises
                    reports its clock.
                  </p>
                </>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
