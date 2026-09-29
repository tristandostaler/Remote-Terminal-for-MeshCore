import { useState } from 'react';
import { RepeaterPane, NotFetched } from './repeaterPaneShared';
import { Button } from '../ui/button';
import { toast } from '../ui/sonner';
import { api } from '../../api';
import { cn } from '@/lib/utils';
import type { RepeaterRegionsResponse, PaneState } from '../../types';

export function RegionsPane({
  data,
  state,
  onRefresh,
  disabled,
}: {
  data: RepeaterRegionsResponse | null;
  state: PaneState;
  onRefresh: () => void;
  disabled?: boolean;
}) {
  const [adding, setAdding] = useState(false);
  const addableNames = (data?.regions ?? []).map((r) => r.name).filter((n) => n && n !== '*');

  const handleAddToKnownRegions = async () => {
    setAdding(true);
    try {
      const current = (await api.getSettings()).known_regions ?? [];
      const seen = new Set(current.map((n) => n.toLowerCase()));
      const additions = addableNames.filter((n) => !seen.has(n.toLowerCase()));
      if (additions.length === 0) {
        toast.info('All of these regions are already in Known Regions');
        return;
      }
      await api.updateSettings({ known_regions: [...current, ...additions] });
      toast.success(
        `Added ${additions.length} region${additions.length === 1 ? '' : 's'} to Known Regions`
      );
    } catch (err) {
      toast.error('Failed to update Known Regions', {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setAdding(false);
    }
  };

  const headerNote = data?.truncated
    ? 'List truncated by the radio — showing the first regions only'
    : data?.source === 'anon'
      ? 'Guest view: flood-allowed regions only (log in as admin for the full hierarchy)'
      : 'Region hierarchy and flood permissions';

  return (
    <RepeaterPane
      title="Regions"
      state={state}
      onRefresh={onRefresh}
      disabled={disabled}
      headerNote={headerNote}
    >
      {!data ? (
        <NotFetched />
      ) : data.regions.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          No regions returned. The repeater may be unreachable, or full region details require admin
          access.
        </p>
      ) : (
        <div className="space-y-0.5">
          {data.regions.map((region, index) => (
            <div
              key={`${region.depth}-${region.name}-${index}`}
              className="flex items-center gap-2 text-sm py-0.5"
              style={{ paddingLeft: `${region.depth * 0.9}rem` }}
            >
              <span className="font-mono truncate">
                {region.name === '*' ? '∗ (all regions)' : region.name}
              </span>
              {region.is_home && (
                <span className="text-[0.625rem] uppercase tracking-wider px-1.5 py-0.5 rounded bg-primary/10 text-primary">
                  Home
                </span>
              )}
              <span
                className={cn(
                  'ml-auto shrink-0 text-[0.625rem] uppercase tracking-wider px-1.5 py-0.5 rounded',
                  region.flood_allowed
                    ? 'bg-success/15 text-success'
                    : 'bg-muted text-muted-foreground'
                )}
                title={
                  region.flood_allowed
                    ? 'Flood is allowed for this region'
                    : 'Flood is blocked for this region'
                }
              >
                {region.flood_allowed ? 'Flood' : 'Blocked'}
              </span>
            </div>
          ))}
          {addableNames.length > 0 && (
            <div className="pt-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleAddToKnownRegions}
                disabled={adding}
              >
                {adding ? 'Adding...' : 'Add to Known Regions'}
              </Button>
            </div>
          )}
        </div>
      )}
    </RepeaterPane>
  );
}
