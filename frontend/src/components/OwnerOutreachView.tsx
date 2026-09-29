/**
 * Owner outreach: nodes with a problem their owner can fix, next to whatever is
 * known about who that owner is.
 *
 * Today the only problem is a clock that has been clearly wrong for its last few
 * readings -- the one fault this server can prove on its own, from the signed
 * advert timestamps. Nothing here sends anything: the page drafts a message,
 * the operator decides how to deliver it, and "Mark contacted" records that
 * they did so the node sinks down the list for a week.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, BarChart3, Copy, MessageSquare, RefreshCw, Router } from 'lucide-react';

import { api, isAbortError } from '../api';
import { ContactAvatar } from './ContactAvatar';
import { toast } from './ui/sonner';
import { FetchOwnerInfoButton, OwnerHints, OwnerNotesEditor } from './nodeStats/OwnerStats';
import { formatDateTime, formatDuration } from './nodeStats/nodeStatsShared';
import { driftSeverityTextClass, formatDrift, formatDriftSigned } from '../utils/clockDrift';
import { getContactDisplayName } from '../utils/pubkey';
import { CONTACT_TYPE_REPEATER, CONTACT_TYPE_ROOM } from '../types';
import type {
  Contact,
  ContactOwnerInfo,
  Conversation,
  OwnerOutreachItem,
  OwnerOutreachResponse,
} from '../types';

const TYPE_NOUNS: Record<number, string> = {
  1: 'node',
  2: 'repeater',
  3: 'room server',
  4: 'sensor',
};

/** A message the operator can paste wherever they reach the owner. */
export function outreachMessage(item: OwnerOutreachItem, displayName: string): string {
  const noun = TYPE_NOUNS[item.type] ?? 'node';
  if (item.issue === 'unset_clock') {
    return (
      `Hi! Your MeshCore ${noun} "${displayName}" is advertising with a clock that was never set ` +
      `(it reports a time near 1970). Setting its time once, e.g. "Sync Clock" from the app or ` +
      `"time <epoch>" in its admin CLI, fixes it; a GPS or RTC module keeps it right across reboots. ` +
      `Thanks for running it!`
    );
  }
  return (
    `Hi! Your MeshCore ${noun} "${displayName}" has a clock ${formatDrift(item.drift_seconds)} ` +
    `(measured from its own signed adverts, ${item.readings} readings in a row). ` +
    `A clock sync, e.g. "Sync Clock" from the app or "time <epoch>" in its admin CLI, should fix it` +
    (item.drift_seconds > 0
      ? `. Firmware refuses to move a clock backwards, so it may need "clkreboot" first. `
      : '. ') +
    `Thanks for running it!`
  );
}

function OutreachCard({
  item,
  contact,
  onOwnerChange,
  onOpenNodeStats,
  onSelectConversation,
}: {
  item: OwnerOutreachItem;
  contact: Contact | undefined;
  onOwnerChange: (owner: ContactOwnerInfo) => void;
  onOpenNodeStats?: (publicKey: string, name?: string) => void;
  onSelectConversation: (conversation: Conversation) => void;
}) {
  const displayName =
    (contact && getContactDisplayName(contact.name, contact.public_key, contact.last_advert)) ||
    item.name ||
    item.public_key.slice(0, 12);
  const type = contact?.type ?? item.type;
  const openLabel =
    type === CONTACT_TYPE_REPEATER
      ? 'Open repeater'
      : type === CONTACT_TYPE_ROOM
        ? 'Open room'
        : 'Open conversation';

  const copyMessage = async () => {
    try {
      await navigator.clipboard.writeText(outreachMessage(item, displayName));
      toast.success('Message copied');
    } catch {
      toast.error('Could not copy to the clipboard');
    }
  };

  const actionClass =
    'inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground';

  return (
    <li
      className={`rounded-lg border border-border p-4 ${item.recently_notified ? 'opacity-70' : ''}`}
      data-testid="owner-outreach-item"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <ContactAvatar
            name={contact?.name ?? item.name}
            publicKey={item.public_key}
            contactType={type}
            size={32}
          />
          <div className="min-w-0">
            <p className="truncate font-semibold">{displayName}</p>
            <p className="text-xs text-muted-foreground">
              <span className="uppercase tracking-wider">{TYPE_NOUNS[type] ?? 'node'}</span>
              {' · '}
              <span className="font-mono">{item.public_key.slice(0, 12)}</span>
            </p>
          </div>
        </div>
        <div className="text-right">
          <p className={`font-semibold ${driftSeverityTextClass(item.severity)}`}>
            {item.issue === 'unset_clock'
              ? 'Clock never set'
              : `${formatDriftSigned(item.drift_seconds)} (${formatDrift(item.drift_seconds)})`}
          </p>
          <p className="text-xs text-muted-foreground">
            {item.readings} readings, newest {formatDateTime(item.last_observed_at)}
          </p>
        </div>
      </div>

      <div className="mt-3 space-y-2">
        {item.owner.firmware_owner_info && (
          <p className="whitespace-pre-wrap break-words rounded-md bg-muted/50 px-3 py-2 text-sm">
            {item.owner.firmware_owner_info.replace(/\|/g, '\n')}
          </p>
        )}
        <OwnerHints hints={item.owner.hints} />
        {!item.owner.firmware_owner_info && item.owner.hints.length === 0 && (
          <p className="text-xs text-muted-foreground">
            No owner known yet.{' '}
            {type === CONTACT_TYPE_REPEATER || type === CONTACT_TYPE_ROOM
              ? 'Fetch it now, or let the background refresh ask the node. Add anything you find below.'
              : type === 1
                ? 'This is a chat node, so a direct message reaches its owner.'
                : 'Add anything you find below.'}
          </p>
        )}
        <OwnerNotesEditor owner={item.owner} onChange={onOwnerChange} compact />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className={actionClass} onClick={() => void copyMessage()}>
          <Copy className="h-3.5 w-3.5" aria-hidden="true" />
          Copy message
        </button>
        {(type === CONTACT_TYPE_REPEATER || type === CONTACT_TYPE_ROOM) && (
          <FetchOwnerInfoButton publicKey={item.public_key} onChange={onOwnerChange} />
        )}
        <button
          type="button"
          className={actionClass}
          onClick={() =>
            onSelectConversation({ type: 'contact', id: item.public_key, name: displayName })
          }
        >
          {type === CONTACT_TYPE_REPEATER ? (
            <Router className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {openLabel}
        </button>
        {onOpenNodeStats && (
          <button
            type="button"
            className={actionClass}
            onClick={() => onOpenNodeStats(item.public_key, displayName)}
          >
            <BarChart3 className="h-3.5 w-3.5" aria-hidden="true" />
            Node stats
          </button>
        )}
      </div>
    </li>
  );
}

export function OwnerOutreachView({
  contacts,
  onOpenNodeStats,
  onSelectConversation,
}: {
  contacts: Contact[];
  onOpenNodeStats?: (publicKey: string, name?: string) => void;
  onSelectConversation: (conversation: Conversation) => void;
}) {
  const [data, setData] = useState<OwnerOutreachResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api.getOwnerOutreach(controller.signal).then(
      (next) => {
        setData(next);
        setLoading(false);
      },
      (err) => {
        if (isAbortError(err)) return;
        setError(err instanceof Error ? err.message : 'Failed to load owner outreach');
        setLoading(false);
      }
    );
    return () => controller.abort();
  }, [reloadNonce]);

  // Editing notes or the contacted mark only touches that one record; keep the
  // list order until the next refresh so a card does not jump out from under
  // the pointer that just clicked it.
  const handleOwnerChange = useCallback((owner: ContactOwnerInfo) => {
    setData((prev) =>
      prev
        ? {
            ...prev,
            items: prev.items.map((item) =>
              item.public_key === owner.public_key ? { ...item, owner } : item
            ),
          }
        : prev
    );
  }, []);

  const byKey = new Map(contacts.map((c) => [c.public_key.toLowerCase(), c]));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">Owner Outreach</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Nodes whose clock has been clearly wrong lately
              {data
                ? ` (more than ${formatDuration(data.threshold_seconds)} off on each of their last ${data.min_readings}+ readings, within ${formatDuration(data.lookback_seconds)})`
                : ''}
              , with what is known about their owner. Nothing is sent from here.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setReloadNonce((n) => n + 1)}
            disabled={loading}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-60"
            title="Refresh"
            aria-label="Refresh owner outreach"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[800px] space-y-4 p-4">
          {data?.server_clock_suspect && (
            <div
              role="alert"
              className="flex gap-2 rounded-md border border-warning/50 bg-warning/10 p-3 text-sm"
            >
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
              <p>
                The median of every node's newest reading is{' '}
                {formatDrift(data.median_drift_seconds ?? 0)}. Independent clocks do not drift
                together, so <strong>this server's clock</strong> is the likelier culprit. Fix it
                before messaging anyone.
              </p>
            </div>
          )}

          {loading && !data ? (
            <p className="py-8 text-center text-muted-foreground">Loading…</p>
          ) : error && !data ? (
            <p className="py-8 text-center text-muted-foreground">{error}</p>
          ) : data && data.items.length === 0 ? (
            <div className="py-8 text-center">
              <p className="text-muted-foreground">Nobody to contact right now.</p>
              <p className="mt-1 text-sm text-muted-foreground">
                {data.nodes_measured} node{data.nodes_measured === 1 ? '' : 's'} measured in the
                last {formatDuration(data.lookback_seconds)}, none with a clock that has stayed
                wrong.
              </p>
            </div>
          ) : data ? (
            <ul className="space-y-3">
              {data.items.map((item) => (
                <OutreachCard
                  key={item.public_key}
                  item={item}
                  contact={byKey.get(item.public_key.toLowerCase())}
                  onOwnerChange={handleOwnerChange}
                  onOpenNodeStats={onOpenNodeStats}
                  onSelectConversation={onSelectConversation}
                />
              ))}
            </ul>
          ) : null}
        </div>
      </div>
    </div>
  );
}
