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
import ProgressBar from "@cloudscape-design/components/progress-bar";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator, { type StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { type BatchOut, call, type FileOut } from "../api/client";
import { Confirm, type ConfirmRequest } from "../components/Confirm";
import { RowActions } from "../components/RowActions";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { chunks, localProblem, putFile, runLimited, sha256Hex } from "../upload";
import { fileProblemText, formatBytes } from "./ingestionFormat";

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

// What is wrong with a companion file, by key (never its values: the API sends none).
const COMPANION_ERRORS: Record<string, string> = {
  tenant_mismatch: "is not this organization's ID",
  must_be_a_uuid: "must be an ID",
  must_be_text: "must be text",
  too_long: "is longer than 500 characters",
  must_be_a_date: "must be a date (YYYY-MM-DD)",
  must_be_a_whole_number: "must be a whole number",
  must_be_a_number: "must be a number",
  out_of_range: "is out of range",
  unknown_signer: "names a signer beyond Signer3",
  invalid_key: "is not a valid key (letters, digits and _ only, starting with a letter)",
  must_not_be_null: "must have a value",
  nested_value: "must be text, a number or true/false",
  duplicate_key: "appears twice",
  too_many_custom_keys: "The file has more than 50 custom keys",
  too_large: "The file is larger than 64 KB",
  not_json: "The file is not valid JSON",
  not_utf8: "The file is not UTF-8 text",
  not_an_object: "The file must be one JSON object",
};

const PROBLEM_TEXT: Record<string, string> = {
  awaiting_scan: "is waiting for its malware scan",
  not_uploaded: "was not uploaded",
  companion_invalid: "is not a valid companion file",
  upload_mismatch: "did not upload as registered",
  companion_unmatched: "has no PDF with the same name",
};

export function companionErrorText(errors: FileOut["errors"]): string {
  return errors
    .map((e) => {
      const text = COMPANION_ERRORS[e.code] ?? e.code.replaceAll("_", " ");
      return e.key ? `${e.key} ${text}` : text;
    })
    .join("; ");
}

export function matchText(file: FileOut): string {
  if (file.kind === "pdf") return file.matched ? "Companion file matched" : "No companion file";
  return file.matched ? "Matches its PDF" : "No PDF with this name";
}

interface Local {
  progress: number;
  error?: string;
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

  // The caller's open batch (or the one given), with its files; null when there is none.
  const fetchBatch = useCallback(
    async (batchId?: string): Promise<BatchOut | null> => {
      const id =
        batchId ?? (await call(api.GET("/api/batches", { params: { query: { status: "open", mine: true } } }))).batches[0]?.id;
      return id ? await call(api.GET("/api/batches/{batch_id}", { params: { path: { batch_id: id } } })) : null;
    },
    [api],
  );

  const load = useCallback(
    async (batchId?: string) => {
      try {
        setBatch(await fetchBatch(batchId));
        setError(null);
      } catch (caught) {
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
    if (!batch) return;
    setSelected([]);
    const problems: { name: string; problem: string }[] = [];
    const good: File[] = [];
    for (const file of files) {
      const problem = await localProblem(file, me.tenant.id);
      if (problem) problems.push({ name: file.name, problem });
      else good.push(file);
    }
    setRejected(problems);
    if (good.length === 0) return;
    setBusy(true);
    try {
      for (const group of chunks(good)) {
        const hashed = await Promise.all(group.map(async (file) => ({ file, sha256: await sha256Hex(file) })));
        const registered = await call(
          api.POST("/api/batches/{batch_id}/files", {
            params: { path: { batch_id: batch.id } },
            body: { files: hashed.map(({ file, sha256 }) => ({ name: file.name, size_bytes: file.size, sha256 })) },
          }),
        );
        await load(batch.id);
        const tasks = registered.files.map((entry, index) => async () => {
          const file = hashed[index]?.file;
          if (!file) return;
          const progress = (fraction: number) => setLocal((prev) => ({ ...prev, [entry.id]: { progress: fraction } }));
          progress(0);
          try {
            await putFile(entry.upload, file, progress);
          } catch {
            setLocal((prev) => ({ ...prev, [entry.id]: { progress: 0, error: "The upload failed. Remove the file and add it again." } }));
          }
        });
        await runLimited(tasks);
      }
      setError(null);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
      await load(batch.id);
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
      },
    });

  const runConfirm = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      await confirm.run();
      setConfirm(null);
    } catch (caught) {
      setError(messageFor(caught));
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  };

  const fileNames = new Map((batch?.files ?? []).map((f) => [f.id, f.name]));
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
                      <Button variant="primary" onClick={() => void closeBatch()} loading={busy} disabled={problems.length > 0}>
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
                    <Box>Drop PDFs and companion files here</Box>
                    <FileInput
                      multiple
                      accept=".pdf,.json,application/pdf,application/json"
                      value={selected}
                      onChange={({ detail }) => void addFiles(detail.value)}
                    >
                      Choose files
                    </FileInput>
                  </SpaceBetween>
                </FileDropzone>
                <Box variant="small" color="text-body-secondary">
                  A companion file has its PDF's name with .json (smith-package.pdf, smith-package.json). Up to 200 PDFs a
                  batch; 100 MB a PDF; 64 KB a companion file.
                </Box>
                {rejected.length > 0 && (
                  <Alert type="warning" dismissible onDismiss={() => setRejected([])} header="Not added">
                    <ul>
                      {rejected.map((r) => (
                        <li key={r.name}>
                          {r.name}: {r.problem}
                        </li>
                      ))}
                    </ul>
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
                { id: "match", header: "Companion", cell: (f) => matchText(f) },
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
                    return (
                      <SpaceBetween size="xxs">
                        <StatusIndicator type={status.type}>{status.label}</StatusIndicator>
                        {held && f.error_code && <Box variant="small">{fileProblemText(f.error_code, f.kind)}</Box>}
                        {f.errors.length > 0 && <Box variant="small">{companionErrorText(f.errors)}</Box>}
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
    </ContentLayout>
  );
}
