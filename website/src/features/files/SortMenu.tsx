import { useEffect, useState } from "react";
import {
    Menu,
    MenuContent,
    MenuItemIndicator,
    MenuRadioGroup,
    MenuRadioItem,
    MenuTrigger,
} from "@ui-sdk/react/menu";
import {
    ArrowUp,
    ArrowDown,
    Check,
    ChevronDown,
    List,
    LayoutGrid,
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

const directionHints = {
    name: { asc: "A–Z", desc: "Z–A" },
    modified: { asc: "Oldest first", desc: "Newest first" },
    size: { asc: "Smallest first", desc: "Largest first" },
};

export default function SortMenu({
    value,
    onChange,
    order,
    onOrderChange,
    view,
    onViewChange,
    fixedSort = false,
}: {
    value: FileSort;
    onChange: (value: FileSort) => void;
    order: SortOrder;
    onOrderChange: (value: SortOrder) => void;
    view?: "list" | "grid";
    onViewChange?: (value: "list" | "grid") => void;
    fixedSort?: boolean;
}) {
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
    if (fixedSort && !combined) return null;

    const label = SORT_OPTIONS.find((option) => option.value === value)!.label;
    const menuLabel = combined ? "View and sort" : "Sort Files";
    const ViewIcon = view === "grid" ? LayoutGrid : List;
    const orderLabel = order === "asc" ? "Ascending" : "Descending";
    const viewLabel = view === "grid" ? "Grid view" : "List view";
    const selected = (
        <MenuItemIndicator>
            <Check size={16} aria-hidden="true" />
        </MenuItemIndicator>
    );

    return (
        <Menu container={document.body} modal={false}>
            <MenuTrigger
                className="file-sort-trigger"
                aria-label={combined ? menuLabel : menuLabel + ": " + label}
                aria-description={
                    combined
                        ? fixedSort
                            ? viewLabel
                            : viewLabel + ". " + orderLabel
                        : orderLabel
                }
            >
                {combined ? (
                    <>
                        <ViewIcon size={18} aria-hidden="true" />
                        <ChevronDown size={12} aria-hidden="true" />
                    </>
                ) : (
                    <>
                        {order === "asc" ? (
                            <ArrowUp size={15} aria-hidden="true" />
                        ) : (
                            <ArrowDown size={15} aria-hidden="true" />
                        )}
                        <span>{label}</span>
                        <ChevronDown size={14} aria-hidden="true" />
                    </>
                )}
            </MenuTrigger>
            <MenuContent
                className="file-sort-menu"
                aria-label={menuLabel}
                align="end"
                sideOffset={6}
                collisionPadding={12}
            >
                {combined && (
                    <MenuRadioGroup
                        className={fixedSort ? undefined : "sort-view-group"}
                        aria-label="File view"
                        value={view}
                        onValueChange={onViewChange}
                    >
                        <MenuRadioItem
                            value="list"
                            label="List view"
                            closeOnSelect
                            render={<button type="button" />}
                        >
                            <span className="sort-view-option">
                                <List size={18} aria-hidden="true" /> List view
                            </span>
                            {selected}
                        </MenuRadioItem>
                        <MenuRadioItem
                            value="grid"
                            label="Grid view"
                            closeOnSelect
                            render={<button type="button" />}
                        >
                            <span className="sort-view-option">
                                <LayoutGrid size={18} aria-hidden="true" /> Grid
                                view
                            </span>
                            {selected}
                        </MenuRadioItem>
                    </MenuRadioGroup>
                )}
                {!fixedSort && (
                    <>
                        <MenuRadioGroup
                            aria-label="Sort field"
                            value={value}
                            onValueChange={onChange}
                        >
                            {SORT_OPTIONS.map((option) => (
                                <MenuRadioItem
                                    key={option.value}
                                    value={option.value}
                                    label={option.label}
                                    closeOnSelect
                                    render={<button type="button" />}
                                >
                                    <span>{option.label}</span>
                                    {selected}
                                </MenuRadioItem>
                            ))}
                        </MenuRadioGroup>
                        <MenuRadioGroup
                            className="sort-direction-group"
                            aria-label="Sort direction"
                            value={order}
                            onValueChange={onOrderChange}
                        >
                            {(["asc", "desc"] as const).map((direction) => {
                                const label =
                                    direction === "asc"
                                        ? "Ascending"
                                        : "Descending";
                                return (
                                    <MenuRadioItem
                                        key={direction}
                                        value={direction}
                                        label={label}
                                        aria-label={label}
                                        closeOnSelect
                                        render={<button type="button" />}
                                    >
                                        <span>
                                            {label}
                                            <small>
                                                {
                                                    directionHints[value][
                                                        direction
                                                    ]
                                                }
                                            </small>
                                        </span>
                                        {selected}
                                    </MenuRadioItem>
                                );
                            })}
                        </MenuRadioGroup>
                    </>
                )}
            </MenuContent>
        </Menu>
    );
}
