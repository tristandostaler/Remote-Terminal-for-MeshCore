/**
 * Vocabulary for the Live Compare trace: the routes one message took to every
 * node that recorded it, and the one-paragraph reading of them.
 *
 * Colours are fixed per role so the map, the legend and the route list agree:
 * this node's own receptions are sky blue, each observer's route gets its own
 * warm colour, relays are told apart by whether this radio knows them.
 */

import type { DistanceUnit } from '../../utils/distanceUnits';
import { calculateDistance, formatDistance, isValidLocation } from '../../utils/pathUtils';
import type { LiveCompareTrace, LiveTraceHop, LiveTraceNode, LiveTraceRoute } from '../../types';

export const SELF_COLOUR = '#8b5cf6'; // violet: this radio
export const SENDER_COLOUR = '#3b82f6'; // blue: the sender
export const OBSERVER_COLOUR = '#22c55e'; // green: an observer feeding the instance
export const RELAY_KNOWN_COLOUR = '#0ea5e9'; // sky: a relay in this node's contacts
export const RELAY_UNKNOWN_COLOUR = '#64748b'; // slate: a relay only the instance can name
export const NODE_ROUTE_COLOUR = RELAY_KNOWN_COLOUR;
export const OBSERVER_ROUTE_COLOURS = ['#f59e0b', '#ec4899', '#f97316', '#14b8a6', '#a855f7'];

export function located(node: LiveTraceNode | null | undefined): node is LiveTraceNode & {
  lat: number;
  lon: number;
} {
  return !!node && isValidLocation(node.lat, node.lon);
}

/** Colour of one route: this node's are all one colour, observers cycle a palette. */
export function routeColour(route: LiveTraceRoute, observerIndex: number): string {
  return route.kind === 'node'
    ? NODE_ROUTE_COLOUR
    : OBSERVER_ROUTE_COLOURS[observerIndex % OBSERVER_ROUTE_COLOURS.length];
}

export function nodeName(node: LiveTraceNode | null | undefined, fallback: string): string {
  if (!node) return fallback;
  return node.name || (node.public_key ? node.public_key.slice(0, 12) : fallback);
}

/** Who heard the message on this route, as a short label. */
export function receiverName(route: LiveTraceRoute): string {
  return nodeName(route.receiver, route.kind === 'node' ? 'This node' : 'Observer');
}

/** The relays a hop could be, for a tooltip: identity, alternatives, distance from here. */
export function hopTitle(
  hop: LiveTraceHop,
  self: LiveTraceNode | null,
  unit: DistanceUnit
): string {
  const parts: string[] = [];
  if (!hop.node) {
    parts.push(`Hop ${hop.prefix}: no known node matches this hash`);
  } else {
    const who = nodeName(hop.node, hop.prefix);
    parts.push(
      hop.identified_by === 'node'
        ? `${who} (from your contacts)`
        : `${who} (named by the live feed instance)`
    );
    if (hop.node.direct_neighbour) parts.push('a direct neighbour of your node');
    else if (hop.node.known_locally) parts.push('in your contacts');
    const away = distanceFrom(self, hop.node, unit);
    if (away) parts.push(`${away} from your node`);
  }
  const others = hop.candidates.filter((c) => c.public_key !== hop.node?.public_key);
  if (hop.ambiguous && others.length > 0) {
    parts.push(`could also be: ${others.map((c) => nodeName(c, '?')).join(', ')}`);
  }
  return parts.join(' · ');
}

export function distanceFrom(
  from: LiveTraceNode | null | undefined,
  to: LiveTraceNode | null | undefined,
  unit: DistanceUnit
): string | null {
  if (!located(from) || !located(to)) return null;
  const km = calculateDistance(from.lat, from.lon, to.lat, to.lon);
  return km === null ? null : formatDistance(km, unit);
}

export interface TraceReading {
  headline: string;
  detail: string | null;
  /** The one fact most likely to explain a miss, when there is one. */
  hint: string | null;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

/** Every distinct relay named on the observers' routes. */
export function observedRelays(trace: LiveCompareTrace): LiveTraceNode[] {
  const seen = new Map<string, LiveTraceNode>();
  for (const route of trace.routes) {
    if (route.kind !== 'observer') continue;
    for (const hop of route.hops) {
      const key = hop.node?.public_key;
      if (hop.node && key && !seen.has(key)) seen.set(key, hop.node);
    }
  }
  return [...seen.values()];
}

/**
 * Read the trace for the operator: did the message get anywhere near this
 * node? Says what the data shows and stops there; the diagnosis is theirs.
 */
export function readTrace(trace: LiveCompareTrace, unit: DistanceUnit): TraceReading {
  const observers = trace.routes.filter((r) => r.kind === 'observer');
  const nodeRoutes = trace.routes.filter((r) => r.kind === 'node');
  const relays = observedRelays(trace);
  const known = relays.filter((r) => r.known_locally);
  const heardBy =
    observers.length === 0
      ? null
      : `${plural(observers.length, 'live feed observer')} heard it` +
        (relays.length > 0
          ? ` through ${plural(relays.length, 'relay')}` +
            (known.length > 0 ? `, ${known.length} of them in your contacts` : '')
          : observers.every((r) => r.hops.length === 0)
            ? ' straight from the sender'
            : '');

  if (trace.outgoing) {
    return {
      headline: 'Sent from this node.',
      detail:
        heardBy ??
        (trace.live_error ? null : 'No live feed observer recorded it in the mirrored window.'),
      hint: null,
    };
  }
  if (trace.heard_by_node) {
    const via =
      nodeRoutes.length > 1
        ? ` via ${plural(nodeRoutes.length, 'route')}`
        : nodeRoutes[0] && nodeRoutes[0].hops.length === 0
          ? ' directly'
          : '';
    return {
      headline: `Your node heard this message${via}.`,
      detail: heardBy ?? (trace.live_error ? null : 'No live feed observer recorded it.'),
      hint: null,
    };
  }
  if (observers.length === 0) {
    return {
      headline: 'Your node missed this message.',
      detail: trace.live_error
        ? 'The live feed instance could not be consulted for its observations.'
        : 'The instance keeps no observation of this packet any more.',
      hint: null,
    };
  }

  let hint: string | null = null;
  const neighbour = relays.find((r) => r.direct_neighbour);
  const nearest = relays
    .filter((r) => located(r) && located(trace.self_node))
    .map((r) => ({
      node: r,
      km: calculateDistance(trace.self_node!.lat, trace.self_node!.lon, r.lat, r.lon) ?? Infinity,
    }))
    .sort((a, b) => a.km - b.km)[0];
  if (neighbour) {
    hint = `${nodeName(neighbour, 'A relay')}, a direct neighbour of your node, repeated it.`;
  } else if (nearest) {
    hint = `The closest located relay that carried it, ${nodeName(nearest.node, '?')}, is ${formatDistance(nearest.km, unit)} from your node.`;
    if (known.length === 0) hint += ' None of the relays are in your contacts.';
  } else if (relays.length > 0 && known.length === 0) {
    hint = 'None of the relays that carried it are in your contacts.';
  } else if (relays.length === 0) {
    hint = 'No relay was involved: every observer heard the sender directly.';
  }
  return { headline: 'Your node missed this message.', detail: heardBy, hint };
}
