import { afterEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { fetchBranchChanges } from "./useBranchDiff";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

afterEach(() => vi.mocked(authenticatedFetch).mockReset());

describe("fetchBranchChanges", () => {
  it("maps renames onto the Changes-list vocabulary and keeps the old path", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          base: "main",
          merge_base: "abc123",
          has_more: false,
          data: [
            {
              path: "src/new.ts",
              name: "new.ts",
              status: "renamed",
              previous_path: "src/old.ts",
              bytes: 10,
              modified_at: 1,
              lines_added: 0,
              lines_removed: 0,
            },
          ],
        }),
        { status: 200 },
      ),
    );

    const result = await fetchBranchChanges("conv_child");

    expect(vi.mocked(authenticatedFetch)).toHaveBeenCalledWith(
      "/v1/sessions/conv_child/resources/git/changes",
    );
    expect(result).toMatchObject({ available: true, base: "main", mergeBase: "abc123" });
    expect(result.data[0]).toMatchObject({
      path: "src/new.ts",
      status: "modified",
      renamed: true,
      previous_path: "src/old.ts",
    });
  });

  it("reports a non-git workspace as unavailable with the server's reason", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "workspace is not a git repository" }), {
        status: 400,
      }),
    );

    const result = await fetchBranchChanges("conv_plain", "main");

    expect(vi.mocked(authenticatedFetch)).toHaveBeenCalledWith(
      "/v1/sessions/conv_plain/resources/git/changes?base=main",
    );
    expect(result).toEqual({
      available: false,
      reason: "workspace is not a git repository",
      base: null,
      mergeBase: null,
      landed: false,
      ports: null,
      data: [],
    });
  });
});
