import { createHash } from "node:crypto";

// Build-only overlay; the published tgdrive.3 vendoring recipe stays unchanged.
export const DURATION_RESCAN_PATCHES = [
    [
        String.raw`  static isLocallyBacked(A2) {
    if (A2.startsWith("file:") || A2.startsWith("blob:")) return true;
    try {
      const I2 = "undefined" != typeof location ? location.href : void 0, g2 = new URL(A2, I2);
      if ("http:" !== g2.protocol && "https:" !== g2.protocol) return false;
      const C2 = g2.hostname;
      if ("127.0.0.1" !== C2 && "localhost" !== C2 && "[::1]" !== C2 && "::1" !== C2) return false;
      for (const A3 of g2.searchParams.values()) if (/^https?:\/\//i.test(A3)) return false;
      return true;
    } catch {
      return false;
    }
  }`,
        `  static isLocallyBacked(A2) {
    return A2.startsWith("file:") || A2.startsWith("blob:");
  }`,
    ],
    [
        "  _durationRescanDone = false;",
        "  _durationRescanDone = false;\n  _durationRescanSource = null;",
    ],
    [
        "    if (this._durationRescanDone || this._destroyed) return;",
        "    if (this._durationRescanDone || this._destroyed || this._lifetimeAbort.signal.aborted) return;",
    ],
    [
        "    const C2 = this._previewGeneration, B2 = () => this._destroyed || C2 !== this._previewGeneration;",
        "    const C2 = this._previewGeneration, B2 = () => this._destroyed || this._lifetimeAbort.signal.aborted || C2 !== this._previewGeneration;",
    ],
    [
        "      if (Q2 = await this.createSource(C3), Q2 instanceof FileSource && Q2.markSecondary(), B2()) return;",
        `      Q2 = await this.createSource(C3);
      if (Q2 instanceof FileSource) Q2.markSecondary();
      if (B2()) { Q2.close(); return; }
      this._durationRescanSource = Q2;`,
    ],
    [
        "      if (E2.setDataSource({ read: async (A3, I3) => new Uint8Array(await Q2.read(A3, I3)), getSize: async () => Q2.getSize() }), await E2.open(), B2()) return;",
        `      if (E2.setDataSource({
        read: async (A3, I3) => {
          if (B2()) return new Uint8Array(0);
          const bytes = await Q2.read(A3, I3);
          return B2() ? new Uint8Array(0) : new Uint8Array(bytes);
        },
        getSize: async () => {
          if (B2()) return 0;
          const size = await Q2.getSize();
          return B2() ? 0 : size;
        }
      }), await E2.open(), B2()) return;`,
    ],
    [
        `      try {
        Q2?.close();
      } catch {
      }
    }
  }
  scheduleThumbBufferClear() {`,
        `      if (this._durationRescanSource === Q2) {
        this._durationRescanSource = null;
        try { Q2?.close(); } catch {}
      }
    }
  }
  scheduleThumbBufferClear() {`,
    ],
    [
        "  destroyPreviewPipeline() {\n",
        `  destroyPreviewPipeline() {
    const scanSource = this._durationRescanSource;
    this._durationRescanSource = null;
    try { scanSource?.close(); } catch {}
`,
    ],
];

const INSTALLED_SHA256 =
    "f14123275c415393e88a5cfed2c529c23c6d0a5ed66d56d8de5bd0c4ff8fb59f";

export function patchDurationRescan(source: string): string {
    if (createHash("sha256").update(source).digest("hex") !== INSTALLED_SHA256)
        throw new Error(
            "The Movi duration overlay requires the exact reviewed tgdrive.3 source.",
        );
    let patched = source;
    for (const [before, after] of DURATION_RESCAN_PATCHES) {
        if (patched.split(before).length !== 2)
            throw new Error(
                "The reviewed Movi duration overlay anchor changed.",
            );
        patched = patched.replace(before, after);
    }
    return patched;
}
