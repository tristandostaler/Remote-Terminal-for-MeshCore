import { useCallback, type MutableRefObject, type RefObject } from 'react';
import { api } from '../api';
import { toast } from '../components/ui/sonner';
import type { MessageInputHandle } from '../components/MessageInput';
import type { Channel, Contact, Conversation, Message, PathDiscoveryResponse } from '../types';
import { mergeContactIntoList } from '../utils/contactMerge';

interface UseConversationActionsArgs {
  activeConversation: Conversation | null;
  activeConversationRef: MutableRefObject<Conversation | null>;
  setContacts: React.Dispatch<React.SetStateAction<Contact[]>>;
  setChannels: React.Dispatch<React.SetStateAction<Channel[]>>;
  observeMessage: (msg: Message) => { added: boolean; activeConversation: boolean };
  removeMessageFromView: (messageId: number) => void;
  applyMessageReactions: (messageId: number, reactions: Record<string, number>) => void;
  messageInputRef: RefObject<MessageInputHandle | null>;
}

interface UseConversationActionsResult {
  handleSendMessage: (text: string) => Promise<void>;
  handleResendChannelMessage: (messageId: number, newTimestamp?: boolean) => Promise<void>;
  handleRetryMessage: (message: Message, newTimestamp?: boolean) => Promise<void>;
  handleCancelMessage: (message: Message) => Promise<void>;
  handleDeleteMessage: (message: Message) => Promise<void>;
  handleReactToMessage: (message: Message, emoji: string) => Promise<void>;
  handleSetChannelFloodScopeOverride: (
    channelKey: string,
    floodScopeOverride: string
  ) => Promise<void>;
  handleSetChannelPathHashModeOverride: (
    channelKey: string,
    pathHashModeOverride: number | null
  ) => Promise<void>;
  handleSenderClick: (sender: string) => void;
  handleTrace: () => Promise<void>;
  handlePathDiscovery: (publicKey: string) => Promise<PathDiscoveryResponse>;
}

export function useConversationActions({
  activeConversation,
  activeConversationRef,
  setContacts,
  setChannels,
  observeMessage,
  removeMessageFromView,
  applyMessageReactions,
  messageInputRef,
}: UseConversationActionsArgs): UseConversationActionsResult {
  const mergeChannelIntoList = useCallback(
    (updated: Channel) => {
      setChannels((prev) => {
        const existingIndex = prev.findIndex((channel) => channel.key === updated.key);
        if (existingIndex === -1) {
          return [...prev, updated].sort((a, b) => a.name.localeCompare(b.name));
        }
        const next = [...prev];
        next[existingIndex] = updated;
        return next;
      });
    },
    [setChannels]
  );

  const handleSendMessage = useCallback(
    async (text: string) => {
      if (!activeConversation) return;

      const conversationId = activeConversation.id;
      const sent =
        activeConversation.type === 'channel'
          ? await api.sendChannelMessage(activeConversation.id, text)
          : await api.sendDirectMessage(activeConversation.id, text);

      if (activeConversationRef.current?.id === conversationId) {
        observeMessage(sent);
      }
    },
    [activeConversation, activeConversationRef, observeMessage]
  );

  const handleResendChannelMessage = useCallback(
    async (messageId: number, newTimestamp?: boolean) => {
      try {
        const resent = await api.resendChannelMessage(messageId, newTimestamp);
        const resentMessage = resent.message;
        if (
          newTimestamp &&
          resentMessage &&
          activeConversationRef.current?.type === 'channel' &&
          activeConversationRef.current.id === resentMessage.conversation_key
        ) {
          observeMessage(resentMessage);
        }
        toast.success(newTimestamp ? 'Message resent with new timestamp' : 'Message resent');
      } catch (err) {
        toast.error('Failed to resend', {
          description: err instanceof Error ? err.message : 'Unknown error',
        });
      }
    },
    [activeConversationRef, observeMessage]
  );

  const handleRetryMessage = useCallback(
    async (message: Message, newTimestamp?: boolean) => {
      try {
        const result = await api.retryMessage(message.id, newTimestamp);
        // A channel retry under a fresh timestamp lands as a new row; a direct
        // retry updates the existing one, which arrives over the WebSocket.
        const retried = result.message;
        if (
          retried &&
          retried.id !== message.id &&
          activeConversationRef.current?.id === retried.conversation_key
        ) {
          observeMessage(retried);
        }
        toast.success('Retrying send');
      } catch (err) {
        toast.error('Failed to retry', {
          description: err instanceof Error ? err.message : 'Unknown error',
        });
      }
    },
    [activeConversationRef, observeMessage]
  );

  const handleCancelMessage = useCallback(async (message: Message) => {
    try {
      const result = await api.cancelMessage(message.id);
      toast.success(
        result.stopped_pending_sends
          ? 'Cancelled the remaining attempts'
          : 'Nothing left to send — the message was already finished'
      );
    } catch (err) {
      toast.error('Failed to cancel', {
        description: err instanceof Error ? err.message : 'Unknown error',
      });
    }
  }, []);

  const handleDeleteMessage = useCallback(
    async (message: Message) => {
      try {
        await api.deleteMessage(message.id);
        // Drop it locally too rather than waiting for our own broadcast, so the
        // row disappears the moment the user confirms.
        removeMessageFromView(message.id);
        toast.success('Message deleted');
      } catch (err) {
        toast.error('Failed to delete', {
          description: err instanceof Error ? err.message : 'Unknown error',
        });
      }
    },
    [removeMessageFromView]
  );

  const handleReactToMessage = useCallback(
    async (message: Message, emoji: string) => {
      try {
        const updated = await api.reactToMessage(message.id, emoji);
        // The message_reaction broadcast covers other clients; applying the
        // response locally makes the chip appear without the round trip.
        if (updated.reactions) {
          applyMessageReactions(updated.id, updated.reactions);
        }
      } catch (err) {
        toast.error('Failed to send reaction', {
          description: err instanceof Error ? err.message : 'Unknown error',
        });
      }
    },
    [applyMessageReactions]
  );

  const handleSetChannelFloodScopeOverride = useCallback(
    async (channelKey: string, floodScopeOverride: string) => {
      try {
        const updated = await api.setChannelFloodScopeOverride(channelKey, floodScopeOverride);
        mergeChannelIntoList(updated);
        toast.success(
          updated.flood_scope_override ? 'Regional override saved' : 'Regional override cleared'
        );
      } catch (err) {
        toast.error('Failed to update regional override', {
          description: err instanceof Error ? err.message : 'Unknown error',
        });
      }
    },
    [mergeChannelIntoList]
  );

  const handleSetChannelPathHashModeOverride = useCallback(
    async (channelKey: string, pathHashModeOverride: number | null) => {
      try {
        const updated = await api.setChannelPathHashModeOverride(channelKey, pathHashModeOverride);
        mergeChannelIntoList(updated);
        toast.success(
          updated.path_hash_mode_override != null
            ? 'Path hop width override saved'
            : 'Path hop width override cleared'
        );
      } catch (err) {
        toast.error('Failed to update path hop width override', {
          description: err instanceof Error ? err.message : 'Unknown error',
        });
      }
    },
    [mergeChannelIntoList]
  );

  const handleSenderClick = useCallback(
    (sender: string) => {
      messageInputRef.current?.appendText(`@[${sender}] `);
    },
    [messageInputRef]
  );

  const handleTrace = useCallback(async () => {
    if (!activeConversation || activeConversation.type !== 'contact') return;
    toast('Trace started...');
    try {
      const result = await api.requestTrace(activeConversation.id);
      const parts: string[] = [];
      if (result.remote_snr !== null) parts.push(`Remote SNR: ${result.remote_snr.toFixed(1)} dB`);
      if (result.local_snr !== null) parts.push(`Local SNR: ${result.local_snr.toFixed(1)} dB`);
      const detail = parts.join(', ');
      toast.success(detail ? `Trace complete! ${detail}` : 'Trace complete!');
    } catch (err) {
      toast.error('Trace failed', {
        description: err instanceof Error ? err.message : 'Unknown error',
      });
    }
  }, [activeConversation]);

  const handlePathDiscovery = useCallback(
    async (publicKey: string) => {
      const result = await api.requestPathDiscovery(publicKey);
      setContacts((prev) => mergeContactIntoList(prev, result.contact));
      return result;
    },
    [setContacts]
  );

  return {
    handleSendMessage,
    handleResendChannelMessage,
    handleRetryMessage,
    handleCancelMessage,
    handleDeleteMessage,
    handleReactToMessage,
    handleSetChannelFloodScopeOverride,
    handleSetChannelPathHashModeOverride,
    handleSenderClick,
    handleTrace,
    handlePathDiscovery,
  };
}
