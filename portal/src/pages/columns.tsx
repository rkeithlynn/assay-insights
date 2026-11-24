/**
 * Which columns a wide table shows (Cloudscape collection preferences).
 *
 * The ingestion histories have ten sortable columns (BUILD_SPEC.md section 11). Beside the
 * navigation they do not all fit legibly at 1280 px, so on a window narrower than
 * `WIDE_PX` a few are hidden by default; every column stays available here and in the
 * row's panel. A viewer's choice is kept in this browser only (localStorage, a per-viewer
 * convenience); without storage the defaults apply.
 */

import CollectionPreferences, {
  type CollectionPreferencesProps,
} from "@cloudscape-design/components/collection-preferences";
import { useState } from "react";

export const WIDE_PX = 1600;

export interface ColumnChoice {
  id: string;
  label: string;
  /** Always shown: the row's identity. */
  alwaysVisible?: boolean;
  /** Hidden by default on a window narrower than WIDE_PX. */
  hiddenWhenNarrow?: boolean;
}

type Display = readonly CollectionPreferencesProps.ContentDisplayItem[];

export function defaultDisplay(columns: readonly ColumnChoice[], windowWidth: number): Display {
  const narrow = windowWidth < WIDE_PX;
  return columns.map((c) => ({ id: c.id, visible: !(narrow && c.hiddenWhenNarrow) }));
}

function stored(key: string, columns: readonly ColumnChoice[]): Display | null {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const known = new Set(columns.map((c) => c.id));
    const items = parsed.filter(
      (item): item is CollectionPreferencesProps.ContentDisplayItem =>
        typeof item === "object" && item !== null && known.has((item as { id: unknown }).id as string),
    );
    // A column added since the choice was saved shows by default.
    const seen = new Set(items.map((i) => i.id));
    return [...items, ...columns.filter((c) => !seen.has(c.id)).map((c) => ({ id: c.id, visible: true }))];
  } catch {
    return null;
  }
}

export function useColumnDisplay(key: string, columns: readonly ColumnChoice[]) {
  const [display, setDisplay] = useState<Display>(
    () => stored(key, columns) ?? defaultDisplay(columns, window.innerWidth),
  );
  const save = (next: Display) => {
    setDisplay(next);
    try {
      window.localStorage.setItem(key, JSON.stringify(next));
    } catch {
      // No storage (a private window, blocked site data): the choice lasts this visit only.
    }
  };
  const preferences = (
    <CollectionPreferences
      title="Preferences"
      confirmLabel="Confirm"
      cancelLabel="Cancel"
      preferences={{ contentDisplay: display }}
      onConfirm={({ detail }) => save(detail.contentDisplay ?? display)}
      contentDisplayPreference={{
        title: "Columns",
        description: "Choose the columns to show. Every column can be sorted.",
        options: columns.map((c) => ({ id: c.id, label: c.label, alwaysVisible: c.alwaysVisible })),
      }}
    />
  );
  return { columnDisplay: display, preferences };
}
