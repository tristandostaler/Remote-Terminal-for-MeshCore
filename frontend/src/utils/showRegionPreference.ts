// Browser-local preference for showing the region scope badge under each
// message (channels, DMs, etc.). Pure display tweak, stored per-browser in
// localStorage. Off by default.

export const SHOW_REGION_KEY = 'remoteterm-show-region';

export function getSavedShowRegion(): boolean {
  try {
    return localStorage.getItem(SHOW_REGION_KEY) === 'true';
  } catch {
    return false;
  }
}

export function setSavedShowRegion(enabled: boolean): void {
  try {
    if (enabled) {
      localStorage.setItem(SHOW_REGION_KEY, 'true');
    } else {
      localStorage.removeItem(SHOW_REGION_KEY);
    }
  } catch {
    // localStorage may be unavailable
  }
}
