import {
    Children,
    createElement,
    isValidElement,
    type ReactElement,
    type ReactNode,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { HistoryPagination } from "./TransferHistory";

function render(page = 1, totalPages = 3, loading = false) {
    return renderToStaticMarkup(
        createElement(HistoryPagination, {
            page,
            totalPages,
            totalTasks: (totalPages - 1) * 20 + 1,
            loading,
            onPageChange: () => {},
        }),
    );
}

type ButtonProps = {
    children?: ReactNode;
    "aria-label"?: string;
    onClick?: () => void;
};
function findButton(
    node: ReactNode,
    label: string,
): ReactElement<ButtonProps> | undefined {
    for (const child of Children.toArray(node)) {
        if (!isValidElement<ButtonProps>(child)) continue;
        if (child.type === "button" && child.props["aria-label"] === label)
            return child;
        const nested = findButton(child.props.children, label);
        if (nested) return nested;
    }
}

describe("numbered transfer task pages", () => {
    it("shows direct numbered pages with the current page identified", () => {
        const html = render(2);
        expect(html).toContain('aria-label="Finished transfer pages"');
        expect(html).toContain("Page 2 of 3 · 41 tasks");
        expect(html).toContain('aria-label="Page 1 of finished transfers"');
        expect(html).toContain(
            'aria-label="Page 2 of finished transfers" aria-current="page"',
        );
        expect(html).toContain('aria-label="Page 3 of finished transfers"');
        expect(html).not.toContain("Latest");
    });

    it("disables the previous and next controls at the respective ends", () => {
        expect(render(1)).toMatch(
            /aria-label="Previous page of finished transfers"[^>]*disabled=""/,
        );
        expect(render(3)).toMatch(
            /aria-label="Next page of finished transfers"[^>]*disabled=""/,
        );
        expect(render(2)).not.toContain('disabled=""');
    });

    it("keeps large histories compact while exposing first, current and last pages", () => {
        const html = render(40, 100);
        for (const page of [1, 39, 40, 41, 100])
            expect(html).toContain(
                'aria-label="Page ' + page + ' of finished transfers"',
            );
        expect(html).not.toContain(
            'aria-label="Page 20 of finished transfers"',
        );
        expect(html.match(/aria-hidden="true">…<\/span>/g)).toHaveLength(2);
    });

    it("disables every page action while loading without hiding page positions", () => {
        const html = render(2, 3, true);
        expect(html).toContain('aria-busy="true"');
        expect(html).toContain("Loading…");
        expect(html.match(/disabled=""/g)).toHaveLength(5);
        expect(html).toContain('aria-current="page"');
    });

    it("opens a selected numbered page directly and moves one page at a time", () => {
        const onPageChange = vi.fn();
        const view = HistoryPagination({
            page: 2,
            totalPages: 5,
            totalTasks: 81,
            loading: false,
            onPageChange,
        });
        findButton(view, "Page 5 of finished transfers")?.props.onClick?.();
        findButton(
            view,
            "Previous page of finished transfers",
        )?.props.onClick?.();
        findButton(view, "Next page of finished transfers")?.props.onClick?.();
        expect(onPageChange.mock.calls).toEqual([[5], [1], [3]]);
    });
});
