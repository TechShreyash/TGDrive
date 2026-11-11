import { useEffect, useRef, useState } from "react";
import { Play } from "lucide-react";
import { FileIcon, fileKind } from "../../shared/components";
import type { DriveItem } from "../../shared/types";
import { fetchThumbnail, thumbnailCandidate, thumbnailUrl } from "./thumbnails";
import {
    formatVideoTime,
    type VideoPosition,
} from "../previews/video/videoWatchProgress";

export default function FileThumbnail({
    item,
    auth,
    disabled = false,
    compact = false,
    progress,
}: {
    item: DriveItem;
    auth: string;
    disabled?: boolean;
    compact?: boolean;
    progress?: VideoPosition;
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
    const fraction =
        progress && progress.duration_seconds > 0
            ? progress.position_seconds / progress.duration_seconds
            : 0;
    const position =
        fileKind(item.name) === "video" && fraction > 0 && fraction <= 1
            ? progress
            : undefined;
    return (
        <>
            <div
                ref={target}
                className={
                    "file-tile-preview" +
                    (compact ? " file-list-thumbnail" : "") +
                    (loading ? " thumbnail-loading" : "")
                }
                data-kind={fileKind(item.name, item.type === "folder")}
                data-has-image={source ? "true" : undefined}
                aria-hidden="true"
            >
                {source ? (
                    <>
                        <img
                            src={source}
                            alt=""
                            decoding="async"
                            onError={() => setSource(null)}
                        />
                        {!compact && fileKind(item.name) === "video" && (
                            <span className="file-tile-play">
                                <Play size={16} />
                            </span>
                        )}
                        {/* Photos and videos explain themselves; page-like thumbnails keep their format. */}
                        {!compact &&
                            !["image", "video"].includes(
                                fileKind(item.name),
                            ) && (
                                <span className="file-tile-format">
                                    {(item.name.split(".").at(-1) || "File")
                                        .slice(0, 8)
                                        .toUpperCase()}
                                </span>
                            )}
                    </>
                ) : (
                    <>
                        <FileIcon
                            name={item.name}
                            folder={item.type === "folder"}
                        />
                        {!compact && item.type !== "folder" && (
                            <span className="file-tile-format">
                                {(item.name.split(".").at(-1) || "File")
                                    .slice(0, 8)
                                    .toUpperCase()}
                            </span>
                        )}
                    </>
                )}
                {position && (
                    <span className="file-playback-progress">
                        <span style={{ width: fraction * 100 + "%" }} />
                    </span>
                )}
            </div>
            {position && (
                <meter
                    className="sr-only"
                    min={0}
                    max={position.duration_seconds}
                    value={position.position_seconds}
                    aria-label={"Saved playback position for " + item.name}
                    aria-valuetext={
                        formatVideoTime(position.position_seconds) +
                        " of " +
                        formatVideoTime(position.duration_seconds)
                    }
                />
            )}
        </>
    );
}
