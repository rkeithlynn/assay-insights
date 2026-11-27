/**
 * Upload (tenant-admin; BUILD_SPEC.md sections 8, 10 and 11). New batch (optional name), a
 * drop zone, the file list with size, progress and "companion file matched" or "no companion
 * file", inline problems, Close batch and Discard batch. Closing switches to that batch's
 * ingestion history.
 *
 * A batch closes only when every file has been scanned, every companion file is valid and
 * matches a PDF, and it holds a PDF (the API decides; this screen shows why not). It closes
 * by itself after 24 hours. While files wait for their malware scan, the batch is read again
 * every few seconds.
 *
 * A companion file is never left out for its content (owner decision A1, 2026-11-24): it is
 * uploaded, what this page can see is shown at once, and the pipeline's full list of problems
 * replaces it after the scan. Its PDF shows "Companion rejected". Close then asks, for each
 * such PDF: fix the companion file (it is removed so a corrected one can be added), remove
 * the PDF, or close without the companion. Each choice is a file removal the API already
 * offers; the API still refuses to close while an invalid companion file is in the batch.
 *
 * Nothing chosen goes missing without a word (owner, 2026-11-27): every addition shows how
 * many files the page was handed and how many are now in the batch, until the next addition
 * or until it is dismissed; files left out stay listed under "Not added" until dismissed;
 * while an addition runs the picker is off and a drop is refused in words; each file goes to
 * the upload link registered under its own name; and the reason an addition or a close was
 * refused stays on the screen until it is dismissed or the next action succeeds.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import FileDropzone from "@cloudscape-design/components/file-dropzone";
import FileInput from "@cloudscape-design/components/file-input";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import RadioGroup from "@cloudscape-design/components/radio-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator, { type StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { type BatchOut, call, type FileOut } from "../api/client";
import { Confirm, type ConfirmRequest } from "../components/Confirm";
import { RowActions } from "../components/RowActions";
import { messageFor } from "../errors";
import { useSession } from "../session";
import {
  additionText,
  chunks,
  classify,
  companionWarnings,
  localProblem,
  matchRegistered,
  putFile,
  runLimited,
  sha256Hex,
} from "../upload";
import { companionErrorLines, fileProblemText, formatBytes } from "./ingestionFormat";

// While any file waits for its scan, read the batch again this often.
const POLL_MS = 4000;

const FILE_STATUS: Record<string, { type: StatusIndicatorProps.Type; label: string }> = {
  registered: { type: "in-progress", label: "Waiting for its scan" },
  clean: { type: "success", label: "Scanned, clean" },
  invalid: { type: "error", label: "Companion file not valid" },
  mismatch: { type: "error", label: "Upload did not match" },
  infected: { type: "error", label: "Malware found: quarantined" },
  // On hold: the reason and what to do show under the label (fileProblemText).
  unsupported: { type: "warning", label: "On hold: could not be scanned" },
  scan_timeout: { type: "warning", label: "On hold: scan did not finish" },
  not_uploaded: { type: "error", label: "Not uploaded: remove it or add it again" },
};

const PROBLEM_TEXT: Record<string, string> = {
  awaiting_scan: "is waiting for its malware scan",
  not_uploaded: "was not uploaded",
  companion_invalid: "is not a valid companion file",
  upload_mismatch: "did not upload as registered",
  companion_unmatched: "has no PDF with the same name",
};

function baseName(file: FileOut): string {
  return classify(file.name)?.base ?? file.name.toLowerCase();
}

/** The PDF a companion file belongs to (same name), if it is in the batch. */
export function pdfFor(companion: FileOut, files: FileOut[]): FileOut | undefined {
  return files.find((f) => f.kind === "pdf" && baseName(f) === baseName(companion));
}

/** The companion files the pipeline found invalid. Each keeps the batch from closing. */
export function rejectedCompanions(files: FileOut[]): FileOut[] {
  return files.filter((f) => f.kind === "companion" && f.status === "invalid");
}

export function matchText(file: FileOut, files: FileOut[] = []): string {
  if (file.kind === "pdf") {
    if (rejectedCompanions(files).some((c) => baseName(c) === baseName(file))) return "Companion rejected";
    return file.matched ? "Companion file matched" : "No companion file";
  }
  if (file.status === "invalid") return "Rejected";
  return file.matched ? "Matches its PDF" : "No PDF with this name";
}

/** What to do with each PDF whose companion file was rejected (owner decision A1). */
export type RejectedChoice = "fix" | "remove_pdf" | "without";

const BULLETS = { margin: 0, paddingLeft: "1.2em" } as const;

function Lines({ lines }: { lines: string[] }) {
  return (
    <ul style={BULLETS}>
      {lines.map((line, i) => (
        <li key={i}>{line}</li>
      ))}
    </ul>
  );
}

interface Local {
  progress: number;
  error?: string;
}

const UPLOAD_FAILED = "The upload failed. Remove the file and add it again.";
const STILL_ADDING = "Other files were still being added. Add it again.";

/** The latest addition: the files the page was handed, and how many are in the batch now. */
interface Addition {
  chosen: number;
  /** Null while the addition is running. */
  added: number | null;
}

export function Upload() {
  const { api, me } = useSession();
  const navigate = useNavigate();
  const [batch, setBatch] = useState<BatchOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState<Record<string, Local>>({});
  const [rejected, setRejected] = useState<{ name: string; problem: string }[]>([]);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [selected, setSelected] = useState<File[]>([]);
  // What this page saw wrong with a companion file before its scan, by file ID.
  const [early, setEarly] = useState<Record<string, string[]>>({});
  // The Close dialog: a choice for each PDF whose companion file was rejected.
  const [choosing, setChoosing] = useState<Record<string, RejectedChoice> | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [addition, setAddition] = useState<Addition | null>(null);
  // An addition is running: the picker is off. The ref answers at once, before a render.
  const [adding, setAdding] = useState(false);
  const addingNow = useRef(false);

  // The caller's open batch (or the one given), with its files; null when there is none.
  const fetchBatch = useCallback(
    async (batchId?: string): Promise<BatchOut | null> => {
      const id =
        batchId ?? (await call(api.GET("/api/batches", { params: { query: { status: "open", mine: true } } }))).batches[0]?.id;
      return id ? await call(api.GET("/api/batches/{batch_id}", { params: { path: { batch_id: id } } })) : null;
    },
    [api],
  );

  // Reading the batch again clears only an error that reading it raised. It runs after every
  // refused action and every few seconds while a file waits for its scan: if it cleared any
  // error, a refused registration or close would show its reason for a moment and then nothing.
  const loadFailed = useRef(false);
  const load = useCallback(
    async (batchId?: string) => {
      try {
        setBatch(await fetchBatch(batchId));
        if (loadFailed.current) {
          loadFailed.current = false;
          setError(null);
        }
      } catch (caught) {
        loadFailed.current = true;
        setError(messageFor(caught));
      }
    },
    [fetchBatch],
  );

  useEffect(() => {
    const screen = { open: true };
    fetchBatch().then(
      (found) => {
        if (!screen.open) return;
        setBatch(found);
        setLoading(false);
      },
      (caught: unknown) => {
        if (!screen.open) return;
        setError(messageFor(caught));
        setLoading(false);
      },
    );
    return () => {
      screen.open = false;
    };
  }, [fetchBatch]);

  // Read the batch again while any file waits for its scan.
  const waiting = batch?.files.some((f) => f.status === "registered") ?? false;
  useEffect(() => {
    if (!batch || !waiting) return;
    const timer = window.setInterval(() => void load(batch.id), POLL_MS);
    return () => window.clearInterval(timer);
  }, [batch, waiting, load]);

  const openBatch = async () => {
    setBusy(true);
    try {
      const opened = await call(api.POST("/api/batches", { body: { name: name.trim() || null } }));
      setBatch(opened);
      setError(null);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  };

  const addFiles = async (files: File[]) => {
    if (!batch) {
      setError("There is no open batch to add files to. Open a batch, then add the files again.");
      return;
    }
    const leftOut = (problems: { name: string; problem: string }[]) => {
      // Kept until dismissed: a later addition never clears an earlier one's list.
      if (problems.length > 0) setRejected((prev) => [...prev, ...problems]);
    };
    if (addingNow.current) {
      // The picker is off while an addition runs; a drop can still arrive. It is not queued,
      // and it is not lost without a word: each file is listed, by name, until dismissed.
      leftOut(files.map((file) => ({ name: file.name, problem: STILL_ADDING })));
      return;
    }
    addingNow.current = true;
    setAdding(true);
    setSelected([]);
    setAddition({ chosen: files.length, added: null });
    let added = 0;
    try {
      const problems: { name: string; problem: string }[] = [];
      const good: File[] = [];
      const warnings = new Map<File, string[]>();
      for (const file of files) {
        const problem = localProblem(file);
        if (problem) {
          problems.push({ name: file.name, problem });
          continue;
        }
        good.push(file);
        if (classify(file.name)?.kind === "companion") {
          const found = await companionWarnings(file, me.tenant.id);
          if (found.length > 0) warnings.set(file, found);
        }
      }
      leftOut(problems);
      if (good.length > 0) setBusy(true);
      for (const group of chunks(good)) {
        const hashed = await Promise.all(group.map(async (file) => ({ file, sha256: await sha256Hex(file) })));
        const registered = await call(
          api.POST("/api/batches/{batch_id}/files", {
            params: { path: { batch_id: batch.id } },
            body: { files: hashed.map(({ file, sha256 }) => ({ name: file.name, size_bytes: file.size, sha256 })) },
          }),
        );
        // By name, never by position: each file goes to the link registered for it.
        const { pairs, unregistered } = matchRegistered(group, registered.files);
        added += pairs.filter((pair) => pair.file).length;
        leftOut(unregistered.map((file) => ({ name: file.name, problem: "The server did not add it. Add it again." })));
        const seen: Record<string, string[]> = {};
        for (const { entry, file } of pairs) {
          const found = file ? warnings.get(file) : undefined;
          if (found) seen[entry.id] = found;
        }
        setEarly((prev) => ({ ...prev, ...seen }));
        await load(batch.id);
        const tasks = pairs.map(({ entry, file }) => async () => {
          if (!file) {
            // Registered, and none of the chosen files has its name: there is nothing to send.
            setLocal((prev) => ({ ...prev, [entry.id]: { progress: 0, error: UPLOAD_FAILED } }));
            return;
          }
          const progress = (fraction: number) => setLocal((prev) => ({ ...prev, [entry.id]: { progress: fraction } }));
          progress(0);
          try {
            await putFile(entry.upload, file, progress);
          } catch {
            setLocal((prev) => ({ ...prev, [entry.id]: { progress: 0, error: UPLOAD_FAILED } }));
          }
        });
        await runLimited(tasks);
      }
      if (good.length > 0) setError(null);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setAddition({ chosen: files.length, added });
      setBusy(false);
      await load(batch.id);
      addingNow.current = false;
      setAdding(false);
    }
  };

  const closeBatch = async () => {
    if (!batch) return;
    setBusy(true);
    try {
      await call(api.POST("/api/batches/{batch_id}/close", { params: { path: { batch_id: batch.id } } }));
      void navigate(`/ingestion?batch=${batch.id}`);
    } catch (caught) {
      setError(messageFor(caught));
      await load(batch.id);
    } finally {
      setBusy(false);
    }
  };

  // Close: with a rejected companion file in the batch, ask what to do with each first.
  const startClose = () => {
    const rejectedFiles = rejectedCompanions(batch?.files ?? []);
    if (rejectedFiles.length === 0) {
      void closeBatch();
      return;
    }
    setNotice(null);
    const fixAll: Record<string, RejectedChoice> = Object.fromEntries(rejectedFiles.map((c) => [c.id, "fix"]));
    setChoosing(fixAll);
  };

  const removeQuietly = async (file: FileOut) => {
    if (!batch) return;
    await call(
      api.DELETE("/api/batches/{batch_id}/files/{file_id}", { params: { path: { batch_id: batch.id, file_id: file.id } } }),
    );
  };

  const applyChoices = async () => {
    if (!batch || !choosing) return;
    setBusy(true);
    try {
      for (const companion of rejectedCompanions(batch.files)) {
        const choice = choosing[companion.id] ?? "fix";
        // Every choice removes the rejected companion file; "remove_pdf" removes its PDF too.
        await removeQuietly(companion);
        const pdf = pdfFor(companion, batch.files);
        if (choice === "remove_pdf" && pdf) await removeQuietly(pdf);
      }
      setChoosing(null);
      if (Object.values(choosing).includes("fix")) {
        await load(batch.id);
        setError(null);
        setNotice("Add the corrected companion files, with the same names as their PDFs, then close the batch.");
        setBusy(false);
        return;
      }
    } catch (caught) {
      setChoosing(null);
      setError(messageFor(caught));
      await load(batch.id);
      setBusy(false);
      return;
    }
    setBusy(false);
    await closeBatch();
  };

  const removeFile = (file: FileOut) =>
    setConfirm({
      title: "Remove file",
      body: `Remove ${file.name} from this batch? It will not be processed.`,
      action: "Remove",
      run: async () => {
        if (!batch) return;
        await call(
          api.DELETE("/api/batches/{batch_id}/files/{file_id}", {
            params: { path: { batch_id: batch.id, file_id: file.id } },
          }),
        );
        await load(batch.id);
      },
    });

  const discard = () =>
    setConfirm({
      title: "Discard batch",
      body: "Discard this batch? None of its files will be processed. This cannot be undone.",
      action: "Discard batch",
      run: async () => {
        if (!batch) return;
        await call(api.DELETE("/api/batches/{batch_id}", { params: { path: { batch_id: batch.id } } }));
        setBatch(null);
        setLocal({});
        setAddition(null);
        setRejected([]);
      },
    });

  const runConfirm = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      await confirm.run();
      setConfirm(null);
      setError(null);
    } catch (caught) {
      setError(messageFor(caught));
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  };

  const fileNames = new Map((batch?.files ?? []).map((f) => [f.id, f.name]));
  // A rejected companion file does not disable Close: Close asks what to do with it.
  const blocking = (batch?.problems ?? []).filter((p) => p.code !== "companion_invalid");
  const problems = (batch?.problems ?? []).map((p) =>
    p.file_id
      ? `${fileNames.get(p.file_id) ?? "A file"} ${PROBLEM_TEXT[p.code] ?? p.code.replaceAll("_", " ")}.`
      : p.code === "batch_empty"
        ? "The batch has no PDF yet."
        : p.code.replaceAll("_", " "),
  );

  return (
    <ContentLayout
      header={
        <Header variant="h1" description="Upload signed PDF packages, each with an optional companion JSON file.">
          Upload
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}
        {loading ? (
          <StatusIndicator type="loading">Loading your batch</StatusIndicator>
        ) : !batch ? (
          <Container header={<Header variant="h2">New batch</Header>}>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void openBatch();
              }}
            >
              <Form actions={<Button variant="primary" loading={busy} formAction="submit">Open batch</Button>}>
                <FormField label="Name" description="Optional. Shown in ingestion history." constraintText="Up to 200 characters.">
                  <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="October applications" />
                </FormField>
              </Form>
            </form>
          </Container>
        ) : (
          <>
            <Container
              header={
                <Header
                  variant="h2"
                  description={
                    batch.closes_by
                      ? `Open. Closes by itself at ${new Date(batch.closes_by).toLocaleString()}.`
                      : undefined
                  }
                  actions={
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button onClick={discard} disabled={busy}>
                        Discard batch
                      </Button>
                      <Button variant="primary" onClick={startClose} loading={busy} disabled={blocking.length > 0}>
                        Close batch
                      </Button>
                    </SpaceBetween>
                  }
                >
                  {batch.name ?? "Batch"}
                </Header>
              }
            >
              <SpaceBetween size="m">
                <FileDropzone onChange={({ detail }) => void addFiles(detail.value)}>
                  <SpaceBetween size="xxs" alignItems="center">
                    <Box>{adding ? "Adding the files. Wait until they are listed before adding more." : "Drop PDFs and companion files here"}</Box>
                    {adding ? (
                      // The file input has no disabled state: while an addition runs it is not there.
                      <Button iconName="upload" disabled>
                        Choose files
                      </Button>
                    ) : (
                      <FileInput
                        multiple
                        accept=".pdf,.json,application/pdf,application/json"
                        value={selected}
                        onChange={({ detail }) => void addFiles(detail.value)}
                      >
                        Choose files
                      </FileInput>
                    )}
                  </SpaceBetween>
                </FileDropzone>
                <Box variant="small" color="text-body-secondary">
                  A companion file has its PDF's name with .json (smith-package.pdf, smith-package.json). Up to 200 PDFs a
                  batch; 100 MB a PDF; 64 KB a companion file.
                </Box>
                {addition && (
                  <Alert
                    type={addition.added === null ? "info" : addition.added === addition.chosen && addition.chosen > 0 ? "success" : "warning"}
                    dismissible
                    onDismiss={() => setAddition(null)}
                    data-testid="addition-alert"
                  >
                    <span data-testid="addition">{additionText(addition.chosen, addition.added)}</span>
                  </Alert>
                )}
                {rejected.length > 0 && (
                  <Alert type="warning" dismissible onDismiss={() => setRejected([])} header="Not added" data-testid="not-added">
                    <ul>
                      {rejected.map((r, i) => (
                        <li key={i}>
                          {r.name}: {r.problem}
                        </li>
                      ))}
                    </ul>
                  </Alert>
                )}
                {notice && (
                  <Alert type="info" dismissible onDismiss={() => setNotice(null)}>
                    {notice}
                  </Alert>
                )}
                {problems.length > 0 && batch.files.length > 0 && (
                  <Alert type="info" header="Before the batch can close">
                    <ul>
                      {problems.map((p) => (
                        <li key={p}>{p}</li>
                      ))}
                    </ul>
                  </Alert>
                )}
              </SpaceBetween>
            </Container>
            <Table
              variant="container"
              items={batch.files}
              trackBy="id"
              wrapLines
              header={<Header counter={`(${batch.files.length})`}>Files</Header>}
              empty={<Box textAlign="center">No files yet. Drop PDFs above, or choose them.</Box>}
              columnDefinitions={[
                { id: "name", header: "Name", cell: (f) => <span style={{ overflowWrap: "anywhere" }}>{f.name}</span>, isRowHeader: true },
                { id: "size", header: "Size", cell: (f) => <span style={{ whiteSpace: "nowrap" }}>{formatBytes(f.size_bytes)}</span> },
                { id: "kind", header: "Type", cell: (f) => (f.kind === "pdf" ? "PDF" : "Companion file") },
                {
                  id: "match",
                  header: "Companion",
                  cell: (f) => {
                    const text = matchText(f, batch.files);
                    return text === "Companion rejected" || text === "Rejected" ? (
                      <StatusIndicator type="warning" wrapText={false}>
                        {text}
                      </StatusIndicator>
                    ) : (
                      text
                    );
                  },
                },
                {
                  id: "status",
                  header: "Status",
                  cell: (f) => {
                    const mine = local[f.id];
                    if (mine?.error) return <StatusIndicator type="error">{mine.error}</StatusIndicator>;
                    if (f.status === "registered" && mine && mine.progress < 1) {
                      return <ProgressBar value={Math.round(mine.progress * 100)} ariaLabel={`Uploading ${f.name}`} />;
                    }
                    const status = FILE_STATUS[f.status] ?? { type: "info" as const, label: f.status };
                    const held = f.status === "unsupported" || f.status === "scan_timeout";
                    // Before the scan, what this page saw; after it, the pipeline's full list.
                    const seenHere = f.status === "registered" ? (early[f.id] ?? []) : [];
                    return (
                      <SpaceBetween size="xxs">
                        <StatusIndicator type={status.type}>{status.label}</StatusIndicator>
                        {held && f.error_code && <Box variant="small">{fileProblemText(f.error_code, f.kind)}</Box>}
                        {f.errors.length > 0 && (
                          <Box variant="small">
                            <Lines lines={companionErrorLines(f.errors)} />
                          </Box>
                        )}
                        {seenHere.length > 0 && (
                          <Box variant="small">
                            Problems found here; the full check follows the scan:
                            <Lines lines={seenHere} />
                          </Box>
                        )}
                      </SpaceBetween>
                    );
                  },
                },
                {
                  id: "actions",
                  header: "",
                  cell: (f) => (
                    <RowActions
                      ariaLabel={`Actions for ${f.name}`}
                      items={[{ id: "remove", text: "Remove from batch" }]}
                      disabled={busy}
                      onItemClick={() => removeFile(f)}
                    />
                  ),
                },
              ]}
            />
          </>
        )}
      </SpaceBetween>
      <Confirm request={confirm} busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void runConfirm()} />
      <Modal
        visible={choosing !== null}
        onDismiss={() => setChoosing(null)}
        header="Rejected companion files"
        size="medium"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setChoosing(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={() => void applyChoices()} loading={busy}>
                Continue
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="l">
          <Box>The batch cannot close while a companion file is rejected. Choose what to do with each PDF.</Box>
          {rejectedCompanions(batch?.files ?? []).map((companion) => {
            const pdf = pdfFor(companion, batch?.files ?? []);
            return (
              <FormField
                key={companion.id}
                label={<span style={{ overflowWrap: "anywhere" }}>{pdf?.name ?? companion.name}</span>}
                description={
                  <>
                    {companion.name} was rejected:
                    <Lines lines={companionErrorLines(companion.errors)} />
                  </>
                }
              >
                <RadioGroup
                  value={choosing?.[companion.id] ?? "fix"}
                  onChange={({ detail }) =>
                    setChoosing((prev) => (prev ? { ...prev, [companion.id]: detail.value as RejectedChoice } : prev))
                  }
                  items={[
                    {
                      value: "fix",
                      label: "Fix the companion file",
                      description: "The rejected file is removed and the batch stays open, so you can add a corrected one.",
                    },
                    { value: "remove_pdf", label: "Remove the PDF", description: "The PDF and its companion file are not processed." },
                    { value: "without", label: "Close without the companion file", description: "The PDF is processed without one." },
                  ]}
                />
              </FormField>
            );
          })}
        </SpaceBetween>
      </Modal>
    </ContentLayout>
  );
}
