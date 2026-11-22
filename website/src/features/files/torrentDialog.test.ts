import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import ActionDialog from "./ActionDialog";

it("explains the destination and file choice in the torrent import dialog", () => {
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
    expect(html).toContain(
        "You can choose files after TGDrive reads the torrent.",
    );
    expect(html).toContain("Save to <strong>My Files</strong>");
    expect(html).toContain('for="remote-url"');
    expect(html).toContain('aria-describedby="torrent-import-hint"');
    expect(html).toContain("TGDrive must stay running.");
    expect(html).not.toContain("Compatibility Mode");
});
