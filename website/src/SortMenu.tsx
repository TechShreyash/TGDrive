import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { ArrowUpDown, Check, ChevronDown } from "lucide-react";
import "./SortMenu.css";

export const SORT_OPTIONS = [
  { value: "name", label: "Name" },
  { value: "modified", label: "Date Added" },
  { value: "size", label: "File Size" },
] as const;
export type FileSort = (typeof SORT_OPTIONS)[number]["value"];

export default function SortMenu({
  value,
  onChange,
}: {
  value: FileSort;
  onChange: (value: FileSort) => void;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0, width: 192 });
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
    const width = Math.min(192, window.innerWidth - 24);
    setPosition({
      top: Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - 160)),
      left: Math.max(
        12,
        Math.min(rect.right - width, window.innerWidth - width - 12),
      ),
      width,
    });
    setOpen(true);
  };
  useLayoutEffect(() => {
    if (open)
      menu.current
        ?.querySelector<HTMLElement>('[aria-checked="true"]')
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
    const reposition = () => setOpen(false);
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
    ];
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
        aria-label={"Sort Files: " + label}
        onClick={() => (open ? close() : show())}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            show();
          }
        }}
      >
        <ArrowUpDown size={15} />
        <span>{label}</span>
        <ChevronDown size={14} />
      </button>
      {open &&
        createPortal(
          <div
            ref={menu}
            id={id}
            className="file-sort-menu"
            role="menu"
            aria-label="Sort Files"
            style={position}
            onKeyDown={keydown}
          >
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
                  className={value === option.value ? "" : "sort-check-hidden"}
                />
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
