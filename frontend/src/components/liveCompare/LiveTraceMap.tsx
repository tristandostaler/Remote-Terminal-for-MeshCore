/**
 * Every known route of one message on a map: the sender, the relays each
 * reception went through, the observers that recorded it, and this radio,
 * so the operator can see whether the message ever came near their antenna.
 *
 * One marker per node however many routes it sits on; one polyline per
 * route, dashed when a hop on it has no known location and the line skips it.
 */

import { useEffect, useMemo, useRef } from 'react';
import L from 'leaflet';
import { MapContainer, Marker, Polyline, TileLayer, Tooltip, useMap } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';

import type { LiveCompareTrace, LiveTraceNode } from '../../types';
import {
  OBSERVER_COLOUR,
  RELAY_KNOWN_COLOUR,
  RELAY_UNKNOWN_COLOUR,
  SELF_COLOUR,
  SENDER_COLOUR,
  located,
  nodeName,
  receiverName,
  routeColour,
} from './liveTraceShared';

interface MapPoint {
  key: string;
  lat: number;
  lon: number;
  label: string;
  colour: string;
  roles: string[];
}

interface MapLine {
  key: string;
  positions: [number, number][];
  colour: string;
  dashed: boolean;
  title: string;
}

function makeIcon(label: string, colour: string): L.DivIcon {
  const size = label.length > 2 ? 30 : 24;
  return L.divIcon({
    className: '',
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    html: `<div style="
      width:${size}px;height:${size}px;border-radius:50%;
      background:${colour};color:#fff;
      display:flex;align-items:center;justify-content:center;
      font-size:11px;font-weight:700;
      border:2px solid rgba(255,255,255,0.85);
      box-shadow:0 1px 4px rgba(0,0,0,0.4);
    ">${label}</div>`,
  });
}

function pointKey(prefix: string, node: LiveTraceNode): string {
  return `${prefix}:${node.public_key ?? `${node.lat},${node.lon}`}`;
}

/** Markers and lines for the trace; nodes are shared across routes by key. */
export function buildMapLayers(trace: LiveCompareTrace): { points: MapPoint[]; lines: MapLine[] } {
  const points = new Map<string, MapPoint>();
  const add = (
    key: string,
    node: LiveTraceNode & { lat: number; lon: number },
    label: string,
    colour: string,
    role: string
  ): MapPoint => {
    const existing = points.get(key);
    if (existing) {
      if (!existing.roles.includes(role)) existing.roles.push(role);
      return existing;
    }
    const point: MapPoint = { key, lat: node.lat, lon: node.lon, label, colour, roles: [role] };
    points.set(key, point);
    return point;
  };

  const selfKey = trace.self_node?.public_key ?? null;
  if (located(trace.self_node)) {
    add('self', trace.self_node, 'me', SELF_COLOUR, `${nodeName(trace.self_node, 'This node')}`);
  }
  if (located(trace.sender) && trace.sender.public_key !== selfKey) {
    add(pointKey('sender', trace.sender), trace.sender, 'S', SENDER_COLOUR, 'Sender');
  }

  const lines: MapLine[] = [];
  let observerIndex = 0;
  trace.routes.forEach((route, routeIndex) => {
    const colour = routeColour(route, observerIndex);
    if (route.kind === 'observer') observerIndex += 1;
    const who = receiverName(route);
    const positions: [number, number][] = [];
    let dashed = false;

    if (located(trace.sender)) positions.push([trace.sender.lat, trace.sender.lon]);
    else dashed = true;

    route.hops.forEach((hop, hopIndex) => {
      const node = hop.node;
      if (!located(node)) {
        dashed = true;
        return;
      }
      const relayColour = node.known_locally ? RELAY_KNOWN_COLOUR : RELAY_UNKNOWN_COLOUR;
      add(
        pointKey('relay', node),
        node,
        String(hopIndex + 1),
        relayColour,
        `hop ${hopIndex + 1} of ${route.hops.length} towards ${who}`
      );
      positions.push([node.lat, node.lon]);
    });

    const receiver = route.receiver;
    if (located(receiver)) {
      if (route.kind === 'node' || receiver.public_key === selfKey) {
        add('self', receiver, 'me', SELF_COLOUR, nodeName(receiver, 'This node'));
      } else {
        add(pointKey('observer', receiver), receiver, 'O', OBSERVER_COLOUR, 'Observer');
      }
      positions.push([receiver.lat, receiver.lon]);
    } else {
      dashed = true;
    }

    if (positions.length >= 2) {
      lines.push({
        key: `route-${routeIndex}`,
        positions,
        colour,
        dashed,
        title: route.kind === 'node' ? 'Heard by this node' : `Heard by ${who}`,
      });
    }
  });

  return { points: [...points.values()], lines };
}

/** Fit the view to every marker once, then leave the user to pan and zoom. */
function FitOnce({ points }: { points: [number, number][] }) {
  const map = useMap();
  const fitted = useRef(false);
  useEffect(() => {
    if (fitted.current || points.length === 0) return;
    fitted.current = true;
    if (points.length === 1) map.setView(points[0], 12);
    else map.fitBounds(points as L.LatLngBoundsExpression, { padding: [30, 30], maxZoom: 13 });
  }, [map, points]);
  return null;
}

export function LiveTraceMap({
  trace,
  height = 320,
}: {
  trace: LiveCompareTrace;
  height?: number;
}) {
  const { points, lines } = useMemo(() => buildMapLayers(trace), [trace]);
  const coords = useMemo<[number, number][]>(() => points.map((p) => [p.lat, p.lon]), [points]);

  if (points.length === 0) {
    return (
      <div className="flex h-14 items-center justify-center rounded border border-border bg-muted/30 text-sm text-muted-foreground">
        No node on these routes has a known location
      </div>
    );
  }

  const unlocated = trace.routes.reduce(
    (count, route) => count + route.hops.filter((hop) => !located(hop.node)).length,
    0
  );

  return (
    <div data-testid="live-trace-map">
      <div
        className="overflow-hidden rounded border border-border"
        role="img"
        aria-label="Map of the routes this message took"
        style={{ height }}
      >
        <MapContainer
          center={coords[0]}
          zoom={10}
          className="h-full w-full"
          style={{ background: '#1a1a2e' }}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          <FitOnce points={coords} />
          {lines.map((line) => (
            <Polyline
              key={line.key}
              positions={line.positions}
              pathOptions={{
                color: line.colour,
                weight: 3,
                opacity: 0.85,
                dashArray: line.dashed ? '6 8' : undefined,
              }}
            >
              <Tooltip sticky>{line.title}</Tooltip>
            </Polyline>
          ))}
          {points.map((point) => (
            <Marker
              key={point.key}
              position={[point.lat, point.lon]}
              icon={makeIcon(point.label, point.colour)}
            >
              <Tooltip direction="top" offset={[0, -14]}>
                {point.roles.join(' · ')}
              </Tooltip>
            </Marker>
          ))}
        </MapContainer>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[0.6875rem] text-muted-foreground">
        <LegendDot colour={SELF_COLOUR} label="this node" />
        <LegendDot colour={SENDER_COLOUR} label="sender" />
        <LegendDot colour={OBSERVER_COLOUR} label="live feed observer" />
        <LegendDot colour={RELAY_KNOWN_COLOUR} label="relay in your contacts" />
        <LegendDot colour={RELAY_UNKNOWN_COLOUR} label="relay named by the instance" />
        {unlocated > 0 && (
          <span>
            dashed: {unlocated} hop{unlocated === 1 ? '' : 's'} without a known location skipped
          </span>
        )}
      </div>
    </div>
  );
}

function LegendDot({ colour, label }: { colour: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className="inline-block h-2.5 w-2.5 rounded-full"
        style={{ background: colour }}
        aria-hidden="true"
      />
      {label}
    </span>
  );
}
