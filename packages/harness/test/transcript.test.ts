import { describe, expect, test } from "bun:test";
import { expectDeepEqual, loadJson } from "../../core/test/helpers";
import { TranscriptPool, contentKey, loadPool, poolPath, savePool } from "../src/transcript";

const fix = loadJson<{
  keys: string[];
  keys2: string[];
  blob: {
    encoding: string;
    entries: Record<string, unknown>;
    turns: string[][];
    stats: Record<string, unknown>;
  };
  replay0: Array<{ role: string | null; content: unknown }>;
  encoding: number;
}>("transcript.json");

const _img = {
  type: "image_url",
  image_url: { url: "data:image/png;base64,QUJD" },
};

describe("transcript pool parity (hashes, byte counts, blob format)", () => {
  test("keys, blob and stats reproduce v2 exactly", () => {
    const msgs = fix.replay0.map((m) => ({ role: m.role ?? undefined, content: m.content }));
    const p = new TranscriptPool();
    const keys = p.addTurn(msgs);
    expect(keys).toEqual(fix.keys);
    const keys2 = p.addTurn(msgs.slice(0, 2));
    expect(keys2).toEqual(fix.keys2);
    expectDeepEqual(p.toBlob(), fix.blob, "blob");
  });

  test("content_key is pyCanonicalDumps-based", () => {
    expect(contentKey("plain string")).toBe(contentKey("plain string"));
    expect(contentKey("café ☃")).not.toBe(contentKey("cafe"));
  });

  test("fromBlob + replayTranscript reconstructs and verifies", () => {
    const p = TranscriptPool.fromBlob(fix.blob as never);
    const replay = p.replayTranscript(0);
    expectDeepEqual(replay, fix.replay0, "replay0");
    // corruption detection: tamper with an entry's content
    const original = fix.blob.entries[fix.keys[0]] as Record<string, unknown>;
    const tampered = TranscriptPool.fromBlob(
      JSON.parse(
        JSON.stringify({
          ...fix.blob,
          entries: {
            ...fix.blob.entries,
            [fix.keys[0]]: {
              ...original,
              content: "TAMPERED",
            },
          },
        }),
      ) as never,
    );
    expect(() => tampered.replayTranscript(0)).toThrow(/corrupt/i);
  });

  test("save + load roundtrip through the on-disk pooled-v1 format", () => {
    const dir = joinScratch("transcript");
    const p = new TranscriptPool();
    p.addTurn(fix.replay0.map((m) => ({ role: m.role ?? undefined, content: m.content })));
    savePool(dir, "rt-1", p);
    const loaded = loadPool(dir, "rt-1");
    expect(loaded).not.toBeNull();
    expectDeepEqual(loaded!.replayTranscript(0), fix.replay0, "roundtrip");
    expect(typeof poolPath(dir, "rt-1")).toBe("string");
  });
});

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
function joinScratch(name: string): string {
  const d = nodePath.join(
    nodePath.dirname(nodePath.dirname(nodePath.dirname(import.meta.dir))),
    ".scratch",
    name,
  );
  nodeFs.rmSync(d, { recursive: true, force: true });
  nodeFs.mkdirSync(d, { recursive: true });
  return d;
}
