/**
 * Dialogs have to fit the screen they open on.
 *
 * `DialogContent` is centred with a translate, so content taller than the
 * viewport used to run off BOTH edges with nothing to scroll: the feature panel
 * on a phone lost its heading and its close button above the top of the screen,
 * leaving tap-outside as the only way out. jsdom does no layout, so these tests
 * guard the rules that produce the fit rather than the resulting pixels.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../components/ui/dialog';
import { ConversationFeaturesModal } from '../components/ConversationFeaturesModal';
import type { AeicStatus } from '../api';
import { api } from '../api';

vi.mock('../components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('../api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api')>();
  return {
    ...original,
    api: {
      ...original.api,
      getAeicStatus: vi.fn(),
      startAeicModelDownload: vi.fn(),
      cancelAeicModelDownload: vi.fn(),
    },
  };
});

function aeicStatus(): AeicStatus {
  return {
    runtime_available: true,
    supports_encode: true,
    supports_decode: true,
    downloading: false,
    download_file: null,
    downloaded_bytes: 0,
    download_total_bytes: 0,
    installed_bytes: 1,
    bundle_total_bytes: 1,
    model_dir: 'data/models/aeic',
    rate_point: 'ft32',
    last_error: null,
    assets: [],
  };
}

describe('the dialog primitive', () => {
  it('caps its height to the viewport and scrolls what does not fit', () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Tall</DialogTitle>
          </DialogHeader>
        </DialogContent>
      </Dialog>
    );

    const dialog = screen.getByRole('dialog');
    expect(dialog.className).toMatch(/max-h-\[calc\(100dvh/);
    expect(dialog.className).toContain('overflow-y-auto');
  });

  it('leaves a dialog that lays out its own scrolling body alone', () => {
    /*
     * Six modals already cap themselves and scroll an inner region so their own
     * header stays pinned. The floor must not fight them: cn() has to resolve to
     * the caller's overflow and max-height, not add a second scroll container.
     */
    render(
      <Dialog open>
        <DialogContent className="flex max-h-[80dvh] flex-col overflow-hidden">
          <DialogHeader>
            <DialogTitle>Self-managed</DialogTitle>
          </DialogHeader>
        </DialogContent>
      </Dialog>
    );

    const dialog = screen.getByRole('dialog');
    expect(dialog.className).toContain('overflow-hidden');
    expect(dialog.className).not.toContain('overflow-y-auto');
    expect(dialog.className).toContain('max-h-[80dvh]');
    expect(dialog.className).not.toMatch(/max-h-\[calc\(100dvh/);
  });
});

describe('the conversation features panel on a short screen', () => {
  it('scrolls the feature list without taking the close button with it', async () => {
    vi.mocked(api.getAeicStatus).mockResolvedValue(aeicStatus());

    render(
      <ConversationFeaturesModal
        open
        onClose={vi.fn()}
        conversationType="contact"
        conversationId={'aa'.repeat(32)}
        conversationName="Alice"
        mcmpEnabled
        mcmpVersion={3}
        imageCodec="ie4"
        rawMediaTextTransport
        onSetMcmpEnabled={vi.fn()}
        onSetImageCodec={vi.fn()}
        onSetRawMediaTextTransport={vi.fn()}
      />
    );

    const dialog = screen.getByRole('dialog');
    const scroller = dialog.querySelector('.overflow-y-auto');
    expect(scroller, 'the feature list is not a scrolling region').not.toBeNull();
    expect(scroller?.textContent).toContain('Compress messages');

    // The whole panel scrolling would carry the heading and the close button off
    // the top of the screen, which is the bug being fixed here.
    const close = screen.getByRole('button', { name: /close/i });
    expect(scroller?.contains(close)).toBe(false);
    expect(dialog.textContent).toContain('Conversation features');
  });
});
