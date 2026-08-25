/**
 * Shared furniture for the node stats page.
 *
 * The page is a stack of independent sections over one node and one window.
 * Adding a stat means writing a component that takes its slice of
 * `NodeStatsResponse` and rendering it in `NodeStatsView` — nothing existing
 * has to change, and a section whose data is absent renders nothing at all
 * rather than an empty box.
 */

import type { ReactNode } from 'react';

import { STATS_WINDOWS, type StatsWindow } from '../../types';

export function windowLabel(window: StatsWindow): string {
  return STATS_WINDOWS.find((w) => w.key === window)?.label ?? window;
}

export function windowPhrase(window: StatsWindow): string {
  return STATS_WINDOWS.find((w) => w.key === window)?.phrase ?? 'the selected window';
}

/** Recharts takes raw colors, so tooltips cannot be themed with tokens alone. */
export const TOOLTIP_STYLE = {
  contentStyle: {
    backgroundColor: 'hsl(var(--popover))',
    border: '1px solid hsl(var(--border))',
    borderRadius: '6px',
    fontSize: '11px',
    color: 'hsl(var(--popover-foreground))',
  },
  itemStyle: { color: 'hsl(var(--popover-foreground))' },
  labelStyle: { color: 'hsl(var(--muted-foreground))' },
} as const;

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

export function formatDateTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * One section of the page. `description` is where a section explains what it is
 * measuring and what would make the number wrong — the page is a diagnostic
 * tool, and a number nobody can interpret is worse than no number.
 */
export function StatSection({
  title,
  description,
  aside,
  children,
}: {
  title: string;
  description?: ReactNode;
  /** Right-aligned slot in the heading row, e.g. a count or a toggle. */
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="text-base font-semibold tracking-tight">{title}</h3>
        {aside && <div className="text-xs text-muted-foreground">{aside}</div>}
      </div>
      {description && <p className="mt-1 text-[0.8125rem] text-muted-foreground">{description}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

/** A subsection heading inside a `StatSection`. */
export function StatSubheading({ title, description }: { title: string; description?: ReactNode }) {
  return (
    <>
      <h4 className="text-sm font-medium">{title}</h4>
      {description && <p className="mt-0.5 mb-2 text-xs text-muted-foreground">{description}</p>}
    </>
  );
}

/** The page's stat tile. Four across on a desktop, two on a phone. */
export function StatTile({
  value,
  label,
  tone,
  suffix,
}: {
  value: ReactNode;
  label: string;
  /** Tailwind text color class for the value, e.g. `text-warning`. */
  tone?: string;
  /** Smaller trailing text inside the tile, e.g. a denominator. */
  suffix?: ReactNode;
}) {
  return (
    <div className="rounded-md bg-muted/50 p-3 text-center">
      <div className={`text-2xl font-bold ${tone ?? ''}`}>
        {value}
        {suffix && <span className="text-sm font-normal text-muted-foreground">{suffix}</span>}
      </div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}

export function StatTileRow({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">{children}</div>;
}

/** Label/value row, for the figures that do not warrant a tile. */
export function StatRow({
  label,
  value,
  tone,
}: {
  label: ReactNode;
  value: ReactNode;
  tone?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className={`text-right font-medium ${tone ?? ''}`}>{value}</span>
    </div>
  );
}

/** Wide content — tables especially — must scroll inside itself, not the page. */
export function ScrollableTable({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">{children}</table>
    </div>
  );
}
