import { useState, useCallback, useEffect } from 'react';
import { Button } from '../ui/button';
import type { HostClockStatus } from '../../types';

/** Two-click confirmation that resets itself after three seconds. */
function useArmedAction(action: () => void): [boolean, () => void] {
  const [armed, setArmed] = useState(false);

  const trigger = useCallback(() => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    action();
  }, [armed, action]);

  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(timer);
  }, [armed]);

  return [armed, trigger];
}

export function ActionsPane({
  onSendZeroHopAdvert,
  onSendFloodAdvert,
  onSyncClock,
  onFixForwardClock,
  onReboot,
  consoleLoading,
  hostClock,
  onRefreshHostClock,
}: {
  onSendZeroHopAdvert: () => void;
  onSendFloodAdvert: () => void;
  onSyncClock: () => void;
  onFixForwardClock: () => void;
  onReboot: () => void;
  consoleLoading: boolean;
  hostClock: HostClockStatus | null;
  onRefreshHostClock: () => void;
}) {
  const [confirmReboot, handleReboot] = useArmedAction(onReboot);
  const [confirmFixClock, handleFixClock] = useArmedAction(onFixForwardClock);
  // A server whose clock cannot be trusted refuses every clock push, so the
  // buttons that would push it are disabled rather than left to fail.
  const clockPushBlocked = hostClock !== null && !hostClock.trusted;

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <div className="px-3 py-2 bg-muted/50 border-b border-border">
        <h3 className="text-sm font-medium">Actions</h3>
      </div>
      <div className="p-3 flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={onSendZeroHopAdvert} disabled={consoleLoading}>
          Zero Hop Advert
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={onSendFloodAdvert}
          disabled={consoleLoading}
        >
          Flood Advert
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onSyncClock}
          disabled={consoleLoading || clockPushBlocked}
          title="Push this server's clock to the repeater (CLI time). The firmware only moves a clock forward."
        >
          Sync Clock
        </Button>
        <Button
          variant={confirmFixClock ? 'destructive' : 'outline'}
          size="sm"
          onClick={handleFixClock}
          disabled={consoleLoading || clockPushBlocked}
          title="For a repeater whose clock is AHEAD: clkreboot resets its clock and reboots it, then the clock is synced again. Takes about half a minute."
        >
          {confirmFixClock ? 'Confirm Fix (reboots!)' : 'Fix Forward Clock'}
        </Button>
        <Button
          variant={confirmReboot ? 'destructive' : 'outline'}
          size="sm"
          onClick={handleReboot}
          disabled={consoleLoading}
        >
          {confirmReboot ? 'Confirm Reboot' : 'Reboot'}
        </Button>
      </div>
      {hostClock && (
        <div
          className={`px-3 pb-3 text-xs ${
            hostClock.trusted ? 'text-muted-foreground' : 'text-destructive'
          }`}
        >
          {hostClock.message}{' '}
          <button
            type="button"
            className="underline hover:text-foreground transition-colors"
            onClick={onRefreshHostClock}
            disabled={consoleLoading}
          >
            re-check
          </button>
        </div>
      )}
    </div>
  );
}
