import { afterEach, describe, expect, it, vi } from "vitest";

import { SIGN_OUT_CHANNEL, signOutChannel } from "./signout";

afterEach(() => {
  vi.unstubAllGlobals();
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

describe("sign-out channel", () => {
  it("reaches every other tab, never the tab that sent it", async () => {
    const sender = vi.fn();
    const receiver = vi.fn();
    const a = signOutChannel(sender);
    const b = signOutChannel(receiver);
    try {
      a.announce();
      await vi.waitFor(() => expect(receiver).toHaveBeenCalledOnce());
      await settle();
      expect(sender).not.toHaveBeenCalled();
    } finally {
      a.close();
      b.close();
    }
  });

  it("carries nothing but the fact of the sign-out and its reason", async () => {
    const listener = new BroadcastChannel(SIGN_OUT_CHANNEL);
    const heard: unknown[] = [];
    listener.onmessage = (event: MessageEvent<unknown>) => heard.push(event.data);
    const tab = signOutChannel(() => undefined);
    try {
      tab.announce();
      tab.announce("idle_timeout");
      await vi.waitFor(() =>
        expect(heard).toEqual([
          { type: "signed-out", reason: "user" },
          { type: "signed-out", reason: "idle_timeout" },
        ]),
      );
    } finally {
      tab.close();
      listener.close();
    }
  });

  it("does nothing, and fails nothing, where BroadcastChannel is missing", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    const tab = signOutChannel(() => undefined);
    expect(() => tab.announce()).not.toThrow();
    expect(() => tab.close()).not.toThrow();
  });

  it("does not fail a sign-out once the channel is closed", () => {
    const tab = signOutChannel(() => undefined);
    tab.close();
    expect(() => tab.announce()).not.toThrow();
  });
});
