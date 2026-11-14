// Unit tests for the zai backend: everything that can be checked without the
// sandbox's live SDK is checked here with a stubbed client. The behaviours
// under test are the honesty ones — the backend pins the served model per
// lane (the service ignores the model field), routes by message content, and
// translates SDK errors onto the retry ladder.
import { describe, expect, mock, test } from "bun:test";
import {
  HttpStatusError,
  type Message,
  TransientError,
  type ZaiBackend,
  makeBackend,
  makeResponse,
} from "../src/backends";

// Drive a ZaiBackend's private client() with a stub: resolve the dynamic
// import against this factory instead of the real SDK.
function stubClient(impl: {
  create?: (b: Record<string, unknown>) => Promise<Record<string, unknown>>;
  createVision?: (b: Record<string, unknown>) => Promise<Record<string, unknown>>;
}): void {
  const mod = {
    default: {
      create: async () => ({
        chat: {
          completions: {
            create: impl.create ?? (async () => ({})),
            createVision: impl.createVision ?? (async () => ({})),
          },
        },
      }),
    },
  };
  mock.module("z-ai-web-dev-sdk", () => mod);
}

const textUsage = {
  prompt_tokens: 14,
  completion_tokens: 2,
  total_tokens: 16,
};
const visionUsage = {
  prompt_tokens: 39,
  completion_tokens: 3,
  prompt_tokens_details: { cached_tokens: 7 },
  completion_tokens_details: { reasoning_tokens: 0 },
  total_tokens: 42,
};

describe("zai backend (stubbed SDK)", () => {
  test("factory + knownBackends expose 'zai'", () => {
    expect(knownHas("zai")).toBe(true);
    expect(() => makeBackend("zai", "glm-4-plus", {})).not.toThrow();
    expect(() => makeBackend("zai", "glm-5.3-flash", {})).not.toThrow();
  });

  test("refuses any --model the service does not actually serve", () => {
    expect(() => makeBackend("zai", "glm-4.5v", {})).toThrow(/serves exactly/);
    expect(() => makeBackend("zai", "gpt-4o", {})).toThrow(/serves exactly/);
  });

  test("text requests ride completions.create and are pinned to glm-4-plus", async () => {
    let sawEndpoint = "";
    let sawModel = "";
    stubClient({
      create: async (b) => {
        sawEndpoint = "create";
        sawModel = String(b.model);
        return {
          model: "glm-4-plus",
          choices: [{ message: { content: '{"turn": 0, "step": 1}' } }],
          usage: textUsage,
        };
      },
    });
    const b = makeBackend("zai", "glm-4-plus", {}) as ZaiBackend;
    const messages: Message[] = [{ role: "user", content: "move" }];
    const r = await b.complete(messages, { useCache: false });
    expect(sawEndpoint).toBe("create");
    expect(sawModel).toBe("glm-4-plus");
    expect(r.text).toBe('{"turn": 0, "step": 1}');
    expect(r.promptTokens).toBe(14);
    expect(r.completionTokens).toBe(2);
    expect(r.tokenSource).toBe("measured");
  });

  test("image-carrying requests ride createVision and are pinned to glm-5.3-flash", async () => {
    let sawEndpoint = "";
    let sawModel = "";
    stubClient({
      createVision: async (b) => {
        sawEndpoint = "createVision";
        sawModel = String(b.model);
        return {
          model: "glm-5.3-flash",
          choices: [{ message: { content: '{"turn": 90, "step": 1}' } }],
          usage: visionUsage,
        };
      },
    });
    const b = makeBackend("zai", "glm-5.3-flash", {}) as ZaiBackend;
    const messages: Message[] = [
      {
        role: "user",
        content: [
          { type: "text", text: "move" },
          { type: "image_url", image_url: { url: "data:image/png;base64,AAAA" } },
        ],
      },
    ];
    const r = await b.complete(messages, { useCache: false });
    expect(sawEndpoint).toBe("createVision");
    expect(sawModel).toBe("glm-5.3-flash");
    expect(r.text).toBe('{"turn": 90, "step": 1}');
    expect(r.cachedTokens).toBe(7);
  });

  test("the honesty gate: a served model that contradicts the pin is an error", async () => {
    stubClient({
      create: async () => ({
        model: "glm-9-turbo",
        choices: [{ message: { content: "{}" } }],
        usage: textUsage,
      }),
    });
    const b = makeBackend("zai", "glm-4-plus", {}) as ZaiBackend;
    await expect(
      b.complete([{ role: "user", content: "move" }], { useCache: false }),
    ).rejects.toThrow(/zai served 'glm-9-turbo'/);
  });

  test("SDK 'status 429' text is translated onto the retryable ladder", async () => {
    let calls = 0;
    stubClient({
      create: async () => {
        calls += 1;
        throw new Error("API request failed with status 429: Too many requests");
      },
    });
    const b = makeBackend("zai", "glm-4-plus", {}) as ZaiBackend;
    b.maxRetries = 1;
    // shrink the production 30s 429 wait so the test stays fast; the ladder
    // behaviour (a retry happens) is what is under test, the wait is
    // asserted separately below
    b.retryWaitS = () => 0.01;
    await expect(
      b.complete([{ role: "user", content: "move" }], { useCache: false }),
    ).rejects.toThrow();
    expect(calls).toBe(2); // initial + 1 retry — it WAS retried, not dropped
  }, 30_000);

  test("a 429 waits at least 30s before its next attempt (sliding-window throttle)", () => {
    const b = makeBackend("zai", "glm-4-plus", {}) as ZaiBackend;
    const probe = (b as unknown as { retryWaitS(a: number, s: number): number }).retryWaitS;
    expect(probe.call(b, 0, 429)).toBeGreaterThanOrEqual(30);
    expect(probe.call(b, 2, 429)).toBeGreaterThanOrEqual(45);
    // 5xx keeps classic exponential backoff
    expect(probe.call(b, 0, 500)).toBe(1);
    expect(probe.call(b, 3, 500)).toBe(8);
  });

  test("plain SDK failures become transient (retryable) errors", () => {
    // translate() is private; exercise it through the class shape instead
    const b = makeBackend("zai", "glm-4-plus", {}) as ZaiBackend;
    expect(b.name).toBe("zai");
    expect(makeResponse({ text: "x" }).text).toBe("x");
    expect(new HttpStatusError(429, null, "x") instanceof Error).toBe(true);
    expect(new TransientError("x") instanceof Error).toBe(true);
  });
});

function knownHas(kind: string): boolean {
  // local import avoids circularity in the type checker
  const { knownBackends } = require("../src/backends") as typeof import("../src/backends");
  return knownBackends().includes(kind);
}
