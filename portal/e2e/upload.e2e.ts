/**
 * Rendered upload checks: files added to a batch are all registered, all listed and all sent
 * to their own upload link, every time; and nothing is ever left out without a word.
 *
 * On 2026-11-27 the owner selected six files for a batch twice and fewer arrived (three, then
 * two), with no error on the screen. The load balancer's log shows one registration request
 * for each of those batches, with three and with two files in it: the missing files were
 * never sent to the API. The first three tests ask whether the page can be handed six files
 * and register fewer: the production build in Chromium, the same six files added many times,
 * in a different order each time, with the stand-in API answering after varying delays and
 * listing the registered files in an order of its own. They pass on the code of that day too,
 * so that loss was not reproduced here.
 *
 * What they cannot show: what the operating system's file picker hands the page. A selection
 * that was already short when it reached the browser looks the same from here. So the screen
 * now says how many files it was handed and how many it added, and the rest of this file
 * holds that and the other ways a file could go missing quietly:
 *
 * - the line after each addition, until the next addition or until dismissed;
 * - "Not added" entries kept until dismissed;
 * - no picker while an addition runs, and a drop made then listed by name, not ignored;
 * - each file sent to the link registered under its own name;
 * - a refused registration's reason still on the screen after the batch is read again.
 *
 * The stand-in API of `fixtures.ts` has an open batch with files in it and answers no
 * registration. This file adds its own: an empty open batch, the registration route, and a
 * place the "presigned" PUT goes to on the portal's own origin. It keeps what it was sent.
 */

import { type BrowserContext, expect, type Page, type Route, test, type TestInfo } from "@playwright/test";

import { HOSTS, open, signIn } from "./fixtures";

const BATCH_ID = "0c490d9d-5e1b-4c2a-9d77-0000000006b1";
const TENANT_ID = "00000000-0000-4000-8000-0000000000a1";

/** The first batch of 2026-11-27 that came up short: names and sizes of the synthetic files. */
const FILES = [
  { name: "01-normal.pdf", size: 2309 },
  { name: "04-with-companion.pdf", size: 2306 },
  { name: "04-with-companion.json", size: 325 },
  { name: "08-scanned.pdf", size: 9586 },
  { name: "09-mixed.pdf", size: 5900 },
  { name: "10-filled-form.pdf", size: 2318 },
] as const;
const NAMES = FILES.map((file) => file.name).sort();

// How often each way is repeated, at each of the two widths.
const CHOSEN = 40;
const DROPPED = 20;
const WHILE_ADDING = 10;

/** A small seeded generator (mulberry32), so a failing run can be repeated exactly. */
function seeded(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffled<T>(items: readonly T[], random: () => number): T[] {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    const a = out[i] as T;
    out[i] = out[j] as T;
    out[j] = a;
  }
  return out;
}

interface Payload {
  name: string;
  mimeType: string;
  buffer: Buffer;
}

/** A file's bytes: a PDF header and filler that differs per file, or a small companion file. */
function payload(file: { name: string; size: number }): Payload {
  if (file.name.endsWith(".json")) {
    const body = { TenantID: TENANT_ID, ApplicationRef: "APP-2026-00123", LoanTerm: 360, Note: "" };
    body.Note = "x".repeat(Math.max(0, file.size - JSON.stringify(body).length));
    return { name: file.name, mimeType: "application/json", buffer: Buffer.from(JSON.stringify(body), "utf-8") };
  }
  const header = `%PDF-1.7\n% ${file.name}\n`;
  const random = seeded(file.size);
  const filler = Array.from({ length: file.size - header.length }, () => 32 + Math.floor(random() * 95));
  return { name: file.name, mimeType: "application/pdf", buffer: Buffer.concat([Buffer.from(header, "latin1"), Buffer.from(filler)]) };
}

const PAYLOADS = FILES.map(payload);
/** Files the page must leave out: neither a PDF nor a companion file. */
const NOTES: Payload = { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("not a package", "utf-8") };
const DRAFT: Payload = { name: "draft.docx", mimeType: "application/octet-stream", buffer: Buffer.from("a draft", "utf-8") };
const NOT_A_PACKAGE = "Only PDF files and their JSON companion files can be uploaded.";

interface Stored {
  id: string;
  name: string;
  kind: "pdf" | "companion";
  size_bytes: number;
  status: string;
  error_code: null;
  errors: never[];
  matched: boolean;
  document_id: string | null;
  registered_at: string;
  scanned_at: string | null;
}

/** The stand-in's one open batch, and everything the page sent it. */
class Batch {
  files: Stored[] = [];
  /** The file names of each registration request, in the order they were sent. */
  requests: string[][] = [];
  /** What arrived at each file's upload link: the file's ID and the size of what was sent. */
  stored: { id: string; bytes: number; body: Buffer | null }[] = [];
  /** How often the batch was read. */
  reads = 0;
  /** Refuse the next registrations with this error code. */
  refuse: string | null = null;
  /** While set, a registration's answer waits for it. */
  hold: Promise<void> | null = null;
  private next = 0;

  reset(): void {
    this.files = [];
    this.requests = [];
    this.stored = [];
    this.reads = 0;
    this.refuse = null;
    this.hold = null;
  }

  registered(): string[] {
    return this.requests.flat().sort();
  }

  register(entries: { name: string; size_bytes: number }[]): Stored[] {
    this.requests.push(entries.map((entry) => entry.name));
    const added = entries.map((entry): Stored => {
      const id = `00000000-0000-4000-8000-${String(++this.next).padStart(12, "0")}`;
      const kind = entry.name.toLowerCase().endsWith(".json") ? "companion" : "pdf";
      return {
        id,
        name: entry.name,
        kind,
        size_bytes: entry.size_bytes,
        status: "registered",
        error_code: null,
        errors: [],
        matched: false,
        document_id: kind === "pdf" ? id : null,
        registered_at: "2026-11-27T20:28:16Z",
        scanned_at: null,
      };
    });
    this.files.push(...added);
    const base = (name: string) => name.slice(0, name.lastIndexOf(".")).toLowerCase();
    for (const file of this.files) {
      file.matched = this.files.some((other) => other.kind !== file.kind && base(other.name) === base(file.name));
    }
    return added;
  }

  /** The file's content arrived: its scan result follows at once. */
  store(id: string, bytes: number, body: Buffer | null): void {
    this.stored.push({ id, bytes, body });
    const file = this.files.find((f) => f.id === id);
    if (file) {
      file.status = "clean";
      file.scanned_at = "2026-11-27T20:28:30Z";
    }
  }

  summary() {
    return {
      id: BATCH_ID,
      name: "Six files at once",
      status: "open",
      opened_by: "Ada Lovelace",
      mine: true,
      opened_at: "2026-11-27T20:28:00Z",
      closed_at: null,
      pdf_count: this.files.filter((f) => f.kind === "pdf").length,
    };
  }

  detail() {
    return { ...this.summary(), closes_by: "2026-11-28T20:28:00Z", files: this.files, problems: [] };
  }
}

/** Answer the batch routes for this context; everything else goes on to `signIn`'s routes. */
async function serveBatch(context: BrowserContext, batch: Batch, random: () => number): Promise<void> {
  const pause = (most: number) => new Promise((resolve) => setTimeout(resolve, Math.floor(random() * most)));
  await context.route("**/*", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (url.host !== HOSTS.tenant) return route.fallback();
    if (method === "GET" && url.pathname === "/api/batches") {
      return route.fulfill({ json: { batches: [batch.summary()] } });
    }
    if (method === "GET" && url.pathname === `/api/batches/${BATCH_ID}`) {
      await pause(60);
      batch.reads += 1;
      return route.fulfill({ json: batch.detail() });
    }
    if (method === "POST" && url.pathname === `/api/batches/${BATCH_ID}/files`) {
      const body = request.postDataJSON() as { files: { name: string; size_bytes: number; sha256: string }[] };
      if (batch.refuse) {
        // All or nothing, as the API does it: one clash refuses the whole request.
        batch.requests.push([]);
        await pause(100);
        return route.fulfill({ status: 409, json: { detail: batch.refuse } });
      }
      // Registered when the request arrives; the answer comes back after a delay, and lists
      // the files in an order of its own: the page must pair them by name.
      const added = shuffled(batch.register(body.files), random);
      await pause(200);
      if (batch.hold) await batch.hold;
      return route.fulfill({
        status: 201,
        json: {
          files: added.map((file) => ({
            id: file.id,
            name: file.name,
            kind: file.kind,
            upload: {
              url: `http://${HOSTS.tenant}/stored/${file.id}`,
              method: "PUT",
              headers: { "Content-Type": file.kind === "pdf" ? "application/pdf" : "application/json" },
              expires_at: "2026-11-27T20:43:16Z",
            },
          })),
        },
      });
    }
    if (method === "PUT" && url.pathname.startsWith("/stored/")) {
      const sent = request.postDataBuffer();
      const bytes = sent ? sent.length : Number((await request.allHeaders())["content-length"] ?? -1);
      await pause(120);
      batch.store(url.pathname.slice("/stored/".length), bytes, sent);
      return route.fulfill({ status: 200, body: "" });
    }
    return route.fallback();
  });
}

/** The names in the Files table, sorted. */
async function listed(page: Page): Promise<string[]> {
  const names = await page
    .locator("table tbody tr")
    .evaluateAll((rows) => rows.map((row) => row.querySelector("th, td")?.textContent.trim() ?? ""));
  return names.filter((name) => name !== "" && !name.startsWith("No files yet")).sort();
}

async function emptyBatch(page: Page, batch: Batch): Promise<void> {
  batch.reset();
  await open(page, "tenant-admin", "/upload");
  await page.getByText("No files yet. Drop PDFs above, or choose them.").waitFor();
}

/** Drop these files on the drop zone, as a drag from a folder does. */
async function drop(page: Page, files: Payload[]): Promise<void> {
  const dropped = files.map((file) => ({ name: file.name, type: file.mimeType, base64: file.buffer.toString("base64") }));
  const dataTransfer = await page.evaluateHandle((entries) => {
    const transfer = new DataTransfer();
    for (const file of entries) {
      const bytes = Uint8Array.from(atob(file.base64), (c) => c.charCodeAt(0));
      transfer.items.add(new File([bytes], file.name, { type: file.type }));
    }
    return transfer;
  }, dropped);
  await page.getByText(/Drop PDFs and companion files here|Adding the files/).dispatchEvent("drop", { dataTransfer });
}

/** The line that says how many files the page was handed and how many it added. */
function additionLine(page: Page) {
  return page.getByTestId("addition");
}

/** Each of these files reached the link registered under its own name, whole. */
function expectStoredUnderTheirOwnNames(batch: Batch, expected: Payload[], run: string): void {
  expect(batch.stored.length, `${run}: the files whose content arrived`).toBe(expected.length);
  for (const file of expected) {
    const row = batch.files.find((f) => f.name === file.name);
    expect(row, `${run}: ${file.name} is registered`).toBeDefined();
    const arrived = batch.stored.filter((s) => s.id === row?.id);
    expect(arrived.length, `${run}: ${file.name} was sent to its own link, once`).toBe(1);
    expect(arrived[0]?.bytes, `${run}: the size of what ${file.name}'s link received`).toBe(file.buffer.length);
    const body = arrived[0]?.body;
    if (body) expect(body.equals(file.buffer), `${run}: ${file.name}'s link received ${file.name}'s bytes`).toBe(true);
  }
}

/** Every file was sent for registration, its content arrived, and the table lists it. */
async function expectAllSix(page: Page, batch: Batch, run: string, requests: number): Promise<void> {
  await expect.poll(() => batch.registered(), { message: `${run}: the files sent for registration` }).toEqual(NAMES);
  expect(batch.requests.length, `${run}: registration requests`).toBe(requests);
  await expect.poll(() => batch.stored.length, { message: `${run}: the files whose content arrived` }).toBe(FILES.length);
  expectStoredUnderTheirOwnNames(batch, PAYLOADS, run);
  await expect.poll(() => listed(page), { message: `${run}: the files in the table` }).toEqual(NAMES);
  await expect(page.getByText(`(${FILES.length})`, { exact: true }), `${run}: the table's count`).toBeVisible();
  await expect(page.getByTestId("not-added"), `${run}: nothing was left out`).toHaveCount(0);
  await expect(page.getByText("The upload failed."), `${run}: no upload failed`).toHaveCount(0);
}

function shot(info: TestInfo, file: string): string {
  return `${info.project.outputDir}/screens/${file}`;
}

test.describe("files added to a batch", () => {
  let batch: Batch;
  let random: () => number;

  test.beforeEach(async ({ context }, testInfo) => {
    test.setTimeout(240_000);
    batch = new Batch();
    // One seed per test and width, so a failure names a run that can be repeated.
    random = seeded(testInfo.title.length * 7919 + testInfo.project.name.length);
    await signIn(context, "tenant-admin");
    await serveBatch(context, batch, random);
  });

  test("six chosen with the file input: all six are registered, stored and listed, every time", async ({ page }) => {
    for (let run = 1; run <= CHOSEN; run++) {
      await emptyBatch(page, batch);
      await page.locator('input[type="file"]').setInputFiles(shuffled(PAYLOADS, random));
      await expectAllSix(page, batch, `chosen, run ${run} of ${CHOSEN}`, 1);
      await expect(additionLine(page), `chosen, run ${run}: the line`).toHaveText("6 files chosen: 6 added.");
    }
  });

  test("six dropped on the drop zone: all six are registered, stored and listed, every time", async ({ page }) => {
    for (let run = 1; run <= DROPPED; run++) {
      await emptyBatch(page, batch);
      await drop(page, shuffled(PAYLOADS, random));
      await expectAllSix(page, batch, `dropped, run ${run} of ${DROPPED}`, 1);
      await expect(additionLine(page), `dropped, run ${run}: the line`).toHaveText("6 files chosen: 6 added.");
    }
  });

  test("while files are being added there is no picker, and a drop made then is listed by name, not lost", async ({ page }) => {
    for (let run = 1; run <= WHILE_ADDING; run++) {
      await emptyBatch(page, batch);
      const files = shuffled(PAYLOADS, random);
      const cut = 1 + Math.floor(random() * (files.length - 1));
      const [first, second] = [files.slice(0, cut), files.slice(cut)];
      const label = `while adding (${first.length} then ${second.length}), run ${run} of ${WHILE_ADDING}`;
      let release = () => {};
      batch.hold = new Promise<void>((resolve) => {
        release = resolve;
      });

      await page.locator('input[type="file"]').setInputFiles(first);
      // The number chosen shows at once, before the API has answered.
      await expect(additionLine(page), `${label}: the line while adding`).toHaveText(
        first.length === 1 ? "1 file chosen: adding it." : `${first.length} files chosen: adding them.`,
      );
      await expect(page.locator('input[type="file"]'), `${label}: no file input while adding`).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Choose files" }), `${label}: the picker is off`).toBeDisabled();

      // A drop can still arrive. It is refused, and every file of it is named.
      await drop(page, second);
      const notAdded = page.getByTestId("not-added");
      for (const file of second) {
        await expect(notAdded.getByText(`${file.name}: Other files were still being added. Add it again.`), `${label}: ${file.name} is named`).toBeVisible();
      }
      expect(batch.requests.length, `${label}: the drop registered nothing`).toBe(1);

      batch.hold = null;
      release();
      await expect(additionLine(page), `${label}: the line when done`).toHaveText(
        first.length === 1 ? "1 file chosen: 1 added." : `${first.length} files chosen: ${first.length} added.`,
      );
      await expect.poll(() => batch.stored.length, { message: `${label}: the first part arrived` }).toBe(first.length);
      // The list of what was left out is still there, and the picker is back.
      await expect(notAdded, `${label}: "Not added" stays`).toBeVisible();
      await notAdded.getByRole("button").click();
      await expect(page.getByTestId("not-added"), `${label}: dismissed`).toHaveCount(0);

      await page.locator('input[type="file"]').setInputFiles(second);
      await expectAllSix(page, batch, label, 2);
      await expect(additionLine(page), `${label}: the second line`).toHaveText(
        second.length === 1 ? "1 file chosen: 1 added." : `${second.length} files chosen: ${second.length} added.`,
      );
    }
  });

  test("the line counts what was chosen and added; \"Not added\" stays until dismissed", async ({ page }, info) => {
    await emptyBatch(page, batch);
    const [first, ...rest] = PAYLOADS as [Payload, ...Payload[]];
    await page.locator('input[type="file"]').setInputFiles([NOTES, first]);
    await expect(additionLine(page)).toHaveText("2 files chosen: 1 added, 1 not added.");
    const notAdded = page.getByTestId("not-added");
    await expect(notAdded.getByText(`notes.txt: ${NOT_A_PACKAGE}`)).toBeVisible();
    await expect.poll(() => batch.registered()).toEqual([first.name]);

    // The next addition replaces the line, and leaves the earlier addition's list alone.
    await page.locator('input[type="file"]').setInputFiles(rest);
    await expect(additionLine(page)).toHaveText("5 files chosen: 5 added.");
    await expect(notAdded.getByText(`notes.txt: ${NOT_A_PACKAGE}`)).toBeVisible();
    await expect.poll(() => batch.registered()).toEqual(NAMES);
    await expect.poll(() => batch.stored.length).toBe(FILES.length);
    expectStoredUnderTheirOwnNames(batch, PAYLOADS, "in two additions");
    await expect.poll(() => listed(page)).toEqual(NAMES);

    // An addition that leaves a file out adds it to the list; the earlier entry stays.
    await page.locator('input[type="file"]').setInputFiles([DRAFT]);
    await expect(additionLine(page)).toHaveText("1 file chosen: 0 added, 1 not added.");
    await expect(notAdded.getByText(`notes.txt: ${NOT_A_PACKAGE}`)).toBeVisible();
    await expect(notAdded.getByText(`draft.docx: ${NOT_A_PACKAGE}`)).toBeVisible();
    expect(batch.requests.length, "nothing was sent for the file left out").toBe(2);
    await page.screenshot({ path: shot(info, `${info.project.name}-tenant-admin-upload-after-adding.png`), animations: "disabled" });

    // Each goes when it is dismissed, and only then.
    await notAdded.getByRole("button").click();
    await expect(page.getByTestId("not-added")).toHaveCount(0);
    await expect(additionLine(page)).toHaveText("1 file chosen: 0 added, 1 not added.");
    await page.getByTestId("addition-alert").getByRole("button").click();
    await expect(additionLine(page)).toHaveCount(0);
  });

  test("a refused registration says why, and still says it after the batch is read again", async ({ page }) => {
    await emptyBatch(page, batch);
    batch.refuse = "duplicate_file_name";
    const readsBefore = batch.reads;
    await page.locator('input[type="file"]').setInputFiles(PAYLOADS);
    await expect(additionLine(page)).toHaveText("6 files chosen: 0 added, 6 not added.");
    // The page reads the batch again after the refusal, and the picker returns once it has.
    await expect(page.locator('input[type="file"]')).toHaveCount(1);
    expect(batch.reads, "the batch was read again after the refusal").toBeGreaterThan(readsBefore);
    await expect(page.getByText("The batch already has a file with that name.")).toBeVisible();
    await page.waitForTimeout(500);
    await expect(page.getByText("The batch already has a file with that name.")).toBeVisible();
    expect(batch.registered()).toEqual([]);
    expect(await listed(page)).toEqual([]);
  });
});
