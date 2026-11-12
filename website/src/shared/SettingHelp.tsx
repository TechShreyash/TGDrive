import { Popover } from "@mantine/core";
import { Info } from "lucide-react";
import type { ReactNode } from "react";
import "./SettingHelp.css";

export default function SettingHelp({
    label,
    children,
}: {
    label: string;
    children: ReactNode;
}) {
    return (
        <Popover
            width={280}
            position="bottom-start"
            withArrow
            trapFocus
            returnFocus
        >
            <Popover.Target>
                <button
                    type="button"
                    className="setting-help-button"
                    aria-label={"About " + label}
                >
                    <Info size={16} aria-hidden="true" />
                </button>
            </Popover.Target>
            <Popover.Dropdown
                className="setting-help-content"
                aria-label={label}
            >
                <div tabIndex={-1} data-autofocus>
                    {children}
                </div>
            </Popover.Dropdown>
        </Popover>
    );
}
