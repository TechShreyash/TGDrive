import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import StartNewDrive from "./StartNewDrive";

it("explains the safe fresh-start path without collecting reset credentials", () => {
    const markup = renderToStaticMarkup(createElement(StartNewDrive));
    expect(markup).toContain("In-place reset is unavailable");
    expect(markup).not.toContain("temporarily");
    expect(markup).toContain(
        "Keep the existing database, channel and encryption key",
    );
    expect(markup).toContain('disabled=""');
    expect(markup).not.toContain("<form");
    expect(markup).not.toContain("<input");
});
