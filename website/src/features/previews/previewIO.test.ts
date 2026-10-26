import { describe, expect, it, vi } from "vitest";
import { PreviewLimitError, readBoundedResponse } from "./previewIO";
describe("bounded preview request cleanup", () => {
    it("cancels an oversized declared body before reading it", async () => {
        const cancel = vi.fn();
        const response = new Response(new ReadableStream({ cancel }), {
            headers: { "Content-Length": "500" },
        });
        await expect(
            readBoundedResponse(response, 100, new AbortController().signal),
        ).rejects.toBeInstanceOf(PreviewLimitError);
        expect(cancel).toHaveBeenCalledOnce();
    });
    it("cancels a source that exceeds its declared length", async () => {
        const cancel = vi.fn();
        const response = new Response(
            new ReadableStream({
                start(controller) {
                    controller.enqueue(new Uint8Array(101));
                },
                cancel,
            }),
            { headers: { "Content-Length": "1" } },
        );
        await expect(
            readBoundedResponse(response, 100, new AbortController().signal),
        ).rejects.toBeInstanceOf(PreviewLimitError);
        expect(cancel).toHaveBeenCalledOnce();
    });
    it("cancels a pending reader when a preview is closed", async () => {
        const cancel = vi.fn(),
            controller = new AbortController();
        const response = new Response(new ReadableStream({ cancel }));
        const task = readBoundedResponse(response, 100, controller.signal);
        controller.abort();
        await expect(task).rejects.toMatchObject({ name: "AbortError" });
        expect(cancel).toHaveBeenCalledOnce();
    });
});
