import { formatByteProgress } from "../../shared/types";
import type { ScratchSnapshot } from "./performanceApi";

export default function StreamMemoryFact({
    scratch,
}: {
    scratch?: ScratchSnapshot;
}) {
    if (!scratch) return null;
    return (
        <div>
            <dt>Active stream memory</dt>
            <dd>{formatByteProgress(scratch.bytes, scratch.max_bytes)}</dd>
            <small>
                {scratch.active_streams} streams · {scratch.waiting_heads}{" "}
                waiting for memory; separate from retained cache
            </small>
        </div>
    );
}
