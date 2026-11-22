import { Popover } from "@mantine/core";
import { Info } from "lucide-react";
import { type ReactNode, useState } from "react";
import "./SettingHelp.css";

export default function SettingHelp({
    label,
    children,
}: {
    label: string;
    children: ReactNode;
}) {
    const [opened, setOpened] = useState(false);
    return (
        <Popover
            opened={opened}
            onChange={setOpened}
            width={280}
            position="bottom-start"
            middlewares={{ size: { padding: 8 } }}
            withArrow
            trapFocus
            returnFocus
        >
            <Popover.Target>
                <button
                    type="button"
                    className="setting-help-button"
                    aria-label={"About " + label}
                    onClick={() => setOpened((value) => !value)}
                    onKeyDown={(event) => {
                        // Escape can arrive before Mantine moves focus inside.
                        if (!opened || event.key !== "Escape") return;
                        event.preventDefault();
                        event.stopPropagation();
                        setOpened(false);
                    }}
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
