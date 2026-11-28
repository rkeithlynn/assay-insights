/**
 * Idle sign-out (owner decision, 2026-11-24). After `idleTimeoutMinutes` (stack config, in
 * /config.json) without activity in any of the portal's tabs in this browser, the portal
 * signs the user out the way the Sign out button does, other tabs included, and records it
 * with the reason `idle_timeout`. A warning shows 2 minutes before.
 *
 * - Activity is a click, a key, a wheel turn, a touch or a scroll in any tab of the same
 *   portal. Each tab tells the others on a BroadcastChannel, at most every 15 seconds, so
 *   every tab keeps the same last-activity time. Moving the pointer alone is not activity.
 * - The check runs every second. A browser slows timers in a hidden tab (to about once a
 *   minute after a few minutes), so a hidden tab can sign out up to about a minute late;
 *   any visible tab of the portal keeps time exactly.
 * - Only one tab signs out and records it: the one that takes the Web Lock first. The others
 *   hear its sign-out on the sign-out channel, and say why on their sign-in screen.
 * - Nothing is stored: the last-activity time lives in each tab's memory only.
 */

export const IDLE_CHANNEL = "assay-portal-activity";
export const IDLE_SIGN_OUT_LOCK = "assay-portal-idle-sign-out";
export const WARNING_MS = 2 * 60_000;
export const SHARE_EVERY_MS = 15_000;
export const CHECK_EVERY_MS = 1_000;
export const ACTIVITY_EVENTS = ["pointerdown", "keydown", "wheel", "touchstart", "scroll"] as const;

const ACTIVITY = "activity";

export interface IdleOptions {
  timeoutMs: number;
  /** The warning is due: the user is signed out at `deadline` (ms since the epoch). */
  onWarn: (deadline: number) => void;
  /** Activity again after a warning: hide it. */
  onActive: () => void;
  /** The timeout passed. Called once. */
  onTimeout: () => void;
  now?: () => number;
  target?: Pick<Window, "addEventListener" | "removeEventListener" | "setInterval" | "clearInterval">;
  channel?: () => { postMessage(message: unknown): void; onmessage: ((event: MessageEvent<unknown>) => void) | null; close(): void } | null;
}

export interface IdleWatch {
  /** Count as activity now (the warning's "Stay signed in"). */
  touch(): void;
  stop(): void;
}

function broadcast(): ReturnType<NonNullable<IdleOptions["channel"]>> {
  return typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(IDLE_CHANNEL);
}

export function watchIdle(options: IdleOptions): IdleWatch {
  const now = options.now ?? (() => Date.now());
  const target = options.target ?? window;
  const channel = (options.channel ?? broadcast)();
  let last = now();
  let shared = 0;
  let warned = false;
  let done = false;

  const share = () => {
    if (!channel || last - shared < SHARE_EVERY_MS) return;
    shared = last;
    try {
      channel.postMessage({ type: ACTIVITY, at: last });
    } catch {
      // A closed channel: the other tabs keep their own time.
    }
  };

  const active = (at: number) => {
    if (done || at <= last) return;
    last = at;
    if (warned) {
      warned = false;
      options.onActive();
    }
  };

  const onLocal = () => {
    active(now());
    share();
  };

  if (channel) {
    channel.onmessage = (event) => {
      const data = event.data as { type?: unknown; at?: unknown } | null;
      // Another tab's activity; never later than now (a clock cannot run ahead of this one).
      if (data?.type === ACTIVITY && typeof data.at === "number") active(Math.min(data.at, now()));
    };
  }
  for (const name of ACTIVITY_EVENTS) target.addEventListener(name, onLocal, { capture: true, passive: true });

  const check = () => {
    if (done) return;
    const idle = now() - last;
    if (idle >= options.timeoutMs) {
      done = true;
      stop();
      options.onTimeout();
    } else if (idle >= options.timeoutMs - WARNING_MS && !warned) {
      warned = true;
      options.onWarn(last + options.timeoutMs);
    }
  };
  const timer = target.setInterval(check, CHECK_EVERY_MS);

  function stop() {
    target.clearInterval(timer);
    for (const name of ACTIVITY_EVENTS) target.removeEventListener(name, onLocal, { capture: true });
    channel?.close();
  }

  return {
    touch: onLocal,
    stop: () => {
      done = true;
      stop();
    },
  };
}

/**
 * Run `signOut` in exactly one tab: the first to take the lock. Returns whether this tab ran
 * it. Where Web Locks is missing, every tab that times out runs it.
 */
export async function inOneTab(
  signOut: () => Promise<void>,
  locks: Pick<LockManager, "request"> | undefined = typeof navigator === "undefined" ? undefined : navigator.locks,
): Promise<boolean> {
  if (!locks) {
    await signOut();
    return true;
  }
  let ran = false;
  await locks.request(IDLE_SIGN_OUT_LOCK, { ifAvailable: true }, async (lock) => {
    if (!lock) return;
    ran = true;
    await signOut();
  });
  return ran;
}

export function formatCountdown(ms: number): string {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
