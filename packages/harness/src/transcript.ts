/**
 * Message de-duplication for the turn log — a port of v2's transcript.py.
 *
 * The prompt for turn t contains every turn before it; a vision policy
 * appends one ~108 KB base64 PNG per turn, so verbatim storage grows O(N^2)
 * (1,176 images ≈ 118 MB for a 48-turn episode that only has 48 distinct
 * frames). We store the SHAPE of the conversation instead: a content pool
 * keyed by truncated content hashes, plus a turn index of short keys.
 *
 * The on-disk format ("pooled-v1") and every hash are identical to v2, so
 * transcript sidecars are interchangeable between implementations.
 */

import { createHash } from "node:crypto";
import * as nodeFs from "node:fs";
import { type Json, pyCanonicalDumps, pyDumps } from "@drawtle/core";
import { atomicWriteJson, runPaths } from "./runstate";

export const KEY_LEN = 16;

type Content = Json;

interface PoolEntry {
  content: Content;
  role: string | null;
  n: number;
  sha256: string;
}

export interface PoolStats {
  distinct_messages: number;
  total_message_refs: number;
  turns_logged: number;
  verbatim_bytes: number;
  pooled_bytes: number;
  saving_bytes: number;
  ratio: number | null;
  processes?: number;
  last_process?: PoolStats;
}

export interface PoolBlob {
  encoding: "pooled-v1";
  entries: Record<string, PoolEntry>;
  turns: string[][];
  stats: PoolStats | null;
}

/** Stable key for one message payload of any supported shape.
 *
 * `role` is part of the key on purpose. Two messages with identical content
 * and different roles are distinct messages, and keying on content alone
 * collapsed them — the pool's first-seen role won, and `replayTranscript`
 * could hand back an assistant reply as a user message. Roles are a closed
 * set, so this cannot split what should be one entry. (Port of the v2 E9
 * fix; the Python reference behaves identically.)
 */
export function contentKey(content: Content, role: string | null = null): string {
  const blob = pyCanonicalDumps([role, content]);
  return createHash("sha256").update(blob, "utf8").digest("hex").slice(0, KEY_LEN);
}

function fullHash(content: Content): string {
  return createHash("sha256").update(pyCanonicalDumps(content), "utf8").digest("hex");
}

export class TranscriptPool {
  entries: Record<string, PoolEntry> = {};
  turns: string[][] = [];
  recorded: PoolStats | null = null;
  private bytesIn = 0;

  add(message: { role?: string; content: unknown }): string {
    const content = message.content as Content;
    const role = message.role ?? null;
    const key = contentKey(content, role);
    const raw = pyCanonicalDumps(content);
    this.bytesIn += raw.length;
    let ent = this.entries[key];
    if (ent === undefined) {
      // Store role alongside content: two messages can share a content
      // payload but differ in role, and collapsing them would silently
      // change the conversation. The key carries the role too, so a repeated
      // payload under a different role gets its own entry.
      ent = { content, role, n: 0, sha256: fullHash(content) };
      this.entries[key] = ent;
    }
    ent.n += 1;
    return key;
  }

  addTurn(messages: Array<{ role?: string; content: unknown }>): string[] {
    const keys = messages.map((m) => this.add(m));
    this.turns.push(keys);
    return keys;
  }

  private measure(): PoolStats {
    // v2 measured the pooled size with json.dumps DEFAULT separators
    // (", ", ": ") — not the tight canonical form used for hashing.
    const pooled = pyDumps({
      entries: this.entries,
      turns: this.turns,
    } as never as Json).length;
    const naive = this.bytesIn;
    return {
      distinct_messages: Object.keys(this.entries).length,
      total_message_refs: Object.values(this.entries).reduce((a, e) => a + e.n, 0),
      turns_logged: this.turns.length,
      verbatim_bytes: naive,
      pooled_bytes: pooled,
      saving_bytes: naive - pooled,
      ratio: pooled ? Math.round((naive / pooled) * 100) / 100 : null,
    };
  }

  get stats(): PoolStats {
    return this.recorded ?? this.measure();
  }

  /** Freeze the measured comparison into the object for serialisation. */
  commitStats(): PoolStats {
    const fresh = this.measure();
    const prev = this.recorded;
    if (prev) {
      const merged: PoolStats = { ...fresh };
      merged.verbatim_bytes = (prev.verbatim_bytes ?? 0) + fresh.verbatim_bytes;
      merged.pooled_bytes = (prev.pooled_bytes ?? 0) + fresh.pooled_bytes;
      merged.saving_bytes = merged.verbatim_bytes - merged.pooled_bytes;
      merged.ratio = merged.pooled_bytes
        ? Math.round((merged.verbatim_bytes / merged.pooled_bytes) * 100) / 100
        : null;
      merged.processes = (prev.processes ?? 1) + 1;
      merged.last_process = fresh;
      this.recorded = merged;
    } else {
      this.recorded = fresh;
    }
    return this.recorded;
  }

  toBlob(): PoolBlob {
    this.commitStats();
    return {
      encoding: "pooled-v1",
      entries: this.entries,
      turns: this.turns,
      stats: this.recorded,
    };
  }

  static fromBlob(blob: PoolBlob | null | undefined): TranscriptPool {
    const p = new TranscriptPool();
    if (!blob || blob.encoding !== "pooled-v1") {
      throw new Error(`unsupported transcript encoding: ${blob?.encoding ?? String(blob)}`);
    }
    p.entries = blob.entries ?? {};
    p.turns = blob.turns ?? [];
    p.recorded = blob.stats ?? null;
    return p;
  }

  /** Reconstruct the exact messages sent on a turn, verifying hashes. */
  replayTranscript(turnIndex: number): Array<{ role: string | null; content: Content }> {
    if (!(turnIndex >= 0 && turnIndex < this.turns.length)) {
      throw new Error(`turn ${turnIndex} not in transcript (${this.turns.length} turns logged)`);
    }
    const out: Array<{ role: string | null; content: Content }> = [];
    for (const key of this.turns[turnIndex]) {
      const ent = this.entries[key];
      if (!ent) throw new Error(`transcript references missing pool entry '${key}'`);
      if (ent.sha256 && fullHash(ent.content) !== ent.sha256) {
        throw new Error(
          `pool entry '${key}' does not match its recorded hash; the transcript is corrupt and cannot be replayed faithfully. (This detects damage, not tampering -- see the v2 module docstring.)`,
        );
      }
      out.push({ role: ent.role, content: ent.content });
    }
    return out;
  }
}

export function poolPath(outDir: string, runId: string): string {
  return `${runPaths(outDir, runId).jsonl}.transcript.json`;
}

export function savePool(outDir: string, runId: string, pool: TranscriptPool): string {
  return atomicWriteJson(poolPath(outDir, runId), pool.toBlob());
}

export function loadPool(outDir: string, runId: string): TranscriptPool | null {
  const p = poolPath(outDir, runId);
  if (!nodeFs.existsSync(p)) return null;
  return TranscriptPool.fromBlob(JSON.parse(nodeFs.readFileSync(p, "utf8")) as PoolBlob);
}

export function attachToRecord(
  rec: Record<string, unknown>,
  pool: TranscriptPool,
  messages: Array<{ role?: string; content: unknown }>,
): Record<string, unknown> {
  rec.prompt_keys = pool.addTurn(messages);
  return rec;
}

/** Relative transcript path for a run dir (helper for the board). */
export function transcriptRel(runJsonl: string): string {
  return `${runJsonl}.transcript.json`;
}
