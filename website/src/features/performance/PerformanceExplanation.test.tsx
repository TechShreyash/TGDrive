import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import PerformanceExplanation from "./PerformanceExplanation";

describe("performance explanations", () => {
    it("keeps unsupported diagnosis distinct from healthy or idle work", () => {
        const html = renderToStaticMarkup(<PerformanceExplanation />);
        expect(html).toContain("Performance explanations are unavailable");
        expect(html).toContain("Raw measurements remain available below");
        expect(html).not.toContain("Healthy");
        expect(html).toContain("<details");
        expect(html).not.toContain(" open=");
        expect(html).toContain("Low throughput alone cannot locate");
    });
});
