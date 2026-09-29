"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Reconnection-aware SSE subscription for one incident.
 *
 * The browser's built-in EventSource retry has three problems for an SRE
 * console: it exposes no connection state, it retries forever with no bound,
 * and it cannot tell the UI that the *backend* restarted (which invalidates
 * the in-memory view and requires a full resync rather than a gap-fill).
 *
 * This hook owns the connection explicitly so it can:
 *
 * - show `connecting | live | reconnecting | offline` in the UI;
 * - back off exponentially and give up into a visible OFFLINE state instead
 *   of silently retrying forever;
 * - detect a backend restart from the `ready` frame's `instance_id` and ask
 *   the caller to resync the whole incident;
 * - deduplicate replayed timeline events by `seq` so a reconnect never
 *   double-applies an event the client already rendered.
 */

export type StreamStatus = "connecting" | "live" | "reconnecting" | "offline";

export interface TimelineEventPayload {
  id: string;
  incident_id: string;
  seq: number;
  ts: string;
  phase: string;
  title: string;
  detail: string;
  actor: string;
  meta: Record<string, unknown>;
}

export interface ReadyPayload {
  type: "ready";
  topic: string;
  instance_id: string;
  resumed_from: number | null;
  replayed: boolean;
  server_time: number;
}

export interface UseIncidentStreamOptions {
  incidentId: string;
  apiBase?: string;
  enabled?: boolean;
  /** Called for each *new* timeline event (replayed duplicates are dropped). */
  onTimelineEvent?: (event: TimelineEventPayload) => void;
  /** Called when the backend instance changes - the view must fully resync. */
  onBackendRestart?: (instanceId: string) => void;
  /** Consecutive failures before the stream gives up into OFFLINE. */
  maxRetries?: number;
}

const BACKOFF_STEPS_MS = [1000, 2000, 4000, 8000, 15000];

export function useIncidentStream({
  incidentId,
  apiBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8765",
  enabled = true,
  onTimelineEvent,
  onBackendRestart,
  maxRetries = 5,
}: UseIncidentStreamOptions) {
  const [status, setStatus] = useState<StreamStatus>("connecting");

  const sourceRef = useRef<EventSource | null>(null);
  const retryCountRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disposedRef = useRef(false);
  const lastSeqRef = useRef(-1);
  const instanceIdRef = useRef<string | null>(null);

  // Callbacks are kept in refs so the effect never re-subscribes just because
  // the caller passed a new inline function.
  const onEventRef = useRef(onTimelineEvent);
  const onRestartRef = useRef(onBackendRestart);
  onEventRef.current = onTimelineEvent;
  onRestartRef.current = onBackendRestart;

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const close = useCallback(() => {
    clearTimer();
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }
  }, [clearTimer]);

  const connect = useCallback(() => {
    if (disposedRef.current || !incidentId) return;
    close();

    const url = `${apiBase}/api/incidents/${incidentId}/stream?replay=true`;
    const source = new EventSource(url);
    sourceRef.current = source;

    if (retryCountRef.current === 0) {
      setStatus("connecting");
    } else {
      setStatus("reconnecting");
    }

    source.onerror = () => {
      // The browser also retries automatically; closing here makes the retry
      // explicit so it is bounded, observable and deduplicated.
      source.close();
      sourceRef.current = null;
      if (disposedRef.current) return;

      retryCountRef.current += 1;
      if (retryCountRef.current > maxRetries) {
        setStatus("offline");
        return;
      }
      const delay = BACKOFF_STEPS_MS[Math.min(retryCountRef.current - 1, BACKOFF_STEPS_MS.length - 1)];
      setStatus("reconnecting");
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        connect();
      }, delay);
    };

    source.addEventListener("ready", (ev) => {
      let payload: ReadyPayload;
      try {
        payload = JSON.parse((ev as MessageEvent).data) as ReadyPayload;
      } catch {
        return;
      }
      retryCountRef.current = 0;
      setStatus("live");

      // A different instance means the API restarted: the in-memory view may
      // be stale, so the caller must reload rather than trust a gap-fill.
      if (instanceIdRef.current && payload.instance_id !== instanceIdRef.current) {
        onRestartRef.current?.(payload.instance_id);
      }
      instanceIdRef.current = payload.instance_id;
    });

    source.addEventListener("timeline", (ev) => {
      let payload: { type: string; event?: TimelineEventPayload };
      try {
        payload = JSON.parse((ev as MessageEvent).data) as { type: string; event?: TimelineEventPayload };
      } catch {
        return;
      }
      const event = payload.event;
      if (!event || typeof event.seq !== "number") return;
      // Replayed frames after a reconnect carry seqs already rendered.
      if (event.seq <= lastSeqRef.current) return;
      lastSeqRef.current = event.seq;
      onEventRef.current?.(event);
    });
  }, [apiBase, incidentId, close, maxRetries]);

  useEffect(() => {
    disposedRef.current = false;
    if (!enabled || !incidentId) {
      close();
      setStatus("connecting");
      return;
    }
    connect();
    return () => {
      disposedRef.current = true;
      close();
    };
  }, [enabled, incidentId, connect, close]);

  /** Manual retry from the OFFLINE state. */
  const retry = useCallback(() => {
    retryCountRef.current = 0;
    lastSeqRef.current = -1;
    connect();
  }, [connect]);

  return { status, retry };
}

export const STREAM_STATUS_LABEL: Record<StreamStatus, string> = {
  connecting: "Connecting",
  live: "Live",
  reconnecting: "Reconnecting",
  offline: "Offline",
};
