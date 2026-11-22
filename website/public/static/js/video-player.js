const player = document.getElementById("video-player");
const errorPanel = document.getElementById("player-error");
const errorMessage = document.getElementById("player-error-message");
const status = document.getElementById("player-status");
const title = document.getElementById("player-title");
const copyButton = document.getElementById("copy-stream");
const copyFileButton = document.getElementById("copy-file");
const downloadLink = document.getElementById("download-link");
const toast = document.getElementById("player-toast");
const copyFallback = document.getElementById("player-copy-fallback");
const copyLabel = document.getElementById("player-copy-label");
const copyValue = document.getElementById("player-copy-value");

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

async function copyText(text, label) {
    try {
        await navigator.clipboard.writeText(text);
        copyFallback.hidden = true;
        copyValue.value = "";
        showToast(label + " copied.");
    } catch {
        toast.hidden = true;
        copyLabel.textContent = label + " to copy";
        copyValue.value = text;
        copyFallback.hidden = false;
        copyValue.focus();
        copyValue.select();
    }
}

async function loadVideoName(fileUrl) {
    try {
        const response = await fetch(fileUrl, {
            method: "HEAD",
            cache: "no-store",
        });
        if (!response.ok) return;
        const disposition = response.headers.get("Content-Disposition");
        const encoded = disposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
        if (!encoded) return;
        const name = decodeURIComponent(encoded);
        title.textContent = name;
        document.title = name + " · TGDrive";
    } catch {
        // The video and download controls still work if the title cannot be read.
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
    void loadVideoName(fileUrl);
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

    copyButton.addEventListener("click", () => {
        void copyText(pageUrl.toString(), "Stream link");
    });

    copyFileButton.addEventListener("click", () => {
        void copyText(fileUrl.toString(), "File link");
    });
}

initializePlayer();
