import { describe, expect, it } from "vitest";
import type { Transfer } from "./transfers";
import { transferFolderDestination, transferTaskId } from "./transferTasks";

const item: Transfer = {
  key: "one",
  name: "one.bin",
  destination: "/root/child",
  size: 1,
  total: 1,
  current: 1,
  lastModified: 0,
  state: "completed",
  phase: "Available in your drive",
};

describe("transfer folder identity", () => {
  it("locates a server-discovered folder root without needing relative file paths", () => {
    const folderUpload = {
      id: "folder-upload-identity",
      name: "Project files",
      root_path: "/original-parent/root-id",
      created_at: 1,
    };
    expect(transferTaskId({ ...item, folderUpload })).toBe(folderUpload.id);
    expect(transferFolderDestination({ ...item, folderUpload })).toBe(
      "/original-parent/root-id",
    );
  });
  it("recovers the root from legacy relative paths without changing standalone destinations", () => {
    expect(
      transferFolderDestination({
        ...item,
        relativePath: "Project/child/one.bin",
      }),
    ).toBe("/root");
    expect(
      transferFolderDestination({
        ...item,
        relativePath: "Project/one.bin",
      }),
    ).toBe("/root/child");
    expect(transferFolderDestination(item)).toBe("/root/child");
  });
});
