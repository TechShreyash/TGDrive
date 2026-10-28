import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import ActionDialog from "./ActionDialog";

it("explains whole-torrent imports in the existing accessible import dialog", () => {
    const html = renderToStaticMarkup(
        createElement(ActionDialog, {
            dialog: { type: "torrent" },
            destination: "/",
            auth: "",
            onClose: () => {},
            onChanged: () => {},
            onNotice: () => {},
            onDelete: async () => {},
            onRestore: async () => {},
        }),
    );
    expect(html).toContain("Import torrent");
    expect(html).toContain("Every file is imported, with folders kept intact.");
    expect(html).toContain('for="remote-url"');
    expect(html).toContain('aria-describedby="torrent-import-hint"');
    expect(html).toContain("TGDrive must stay running.");
    expect(html).not.toContain("Compatibility Mode");
});
