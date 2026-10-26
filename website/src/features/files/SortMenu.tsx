import {
    useEffect,
    useId,
    useLayoutEffect,
    useRef,
    useState,
    type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import {
    ArrowUp,
    ArrowDown,
    Check,
    ChevronDown,
    List,
    LayoutGrid,
    SlidersHorizontal,
} from "lucide-react";
import "./SortMenu.css";
import type { SortOrder } from "./fileSorting";
import { COMPACT_LAYOUT_QUERY } from "../../shared/responsive";

export const SORT_OPTIONS = [
    { value: "name", label: "Name" },
    { value: "modified", label: "Date Added" },
    { value: "size", label: "File Size" },
] as const;
export type FileSort = (typeof SORT_OPTIONS)[number]["value"];

export default function SortMenu({
    value,
    onChange,
    order,
    onOrderChange,
    view,
    onViewChange,
}: {
    value: FileSort;
    onChange: (value: FileSort) => void;
    order: SortOrder;
    onOrderChange: (value: SortOrder) => void;
    view?: "list" | "grid";
    onViewChange?: (value: "list" | "grid") => void;
}) {
    const [open, setOpen] = useState(false);
    const [mobile, setMobile] = useState(
        () => window.matchMedia(COMPACT_LAYOUT_QUERY).matches,
    );
    useEffect(() => {
        const media = window.matchMedia(COMPACT_LAYOUT_QUERY);
        const update = () => setMobile(media.matches);
        media.addEventListener("change", update);
        return () => media.removeEventListener("change", update);
    }, []);
    const combined = mobile && Boolean(onViewChange);
    const [position, setPosition] = useState({ top: 0, left: 0, width: 224 });
    const trigger = useRef<HTMLButtonElement>(null);
    const menu = useRef<HTMLDivElement>(null);
    const id = useId();
    const label = SORT_OPTIONS.find((option) => option.value === value)!.label;
    const close = (returnFocus = true) => {
        setOpen(false);
        if (returnFocus) trigger.current?.focus();
    };
    const show = () => {
        const rect = trigger.current!.getBoundingClientRect();
        const width = Math.min(224, window.innerWidth - 24);
        setPosition({
            top: Math.max(
                12,
                Math.min(rect.bottom + 6, window.innerHeight - 260),
            ),
            left: Math.max(
                12,
                Math.min(rect.right - width, window.innerWidth - width - 12),
            ),
            width,
        });
        setOpen(true);
    };
    useLayoutEffect(() => {
        if (!open || !menu.current || !trigger.current) return;
        const height = menu.current.getBoundingClientRect().height;
        const anchor = trigger.current.getBoundingClientRect();
        setPosition((current) => ({
            ...current,
            top: Math.max(
                12,
                Math.min(anchor.bottom + 6, window.innerHeight - height - 12),
            ),
        }));
        Array.from(
            menu.current.querySelectorAll<HTMLElement>('[aria-checked="true"]'),
        )
            .find((option) => option.getClientRects().length > 0)
            ?.focus();
    }, [open]);
    useEffect(() => {
        if (!open) return;
        const outside = (event: PointerEvent) => {
            if (
                !menu.current?.contains(event.target as Node) &&
                !trigger.current?.contains(event.target as Node)
            )
                setOpen(false);
        };
        const reposition = (event: Event) => {
            if (
                !(event.target instanceof Node) ||
                !menu.current?.contains(event.target)
            )
                setOpen(false);
        };
        document.addEventListener("pointerdown", outside);
        window.addEventListener("resize", reposition);
        window.addEventListener("scroll", reposition, true);
        return () => {
            document.removeEventListener("pointerdown", outside);
            window.removeEventListener("resize", reposition);
            window.removeEventListener("scroll", reposition, true);
        };
    }, [open]);
    const keydown = (event: KeyboardEvent<HTMLDivElement>) => {
        const options = [
            ...menu.current!.querySelectorAll<HTMLButtonElement>(
                '[role="menuitemradio"]',
            ),
        ].filter((option) => option.getClientRects().length > 0);
        const current = options.indexOf(
            document.activeElement as HTMLButtonElement,
        );
        let next = current;
        if (event.key === "ArrowDown") next = (current + 1) % options.length;
        else if (event.key === "ArrowUp")
            next = (current + options.length - 1) % options.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = options.length - 1;
        else if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            close();
            return;
        } else if (event.key === "Tab") {
            close();
            return;
        } else return;
        event.preventDefault();
        options[next]?.focus();
    };
    return (
        <>
            <button
                ref={trigger}
                className="file-sort-trigger"
                type="button"
                aria-haspopup="menu"
                aria-expanded={open}
                aria-controls={open ? id : undefined}
                aria-label={combined ? "View and sort" : "Sort Files: " + label}
                aria-description={order === "asc" ? "Ascending" : "Descending"}
                onClick={() => (open ? close() : show())}
                onKeyDown={(event) => {
                    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                        event.preventDefault();
                        show();
                    }
                }}
            >
                {combined ? (
                    <SlidersHorizontal size={19} aria-hidden="true" />
                ) : (
                    <>
                        {order === "asc" ? (
                            <ArrowUp size={15} />
                        ) : (
                            <ArrowDown size={15} />
                        )}
                        <span>{label}</span>
                        <ChevronDown size={14} />
                    </>
                )}
            </button>
            {open &&
                createPortal(
                    <div
                        ref={menu}
                        id={id}
                        className="file-sort-menu"
                        role="menu"
                        aria-label={combined ? "View and sort" : "Sort Files"}
                        style={position}
                        onKeyDown={keydown}
                    >
                        {combined && (
                            <div
                                className="sort-view-group"
                                role="group"
                                aria-label="File view"
                            >
                                {(["list", "grid"] as const).map((layout) => (
                                    <button
                                        key={layout}
                                        type="button"
                                        role="menuitemradio"
                                        tabIndex={-1}
                                        aria-checked={view === layout}
                                        onClick={() => {
                                            onViewChange?.(layout);
                                            close();
                                        }}
                                    >
                                        <span className="sort-view-option">
                                            {layout === "list" ? (
                                                <List
                                                    size={18}
                                                    aria-hidden="true"
                                                />
                                            ) : (
                                                <LayoutGrid
                                                    size={18}
                                                    aria-hidden="true"
                                                />
                                            )}
                                            {layout === "list"
                                                ? "List view"
                                                : "Grid view"}
                                        </span>
                                        <Check
                                            size={16}
                                            aria-hidden="true"
                                            className={
                                                view === layout
                                                    ? ""
                                                    : "sort-check-hidden"
                                            }
                                        />
                                    </button>
                                ))}
                            </div>
                        )}
                        <div role="group" aria-label="Sort field">
                            {SORT_OPTIONS.map((option) => (
                                <button
                                    key={option.value}
                                    type="button"
                                    role="menuitemradio"
                                    tabIndex={-1}
                                    aria-checked={value === option.value}
                                    onClick={() => {
                                        onChange(option.value);
                                        close();
                                    }}
                                >
                                    <span>{option.label}</span>
                                    <Check
                                        size={16}
                                        aria-hidden="true"
                                        className={
                                            value === option.value
                                                ? ""
                                                : "sort-check-hidden"
                                        }
                                    />
                                </button>
                            ))}
                        </div>
                        <div
                            className="sort-direction-group"
                            role="group"
                            aria-label="Sort direction"
                        >
                            {(["asc", "desc"] as const).map((direction) => (
                                <button
                                    key={direction}
                                    type="button"
                                    role="menuitemradio"
                                    tabIndex={-1}
                                    aria-checked={order === direction}
                                    aria-label={
                                        direction === "asc"
                                            ? "Ascending"
                                            : "Descending"
                                    }
                                    onClick={() => {
                                        onOrderChange(direction);
                                        close();
                                    }}
                                >
                                    <span>
                                        {direction === "asc"
                                            ? "Ascending"
                                            : "Descending"}
                                        <small>
                                            {value === "name"
                                                ? direction === "asc"
                                                    ? "A–Z"
                                                    : "Z–A"
                                                : value === "size"
                                                  ? direction === "asc"
                                                      ? "Smallest first"
                                                      : "Largest first"
                                                  : direction === "asc"
                                                    ? "Oldest first"
                                                    : "Newest first"}
                                        </small>
                                    </span>
                                    <Check
                                        size={16}
                                        aria-hidden="true"
                                        className={
                                            order === direction
                                                ? ""
                                                : "sort-check-hidden"
                                        }
                                    />
                                </button>
                            ))}
                        </div>
                    </div>,
                    document.body,
                )}
        </>
    );
}
