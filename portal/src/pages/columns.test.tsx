import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { type ColumnChoice, defaultDisplay, restoreDisplay, WIDE_PX } from "./columns";
import { TENANT_COLUMNS, TenantId } from "./Tenants";

const COLUMNS: readonly ColumnChoice[] = [
  { id: "name", label: "Name", alwaysVisible: true },
  { id: "size", label: "Size", hiddenWhenNarrow: true },
  { id: "optional", label: "Optional", hiddenByDefault: true },
];

function visible(display: readonly { id: string; visible: boolean }[]): string[] {
  return display.filter((d) => d.visible).map((d) => d.id);
}

describe("column display", () => {
  it("hides an optional column at any width, and a narrow-only one only when narrow", () => {
    expect(visible(defaultDisplay(COLUMNS, WIDE_PX))).toEqual(["name", "size"]);
    expect(visible(defaultDisplay(COLUMNS, WIDE_PX - 1))).toEqual(["name"]);
  });

  it("keeps a saved choice, drops unknown columns, and adds new ones at their default", () => {
    const saved = JSON.stringify([
      { id: "size", visible: false },
      { id: "gone", visible: true },
      { id: "name", visible: true },
    ]);
    expect(restoreDisplay(saved, COLUMNS)).toEqual([
      { id: "size", visible: false },
      { id: "name", visible: true },
      { id: "optional", visible: false },
    ]);
  });

  it("restores a shown optional column, and ignores what it cannot read", () => {
    const saved = JSON.stringify([{ id: "optional", visible: true }]);
    expect(visible(restoreDisplay(saved, COLUMNS) ?? [])).toEqual(["optional", "name", "size"]);
    expect(restoreDisplay(null, COLUMNS)).toBeNull();
    expect(restoreDisplay("not json", COLUMNS)).toBeNull();
    expect(restoreDisplay('{"id":"name"}', COLUMNS)).toBeNull();
  });
});

describe("Tenants table", () => {
  it("hides the tenant ID column by default at every width", () => {
    for (const width of [1280, 1800]) {
      const display = defaultDisplay(TENANT_COLUMNS, width);
      expect(display.find((d) => d.id === "tenant_id")?.visible).toBe(false);
      expect(visible(display)).toEqual(["name", "kind", "status", "users", "documents", "created"]);
    }
  });

  it("shows the full tenant ID with a copy button", () => {
    const id = "cfd08356-aac4-4dfe-a3f5-32a2f3294c4e";
    render(<TenantId id={id} />);
    expect(screen.getByText(id)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy tenant ID" })).toBeTruthy();
  });

  it("shows the first 8 characters in the table's column", () => {
    const id = "cfd08356-aac4-4dfe-a3f5-32a2f3294c4e";
    render(<TenantId id={id} short />);
    expect(screen.getByText("cfd08356")).toBeTruthy();
    expect(screen.queryByText(id)).toBeNull();
  });
});
