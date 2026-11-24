/**
 * Brand colours (BUILD_SPEC.md section 11): navy #14213D for the top bar, headings and
 * primary buttons; gold #F2B632 accents on navy; #B07D12 where gold appears on white
 * (focus rings). Gold text on white fails WCAG AA, so links stay navy.
 */

import { applyTheme } from "@cloudscape-design/components/theming";

export const NAVY = "#14213D";
export const NAVY_HOVER = "#22345C";
export const GOLD = "#F2B632";
export const GOLD_ON_WHITE = "#B07D12";

export function applyBrand(): void {
  applyTheme({
    theme: {
      tokens: {
        colorBackgroundButtonPrimaryDefault: NAVY,
        colorBackgroundButtonPrimaryHover: NAVY_HOVER,
        colorBackgroundButtonPrimaryActive: NAVY,
        colorTextLinkDefault: NAVY,
        colorTextLinkHover: NAVY_HOVER,
        colorTextHeadingDefault: NAVY,
        colorBorderItemFocused: GOLD_ON_WHITE,
      },
      contexts: {
        "top-navigation": {
          tokens: {
            colorBackgroundContainerContent: NAVY,
            colorTextInteractiveHover: GOLD,
          },
        },
      },
    },
  });
}
