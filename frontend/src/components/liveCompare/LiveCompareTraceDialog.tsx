/**
 * "Where did this message travel?" -- the diagnosis dialog behind the
 * hops/observers line of a Live Compare row.
 *
 * It fetches `/live-feed/trace` for the row and shows every reception of the
 * message laid side by side: this node's own paths and each observer's, hop
 * by hop with the relays named, plus a map of all of it. A one-line reading
 * at the top says the thing the operator opened it for: did the message get
 * anywhere near this antenna, or was it only ever relayed elsewhere?
 */

import { Suspense, lazy, useEffect, useState } from 'react';
import { ExternalLink, Radio, RadioTower } from 'lucide-react';

import { api, isAbortError } from '../../api';
import { cn } from '@/lib/utils';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '../ui/dialog';
import { useDistanceUnit } from '../../contexts/DistanceUnitContext';
import { formatDateTime } from '../nodeStats/nodeStatsShared';
import type {
  LiveCompareMessage,
  LiveCompareTrace,
  LiveTraceHop,
  LiveTraceRoute,
} from '../../types';
import { SourceBadge, liveHostLabel } from './liveCompareShared';
import { hopTitle, nodeName, readTrace, receiverName, routeColour } from './liveTraceShared';

const LiveTraceMap = lazy(() =>
  import('./LiveTraceMap').then((m) => ({ default: m.LiveTraceMap }))
);

interface LiveCompareTraceDialogProps {
  message: LiveCompareMessage;
  open: boolean;
  onClose: () => void;
}

export function LiveCompareTraceDialog({ message, open, onClose }: LiveCompareTraceDialogProps) {
  const { distanceUnit } = useDistanceUnit();
  const [trace, setTrace] = useState<LiveCompareTrace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setTrace(null);
    setError(null);
    api
      .getLiveCompareTrace(
        {
          packetHash: message.packet_hash,
          messageId: message.message_id,
        },
        controller.signal
      )
      .then((next) => {
        if (!controller.signal.aborted) setTrace(next);
      })
      .catch((err) => {
        if (isAbortError(err)) return;
        setError(err instanceof Error ? err.message : 'Failed to load the trace');
      });
    return () => controller.abort();
  }, [message.packet_hash, message.message_id]);

  const reading = trace ? readTrace(trace, distanceUnit) : null;
  let observerIndex = 0;

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent
        className="flex max-h-[88dvh] flex-col sm:max-w-2xl"
        data-testid="live-trace-dialog"
      >
        <DialogHeader>
          <DialogTitle>Where this message travelled</DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <SourceBadge source={message.source} compact />
                {message.channel_name && (
                  <span className="rounded bg-muted px-1.5 py-0.5">{message.channel_name}</span>
                )}
                <span className="tabular-nums">{formatDateTime(message.seen_at)}</span>
              </div>
              <div className="break-words text-sm text-foreground">
                {message.sender && <span className="font-medium">{message.sender}: </span>}
                <span className="whitespace-pre-wrap">{stripSender(message)}</span>
              </div>
            </div>
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto py-1">
          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm">
              {error}
            </div>
          )}
          {!trace && !error && (
            <div className="py-6 text-center text-sm text-muted-foreground">Tracing…</div>
          )}
          {trace && reading && (
            <>
              <div
                className="rounded-md border border-border bg-muted/20 p-3 text-sm"
                data-testid="live-trace-summary"
              >
                <p className="font-medium">{reading.headline}</p>
                {reading.detail && <p className="mt-0.5 text-muted-foreground">{reading.detail}</p>}
                {reading.hint && <p className="mt-1">{reading.hint}</p>}
                {(trace.live_error || trace.live_warning) && (
                  <p className="mt-1 text-xs text-warning">
                    {trace.live_error
                      ? `Live feed: ${trace.live_error}`
                      : `Live feed (partial): ${trace.live_warning}`}
                  </p>
                )}
              </div>

              {trace.routes.length > 0 && (
                <Suspense
                  fallback={
                    <div
                      className="animate-pulse rounded border border-border bg-muted/30"
                      style={{ height: 320 }}
                    />
                  }
                >
                  <LiveTraceMap trace={trace} />
                </Suspense>
              )}

              <ul className="space-y-2" data-testid="live-trace-routes">
                {trace.routes.map((route, index) => {
                  const colour = routeColour(route, observerIndex);
                  if (route.kind === 'observer') observerIndex += 1;
                  return (
                    <RouteCard
                      key={index}
                      route={route}
                      colour={colour}
                      trace={trace}
                      distanceUnit={distanceUnit}
                    />
                  );
                })}
              </ul>
            </>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          {trace?.live_url ? (
            <a
              href={trace.live_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              Open this packet on {liveHostLabel(trace.live_url)}
              <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          ) : (
            <span />
          )}
          <Button variant="secondary" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function stripSender(message: LiveCompareMessage): string {
  const prefix = message.sender ? `${message.sender}: ` : null;
  return prefix && message.text.startsWith(prefix)
    ? message.text.slice(prefix.length)
    : message.text;
}

function signalLine(route: LiveTraceRoute): string {
  const bits: string[] = [];
  if (route.heard_at) bits.push(formatDateTime(route.heard_at));
  if (route.snr !== null) bits.push(`SNR ${route.snr.toFixed(1)}`);
  if (route.rssi !== null) bits.push(`RSSI ${Math.round(route.rssi)}`);
  return bits.join(' · ');
}

function RouteCard({
  route,
  colour,
  trace,
  distanceUnit,
}: {
  route: LiveTraceRoute;
  colour: string;
  trace: LiveCompareTrace;
  distanceUnit: ReturnType<typeof useDistanceUnit>['distanceUnit'];
}) {
  const Icon = route.kind === 'node' ? Radio : RadioTower;
  const who = receiverName(route);
  return (
    <li
      className="rounded-md border border-border p-2 text-sm"
      style={{ borderLeft: `3px solid ${colour}` }}
      data-testid="live-trace-route"
      data-kind={route.kind}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" style={{ color: colour }} />
          {route.kind === 'node' ? (route.receiver?.name ? `${who} (this node)` : who) : who}
          {route.region && (
            <span className="rounded bg-muted px-1 py-0.5 text-[0.625rem] font-normal">
              {route.region}
            </span>
          )}
        </span>
        <span className="text-xs tabular-nums text-muted-foreground">{signalLine(route)}</span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1 text-xs">
        <span className="text-muted-foreground">{nodeName(trace.sender, 'Sender')}</span>
        {route.hops.length === 0 ? (
          <span className="text-muted-foreground">→ direct →</span>
        ) : (
          route.hops.map((hop, index) => (
            <span key={index} className="inline-flex items-center gap-1">
              <span className="text-muted-foreground">→</span>
              <HopChip hop={hop} trace={trace} distanceUnit={distanceUnit} />
            </span>
          ))
        )}
        <span className="text-muted-foreground">→ {who}</span>
      </div>
    </li>
  );
}

function HopChip({
  hop,
  trace,
  distanceUnit,
}: {
  hop: LiveTraceHop;
  trace: LiveCompareTrace;
  distanceUnit: ReturnType<typeof useDistanceUnit>['distanceUnit'];
}) {
  const title = hopTitle(hop, trace.self_node, distanceUnit);
  const known = hop.node?.known_locally ?? false;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded border px-1.5 py-0.5',
        !hop.node && 'border-dashed text-muted-foreground',
        hop.node && known && 'border-info/40 text-info',
        hop.node && !known && 'border-border text-foreground'
      )}
      title={title}
      aria-label={title}
      data-testid="live-trace-hop"
    >
      <span className="font-mono text-[0.625rem] opacity-70">{hop.prefix}</span>
      <span>{hop.node ? nodeName(hop.node, hop.prefix) : 'unknown'}</span>
      {hop.ambiguous && <span className="text-warning">?</span>}
      {hop.node?.direct_neighbour && (
        <span className="text-[0.625rem] uppercase tracking-wider opacity-80">neighbour</span>
      )}
    </span>
  );
}
