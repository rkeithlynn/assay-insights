/**
 * Load data when a screen opens, and again on request: loading, data and error state for
 * every screen (BUILD_SPEC.md section 11). State changes only when a request settles, and a
 * response that arrives after the screen closed is ignored.
 */

import { useCallback, useEffect, useState } from "react";

import { messageFor } from "./errors";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  reload: () => Promise<void>;
}

export function useLoad<T>(fetcher: () => Promise<T>): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(
    () =>
      fetcher().then(
        (result) => {
          setData(result);
          setError(null);
        },
        (caught: unknown) => {
          setError(messageFor(caught));
        },
      ),
    [fetcher],
  );

  useEffect(() => {
    const screen = { open: true };
    fetcher().then(
      (result) => {
        if (!screen.open) return;
        setData(result);
        setError(null);
      },
      (caught: unknown) => {
        if (screen.open) setError(messageFor(caught));
      },
    );
    return () => {
      screen.open = false;
    };
  }, [fetcher]);

  return { data, error, reload };
}
