/** System: doctor, keys, settings, registry, response cache, integrity. */
import { useEffect, useState } from "preact/hooks";
import { get, post } from "../api.ts";
import { Chip, H } from "../ui.tsx";

interface KeyRow {
  backend: string;
  masked: string | null;
  source: string | null;
}
interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

export function System() {
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [raster, setRaster] = useState<null | { usable: boolean; detail: string }>(null);
  const [cache, setCache] = useState<null | { entries: number; bytes: number }>(null);
  const [integrity, setIntegrity] = useState<null | {
    runs: Array<{ run_id: string; mode: string; tracked: boolean; note: string }>;
  }>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [backend, setBackend] = useState("");
  const [key, setKey] = useState("");

  const refresh = (): void => {
    get<{ keys: KeyRow[] }>("/api/v3/keys").then((d) => setKeys(d.keys));
    get<{ settings: Record<string, unknown> }>("/api/v3/settings").then((d) =>
      setSettings(d.settings),
    );
    get<{ rasteriser: { usable: boolean; detail: string } }>("/api/v3/vision-probe").then((d) =>
      setRaster(d.rasteriser),
    );
    get<{ entries: number; bytes: number }>("/api/v3/response-cache").then(setCache);
    get<{ runs: Array<{ run_id: string; mode: string; tracked: boolean; note: string }> }>(
      "/api/v3/integrity",
    ).then(setIntegrity);
  };
  useEffect(refresh, []);

  const setKeyAction = async (): Promise<void> => {
    if (!backend || !key) return;
    const r = await post<{ ok: boolean }>("/api/v3/keys", { backend, key });
    setMsg(r.ok ? `stored ${backend} (0600, gitignored, masked in every view)` : "failed");
    setKey("");
    refresh();
  };
  const clearKeyAction = async (b: string): Promise<void> => {
    await post("/api/v3/keys", { backend: b, action: "clear" });
    setMsg(`cleared ${b}`);
    refresh();
  };
  const saveSettings = async (patch: Record<string, unknown>): Promise<void> => {
    try {
      await post("/api/v3/settings", patch);
      setMsg("settings saved (configs/local/settings.json — never dirties the repo)");
      refresh();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const theme = String(settings?.theme ?? "light");
  return H`
    <div class="grid cols-2">
      <div class="card">
        <h2>Readiness <span class="sub">probed, not assumed</span></h2>
        <table class="tbl">
          <tbody>
            <tr><td>rasteriser (resvg)</td><td>${raster ? (raster.usable ? H`<${Chip} tone="good">usable</${Chip}>` : H`<${Chip} tone="bad">unusable</${Chip}>`) : "…"}</td><td class="hint">${raster?.detail ?? ""}</td></tr>
            <tr><td>response cache</td><td>${cache ? H`<${Chip} tone="good">${cache.entries} entries</${Chip}>` : "…"}</td><td class="hint">${cache ? `${(cache.bytes / 1024).toFixed(1)} KB — re-runs replay for free` : ""}</td></tr>
            <tr><td>theme</td><td>
              <select value=${theme} onChange=${(e: Event) => saveSettings({ theme: (e.target as HTMLSelectElement).value })}>
                <option value="light">light</option><option value="dark">dark</option>
              </select>
            </td><td class="hint">settings live in configs/local (gitignored)</td></tr>
            <tr><td>test mode default</td><td>
              <input type="checkbox" checked=${Boolean(settings?.test_mode)} onChange=${(e: Event) => saveSettings({ test_mode: (e.target as HTMLInputElement).checked })} />
            </td><td class="hint">show test runs instead of live ones</td></tr>
          </tbody>
        </table>
        ${msg ? H`<p class="hint">${msg}</p>` : null}
      </div>

      <div class="card">
        <h2>Provider keys <span class="sub">masked everywhere; stored 0600 under configs/local; env always wins</span></h2>
        <table class="tbl">
          <thead><tr><th>backend</th><th>key</th><th>source</th><th></th></tr></thead>
          <tbody>
            ${keys.map(
              (k) => H`<tr key=${k.backend}>
              <td>${k.backend}</td>
              <td class="mono">${k.masked ?? H`<span class="hint">(none)</span>`}</td>
              <td><${Chip} tone="muted">${k.source ?? "—"}</${Chip}></td>
              <td>${k.masked ? H`<button onClick=${() => clearKeyAction(k.backend)}>clear</button>` : null}</td>
            </tr>`,
            )}
          </tbody>
        </table>
        <div style=${{ display: "flex", gap: "0.5rem", marginTop: "0.6rem" }}>
          <select onChange=${(e: Event) => setBackend((e.target as HTMLSelectElement).value)}>
            <option value="">backend…</option>
            ${keys.map((k) => H`<option value=${k.backend}>${k.backend}</option>`)}
          </select>
          <input type="password" placeholder="paste key" value=${key} onInput=${(e: Event) => setKey((e.target as HTMLInputElement).value)} />
          <button class="primary" onClick=${setKeyAction} disabled=${!backend || !key}>store</button>
        </div>
        <p class="hint" style=${{ marginTop: "0.5rem" }}>The board binds to 127.0.0.1; set DRAWTLE_BOARD_TOKEN to require a token beyond localhost.</p>
      </div>

      <div class="card" style=${{ gridColumn: "1 / -1" }}>
        <h2>Integrity <span class="sub">a live run that is not committed is at risk — this says so</span></h2>
        ${
          integrity
            ? H`
          <table class="tbl">
            <thead><tr><th>run</th><th>mode</th><th>committed</th><th>note</th></tr></thead>
            <tbody>
              ${integrity.runs.map(
                (r) => H`<tr key=${r.run_id}>
                <td class="mono">${r.run_id}</td>
                <td><${Chip} tone=${r.mode === "live" ? "good" : "warn"}>${r.mode}</${Chip}></td>
                <td>${r.tracked ? H`<${Chip} tone="good">yes</${Chip}>` : H`<${Chip} tone="bad">no</${Chip}>`}</td>
                <td class="hint">${r.note}</td>
              </tr>`,
              )}
            </tbody>
          </table>`
            : H`<p class="hint">loading…</p>`
        }
      </div>
    </div>
  `;
}
