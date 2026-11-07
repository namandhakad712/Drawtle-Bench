/**
 * Entry point: hash router + layout. Everything is client-rendered Preact;
 * the server only ships JSON, the shell, the importmap, and transpiled TSX.
 */
import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { H } from "./ui.tsx";
import { Overview } from "./views/overview.tsx";
import { Runs } from "./views/runs.tsx";
import { RunDetail } from "./views/run.tsx";
import { Compare } from "./views/compare.tsx";
import { Launch } from "./views/launch.tsx";
import { System } from "./views/system.tsx";

const TABS = [
  { hash: "#/overview", label: "Overview" },
  { hash: "#/runs", label: "Runs" },
  { hash: "#/launch", label: "Launch" },
  { hash: "#/compare", label: "Compare" },
  { hash: "#/system", label: "System" },
];

function useHash(): string {
  const [hash, setHash] = useState(location.hash || "#/overview");
  useEffect(() => {
    const on = (): void => setHash(location.hash || "#/overview");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function App() {
  const hash = useHash();
  const go = (h: string): void => {
    location.hash = h;
  };
  const [cmp, setCmp] = useState<{ a: string; b: string }>({ a: "", b: "" });

  const [runMatch, runId] = hash.match(/^#\/run\/(.+)$/) ?? [null, null];
  const [compareMatch] = hash.match(/^#\/compare/) ?? [null];
  const tab = runMatch
    ? "#/runs"
    : compareMatch
      ? "#/compare"
      : TABS.some((t) => t.hash === hash)
        ? hash
        : "#/overview";

  return H`
    <div>
      <header class="top">
        <span class="brand">drawtle <span class="q">control centre v3</span></span>
        <nav>
          ${TABS.map((t) => H`<a class=${t.hash === tab ? "on" : ""} href=${t.hash}>${t.label}</a>`)}
        </nav>
        <span class="spacer" />
        <span class="hint"><span class="live-dot"></span>is the model reading the frame in front of it?</span>
      </header>
      <main>
        ${tab === "#/overview" ? H`<${Overview} go=${go} />` : null}
        ${tab === "#/runs" ? H`<${Runs} go=${go} />` : null}
        ${tab === "#/launch" ? H`<${Launch} go=${go} />` : null}
        ${tab === "#/system" ? H`<${System} />` : null}
        ${runId ? H`<${RunDetail} runId=${decodeURIComponent(runId)} />` : null}
        ${compareMatch
          ? H`<${Compare}
              a=${cmp.a}
              b=${cmp.b}
              pick=${(which: "a" | "b", id: string) => setCmp((c) => ({ ...c, [which]: id }))}
            />`
          : null}
      </main>
    </div>
  `;
}

render(H`<${App} />`, document.getElementById("root")!);
