// TanStack Query hooks for the comments API:
// POST/GET/PATCH/DELETE /v1/sessions/{id}/comments
// POST /v1/sessions/{id}/comments/send
//
// `useSendCommentsToAgent` calls `useChatStore.send()` directly on success
// so the message is submitted immediately without requiring a manual send.
// It requires a non-null `agentId`; for the FileViewer case where an
// agent may not be registered, see `CommentSenderProvider` /
// `useOptionalCommentSender` in `CommentSenderContext.tsx`.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { useChatStore } from "@/store/chatStore";

export interface Comment {
  id: string;
  conversation_id: string;
  path: string;
  /** 0-based absolute character offset (inclusive) within the file. */
  start_index: number;
  /** 0-based absolute character offset (exclusive) within the file. */
  end_index: number;
  body: string;
  status: "draft" | "addressed";
  created_at: number;
  /**
   * Unix **microseconds** of the last body/status mutation (set at creation
   * when never edited). Used for change detection, not display; divide by
   * 1000 before passing to `new Date()` if it's ever rendered.
   */
  updated_at: number;
  anchor_content: string | null;
  created_by: string | null;
  /** 1-based first line of the anchor; absent on comments made before line anchoring. */
  start_line?: number | null;
  /** 1-based last line of the anchor (inclusive). */
  end_line?: number | null;
  /** Diff side: "after" (current file) or "before" (a removed line). */
  side?: "before" | "after" | null;
}

/**
 * 1-based line numbers of a character range within `text`.
 *
 * @param text - The content the offsets index into (the "after" side).
 * @param start - 0-based start offset (inclusive).
 * @param end - 0-based end offset (exclusive).
 * @returns `{start_line, end_line}`; a range ending right after a newline
 *   does not count the following empty line.
 */
export function lineRange(
  text: string,
  start: number,
  end: number,
): { start_line: number; end_line: number } {
  const lineAt = (offset: number) => {
    let line = 1;
    for (let i = 0; i < Math.min(offset, text.length); i++) if (text[i] === "\n") line++;
    return line;
  };
  const start_line = lineAt(start);
  const last = end > start ? end - 1 : start;
  return { start_line, end_line: Math.max(start_line, lineAt(last)) };
}

// ── Query helpers ────────────────────────────────────────────────────────────

// Exported for `useCommentInbox`, which mounts the same queries for
// every session with comments — sharing this key keeps its cache and
// the SessionUpdatesProvider fingerprint invalidation in sync.
export function commentsQueryKey(sessionId: string, path?: string) {
  return path ? ["comments", sessionId, path] : ["comments", sessionId];
}

export async function fetchComments(sessionId: string, path?: string): Promise<Comment[]> {
  const url = path
    ? `/v1/sessions/${encodeURIComponent(sessionId)}/comments?path=${encodeURIComponent(path)}`
    : `/v1/sessions/${encodeURIComponent(sessionId)}/comments`;
  const res = await authenticatedFetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Comment[];
}

// ── Hooks ────────────────────────────────────────────────────────────────────

/**
 * Fetch comments for a session, optionally filtered to a single file.
 *
 * Disabled when `sessionId` is falsy.
 */
export function useComments(sessionId: string | undefined, path?: string) {
  return useQuery({
    queryKey: commentsQueryKey(sessionId ?? "", path),
    queryFn: () => fetchComments(sessionId!, path),
    enabled: !!sessionId,
    staleTime: 2_000,
  });
}

/** POST /v1/sessions/{id}/comments */
export function useAddComment(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      path: string;
      start_index: number;
      end_index: number;
      body: string;
      anchor_content?: string | null;
      start_line?: number;
      end_line?: number;
      side?: "before" | "after";
    }) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as Comment;
    },
    onSuccess: (comment) => {
      // Invalidate both the full-session list and the per-file list
      // so the sidebar and any per-file views refresh.
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId, comment.path],
      });
    },
  });
}

/** DELETE /v1/sessions/{id}/comments/{commentId} */
export function useDeleteComment(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (commentId: string) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
    },
  });
}

/** PATCH /v1/sessions/{id}/comments/{commentId} */
export function useUpdateComment(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { commentId: string; status?: string; body?: string }) => {
      const { commentId, ...fields } = payload;
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(fields),
        },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as Comment;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
    },
  });
}

/**
 * POST /v1/sessions/{id}/comments/send
 *
 * Requires a non-null `agentId` — without an agent there is nowhere to
 * dispatch the formatted message. Callers that may not have an agent
 * should mount this hook only inside `CommentSenderProvider`, which
 * skips the mutation entirely when no agent is registered. Consumers
 * then read the sender via `useOptionalCommentSender()` and treat
 * `null` as "no agent, hide the button".
 */
export function useSendCommentsToAgent(sessionId: string, agentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      comment_ids: string[];
      instruction?: string;
      /** Deliver server-side into this session (the comments' own or an ancestor). */
      target_session_id?: string;
    }) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments/send`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as {
        formatted_message: string;
        sent_comment_ids: string[];
        /** Set when the server already delivered the message. */
        delivered_to?: string;
      };
    },
    onSuccess: (data) => {
      if (!data.delivered_to) void useChatStore.getState().send(data.formatted_message, agentId);
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
    },
  });
}
