// Each Movi WASM instance owns one Asyncify stack and one pending source read.
// Serialize complete operations so packet data is copied before its shared
// buffer can be reused by a different read or seek.
export function serializeDemuxOperations(bindings) {
    let pending = null;
    for (const name of [
        "open",
        "scanDuration",
        "seek",
        "readFrame",
        "decodeSubtitle",
        "prefetchSubtitleCues",
    ]) {
        const operation = bindings[name].bind(bindings);
        bindings[name] = (...args) => {
            const run = () => operation(...args);
            const afterPrevious = () => {
                if (!bindings.contextPtr || bindings.teardownPending)
                    throw new DOMException("Source closed", "AbortError");
                return run();
            };
            const result = pending
                ? pending.then(afterPrevious, afterPrevious)
                : run();
            pending = result;
            const release = () => {
                if (pending === result) pending = null;
            };
            result.then(release, release);
            return result;
        };
    }
}
