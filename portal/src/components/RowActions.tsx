/**
 * A table row's actions menu (the three-dot button). Every row-action menu in the portal uses
 * this, never a bare ButtonDropdown: a table scrolls inside its own container, and a menu drawn
 * inside that container is clipped by it, so the lower items of a row near the bottom cannot be
 * seen. `expandToViewport` draws the menu at the top of the page instead, positioned against
 * the window. The rendered checks (portal/e2e/layout.e2e.ts) open every menu on every screen.
 */

import ButtonDropdown, { type ButtonDropdownProps } from "@cloudscape-design/components/button-dropdown";

export interface RowActionsProps {
  items: ButtonDropdownProps.Items;
  /** What the menu acts on, for screen readers: "Actions for Ada Lovelace". */
  ariaLabel: string;
  disabled?: boolean;
  onItemClick: (id: string) => void;
}

export function RowActions({ items, ariaLabel, disabled, onItemClick }: RowActionsProps) {
  return (
    <ButtonDropdown
      items={items}
      variant="inline-icon"
      ariaLabel={ariaLabel}
      disabled={disabled}
      expandToViewport
      onItemClick={({ detail }) => onItemClick(detail.id)}
    />
  );
}
