import { useEffect, useRef, useState } from "react";
import { FileIcon } from "../../shared/components";
import type { DriveItem } from "../../shared/types";
import { fetchThumbnail, thumbnailCandidate, thumbnailUrl } from "./thumbnails";

export default function FileThumbnail({
    item,
    auth,
    disabled = false,
}: {
    item: DriveItem;
    auth: string;
    disabled?: boolean;
}) {
    const target = useRef<HTMLDivElement>(null);
    const [source, setSource] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    useEffect(() => {
        setSource(null);
        setLoading(false);
        const connection = (
            navigator as Navigator & { connection?: { saveData?: boolean } }
        ).connection;
        if (disabled || !thumbnailCandidate(item) || connection?.saveData)
            return;
        const element = target.current!;
        let controller: AbortController | null = null;
        let visible = false;
        let loaded = false;
        const load = () => {
            if (controller || !visible || loaded) return;
            const work = new AbortController();
            controller = work;
            setLoading(true);
            void fetchThumbnail(thumbnailUrl(item, auth), work.signal)
                .then((url) => {
                    if (!work.signal.aborted && visible) {
                        loaded = Boolean(url);
                        setSource(url);
                    }
                })
                .catch(() => {})
                .finally(() => {
                    if (controller === work) {
                        controller = null;
                        setLoading(false);
                    }
                });
        };
        const observer = new IntersectionObserver(
            (entries) => {
                const entry = entries.find((value) => value.target === element);
                if (!entry) return;
                visible = entry.isIntersecting;
                if (visible) load();
                else {
                    controller?.abort();
                    controller = null;
                    setLoading(false);
                }
            },
            { rootMargin: "120px" },
        );
        observer.observe(element);
        return () => {
            visible = false;
            observer.disconnect();
            controller?.abort();
            controller = null;
        };
    }, [
        item.id,
        item.path,
        item.name,
        item.size,
        item.upload_date,
        auth,
        disabled,
    ]);
    return (
        <div
            ref={target}
            className={
                "file-tile-preview" + (loading ? " thumbnail-loading" : "")
            }
            aria-hidden="true"
        >
            {source ? (
                <img
                    src={source}
                    alt=""
                    decoding="async"
                    onError={() => setSource(null)}
                />
            ) : (
                <>
                    <FileIcon
                        name={item.name}
                        folder={item.type === "folder"}
                    />
                    {item.type !== "folder" && (
                        <span className="file-tile-format">
                            {(item.name.split(".").at(-1) || "File")
                                .slice(0, 8)
                                .toUpperCase()}
                        </span>
                    )}
                </>
            )}
        </div>
    );
}
