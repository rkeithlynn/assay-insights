import "@cloudscape-design/global-styles/index.css";

import markUrl from "@assets/assay-insights-mark.svg";
import { I18nProvider } from "@cloudscape-design/components/i18n";
import messages from "@cloudscape-design/components/i18n/messages/all.en";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { Root } from "./Root";
import { applyBrand } from "./theme";

applyBrand();

const icon = document.createElement("link");
icon.rel = "icon";
icon.type = "image/svg+xml";
icon.href = markUrl;
document.head.appendChild(icon);

const container = document.getElementById("root");
if (!container) throw new Error("no #root element");
createRoot(container).render(
  <StrictMode>
    <I18nProvider locale="en" messages={[messages]}>
      <Root />
    </I18nProvider>
  </StrictMode>,
);
