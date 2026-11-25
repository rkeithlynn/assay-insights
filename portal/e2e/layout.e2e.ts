/**
 * Rendered layout checks: every screen of both portals, in the real production build, at
 * 1280 and 1800 px wide (playwright.config.ts). Component tests run in jsdom, which has no
 * layout; these three defects passed them and were only visible in a browser:
 *
 * 1. Top banner: the product name cut to "Assay Insi..." and the mark too small to read.
 * 2. Row-action menus (and any dropdown) opened inside the table's scrolling container and
 *    were clipped by it.
 * 3. The left navigation far wider than its few short items.
 *
 * Each screen is also saved as a screenshot (`screens/` in the output directory), for a person
 * to look at.
 */

import { expect, type Locator, type Page, test, type TestInfo } from "@playwright/test";

import { open, type Persona, signIn } from "./fixtures";

interface Screen {
  id: string;
  persona: Persona;
  path: string;
  /** Text that shows once the screen has its data. */
  ready: string;
  /** Opens what the screen hides behind a click (a dialog, a detail panel). */
  setup?: (page: Page) => Promise<void>;
  /** The screen has the shell (banner and navigation). */
  shell?: boolean;
  /** The Audit history table: Time and Actor on one line. */
  audit?: boolean;
}

const SCREENS: Screen[] = [
  { id: "tenant-admin-home", persona: "tenant-admin", path: "/", ready: "Welcome, Ada Lovelace" },
  { id: "tenant-admin-users", persona: "tenant-admin", path: "/users", ready: "User Number 9" },
  {
    id: "tenant-admin-users-invite",
    persona: "tenant-admin",
    path: "/users",
    ready: "User Number 9",
    setup: async (page) => {
      await page.getByRole("button", { name: "Invite user" }).click();
      await page.getByRole("dialog").waitFor();
    },
  },
  { id: "tenant-admin-settings", persona: "tenant-admin", path: "/settings", ready: "Claude Sonnet" },
  { id: "tenant-admin-upload", persona: "tenant-admin", path: "/upload", ready: "garcia-package.json" },
  {
    // Close with a rejected companion file asks what to do with its PDF (owner decision A1).
    id: "tenant-admin-upload-rejected-companion",
    persona: "tenant-admin",
    path: "/upload",
    ready: "garcia-package.json",
    setup: async (page) => {
      await page.getByRole("button", { name: "Close batch" }).click();
      await page.getByRole("dialog").getByText("Close without the companion file").waitFor();
    },
  },
  { id: "tenant-admin-ingestion", persona: "tenant-admin", path: "/ingestion", ready: "application-package-1.pdf" },
  {
    id: "tenant-admin-ingestion-detail",
    persona: "tenant-admin",
    path: "/ingestion",
    ready: "application-package-1.pdf",
    setup: async (page) => {
      await page.getByRole("link", { name: "application-package-1.pdf" }).click();
      await page.getByRole("dialog").getByText("Fingerprint", { exact: true }).waitFor();
    },
  },
  { id: "tenant-user-home", persona: "tenant-user", path: "/", ready: "Welcome, Ada Lovelace" },
  { id: "tenant-user-ingestion", persona: "tenant-user", path: "/ingestion", ready: "application-package-1.pdf" },
  { id: "master-admin-tenants", persona: "master-admin", path: "/tenants", ready: "Northwind Credit" },
  {
    id: "master-admin-tenant-detail",
    persona: "master-admin",
    path: "/tenants",
    ready: "Northwind Credit",
    setup: async (page) => {
      await page.getByRole("radio", { name: "Infinity Loans" }).check();
      await page.getByText("user1@example.test").first().waitFor();
    },
  },
  {
    id: "master-admin-add-tenant",
    persona: "master-admin",
    path: "/tenants",
    ready: "Northwind Credit",
    setup: async (page) => {
      await page.getByRole("button", { name: /add tenant/i }).click();
      await page.getByRole("dialog").waitFor();
    },
  },
  { id: "master-admin-audit", persona: "master-admin", path: "/audit", ready: "session_started", audit: true },
  { id: "master-admin-ingestion", persona: "master-admin", path: "/ingestion", ready: "Northwind Credit" },
  {
    id: "master-admin-ingestion-detail",
    persona: "master-admin",
    path: "/ingestion",
    ready: "Northwind Credit",
    setup: async (page) => {
      await page.getByRole("row").nth(1).click();
      await page.getByRole("dialog").getByText("Fingerprint", { exact: true }).waitFor();
    },
  },
  { id: "master-user-tenants", persona: "master-user", path: "/tenants", ready: "Northwind Credit" },
  { id: "master-user-audit", persona: "master-user", path: "/audit", ready: "session_started", audit: true },
  { id: "master-user-ingestion", persona: "master-user", path: "/ingestion", ready: "Northwind Credit" },
];

// Cloudscape draws the banner logo at most 32 px high (24 px below 688 px wide).
const LOGO_HEIGHT = 32;
// The navigation may be this much wider than its widest item, for padding and the collapse
// button. Cloudscape's default (280 px) leaves about 170 px empty beside "Audit history".
const NAV_SLACK = 90;

// What a dropdown shows: menu items and options, or (a date-range picker) its own buttons.
const POPUP_PARTS = ['[role="menuitem"]', '[role="menuitemradio"]', '[role="menuitemcheckbox"]', '[role="option"]', "button"];
const SEEN = "data-seen-before-open";

/** Where a screenshot goes: `screens/` in the output directory, one file per screen and width. */
function shot(info: TestInfo, file: string): string {
  return `${info.project.outputDir}/screens/${file}`;
}

async function waitForScreen(page: Page, screen: Screen): Promise<void> {
  await open(page, screen.persona, screen.path);
  await page.getByText(screen.ready, { exact: false }).first().waitFor();
  await screen.setup?.(page);
  // Fonts and images in place before anything is measured.
  await page.evaluate(() => document.fonts.ready);
}

/** Why the element cannot be seen and clicked at its centre, or null when it can. */
async function unreachable(item: Locator): Promise<string | null> {
  return item.evaluate((el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return "has no size";
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) {
      return `is outside the window at (${Math.round(x)}, ${Math.round(y)})`;
    }
    // A clipping ancestor (overflow) or anything drawn on top wins the hit test instead.
    const hit = document.elementFromPoint(x, y);
    if (hit && (hit === el || el.contains(hit))) return null;
    return `is hidden under ${hit ? `${hit.tagName.toLowerCase()}.${(hit.getAttribute("class") ?? "").slice(0, 60)}` : "nothing"}`;
  });
}

async function checkBanner(page: Page): Promise<void> {
  const banner = page.getByRole("banner");
  const title = banner.getByText("Assay Insights", { exact: true });
  await expect.soft(title, "the product name is shown").toBeVisible();
  const cut = await title.evaluate((el) => el.scrollWidth > el.clientWidth + 1);
  expect.soft(cut, "the product name is not cut off").toBe(false);

  const logo = banner.getByRole("img", { name: "Assay Insights logo" });
  await expect.soft(logo).toBeVisible();
  const box = await logo.boundingBox();
  expect.soft(box?.height ?? 0, "the logo uses the banner's logo height").toBeGreaterThanOrEqual(LOGO_HEIGHT);
  // The mark must be visible as drawn, not lost in the banner's colour: the share of the
  // logo's pixels that differ from the banner's background.
  const visible = await logo.evaluate(async (img: HTMLImageElement) => {
    await img.decode();
    const canvas = document.createElement("canvas");
    const size = 64;
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");
    if (!ctx) return 0;
    const bg = getComputedStyle(img.closest("header") ?? document.body).backgroundColor;
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, size, size);
    ctx.drawImage(img, 0, 0, size, size);
    const { data } = ctx.getImageData(0, 0, size, size);
    ctx.fillRect(0, 0, size, size);
    const base = ctx.getImageData(0, 0, 1, 1).data;
    let differ = 0;
    for (let i = 0; i < data.length; i += 4) {
      const d =
        Math.abs((data[i] ?? 0) - (base[0] ?? 0)) +
        Math.abs((data[i + 1] ?? 0) - (base[1] ?? 0)) +
        Math.abs((data[i + 2] ?? 0) - (base[2] ?? 0));
      if (d > 60) differ += 1;
    }
    return differ / (size * size);
  });
  expect.soft(visible, "the mark stands out from the banner over a fair share of its area").toBeGreaterThan(0.35);
}

async function checkNavigation(page: Page): Promise<void> {
  const links = page.getByRole("navigation").getByRole("link");
  const count = await links.count();
  expect.soft(count, "the navigation has links").toBeGreaterThan(0);
  const facts = await links.first().evaluate((first) => {
    // The panel is the side navigation's widest ancestor short of the page layout.
    let panel: HTMLElement | null = first.closest("nav");
    for (let el = panel?.parentElement ?? null; el; el = el.parentElement) {
      if (el.getBoundingClientRect().width >= window.innerWidth * 0.9) break;
      panel = el;
    }
    const list = [...(first.closest("ul")?.querySelectorAll("a") ?? [])];
    const widest = Math.max(...list.map((a) => {
      const range = document.createRange();
      range.selectNodeContents(a);
      return range.getBoundingClientRect().right;
    }));
    const close = panel?.querySelector("button");
    const closeBox = close?.getBoundingClientRect();
    const overlaps = list.some((a) => {
      const range = document.createRange();
      range.selectNodeContents(a);
      const t = range.getBoundingClientRect();
      return closeBox ? !(t.right <= closeBox.left || t.left >= closeBox.right || t.bottom <= closeBox.top || t.top >= closeBox.bottom) : false;
    });
    const cut = list.filter((a) => a.scrollWidth > a.clientWidth + 1).map((a) => a.textContent);
    const r = panel?.getBoundingClientRect();
    return { panelLeft: r?.left ?? 0, panelWidth: r?.width ?? 0, widest, overlaps, cut };
  });
  expect.soft(facts.cut, "no navigation item is cut off").toEqual([]);
  expect.soft(facts.overlaps, "no navigation item runs under the collapse button").toBe(false);
  const contentWidth = facts.widest - facts.panelLeft;
  expect.soft(
    facts.panelWidth,
    `the navigation (${Math.round(facts.panelWidth)} px) suits its widest item (${Math.round(contentWidth)} px)`,
  ).toBeLessThanOrEqual(contentWidth + NAV_SLACK);
}

/** Open every dropdown on the screen (inside the open dialog, if there is one) and check
 * that the first and last things it shows can be seen and clicked. */
async function checkPopups(page: Page): Promise<number> {
  const dialog = page.getByRole("dialog").filter({ visible: true });
  const scope = (await dialog.count()) > 0 ? dialog.first() : page.locator("body");
  const triggers = scope.locator('[aria-haspopup]:not([aria-haspopup="false"])');
  const total = await triggers.count();
  const revealed = page.locator(POPUP_PARTS.map((s) => `${s}:not([${SEEN}])`).join(", ")).filter({ visible: true });
  let opened = 0;
  for (let i = 0; i < total; i++) {
    const trigger = triggers.nth(i);
    if (!(await trigger.isVisible()) || !(await trigger.isEnabled())) continue;
    const name = (await trigger.getAttribute("aria-label")) ?? (await trigger.innerText()).trim();
    await page.evaluate(
      ([parts, seen]) => {
        for (const el of document.querySelectorAll(parts)) el.setAttribute(seen, "");
      },
      [POPUP_PARTS.join(", "), SEEN] as const,
    );
    await trigger.click();
    await expect(revealed.first(), `"${name}" opens`).toBeVisible();
    for (const item of [revealed.first(), revealed.last()]) {
      const label = (await item.innerText()).trim() || ((await item.getAttribute("aria-label")) ?? "?");
      expect.soft(await unreachable(item), `"${label}" in "${name}" can be seen and clicked`).toBeNull();
    }
    await page.keyboard.press("Escape");
    await expect(revealed).toHaveCount(0);
    await page.evaluate((seen) => {
      for (const el of document.querySelectorAll(`[${seen}]`)) el.removeAttribute(seen);
    }, SEEN);
    opened += 1;
  }
  return opened;
}

/** Every table on the screen fits its container: no column starts off screen. */
async function checkTables(page: Page): Promise<number> {
  const tables = page.locator("table").filter({ visible: true });
  const count = await tables.count();
  for (let i = 0; i < count; i++) {
    const facts = await tables.nth(i).evaluate((table) => {
      // The container that scrolls the table sideways when it is too wide.
      let box: HTMLElement | null = table.parentElement;
      while (box && !["auto", "scroll"].includes(getComputedStyle(box).overflowX)) box = box.parentElement;
      const frame = (box ?? document.documentElement).getBoundingClientRect();
      const headers = [...table.querySelectorAll("thead th")].map((th) => ({
        text: th.textContent.trim(),
        right: th.getBoundingClientRect().right,
      }));
      return {
        overflow: box ? box.scrollWidth - box.clientWidth : 0,
        frameRight: frame.right,
        offscreen: headers.filter((h) => h.right > frame.right + 1).map((h) => h.text),
        headers: headers.map((h) => h.text),
      };
    });
    expect.soft(facts.offscreen, `no column of [${facts.headers.join(", ")}] starts or ends off screen`).toEqual([]);
    expect.soft(facts.overflow, `the table [${facts.headers.join(", ")}] does not scroll sideways`).toBeLessThanOrEqual(1);
  }
  return count;
}

/** Audit history: Time, Actor and Outcome stay on one line in every row. */
async function checkAuditLines(page: Page): Promise<void> {
  const wrapped = await page.locator("table").filter({ visible: true }).first().evaluate((table) => {
    const names = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
    const columns = ["Time", "Actor", "Outcome"].map((name) => names.findIndex((n) => n.startsWith(name)));
    const out: string[] = [];
    for (const row of table.querySelectorAll("tbody tr")) {
      for (const index of columns) {
        const cell = row.children[index];
        if (!cell) continue;
        // The lines the text itself occupies (not the boxes around it).
        const tops = new Set<number>();
        const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          if (!node.textContent?.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          for (const r of range.getClientRects()) if (r.width > 0) tops.add(Math.round(r.top));
        }
        if (tops.size > 1) out.push(cell.textContent.trim());
      }
    }
    return out;
  });
  expect.soft(wrapped, "Time, Actor and Outcome each fit on one line").toEqual([]);
}

for (const screen of SCREENS) {
  test(screen.id, async ({ page, context }, info) => {
    await signIn(context, screen.persona);
    await waitForScreen(page, screen);
    await page.screenshot({ path: shot(info, `${info.project.name}-${screen.id}.png`), animations: "disabled" });
    if (screen.shell !== false) {
      await checkBanner(page);
      await checkNavigation(page);
    }
    await checkTables(page);
    if (screen.audit) await checkAuditLines(page);
    const opened = await checkPopups(page);
    // Outside a dialog every screen has at least the banner's user menu; a dialog may have none.
    if (!screen.setup || !(await page.getByRole("dialog").filter({ visible: true }).count())) {
      expect(opened, "at least one dropdown was opened and checked").toBeGreaterThan(0);
    }
  });
}

test("tenant-admin-users: the last row's actions menu, open", async ({ page, context }, info) => {
  await signIn(context, "tenant-admin");
  await waitForScreen(page, SCREENS[1] as Screen);
  await page.getByRole("button", { name: "Actions for User Number 9" }).click();
  const items = page.getByRole("menuitem").filter({ visible: true });
  await expect(items.first()).toBeVisible();
  // Cloudscape fades a menu in; the screenshot and the checks wait for it to finish.
  await page.waitForTimeout(400);
  await page.screenshot({ path: shot(info, `${info.project.name}-users-last-row-menu.png`), animations: "disabled" });
  for (let i = 0; i < (await items.count()); i++) {
    const item = items.nth(i);
    expect(await unreachable(item)).toBeNull();
    // Drawn as an enabled item, at full opacity (not faded, not greyed out).
    await expect(item).not.toHaveAttribute("aria-disabled", "true");
    const opacity = await item.evaluate((el) => {
      let value = 1;
      for (let e: Element | null = el; e; e = e.parentElement) value *= Number(getComputedStyle(e).opacity);
      return value;
    });
    expect(opacity).toBe(1);
  }
});

test("mfa-setup and sign-in screens render", async ({ browser }, info) => {
  const mfa = await browser.newContext({ viewport: info.project.use.viewport ?? null });
  const mfaPage = await mfa.newPage();
  await signIn(mfa, "tenant-admin", { mfaRequired: true });
  await open(mfaPage, "tenant-admin", "/");
  await mfaPage.getByText("Set up multi-factor authentication").waitFor();
  await mfaPage.screenshot({ path: shot(info, `${info.project.name}-mfa-setup.png`), animations: "disabled" });
  await mfa.close();

  const out = await browser.newContext({ viewport: info.project.use.viewport ?? null });
  const outPage = await out.newPage();
  await signIn(out, "master-admin", { signedIn: false });
  await open(outPage, "master-admin", "/");
  await outPage.getByRole("button", { name: "Sign in" }).waitFor();
  await outPage.screenshot({ path: shot(info, `${info.project.name}-start.png`), animations: "disabled" });
  await out.close();
});

test("tenant-admin-users: an invited user's menu offers a resent invitation, with its reason", async ({ page, context }, info) => {
  await signIn(context, "tenant-admin");
  await waitForScreen(page, SCREENS[1] as Screen);
  // User Number 3 is invited (fixtures.ts).
  await page.getByRole("button", { name: "Actions for User Number 3" }).click();
  const resend = page.getByRole("menuitem", { name: /Resend invitation/ });
  await expect(resend).toBeVisible();
  await expect(resend).toContainText("Forgot password can't reach them until they first sign in.");
  await expect(page.getByRole("menuitem", { name: /Reset password/ })).toHaveCount(0);
  await page.waitForTimeout(400);
  await page.screenshot({ path: shot(info, `${info.project.name}-users-invited-menu.png`), animations: "disabled" });
  expect(await unreachable(resend)).toBeNull();
});
