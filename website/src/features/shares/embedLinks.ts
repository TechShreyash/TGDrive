export function embedUrl(shareLink: string): string {
    const url = new URL(shareLink);
    url.pathname = "/embed";
    return url.toString();
}
