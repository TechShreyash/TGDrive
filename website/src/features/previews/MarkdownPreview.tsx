import { Button } from "@mantine/core";
import { useState } from "react";
import Markdown from "react-markdown";
import "./MarkdownPreview.css";

export default function MarkdownPreview({
    text,
    name,
    truncated,
}: {
    text: string;
    name: string;
    truncated?: boolean;
}) {
    const [source, setSource] = useState(false);
    return (
        <div className="text-preview markdown-reader">
            <div className="preview-document-toolbar">
                <span>Markdown · Read-only</span>
                <Button
                    type="button"
                    variant="subtle"
                    size="compact-sm"
                    className="markdown-source-toggle"
                    aria-pressed={source}
                    onClick={() => setSource((show) => !show)}
                >
                    Show source
                </Button>
            </div>
            {truncated && (
                <p className="preview-limit" role="status">
                    Showing the first 256 KB. Download the original for the
                    complete file.
                </p>
            )}
            {source ? (
                <pre
                    className="wrap-lines"
                    tabIndex={0}
                    role="region"
                    aria-label={"Source of " + name}
                >
                    <code>{text}</code>
                </pre>
            ) : (
                <article
                    className="markdown-preview"
                    aria-label={"Contents of " + name}
                >
                    <Markdown
                        skipHtml
                        components={{
                            a: ({ children }) => <span>{children}</span>,
                            img: ({ alt }) => (alt ? <span>{alt}</span> : null),
                        }}
                    >
                        {text}
                    </Markdown>
                </article>
            )}
        </div>
    );
}
