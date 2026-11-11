import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import PerformanceExplanation from "./PerformanceExplanation";
import type { PerformanceDiagnosis } from "./performanceApi";

const diagnosis: PerformanceDiagnosis = {
    sampled_at: 1000,
    window_seconds: 30,
    observed_seconds: 12,
    stale_after_seconds: 15,
    state: "active",
    workload: {
        playback_requests: 1,
        download_requests: 2,
        unknown_file_requests: 0,
        upload_requests: 0,
    },
    telegram_inbound_bytes_per_second: null,
    http_delivered_bytes_per_second: 1024,
    cache_hits: null,
    cache_misses: 0,
    constraints: [],
};

function render(changes: Partial<PerformanceDiagnosis> = {}, serverAt = 1003) {
    return renderToStaticMarkup(
        <PerformanceExplanation
            diagnosis={{ ...diagnosis, ...changes }}
            serverAt={serverAt}
        />,
    );
}

describe("performance explanations", () => {
    it("does not claim idle while current Telegram operations are active", () => {
        const html = renderToStaticMarkup(
            <PerformanceExplanation
                diagnosis={{
                    ...diagnosis,
                    state: "idle",
                    workload: {
                        playback_requests: 0,
                        download_requests: 0,
                        unknown_file_requests: 0,
                        upload_requests: 0,
                    },
                }}
                serverAt={1003}
                telegram={{ active: 1, queued: 0 }}
                liveRates={{ toViewers: 0, fromDevices: 0 }}
            />,
        );
        expect(html).toContain("Transfers are running");
        expect(html).not.toContain("Nothing is transferring right now");
        expect(html).toContain('class="performance-file-rates"');
        expect(html).toContain("0 B/s");
    });
    it("keeps an idle summary compact while preserving zero readings in details", () => {
        const html = render({
            state: "idle",
            workload: {
                playback_requests: 0,
                download_requests: 0,
                unknown_file_requests: 0,
                upload_requests: 0,
            },
        });
        expect(html).not.toContain('class="performance-workload"');
        expect(html).toContain("Streaming requests</dt><dd>0");
        expect(html).toContain("Nothing is transferring");
    });
    it("shows measured idle rates without treating missing readings as zero", () => {
        const html = renderToStaticMarkup(
            <PerformanceExplanation
                diagnosis={{
                    ...diagnosis,
                    state: "idle",
                    workload: {
                        playback_requests: 0,
                        download_requests: 0,
                        unknown_file_requests: 0,
                        upload_requests: 0,
                    },
                }}
                serverAt={1003}
                liveRates={{ toViewers: 0, fromDevices: null }}
            />,
        );
        expect(html).toContain('data-unavailable="false">0 B/s</dd>');
        expect(html).toContain('data-unavailable="true">Unavailable</dd>');
        expect(html).toContain("Nothing is transferring right now");
    });
    it("does not label a recent active window as an in-flight file request", () => {
        const html = render({
            state: "active",
            workload: {
                playback_requests: 0,
                download_requests: 0,
                unknown_file_requests: 0,
                upload_requests: 0,
            },
        });
        expect(html).toContain("Nothing is transferring in the latest reading");
        expect(html).not.toContain('class="performance-workload"');
    });
    it("keeps unsupported diagnosis distinct from healthy or idle work", () => {
        const html = renderToStaticMarkup(<PerformanceExplanation />);
        expect(html).toContain("Current information is unavailable");
        expect(html).toContain("Refresh to try again");
        expect(html).not.toContain("Healthy");
        expect(html).toContain("<details");
        expect(html).not.toContain(" open=");
        expect(html).toContain("Low throughput alone cannot locate");
    });
    it("does not turn a measured active window into an identified bottleneck", () => {
        expect(render()).toContain("no measured wait reported");
        expect(render()).not.toContain("bottleneck identified");
        expect(render()).toContain("12 s");
        expect(render()).toContain("30 s");
    });
    it.each([
        ["idle", "Nothing is transferring right now"],
        ["observing", "Checking recent activity"],
        ["unavailable", "Current information is unavailable"],
    ] as const)("reports %s honestly", (state, copy) => {
        const html = render({
            state,
            workload:
                state === "idle"
                    ? {
                          playback_requests: 0,
                          download_requests: 0,
                          unknown_file_requests: 0,
                          upload_requests: 0,
                      }
                    : diagnosis.workload,
        });
        expect(html).toContain(copy);
        expect(html).not.toContain("Healthy");
    });
    it.each([999, 1016, NaN])(
        "suppresses current evidence for invalid or stale server time %s",
        (serverAt) => {
            const html = render({}, serverAt);
            expect(html).toContain("Last sample (not current)");
            expect(html).not.toContain("<dt>Streaming</dt>");
            expect(html).not.toContain("1 KiB/s");
        },
    );
    it("keeps unavailable metrics separate from measured zero and scopes the rates", () => {
        const html = render();
        expect(html).toContain(
            "Telegram inbound - reporting accounts</dt><dd>Unavailable",
        );
        expect(html).toContain("HTTP delivered - server-wide</dt><dd>1 KiB/s");
        expect(html).toContain(
            "Media cache hits - observed window</dt><dd>Unavailable",
        );
        expect(html).toContain(
            "Media cache misses - observed window</dt><dd>0",
        );
        expect(html).toContain("not users or background-job counts");
    });
    it("shows simultaneous waits and distinguishes reported certainty without guessing actions", () => {
        const html = render({
            constraints: [
                {
                    code: "telegram_queue",
                    certainty: "observed",
                    evidence: [
                        {
                            name: "configured_operations_per_account",
                            value: 3,
                            unit: "operations",
                        },
                    ],
                },
                {
                    code: "scratch_wait",
                    certainty: "observed",
                    evidence: [
                        { name: "reserved_bytes", value: 1024, unit: "bytes" },
                        { name: "waiting_heads", value: 2, unit: "streams" },
                    ],
                },
                { code: "future_code", certainty: "likely", evidence: [] },
            ],
        });
        const summaries = html.match(/<summary>[\s\S]*?<\/summary>/g) || [];
        expect(summaries[0]).toContain("Waiting for Telegram");
        expect(summaries[0]).toContain("Observed");
        expect(summaries[0]).not.toContain("3 operations");
        expect(summaries[1]).toContain("Waiting for a stream buffer");
        expect(summaries[1]).toContain("2 streams");
        expect(summaries[2]).toContain("Additional constraint");
        expect(summaries[2]).toContain("Likely");
        expect(html).toContain("3 operations");
        expect(html).toContain("1 KiB");
        expect(html).toContain("not a measurement of physical capacity");
        expect(html).toContain("Reported code: future_code");
    });
});
