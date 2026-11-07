/** Runs: every run in the results tree, including unfinished and failed. */
import { useEffect, useState } from "preact/hooks";
import { type RunRow, fmt, get } from "../api.ts";
import { Chip, H } from "../ui.tsx";

export function Runs(_props: { go: (hash: string) => void }) {
  const [rows, setRows] = useState<RunRow[] | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    get<{ runs: RunRow[] }>("/api/v3/runs").then((d) => setRows(d.runs));
    const t = setInterval(() => {
      get<{ runs: RunRow[] }>("/api/v3/runs").then((d) => setRows(d.runs));
    }, 4000);
    return () => clearInterval(t);
  }, []);

  const shown = (rows ?? []).filter((r) =>
    filter
      ? (r.run_id + (r.summary?.model ?? "")).toLowerCase().includes(filter.toLowerCase())
      : true,
  );

  return H`
    <div class="card">
      <h2>All runs <span class="sub">live refresh every 4s — status, progress, cost, honesty</span></h2>
      <div style=${{ display: "flex", gap: "0.6rem", alignItems: "center", marginBottom: "0.6rem" }}>
        <input type="text" placeholder="filter by run id or model…" value=${filter} onInput=${(e: Event) => setFilter((e.target as HTMLInputElement).value)} style=${{ flex: 1 }} />
        <span class="hint">${shown.length}/${rows?.length ?? 0} shown</span>
      </div>
      ${
        rows === null
          ? H`<p class="hint">loading…</p>`
          : H`
        <table class="tbl">
          <thead><tr>
            <th>run</th><th>model</th><th>status</th><th class="num">progress</th><th>CI</th><th class="num">turns</th><th class="num">cost</th><th>tokens in/out</th><th></th>
          </tr></thead>
          <tbody>
            ${shown.map(
              (r) => H`
              <tr key=${r.run_id}>
                <td class="mono" title=${r.note ?? ""}>${r.run_id}</td>
                <td>${r.summary?.model ?? H`<span class="hint">no summary</span>`}</td>
                <td><${Chip} tone=${r.status === "success" ? "good" : r.status === "started" ? "warn" : "bad"}>${r.status}</${Chip}> <${Chip} tone="muted">${r.summary?.mode ?? "?"}</${Chip}></td>
                <td class="num">${fmt.pct(r.summary?.progress_rate ?? null)}</td>
                <td class="mono">${fmt.ci(r.summary?.progress_ci95 ?? null)}</td>
                <td class="num">${fmt.num(r.summary?.n_turns ?? null)}</td>
                <td class="num">${fmt.usd(r.summary?.total_cost_usd ?? null)}${r.summary?.cost_known === false ? H` <span class="hint" title="price unknown — 0.0 does not mean free">?</span>` : null}</td>
                <td class="mono hint">${fmt.num(r.summary?.input_tokens ?? null)} / ${fmt.num(r.summary?.output_tokens ?? null)}</td>
                <td><a href=${`#/run/${r.run_id}`}>open</a></td>
              </tr>
            `,
            )}
          </tbody>
        </table>
        ${shown.some((r) => !r.has_summary) ? H`<p class="hint">Runs without a summary are the ones a reader most needs to see — stopped or crashed. Their logs hold the turns that completed.</p>` : null}
      `
      }
    </div>
  `;
}
