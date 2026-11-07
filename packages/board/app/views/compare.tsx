/** Compare: paired bootstrap verdict between two runs. */
import { useEffect, useState } from "preact/hooks";
import { H, Chip } from "../ui.tsx";
import { get, fmt } from "../api.ts";

interface Verdict {
  a: string;
  b: string;
  rateA: number | null;
  rateB: number | null;
  diff: number | null;
  pValue: number | null;
  significant: boolean;
  tier: "a-wins" | "b-wins" | "tie" | "incomparable";
  note: string;
}

export function Compare(props: { a: string; b: string; pick: (which: "a" | "b", id: string) => void }) {
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!props.a || !props.b) return;
    setVerdict(null);
    setErr(null);
    get<Verdict>(`/api/v3/compare?a=${encodeURIComponent(props.a)}&b=${encodeURIComponent(props.b)}`)
      .then(setVerdict)
      .catch((e) => setErr(String(e.message)));
  }, [props.a, props.b]);

  return H`
    <div class="card">
      <h2>Compare <span class="sub">paired bootstrap of the progress-rate difference</span></h2>
      <div style=${{ display: "flex", gap: "0.8rem", flexWrap: "wrap" }}>
        <div class="field"><label>run A</label><input type="text" value=${props.a} onInput=${(e: Event) => props.pick("a", (e.target as HTMLInputElement).value)} /></div>
        <div class="field"><label>run B</label><input type="text" value=${props.b} onInput=${(e: Event) => props.pick("b", (e.target as HTMLInputElement).value)} /></div>
      </div>
      ${err ? H`<p class="err">${err}</p>` : null}
      ${verdict ? H`
        <table class="tbl">
          <thead><tr><th></th><th>run</th><th class="num">progress</th></tr></thead>
          <tbody>
            <tr><td>A</td><td class="mono">${verdict.a}</td><td class="num">${fmt.pct(verdict.rateA)}</td></tr>
            <tr><td>B</td><td class="mono">${verdict.b}</td><td class="num">${fmt.pct(verdict.rateB)}</td></tr>
          </tbody>
        </table>
        <p style=${{ fontSize: "1.05rem" }}>
          <${Chip} tone=${verdict.tier === "tie" ? "warn" : verdict.tier === "incomparable" ? "muted" : "good"}>${verdict.tier}</${Chip}>
          <span style=${{ marginLeft: "0.5rem" }}>${verdict.note}</span>
        </p>
        <p class="hint">diff ${(verdict.diff ?? 0).toFixed(4)} · p≈${verdict.pValue ?? "?"} (two-sided resampling, 2000 draws, seed 0). Overlapping CIs do not support calling a winner; this test says so out loud.</p>
      ` : H`<p class="hint">pick two run ids (open a run and press “compare” to prefill).</p>`}
    </div>
  `;
}
