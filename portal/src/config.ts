/**
 * The portal's runtime configuration, `/config.json`, written by the Pulumi program
 * (infra/assay_infra/edge.py). The code holds no pool IDs, client IDs or hostnames.
 *
 * One app serves both hosts: the host it is loaded from decides which user pool it signs in
 * against (BUILD_SPEC.md section 11).
 */

export type PoolKind = "tenant" | "platform";

export interface PoolConfig {
  userPoolId: string;
  clientId: string;
  loginUrl: string;
}

export interface PortalConfig {
  region: string;
  hosts: { tenant: string; admin: string };
  pools: Record<PoolKind, PoolConfig>;
  /** Sign out after this many minutes without activity (stack config portalIdleTimeoutMinutes). */
  idleTimeoutMinutes: number;
}

// Used only while a deploy has published the new portal before its new config.json.
export const DEFAULT_IDLE_MINUTES = 30;
const IDLE_MINUTES_RANGE = [5, 480] as const;

function idleMinutes(value: unknown): number {
  if (value === undefined) return DEFAULT_IDLE_MINUTES;
  const [low, high] = IDLE_MINUTES_RANGE;
  if (typeof value !== "number" || !Number.isInteger(value) || value < low || value > high) {
    throw new Error("config.json: idleTimeoutMinutes is not a whole number of minutes from 5 to 480");
  }
  return value;
}

export interface ActivePool extends PoolConfig {
  kind: PoolKind;
  region: string;
  host: string;
  idleTimeoutMinutes: number;
}

function isPool(value: unknown): value is PoolConfig {
  if (typeof value !== "object" || value === null) return false;
  const pool = value as Record<string, unknown>;
  return (
    typeof pool.userPoolId === "string" &&
    typeof pool.clientId === "string" &&
    typeof pool.loginUrl === "string" &&
    pool.loginUrl.startsWith("https://")
  );
}

export function parseConfig(raw: unknown): PortalConfig {
  if (typeof raw !== "object" || raw === null) throw new Error("config.json is not an object");
  const config = raw as Record<string, unknown>;
  const hosts = config.hosts as Record<string, unknown> | undefined;
  const pools = config.pools as Record<string, unknown> | undefined;
  if (
    typeof config.region !== "string" ||
    typeof hosts?.tenant !== "string" ||
    typeof hosts.admin !== "string" ||
    !isPool(pools?.tenant) ||
    !isPool(pools.platform)
  ) {
    throw new Error("config.json is incomplete");
  }
  return {
    region: config.region,
    hosts: { tenant: hosts.tenant, admin: hosts.admin },
    pools: { tenant: pools.tenant, platform: pools.platform },
    idleTimeoutMinutes: idleMinutes(config.idleTimeoutMinutes),
  };
}

/** The admin host signs in against the platform pool; every other host the tenant pool. */
export function poolFor(config: PortalConfig, host: string): ActivePool {
  const kind: PoolKind = host.toLowerCase() === config.hosts.admin.toLowerCase() ? "platform" : "tenant";
  return { ...config.pools[kind], kind, region: config.region, host, idleTimeoutMinutes: config.idleTimeoutMinutes };
}

export async function loadConfig(): Promise<PortalConfig> {
  const response = await fetch("/config.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`config.json: HTTP ${response.status}`);
  return parseConfig(await response.json());
}
