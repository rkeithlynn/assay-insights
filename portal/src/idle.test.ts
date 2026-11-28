import { describe, expect, it, vi } from "vitest";

import { formatCountdown, inOneTab, SHARE_EVERY_MS, WARNING_MS, watchIdle } from "./idle";

const MINUTE = 60_000;
const TIMEOUT = 30 * MINUTE;

class FakeChannel {
  static all: FakeChannel[] = [];
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  sent: unknown[] = [];
  constructor() {
    FakeChannel.all.push(this);
  }
  postMessage(message: unknown) {
    this.sent.push(message);
    for (const other of FakeChannel.all) if (other !== this) other.onmessage?.({ data: message } as MessageEvent<unknown>);
  }
  close() {
    FakeChannel.all = FakeChannel.all.filter((c) => c !== this);
  }
}

function tab(clock: { now: number }) {
  const target = new EventTarget();
  const timers: (() => void)[] = [];
  const events = { warn: [] as number[], active: 0, timeout: 0 };
  const watch = watchIdle({
    timeoutMs: TIMEOUT,
    onWarn: (deadline) => events.warn.push(deadline),
    onActive: () => (events.active += 1),
    onTimeout: () => (events.timeout += 1),
    now: () => clock.now,
    target: {
      addEventListener: target.addEventListener.bind(target),
      removeEventListener: target.removeEventListener.bind(target),
      setInterval: ((fn: () => void) => timers.push(fn)) as unknown as Window["setInterval"],
      clearInterval: (() => timers.splice(0)) as unknown as Window["clearInterval"],
    },
    channel: () => new FakeChannel(),
  });
  const tick = () => timers.forEach((fn) => fn());
  const act = (name = "pointerdown") => target.dispatchEvent(new Event(name));
  return { watch, events, tick, act };
}

describe("watchIdle", () => {
  it("warns 2 minutes before the timeout, then signs out once", () => {
    FakeChannel.all = [];
    const clock = { now: 0 };
    const t = tab(clock);
    clock.now = TIMEOUT - WARNING_MS - 1;
    t.tick();
    expect(t.events.warn).toEqual([]);
    clock.now = TIMEOUT - WARNING_MS;
    t.tick();
    t.tick();
    expect(t.events.warn).toEqual([TIMEOUT]);
    clock.now = TIMEOUT;
    t.tick();
    t.tick();
    expect(t.events.timeout).toBe(1);
  });

  it("counts clicks, keys, wheel, touch and scroll as activity; a warning then goes away", () => {
    FakeChannel.all = [];
    const clock = { now: 0 };
    const t = tab(clock);
    clock.now = TIMEOUT - MINUTE;
    t.tick();
    expect(t.events.warn).toHaveLength(1);
    t.act("keydown");
    expect(t.events.active).toBe(1);
    for (const name of ["wheel", "touchstart", "scroll", "pointerdown"]) {
      clock.now += TIMEOUT - MINUTE;
      t.tick();
      t.act(name);
    }
    clock.now += TIMEOUT - 1;
    t.tick();
    expect(t.events.timeout).toBe(0);
    // Moving the pointer is not activity.
    t.act("pointermove");
    clock.now += 1;
    t.tick();
    expect(t.events.timeout).toBe(1);
  });

  it("shares activity with the other tabs, at most every 15 seconds", () => {
    FakeChannel.all = [];
    const clock = { now: 0 };
    const busy = tab(clock);
    const quiet = tab(clock);
    clock.now = SHARE_EVERY_MS;
    busy.act();
    clock.now = SHARE_EVERY_MS + 1000;
    busy.act(); // not shared: too soon
    clock.now = SHARE_EVERY_MS + TIMEOUT - 1;
    quiet.tick();
    expect(quiet.events.timeout).toBe(0);
    clock.now = SHARE_EVERY_MS + TIMEOUT;
    quiet.tick();
    busy.tick();
    expect(quiet.events.timeout).toBe(1);
    expect(busy.events.timeout).toBe(0); // its own last click was a second later
  });

  it("stops: no more checks after stop()", () => {
    FakeChannel.all = [];
    const clock = { now: 0 };
    const t = tab(clock);
    t.watch.stop();
    clock.now = TIMEOUT * 2;
    t.tick();
    expect(t.events.timeout).toBe(0);
  });

  it("touch() counts as activity (the warning's Stay signed in)", () => {
    FakeChannel.all = [];
    const clock = { now: 0 };
    const t = tab(clock);
    clock.now = TIMEOUT - MINUTE;
    t.tick();
    t.watch.touch();
    expect(t.events.active).toBe(1);
    clock.now = 2 * TIMEOUT - MINUTE - 1;
    t.tick();
    expect(t.events.timeout).toBe(0);
  });
});

describe("inOneTab", () => {
  it("signs out only in the tab that takes the lock", async () => {
    let held = false;
    const locks = {
      request: vi.fn(async (_name: string, _options: unknown, callback: (lock: unknown) => Promise<void>) => {
        if (held) return callback(null);
        held = true;
        await callback({});
      }),
    } as unknown as Pick<LockManager, "request">;
    const signOut = vi.fn(() => Promise.resolve());
    expect(await inOneTab(signOut, locks)).toBe(true);
    expect(await inOneTab(signOut, locks)).toBe(false);
    expect(signOut).toHaveBeenCalledTimes(1);
  });

  it("signs out anyway without Web Locks", async () => {
    const signOut = vi.fn(() => Promise.resolve());
    expect(await inOneTab(signOut, undefined)).toBe(true);
    expect(signOut).toHaveBeenCalledTimes(1);
  });
});

describe("formatCountdown", () => {
  it("shows minutes and seconds, never below zero", () => {
    expect(formatCountdown(WARNING_MS)).toBe("2:00");
    expect(formatCountdown(61_000)).toBe("1:01");
    expect(formatCountdown(-5)).toBe("0:00");
  });
});
