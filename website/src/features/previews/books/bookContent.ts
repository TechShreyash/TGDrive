export type BookEntry = { name: string; label: string };
export type Book = {
    kind: "epub" | "comic";
    title?: string;
    entries: BookEntry[];
};
export type BookNode =
    | string
    | {
          tag: string;
          children: BookNode[];
          src?: string;
          alt?: string;
          href?: string;
          id?: string;
      };
const tags = new Set(
    "p div span h1 h2 h3 h4 h5 h6 strong b em i u s a img ul ol li blockquote pre code br hr table thead tbody tr th td caption dl dt dd sup sub figure figcaption section article".split(
        " ",
    ),
);
function safeEntry(name: string) {
    return (
        name.length > 0 &&
        name.length <= 1024 &&
        !name.startsWith("/") &&
        !/^[a-z][a-z0-9+.-]*:/i.test(name) &&
        !name.includes("\\") &&
        !/[\x00-\x1f]/.test(name) &&
        !name.split("/").some((part) => part === ".." || part === ".")
    );
}
export function validateBook(data: Book): Book {
    if (
        !data ||
        !["epub", "comic"].includes(data.kind) ||
        !Array.isArray(data.entries) ||
        !data.entries.length ||
        data.entries.length > 10000 ||
        !data.entries.every(
            (entry) =>
                entry &&
                typeof entry.name === "string" &&
                safeEntry(entry.name) &&
                typeof entry.label === "string" &&
                entry.label.length <= 1024,
        )
    )
        throw new Error("The server returned an unreadable book index.");
    return data;
}
export function safeNodes(data: unknown): BookNode[] {
    let count = 0;
    function visit(value: unknown, depth: number): BookNode {
        if (++count > 20000 || depth > 100)
            throw new Error("This chapter is too complex to display.");
        if (typeof value === "string") return value;
        const node = value as Exclude<BookNode, string>;
        if (
            !node ||
            typeof node !== "object" ||
            !tags.has(node.tag) ||
            !Array.isArray(node.children)
        )
            throw new Error("The server returned an unreadable chapter.");
        return {
            tag: node.tag,
            children: node.children.map((child) => visit(child, depth + 1)),
            src:
                typeof node.src === "string" &&
                safeEntry(node.src.split("#")[0])
                    ? node.src.split("#")[0]
                    : undefined,
            href:
                typeof node.href === "string" &&
                safeEntry(node.href.split("#")[0])
                    ? node.href
                    : undefined,
            alt: typeof node.alt === "string" ? node.alt.slice(0, 500) : "",
            id: typeof node.id === "string" ? node.id.slice(0, 128) : undefined,
        };
    }
    if (!Array.isArray(data)) throw new Error("The chapter could not be read.");
    return data.map((node) => visit(node, 0));
}
