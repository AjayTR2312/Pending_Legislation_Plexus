import { useEffect, useState } from "react";
import type { Job } from "../types";
import { apiUrl } from "../api";

/**
 * Live job list over server-sent events.
 *
 * Replaces per-client polling: one persistent connection, and the server only
 * pushes when the payload actually changes. Falls back to a slow poll if the
 * stream cannot be established.
 */
export function useJobStream(): { jobs: Job[] | null; live: boolean } {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [live, setLive] = useState(false);

  useEffect(() => {
    let source: EventSource | null = null;
    let fallback: ReturnType<typeof setInterval> | null = null;
    let closed = false;

    const startFallback = () => {
      if (fallback) return;
      fallback = setInterval(async () => {
        try {
          const res = await fetch(apiUrl("/api/jobs"));
          if (res.ok) setJobs(await res.json());
        } catch {
          /* ignore transient failures */
        }
      }, 5000);
    };

    try {
      source = new EventSource(apiUrl("/api/stream/jobs"));
      source.addEventListener("jobs", (e) => {
        if (closed) return;
        setLive(true);
        try {
          setJobs(JSON.parse((e as MessageEvent).data));
        } catch {
          /* malformed frame — wait for the next one */
        }
      });
      source.onerror = () => {
        setLive(false);
        startFallback();
      };
    } catch {
      startFallback();
    }

    return () => {
      closed = true;
      source?.close();
      if (fallback) clearInterval(fallback);
    };
  }, []);

  return { jobs, live };
}
