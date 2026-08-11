import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api";

describe("project upload CSRF contract", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sends the fetched token in both the header and multipart form", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, csrf_token: "upload-token" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, files: ["coffee.mp4"] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["coffee"], "coffee.mp4", { type: "video/mp4" });
    await api.uploadProject(7, [file], 11);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, uploadInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(new Headers(uploadInit.headers).get("x-video-vault-csrf")).toBe("upload-token");
    expect(uploadInit.body).toBeInstanceOf(FormData);
    const body = uploadInit.body as FormData;
    expect(body.get("csrf_token")).toBe("upload-token");
    expect(body.get("project_id")).toBe("7");
    expect(body.get("base_revision")).toBe("11");
  });
});
