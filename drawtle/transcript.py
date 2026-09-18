"""Message de-duplication for the turn log.

The problem
-----------
The prompt for turn `t` contains every turn before it. The vision policy appends
one ~108 KB base64 PNG per turn and never drops anything, so turn `t`'s request
carries `t` images. Storing each request verbatim makes the log grow as O(N^2):
for a 48-turn episode that is 1+2+...+48 = 1,176 images, roughly 118 MB, for a
maze that only ever has 48 distinct frames.

Inspect AI hit exactly this on agentic benchmarks and solved it by collapsing
repeated messages before serialising, with roughly 10:1 on their workloads.

What we do instead
------------------
We store the *shape* of the conversation rather than its contents. The transcript
is reconstructed on read from:

  * a **content pool** of distinct message payloads, keyed by a content hash
    (sha256 of the canonical JSON, truncated). A frame re-sent on turn 40 is the
    same bytes as on turn 4, so it is stored once.
  * a **turn index** recording which pool entries made up each turn's request,
    in order. This is a list of short keys, so the quadratic term becomes
    quadratic in *keys* (8 bytes each) instead of in *images* (108 KB each).

Measured effect for a 48-turn vision episode with 48 distinct frames:
  verbatim   1,176 image payloads        ~118 MB
  pooled        48 image payloads        ~5 MB, plus 1,176 short keys
That is the difference between a log you can commit and one you cannot.

What is NOT lost
----------------
`replay_transcript` reconstructs byte-identical messages from the pool, so this
is a storage encoding, not a summary. The pool is verified by hash on read: a
truncated or tampered pool is a loud error, not a quiet corruption.

Why not just drop old images
----------------------------
Because that changes the experiment. The whole point of the vision arm is that
the model is sent the current frame *in the context of the frames before it*.
Trimming the history would make the bench measure something else. We keep the
experiment intact and only change how it is written down.
"""
import hashlib
import json
import os

from . import runstate as RS

#: A pool key is a truncated content hash. 16 hex chars = 64 bits; at the scale
#: of a benchmark run (thousands of frames) a collision is not a realistic
#: concern, and the full hash is kept alongside the key for verification.
KEY_LEN = 16


def content_key(content):
    """Stable key for one message payload of any supported shape."""
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:KEY_LEN]


class TranscriptPool:
    """Deduplicating store for the messages a run sent.

    Usage:
        pool = TranscriptPool()
        key = pool.add({"role": "user", "content": ...})
        pool.turns.append([key])          # the request sent on this turn
        blob = pool.to_blob()
    """

    def __init__(self):
        self.entries = {}      # key -> {"content": ..., "n": refcount}
        self.turns = []        # list of list-of-keys, one per logged request
        self._bytes_in = 0
        self._bytes_out = 0
        #: Stats recorded at write time. `_bytes_in` counts only what this
        #: process pooled, so recomputing after a reload reports a saving of
        #: zero (or negative) for a pool that in fact deduplicated heavily --
        #: the inline cost was paid by the process that has since exited. The
        #: blob therefore carries the measured comparison, and `stats` prefers
        #: it over anything recomputed.
        self.recorded = None

    def add(self, message):
        """Pool one message; returns its key. Repeated content reuses the key."""
        content = message.get("content")
        key = content_key(content)
        raw = len(json.dumps(content, sort_keys=True, separators=(",", ":")))
        self._bytes_in += raw
        ent = self.entries.get(key)
        if ent is None:
            # Store role alongside content: two messages can share a content
            # payload but differ in role, and collapsing them would silently
            # change the conversation.
            ent = {"content": content, "role": message.get("role"), "n": 0,
                   "sha256": _full_hash(content)}
            self.entries[key] = ent
        ent["n"] += 1
        return key

    def add_turn(self, messages):
        """Pool a whole request; returns the list of keys for this turn."""
        keys = [self.add(m) for m in messages]
        self.turns.append(keys)
        return keys

    def _measure(self):
        """The comparison as observed by THIS process, ignoring any recorded one."""
        self._bytes_out = len(json.dumps(
            {"entries": {k: v for k, v in self.entries.items()},
             "turns": self.turns}, default=str))
        naive = self._bytes_in
        pooled = self._bytes_out
        return {
            "distinct_messages": len(self.entries),
            "total_message_refs": sum(v["n"] for v in self.entries.values()),
            "turns_logged": len(self.turns),
            "verbatim_bytes": naive,
            "pooled_bytes": pooled,
            "saving_bytes": naive - pooled,
            "ratio": round(naive / pooled, 2) if pooled else None,
        }

    @property
    def stats(self):
        """Storage comparison: what this log would have cost inline vs pooled.

        Honest about the sign. Pooling only wins when payloads repeat, which for
        this bench means the vision arm (the same base64 frame is re-sent every
        turn). A short text-only run can legitimately come out *worse*, because
        the pool pays for a key list and a per-entry hash it cannot amortise; a
        ratio below 1.0 is reported as-is rather than hidden, and `saving_bytes`
        is negative to make that unmissable.

        When the pool was loaded from disk, the figures measured by the writing
        process are returned. Recomputation is only correct in-process, where
        the inline cost was actually observed.
        """
        return dict(self.recorded) if self.recorded is not None else self._measure()

    def commit_stats(self):
        """Freeze the measured comparison into the object for serialisation.

        Merges with anything already recorded. A resumed run loads a pool whose
        stats came from a process that has exited; replacing them with this
        process's partial count would report a worse ratio for a run that in
        fact pooled more. The two byte totals are summed because both were
        genuinely paid, once per process.
        """
        fresh = self._measure()
        prev = self.recorded
        if prev:
            merged = dict(fresh)
            merged["verbatim_bytes"] = (prev.get("verbatim_bytes") or 0) + fresh["verbatim_bytes"]
            merged["pooled_bytes"] = (prev.get("pooled_bytes") or 0) + fresh["pooled_bytes"]
            merged["saving_bytes"] = merged["verbatim_bytes"] - merged["pooled_bytes"]
            merged["ratio"] = (round(merged["verbatim_bytes"] / merged["pooled_bytes"], 2)
                               if merged.get("pooled_bytes") else None)
            merged["processes"] = (prev.get("processes") or 1) + 1
            merged["last_process"] = fresh
            self.recorded = merged
        else:
            self.recorded = fresh
        return self.recorded

    def to_blob(self):
        # Freeze the measured comparison before serialising, so a reader gets
        # the real ratio rather than one recomputed from a partial in-process
        # count. Called here rather than by the caller so it cannot be forgotten.
        self.commit_stats()
        return {
            "encoding": "pooled-v1",
            "entries": self.entries,
            "turns": self.turns,
            "stats": self.recorded,
        }

    @classmethod
    def from_blob(cls, blob):
        p = cls()
        if (blob or {}).get("encoding") != "pooled-v1":
            raise ValueError(f"unsupported transcript encoding: "
                             f"{(blob or {}).get('encoding')!r}")
        p.entries = blob.get("entries") or {}
        p.turns = blob.get("turns") or []
        p.recorded = blob.get("stats")
        return p

    def replay_transcript(self, turn_index):
        """Reconstruct the exact messages sent on a turn, verifying hashes.

        Verification is the point: without it a corrupted pool silently rewrites
        history, and every downstream number is computed over a conversation the
        model never actually had.
        """
        if not (0 <= turn_index < len(self.turns)):
            raise IndexError(f"turn {turn_index} not in transcript "
                             f"({len(self.turns)} turns logged)")
        out = []
        for key in self.turns[turn_index]:
            ent = self.entries.get(key)
            if ent is None:
                raise KeyError(f"transcript references missing pool entry {key!r}")
            if ent.get("sha256") and _full_hash(ent["content"]) != ent["sha256"]:
                raise ValueError(
                    f"pool entry {key!r} does not match its recorded hash; the "
                    f"transcript has been modified and cannot be trusted")
            out.append({"role": ent.get("role"), "content": ent["content"]})
        return out


def _full_hash(content):
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def attach_to_record(rec, pool, messages):
    """Add the turn's transcript keys to a per-turn record.

    The per-turn JSONL record keeps `prompt_keys` (the pool keys for that
    turn's request) and drops the payloads. The payloads live once in the
    sidecar written by `save_pool`. A reader that only wants per-turn metrics
    never needs to touch the sidecar; a reader that wants to audit what the
    model saw calls `replay_transcript`.
    """
    keys = pool.add_turn(messages)
    rec["prompt_keys"] = keys
    return rec


def pool_path(out_dir, run_id):
    return RS.run_paths(out_dir, run_id)["jsonl"] + ".transcript.json"


def save_pool(out_dir, run_id, pool):
    return RS.atomic_write_json(pool_path(out_dir, run_id), pool.to_blob())


def load_pool(out_dir, run_id):
    p = pool_path(out_dir, run_id)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return TranscriptPool.from_blob(json.load(fh))
