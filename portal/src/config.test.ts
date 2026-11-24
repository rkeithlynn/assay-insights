import { describe, expect, it } from "vitest";

import { parseConfig, poolFor } from "./config";

const RAW = {
  region: "us-east-1",
  hosts: { tenant: "portal.example.test", admin: "admin.portal.example.test" },
  pools: {
    tenant: { userPoolId: "us-east-1_T", clientId: "tenant-client", loginUrl: "https://t.example.test" },
    platform: { userPoolId: "us-east-1_P", clientId: "platform-client", loginUrl: "https://p.example.test" },
  },
};

describe("config", () => {
  it("parses a complete config", () => {
    expect(parseConfig(RAW).pools.platform.clientId).toBe("platform-client");
  });

  it("refuses an incomplete config or a non-HTTPS login URL", () => {
    expect(() => parseConfig({ region: "us-east-1" })).toThrow();
    expect(() =>
      parseConfig({ ...RAW, pools: { ...RAW.pools, tenant: { ...RAW.pools.tenant, loginUrl: "http://x" } } }),
    ).toThrow();
  });

  it("the admin host signs in against the platform pool, every other host the tenant pool", () => {
    const config = parseConfig(RAW);
    expect(poolFor(config, "admin.portal.example.test").kind).toBe("platform");
    expect(poolFor(config, "ADMIN.portal.example.test").clientId).toBe("platform-client");
    expect(poolFor(config, "portal.example.test").kind).toBe("tenant");
    expect(poolFor(config, "anything.else").kind).toBe("tenant");
  });
});
