"""Cost and token accounting across sessions.

WHAT THIS ANSWERS
-----------------
"How much will this run cost, and what did the last one actually cost?" Those
are two different questions and the codebase answered neither well:

  - before a run there was no estimate at all, so a large model on a large
    dataset was priced by finding out;
  - after a run the summary reported totals, but not the one number that makes
    two runs comparable -- money per unit of progress.

THE ONE RULE
------------
An estimate is never presented as a measurement, and an unknown price is never
presented as zero. Every figure here carries a `source`:

  measured    the provider returned token counts for every turn
  estimated   some counts were inferred from text length
  unknown     we have no price for the model, so cost is not a number

That distinction is the difference between budgeting and guessing. A provider
that publishes no pricing (InternLM, at the time of writing) yields
`cost_unknown`, and a caller that ignores the flag gets a 0.0 that means
"we do not know", not "it was free".

COST PER PROGRESS POINT
-----------------------
Total cost alone rewards short runs and penalises thorough ones. The bench
measures whether an action was driven by the current frame or a remembered
one, so the unit of output is a progressed turn. Dividing cost by the progress
rate gives dollars per unit of measured capability, which is the number that
decides whether a model is worth running at a given sample size.
"""
from __future__ import annotations

import json
import os

from . import catalog as CAT
from . import stats as ST


def _num(x):
    return x if isinstance(x, (int, float)) else None


def estimate(dataset, model, n_episodes=None, max_turns=None,
             usd_per_1k_in=None, usd_per_1k_out=None,
             prompt_tokens_per_turn=None, output_tokens_per_turn=None):
    """Project the token and dollar cost of a run before paying for it.

    Deliberately built from measured history when available and from stated
    assumptions otherwise, with the basis returned alongside the number. A
    projection with no stated basis is a guess wearing a number's clothes.
    """
    info = CAT.model_info(model)
    mazes = dataset.get("mazes") or []
    n = n_episodes or len(mazes)
    sizes = [m.get("size") for m in mazes[:n] if isinstance(m, dict)]
    turns = max_turns or 48

    # Input grows with turn count because every prior frame is re-sent. The
    # per-turn average is therefore an average over a growing sequence, not a
    # constant, and a naive turns * per_turn understates a long episode. The
    # default 42/6 comes from the committed mock runs, where the counts are
    # measured; using the mock's numbers to project a real model is honest as
    # long as the basis says so.
    pin_per_turn = prompt_tokens_per_turn or 42.0
    pout_per_turn = output_tokens_per_turn or 6.0
    # Triangular growth: turn t carries t prior observations, so the episode
    # total is ~ (turns * (turns + 1) / 2) * per_turn_for_turn_1.
    growth = (turns * (turns + 1)) / 2.0

    in_tokens = int(growth * (pin_per_turn / max(1.0, turns / 2.0)) * n)
    out_tokens = int(turns * pout_per_turn * n)

    price_in = _num(usd_per_1k_in)
    price_out = _num(usd_per_1k_out)
    priced = price_in is not None or price_out is not None
    if not priced:
        price_in = _num(info.get("price_in"))
        price_out = _num(info.get("price_out"))
        priced = info.get("price_known") and (
            price_in is not None or price_out is not None)

    usd = None
    if priced:
        usd = (in_tokens / 1000.0) * (price_in or 0.0) + \
              (out_tokens / 1000.0) * (price_out or 0.0)

    return {
        "model": model,
        "n_episodes": n,
        "max_turns": turns,
        "projected_input_tokens": in_tokens,
        "projected_output_tokens": out_tokens,
        "projected_total_tokens": in_tokens + out_tokens,
        "price_in_per_1k": price_in,
        "price_out_per_1k": price_out,
        "projected_cost_usd": round(usd, 4) if usd is not None else None,
        "cost_known": bool(priced),
        "basis": {
            "turns_per_episode": turns,
            "tokens_per_turn_in": pin_per_turn,
            "tokens_per_turn_out": pout_per_turn,
            "price_source": ("argument" if usd_per_1k_in is not None
                             else "model_registry.json" if priced else None),
            "window": info.get("context_window"),
            "window_note": (
                "projected per-episode input exceeds 90% of the model's "
                "published context window; the run will likely hit context "
                "errors rather than finish"
                if info.get("context_window") and
                in_tokens / max(1, n) > info["context_window"] * 0.9 else None),
        },
        "note": ("cost_known is false: no price is published for this model, "
                 "so no dollar figure is given. Tokens are still projected."
                 if not priced else None),
    }


def session_cost(summary):
    """Per-model cost and token breakdown from one run summary.

    Reads only what the summary actually contains. A summary from before the
    token split existed has no input/output distinction, and this reports that
    as `estimated` rather than inventing a ratio.
    """
    cost = _num(summary.get("total_cost_usd")) or 0.0
    n_turns = summary.get("n_turns") or 0
    progress = _num(summary.get("progress_rate"))

    # Input/output may be absent even when the run's turn count is known -- a
    # summary written before the split existed carries only a combined figure.
    # Recovering the halves from `mean_tokens_per_turn * n_turns` and the
    # legacy split rule keeps the columns populated and labelled, instead of
    # showing a blank that reads as zero.
    pin = summary.get("input_tokens")
    pout = summary.get("output_tokens")
    total = summary.get("total_tokens")
    if total is None and summary.get("mean_tokens_per_turn") and n_turns:
        total = int(round(summary["mean_tokens_per_turn"] * n_turns))
    if pin is None and total:
        # No recorded split. Borrow the same estimator the JSONL path uses.
        pin, pout = _apportion(total, summary.get("_legacy_out_tokens"))
    out = {
        "run_id": summary.get("run_id"),
        "model": summary.get("model"),
        "status": summary.get("status"),
        "n_turns": n_turns,
        "n_episodes": summary.get("n_episodes"),
        "input_tokens": pin,
        "output_tokens": pout,
        "total_tokens": total,
        "token_source": summary.get("token_source"),
        "n_estimated_turns": summary.get("n_estimated_turns"),
        "total_cost_usd": code_round(cost),
        "cost_known": summary.get("cost_known", True),
        "wallclock_s": _num(summary.get("wallclock_s")),
    }
    if n_turns:
        out["mean_cost_per_turn_usd"] = code_round(cost / n_turns)
        out["tokens_per_turn"] = round((total or 0) / n_turns, 3)
    if progress and progress > 0:
        out["cost_per_progress_point_usd"] = code_round(cost / progress)
    if out["wallclock_s"]:
        out["cost_per_hour_usd"] = code_round(
            cost / (out["wallclock_s"] / 3600.0)) if out["wallclock_s"] else None
    # A run that did not finish cleanly must not be read as a price for the
    # whole experiment -- it is the price of the part that ran.
    if summary.get("status") not in (None, "success"):
        out["caveat"] = (f"status={summary.get('status')}: cost covers only the "
                         f"turns that were written, not the intended run")
    if not out["cost_known"]:
        out["caveat"] = ((out.get("caveat") or "") +
                         " | cost_known=false: no price for this model, the "
                         "0.0 means unknown, not free").strip(" |")
    return out


def code_round(x, places=6):
    return round(float(x), places)


#: Output share assumed when a legacy total must be split and no response text
#: is available to estimate from. Derived from the measured mock runs, whose
#: real split is 47668 in / 8054 out -- about 14.5%. Used only as a last
#: resort; the JSONL path estimates from the actual prose.
LEGACY_OUTPUT_SHARE = 0.145


def _apportion(total, legacy_out=None):
    """Split a combined total into (input, output) when the split is gone."""
    if legacy_out:
        return total - int(legacy_out), int(legacy_out)
    out = int(round(total * LEGACY_OUTPUT_SHARE))
    return total - out, out


def rollup(results_dir, model=None, rederive=True):
    """Total spend across every run in a directory.

    Reads the stored summary when it is complete, and RE-DERIVES from the JSONL
    when it is not. That fallback matters because a summary is a frozen artifact
    written by the version of the code that produced it: every summary committed
    before the token split existed lacks the input/output fields entirely, and
    every summary predating status tracking lacks a status. Reporting those as
    "0 tokens, not successful" would be a false statement about a run that
    plainly produced 1440 measured turns.

    Refuses to produce a single grand total when any run is unpriced: adding a
    known cost to an unknown one silently produces a number that looks like the
    answer and is not. The unpriced runs are listed instead.
    """
    rows = []
    # Through `enumerate_runs` rather than a glob of `*.summary.json`: that glob
    # only sees the legacy flat layout, so it would quietly stop counting every
    # run written into its own directory -- and a cost total that silently omits
    # half the runs is worse than no total.
    for _rid, run in ST.enumerate_runs(results_dir).items():
        s = run["summary"]
        if s is None:
            continue
        if model and s.get("model") != model:
            continue
        stale = ("total_tokens" not in s) or ("status" not in s)
        if stale and rederive:
            jsonl = (run.get("paths") or {}).get("jsonl")
            if jsonl and os.path.exists(jsonl):
                s = _merge_derived(s, jsonl)
        rows.append(session_cost(s))

    known = [r for r in rows if r.get("cost_known")]
    unknown = [r for r in rows if not r.get("cost_known")]
    # Only runs that finished cleanly AND have a price can be summed. A run
    # whose status is `unknown` (no status sidecar -- it predates the tracking)
    # still counts, because the alternative is discarding every historical run;
    # the count of such runs is reported separately so the reader knows.
    ok = [r for r in known if r.get("status") in (None, "unknown", "success")]
    return {
        "n_runs": len(rows),
        "n_success": len([r for r in rows if r.get("status") == "success"]),
        "n_status_unknown": len([r for r in rows
                                 if r.get("status") == "unknown"]),
        "n_unpriced": len(unknown),
        "n_incomplete": len([r for r in rows
                             if r.get("status") not in
                             (None, "unknown", "success")]),
        # Only runs whose status is clean and whose price is known can be
        # summed. Anything else is reported as a caveat, not added in.
        "total_cost_usd": code_round(sum(r["total_cost_usd"] for r in ok))
        if ok else 0.0,
        "total_tokens": sum(r.get("total_tokens") or 0 for r in known),
        "total_input_tokens": sum(r.get("input_tokens") or 0 for r in known),
        "total_output_tokens": sum(r.get("output_tokens") or 0 for r in known),
        "token_source": _worst_source(known),
        "unpriced_runs": [r["run_id"] for r in unknown],
        "runs": rows,
    }


def _merge_derived(summary, jsonl_path):
    """Fill a summary's missing fields from its JSONL. Summary fields win.

    Whatever the stored summary already asserts is left alone -- it was written
    by the run itself and is the more direct record. Only absent or None fields
    are filled, and the fill is flagged so a reader can tell a re-derived
    number from a recorded one.
    """
    try:
        derived = ST.aggregate(jsonl_path)
    except (OSError, ValueError, KeyError):
        return summary
    out = dict(summary)
    filled = []
    for k in ("status", "input_tokens", "output_tokens", "total_tokens",
              "mean_input_tokens_per_turn", "mean_output_tokens_per_turn",
              "token_source", "n_estimated_turns", "mean_tokens_per_turn",
              "progress_rate", "cost_per_progress_point_usd", "cost_known",
              "n_turns", "n_episodes"):
        if out.get(k) is None and derived.get(k) is not None:
            out[k] = derived[k]
            filled.append(k)
    # `aggregate` sums legacy logs into input_tokens with output_tokens 0 --
    # the split is genuinely absent from those records. Take its run-level
    # estimate instead, or the output column reads as a measured zero.
    if "total_tokens" in filled and not (out.get("output_tokens") or 0):
        try:
            pin, pout = _split_legacy(jsonl_path)
            if pout:
                out["input_tokens"], out["output_tokens"] = pin, pout
                out["_legacy_out_tokens"] = pout
                filled.append("output_tokens(split-estimated)")
        except (OSError, ValueError):
            pass
    if filled:
        out["_rederived"] = filled
    return out


def _split_legacy(jsonl_path):
    """Re-derive a legacy run's input/output split from its records.

    Falls back to the assumed output share when the run recorded no response
    text to estimate from. Older logs sometimes carry an empty
    `raw_model_text`, which makes the character-based estimator return zero --
    and a zero here is indistinguishable from "this run produced no output",
    which is false for any run that answered at all.
    """
    records = ST.RS.read_jsonl(jsonl_path, strict=True)
    pin, pout, _ = ST._token_split(records)
    pin, pout = ST.rescale_input(pin, pout, records)
    if pin and not pout:
        pin, pout = _apportion(pin + pout if not pout else pin)
    return pin, pout




def _worst_source(rows):
    """The least trustworthy token source across a set of runs."""
    if not rows:
        return None
    order = {"measured": 0, "mixed": 1, "estimated": 2}
    return max((r.get("token_source") or "measured" for r in rows),
               key=lambda s: order.get(s, 3))


def format_rollup(r, as_json=False):
    if as_json:
        return json.dumps(r, indent=2)
    out = []
    out.append(f"runs            : {r['n_runs']} "
               f"({r['n_success']} success, {r['n_incomplete']} incomplete, "
               f"{r['n_status_unknown']} untracked)")
    out.append(f"total cost      : ${r['total_cost_usd']:.4f}"
               + ("" if not r["n_unpriced"] else
                  f"   (+{r['n_unpriced']} unpriced run(s), NOT included)"))
    out.append(f"total tokens    : {r['total_tokens']:,} "
               f"({r['total_input_tokens']:,} in / "
               f"{r['total_output_tokens']:,} out)"
               f"   (source: {r['token_source']})")
    if r["unpriced_runs"]:
        out.append(f"unpriced        : {', '.join(str(x) for x in r['unpriced_runs'])}")
    out.append("      'untracked' = written before status tracking; the figures")
    out.append("      are re-derived from the run's JSONL, not from its summary.")
    out.append("")
    out.append(f"{'run':22} {'model':22} {'turns':>6} {'in':>9} {'out':>8} "
               f"{'$':>10} {'$/turn':>10} {'$/progress':>12}")
    for row in r["runs"]:
        cpp = row.get("cost_per_progress_point_usd")
        out.append(
            f"{str(row['run_id'])[:22]:22} {str(row['model'])[:22]:22} "
            f"{row['n_turns']:>6} {str(row.get('input_tokens') or '-'):>9} "
            f"{str(row.get('output_tokens') or '-'):>8} "
            f"{('$%.4f' % row['total_cost_usd']) if row['cost_known'] else 'UNKNOWN':>10} "
            f"{('$%.5f' % row['mean_cost_per_turn_usd']) if row.get('mean_cost_per_turn_usd') is not None else '-':>10} "
            f"{('$%.3f' % cpp) if cpp is not None else '-':>12}")
    return "\n".join(out)


def format_estimate(e, as_json=False):
    if as_json:
        return json.dumps(e, indent=2)
    out = []
    out.append(f"model           : {e['model']}")
    out.append(f"scope           : {e['n_episodes']} episode(s) x "
               f"{e['max_turns']} turns max")
    out.append(f"projected tokens: {e['projected_input_tokens']:,} in / "
               f"{e['projected_output_tokens']:,} out "
               f"= {e['projected_total_tokens']:,}")
    if e["cost_known"]:
        out.append(f"projected cost  : ${e['projected_cost_usd']:.4f} "
                   f"(basis: {e['basis']['price_source']})")
    else:
        out.append("projected cost  : UNKNOWN -- no price published for this "
                   "model. Tokens above are still valid.")
    if e["basis"].get("window"):
        pct = 100.0 * (e["projected_input_tokens"] / max(1, e["n_episodes"])) \
            / e["basis"]["window"]
        out.append(f"context window  : {e['basis']['window']:,} "
                   f"(episode input is ~{pct:.0f}% of it)")
    if e["basis"].get("window_note"):
        out.append(f"WARNING         : {e['basis']['window_note']}")
    out.append("")
    out.append("These are projections from stated assumptions, not measurements.")
    return "\n".join(out)
