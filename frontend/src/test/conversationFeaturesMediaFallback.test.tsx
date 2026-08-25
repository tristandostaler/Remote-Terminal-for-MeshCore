/**
 * The "send media as text" switch in the conversation features panel.
 *
 * It exists because a node whose firmware has no CMD_SEND_RAW_DATA cannot open a
 * received photo or voice note at all. On by default, so the tests care most
 * about the two ways it can be wrong: absent for a channel (where the setting
 * has nothing to act on), and silently defaulting to off.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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

function status(): AeicStatus {
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

const CONTACT_ID = 'aa'.repeat(32);

describe('media text fallback switch', () => {
  const onSetRawMediaTextFallback = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAeicStatus).mockResolvedValue(status());
  });

  function renderModal(
    overrides: {
      rawMediaTextFallback?: boolean;
      conversationType?: 'contact' | 'channel';
      onSetRawMediaTextFallback?: typeof onSetRawMediaTextFallback;
    } = {}
  ) {
    return render(
      <ConversationFeaturesModal
        open
        onClose={vi.fn()}
        conversationType={overrides.conversationType ?? 'contact'}
        conversationId={CONTACT_ID}
        conversationName="Alice"
        mcmpEnabled={false}
        mcmpVersion={2}
        imageCodec="ie4"
        rawMediaTextFallback={overrides.rawMediaTextFallback ?? true}
        onSetMcmpEnabled={vi.fn()}
        onSetImageCodec={vi.fn()}
        onSetRawMediaTextFallback={
          'onSetRawMediaTextFallback' in overrides
            ? overrides.onSetRawMediaTextFallback
            : onSetRawMediaTextFallback
        }
      />
    );
  }

  it('shows the switch on for a contact by default', () => {
    renderModal();

    // The label names the "disable" action, which is how the Switch reports being on.
    expect(screen.getByLabelText('Disable the media text fallback')).toBeInTheDocument();
  });

  it('turns the fallback off through the callback', () => {
    renderModal({ rawMediaTextFallback: true });

    fireEvent.click(screen.getByLabelText('Disable the media text fallback'));

    expect(onSetRawMediaTextFallback).toHaveBeenCalledWith(CONTACT_ID, false);
  });

  it('turns the fallback back on through the callback', () => {
    renderModal({ rawMediaTextFallback: false });

    fireEvent.click(screen.getByLabelText('Enable the media text fallback'));

    expect(onSetRawMediaTextFallback).toHaveBeenCalledWith(CONTACT_ID, true);
  });

  it('warns what being off means, and only when it is off', () => {
    renderModal({ rawMediaTextFallback: false });
    expect(screen.getByText(/will fail instead of falling back/)).toBeInTheDocument();

    renderModal({ rawMediaTextFallback: true });
    expect(screen.queryAllByText(/will fail instead of falling back/)).toHaveLength(1);
  });

  it('is absent for a channel', () => {
    /*
     * The raw media transport is contact-directed even for a picture announced on
     * a channel: the fetch request goes to the sender's contact, so that contact's
     * setting governs. A channel switch would be dead UI.
     */
    renderModal({ conversationType: 'channel' });

    expect(screen.queryByLabelText('Disable the media text fallback')).not.toBeInTheDocument();
  });

  it('is absent when no handler is wired', () => {
    renderModal({ onSetRawMediaTextFallback: undefined });

    expect(screen.queryByLabelText('Disable the media text fallback')).not.toBeInTheDocument();
  });
});
