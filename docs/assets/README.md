# Screenshots and demo videos

[Docs home](../README.md) · [Project README](../../README.md)

The SVGs in this folder are labelled placeholders. Replace them with real captures
when ready; no screenshot or video has been recorded for these slots.

## Screenshot slots

| Placeholder | Capture to add |
| --- | --- |
| `drive-overview.svg` | Desktop My Files with folders and sample files |
| `upload-progress.svg` | Transfers showing device upload and server processing |
| `media-preview.svg` | A video or book open in the viewer |
| `mobile-drive.svg` | The file browser on a phone |
| `demo-preview.svg` | Poster frame for the walkthrough below |

1. Add a PNG/JPEG here, for example `drive-overview.png`.
2. Change the matching `.svg` reference in the Markdown to your filename.
3. Update the alt text and remove its “placeholder” wording.

Use a wide desktop capture (for example 1440 × 810) and a portrait phone capture.
Keep text legible. Use disposable demo files; hide credentials, private filenames
and share tokens. Search for `MEDIA:` comments to find the prepared slots.

## Demo video

Record a short walkthrough: browse files → upload a folder → preview a file →
create and revoke a share. Keep setup instructions in the written guide.

Upload an MP4 in GitHub's Markdown editor, then replace the demo placeholder with
the generated attachment URL on its own line. GitHub supports
[video in repository Markdown](https://github.blog/changelog/2021-05-13-video-uploads-now-generally-available/);
see its [attachment formats and limits](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files).

For a preview that also works outside GitHub, link a real poster image to the video:

```markdown
[![Watch the TGDrive demo](docs/assets/demo-poster.png)](YOUR_VIDEO_URL)
```

The example path is relative to the root README; use `assets/demo-poster.png`
from a guide in `docs/`. Do not add an iframe or a dummy video URL.

## Source releases

Add replacement images to `PUBLIC_DOCS` in
[build_release.py](../../scripts/build_release.py), replacing the corresponding
placeholder entries. This keeps exported source documentation complete.
