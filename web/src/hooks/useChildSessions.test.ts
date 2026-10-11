import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchChildSessions } from "./useChildSessions";

const fetchMock = vi.fn();

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchChildSessions", () => {
  it("requests the full direct-child page so the Agents count is not capped at 21", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ object: "list", data: [] }));

    await fetchChildSessions("conv_parent");

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv_parent/child_sessions?limit=1000");
  });
});
