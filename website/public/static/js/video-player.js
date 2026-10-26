const player = document.getElementById("video-player");
const errorPanel = document.getElementById("player-error");
const errorMessage = document.getElementById("player-error-message");
const status = document.getElementById("player-status");
const copyButton = document.getElementById("copy-stream");
const copyFileButton = document.getElementById("copy-file");
const downloadLink = document.getElementById("download-link");
const toast = document.getElementById("player-toast");

let toastTimer = null;

function showToast(message) {
    toast.textContent = message;
    toast.hidden = false;
    if (toastTimer) {
        window.clearTimeout(toastTimer);
    }
    toastTimer = window.setTimeout(() => {
        toast.hidden = true;
        toastTimer = null;
    }, 4000);
}

function showError(message) {
    errorMessage.textContent = message;
    errorPanel.hidden = false;
    status.textContent = "Playback unavailable";
}

function buildDownloadUrl(path, auth) {
    const fileUrl = new URL("/file", window.location.origin);
    fileUrl.searchParams.set("path", path);
    if (auth) {
        fileUrl.searchParams.set("auth", auth);
    }
    return fileUrl;
}

async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        const textArea = document.createElement("textarea");
        textArea.className = "clipboard-helper";
        textArea.value = text;
        textArea.setAttribute("readonly", "");
        document.body.append(textArea);
        textArea.select();
        const copied = document.execCommand("copy");
        textArea.remove();
        if (!copied) {
            throw new Error("Clipboard access was denied.");
        }
    }
}

function initializePlayer() {
    const pageUrl = new URL(window.location.href);
    const path = pageUrl.searchParams.get("path");
    const auth = pageUrl.searchParams.get("auth") || "";

    if (!path) {
        showError("The video link is missing a file path.");
        copyButton.disabled = true;
        copyFileButton.disabled = true;
        downloadLink.removeAttribute("href");
        return;
    }

    const fileUrl = buildDownloadUrl(path, auth);
    player.src = fileUrl;
    const downloadUrl = new URL(fileUrl);
    downloadUrl.searchParams.set("download", "1");
    downloadLink.href = downloadUrl;
    downloadLink.setAttribute("download", "");
    status.textContent = "Ready to play";

    player.addEventListener("loadstart", () => {
        status.textContent = "Loading video…";
    });
    player.addEventListener("canplay", () => {
        status.textContent = "Ready to play";
    });
    player.addEventListener("playing", () => {
        status.textContent = "Playing";
    });
    player.addEventListener("pause", () => {
        if (!player.ended) {
            status.textContent = "Paused";
        }
    });
    player.addEventListener("ended", () => {
        status.textContent = "Playback complete";
    });
    player.addEventListener("error", () => {
        showError(
            "The file is unavailable or its format is not supported by this browser.",
        );
    });

    copyButton.addEventListener("click", async () => {
        try {
            await copyText(pageUrl.toString());
            showToast("Stream link copied.");
        } catch (error) {
            showToast(
                error instanceof Error
                    ? error.message
                    : "Could not copy the stream link.",
            );
        }
    });

    copyFileButton.addEventListener("click", async () => {
        try {
            await copyText(fileUrl.toString());
            showToast("File link copied.");
        } catch (error) {
            showToast(
                error instanceof Error
                    ? error.message
                    : "Could not copy the file link.",
            );
        }
    });
}

initializePlayer();
