/** Launch: preflight, cost estimate, start, and the live stream. */
import { useEffect, useRef, useState } from "preact/hooks";
import { fmt, get, post } from "../api.ts";
import { H } from "../ui.tsx";

interface ModelEntry {
  provider: string;
  context_window?: number;
  max_output?: number;
  price_in?: number;
  price_out?: number;
  price_known?: boolean;
  capabilities: string[];
  matched?: string | null;
}

interface LiveLine {
  kind: "turn" | "status" | "done" | "hello" | "ping";
  turn?: {
    episode: number;
    turn: number;
    rotation_deg: number;
    error_class: string;
    progressed: boolean | null;
    parsed_action: { turn: number; step: number } | null;
    latency_s: number;
    prompt_tokens: number;
    completion_tokens: number;
    from_cache?: boolean;
  };
  status?: string;
}

export function Launch(_props: { go: (hash: string) => void }) {
  const [backends, setBackends] = useState<string[]>([]);
  const [models, setModels] = useState<Record<string, ModelEntry>>({});
  const [backend, setBackend] = useState("mock");
  const [model, setModel] = useState("mock");
  const [datasets, setDatasets] = useState<string[]>([]);
  const [dataset, setDataset] = useState("");
  const [mode, setMode] = useState("optimal");
  const [lag, setLag] = useState(1);
  const [navigate, setNavigate] = useState(false);
  const [limit, setLimit] = useState(0);
  const [maxTurns, setMaxTurns] = useState(48);
  const [runMode, setRunMode] = useState("test");
  const [estimate, setEstimate] = useState<null | {
    estimated_cost_usd: number;
    price_known: boolean;
  }>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [liveRun, setLiveRun] = useState<string | null>(null);
  const [lines, setLines] = useState<LiveLine[]>([]);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    get<{ providers: Record<string, unknown>; models: Record<string, ModelEntry> }>(
      "/api/v3/registry",
    ).then((d) => {
      setBackends(["mock", ...Object.keys(d.providers)]);
      setModels(d.models);
    });
    get<{ runs: Array<{ run_id: string }> }>("/api/v3/runs").then((d) => {
      const ids = d.runs.filter((r) => r.run_id.startsWith("ci-")).map((r) => r.run_id);
      setDatasets([
        "results/dataset-20.json",
        "results/dataset-50.json",
        "results/dataset-100.json",
        "results/dataset-200.json",
      ]);
      setDataset("results/dataset-20.json");
      void ids;
    });
  }, []);

  useEffect(() => {
    if (backend !== "mock") return;
    get<null | { estimated_cost_usd: number; price_known: boolean }>(
      `/api/v3/cost-estimate?model=${encodeURIComponent(model)}&dataset=${encodeURIComponent(dataset)}&limit=${limit || 0}&max_turns=${maxTurns}`,
    ).then((e) => setEstimate(e));
  }, [backend, model, dataset, limit, maxTurns]);

  const startLive = (runId: string): void => {
    esRef.current?.close();
    setLines([]);
    setLiveRun(runId);
    const es = new EventSource(`/api/v3/live/${encodeURIComponent(runId)}`);
    esRef.current = es;
    es.addEventListener("turn", (ev) => {
      setLines((ls) => [
        ...ls.slice(-400),
        { kind: "turn", turn: JSON.parse((ev as MessageEvent).data) },
      ]);
    });
    es.addEventListener("status", (ev) => {
      const d = JSON.parse((ev as MessageEvent).data) as { status: string };
      setLines((ls) => [...ls.slice(-400), { kind: "status", status: d.status }]);
      if (["success", "error", "interrupted"].includes(d.status)) {
        es.close();
        setMsg(`run ${runId} finished: ${d.status}`);
      }
    });
    es.addEventListener("done", () => es.close());
  };
  useEffect(() => () => esRef.current?.close(), []);

  const launch = async (): Promise<void> => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<{ run_id: string; cmd: string[] }>("/api/v3/run", {
        backend,
        model,
        dataset,
        mode: backend === "mock" ? mode : undefined,
        lag: backend === "mock" && mode === "stale" ? lag : undefined,
        navigate,
        limit: limit || 0,
        max_turns: maxTurns,
        run_mode: runMode,
      });
      setMsg(`started ${r.run_id}`);
      startLive(r.run_id);
    } catch (e) {
      setMsg(`failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const stop = async (): Promise<void> => {
    if (!liveRun) return;
    const r = await post<{ stopped: boolean; note: string }>("/api/v3/kill", { run_id: liveRun });
    setMsg(r.note);
  };

  const isMock = backend === "mock";
  const modelNames = Object.keys(models);
  const filteredModels = isMock
    ? ["mock"]
    : modelNames.filter((m) => (models[m]?.provider ?? "") !== "");

  return H`
    <div class="grid cols-2">
      <div class="card">
        <h2>Launch a run <span class="sub">preflight → estimate → start → watch live</span></h2>
        <div class="field"><label>backend</label>
          <select value=${backend} onChange=${(e: Event) => {
            const v = (e.target as HTMLSelectElement).value;
            setBackend(v);
            setModel(v === "mock" ? "mock" : "");
          }}>
            ${backends.map((b) => H`<option value=${b} selected=${b === backend}>${b}</option>`)}
          </select>
        </div>
        <div class="field"><label>model</label>
          ${
            isMock
              ? H`<input type="text" value="mock" disabled />`
              : H`
            <input type="text" list="model-list" value=${model} onInput=${(e: Event) => setModel((e.target as HTMLInputElement).value)} placeholder="start typing — registry matches by prefix" />
            <datalist id="model-list">${filteredModels.slice(0, 200).map((m) => H`<option value=${m}>${m}</option>`)}</datalist>
          `
          }
        </div>
        <div class="field"><label>dataset (the ladder is prefix-compatible)</label>
          <select value=${dataset} onChange=${(e: Event) => setDataset((e.target as HTMLSelectElement).value)}>
            ${datasets.map((d) => H`<option value=${d} selected=${d === dataset}>${d}</option>`)}
          </select>
        </div>
        <div class="grid cols-2">
          <div class="field"><label>turn cap</label><input type="number" value=${maxTurns} onInput=${(e: Event) => setMaxTurns(Number((e.target as HTMLInputElement).value) || 48)} /></div>
          <div class="field"><label>limit (0 = whole set)</label><input type="number" value=${limit} onInput=${(e: Event) => setLimit(Number((e.target as HTMLInputElement).value) || 0)} /></div>
        </div>
        ${
          isMock
            ? H`
          <div class="grid cols-2">
            <div class="field"><label>mock mode</label>
              <select value=${mode} onChange=${(e: Event) => setMode((e.target as HTMLSelectElement).value)}>
                <option value="optimal">optimal (ceiling)</option>
                <option value="stale">stale (failure mode)</option>
              </select>
            </div>
            ${mode === "stale" ? H`<div class="field"><label>lag</label><input type="number" value=${lag} onInput=${(e: Event) => setLag(Number((e.target as HTMLInputElement).value) || 1)} /></div>` : null}
          </div>`
            : null
        }
        <div class="field"><label>run mode (stamped into the artifact, forever)</label>
          <select value=${runMode} onChange=${(e: Event) => setRunMode((e.target as HTMLSelectElement).value)}>
            <option value="test">test (mock / self-test — never ranked as live)</option>
            <option value="live">live (a real measurement)</option>
          </select>
        </div>
        <div class="field" style=${{ display: "flex", gap: "1rem", alignItems: "center" }}>
          <label style=${{ margin: 0 }}><input type="checkbox" checked=${navigate} onChange=${(e: Event) => setNavigate((e.target as HTMLInputElement).checked)} /> navigation mode (turtle walks to a fixed exit)</label>
        </div>
        ${
          !isMock && model
            ? H`
          <p class="hint">
            ${models[model]?.matched ? "" : "unknown model — add it to registry/models.json for honest cost/context; "}
            price ${fmt.usd(models[model]?.price_in ?? 0)}/1k in · context ${fmt.num(models[model]?.context_window)} ·
            estimate ${estimate ? H`<b>${fmt.usd(estimate.estimated_cost_usd)}</b>${estimate.price_known ? "" : " (price unknown — this is 0, not free)"}` : "…"}
            <br />re-runs are free and identical through the response cache.
          </p>`
            : null
        }
        <div style=${{ display: "flex", gap: "0.6rem", marginTop: "0.6rem" }}>
          <button class="primary" disabled=${busy || !backend || !model || !dataset} onClick=${launch}>${busy ? "starting…" : "Start run"}</button>
          ${liveRun ? H`<button class="danger" onClick=${stop}>Stop ${liveRun}</button>` : null}
          ${liveRun ? H`<a class="btn" href=${`#/run/${liveRun}`}>open ${liveRun}</a>` : null}
        </div>
        ${msg ? H`<p class="hint" style=${{ marginTop: "0.5rem" }}>${msg}</p>` : null}
      </div>

      <div class="card">
        <h2>Live <span class="sub">${liveRun ?? "start a run to stream it turn by turn"}</span></h2>
        <div class="live-log">
          ${lines.length === 0 ? H`<span class="muted">waiting for the first turn…</span>` : null}
          ${lines.map((l, i) =>
            l.kind === "turn" && l.turn
              ? H`<span class="turn-line" key=${i}>
                  <span class="muted">ep${l.turn.episode} t${String(l.turn.turn).padStart(2, "0")}</span>
                  rot ${l.turn.rotation_deg}° · model=${l.turn.parsed_action ? `${l.turn.parsed_action.turn}/${l.turn.parsed_action.step}` : "—"} ·
                  <span class=${l.turn.error_class === "ok" ? "ok" : "bad"}>${l.turn.error_class}</span> ·
                  ${l.turn.latency_s.toFixed(2)}s · ${l.turn.prompt_tokens}/${l.turn.completion_tokens} tok${l.turn.from_cache ? " · cache" : ""}
                </span>`
              : l.kind === "status"
                ? H`<span class="turn-line" key=${i}><span class="muted">— status: ${l.status}</span></span>`
                : null,
          )}
        </div>
        <p class="hint" style=${{ marginTop: "0.5rem" }}>Every turn is written the moment it exists; the log is the same file the summary is computed from. Nothing is graded by hand.</p>
      </div>
    </div>
  `;
}
