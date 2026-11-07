import htm from "htm";
/** Shared UI primitives: htm-bound h, chips, bars, and hand-rolled SVG charts. */
import { h } from "preact";

export const H = htm.bind(h);

export function Chip(props: {
  tone?: "good" | "bad" | "warn" | "muted";
  children: preact.ComponentChild;
}) {
  return H`<span class="chip ${props.tone ?? ""}">${props.children}</span>`;
}

export function Bar(props: { value: number | null; tone?: "g" | "r"; min?: number; max?: number }) {
  const v = props.value ?? 0;
  const min = props.min ?? 0;
  const max = props.max ?? 1;
  const pct = Math.max(0, Math.min(1, (v - min) / (max - min))) * 100;
  return H`<span class="bar ${props.tone ?? ""}" title="${v}"><i style=${{ width: `${pct.toFixed(1)}%` }}></i></span>`;
}

export function Kpi(props: { k: string; v: preact.ComponentChild; d?: preact.ComponentChild }) {
  return H`<div class="kpi"><span class="k">${props.k}</span><span class="v">${props.v}</span>${props.d ? H`<span class="d">${props.d}</span>` : null}</div>`;
}

/** Grouped bar chart in inline SVG (no chart library, no CDN). */
export function BarChart(props: {
  title?: string;
  caption?: string;
  width?: number;
  height?: number;
  groups: Array<{
    label: string;
    bars: Array<{ label: string; value: number | null; color: string }>;
  }>;
  yMax?: number;
  yFormat?: (v: number) => string;
}) {
  const W = props.width ?? 560;
  const Hh = props.height ?? 260;
  const L = 46;
  const T = 18;
  const bw = W - L - 12;
  const yMax = props.yMax ?? 1;
  const yf = props.yFormat ?? ((v: number) => `${Math.round(v * 100)}%`);
  const n = props.groups.length || 1;
  const gw = bw / n;
  const barsInGroup = Math.max(...props.groups.map((g) => g.bars.length), 1);
  const innerW = gw * 0.72;
  const unitW = innerW / barsInGroup;
  const palette = ["var(--good)", "var(--bad)", "var(--accent)", "var(--warn)"];
  const parts: preact.ComponentChild[] = [];
  props.groups.forEach((g, gi) => {
    const x0 = L + gi * gw + (gw - innerW) / 2;
    g.bars.forEach((b, bi) => {
      const hgt = b.value === null ? 0 : (b.value / yMax) * (Hh - T - 34);
      const x = x0 + bi * unitW;
      parts.push(
        H`<rect x=${x.toFixed(1)} y=${(Hh - 34 - hgt).toFixed(1)} width=${(unitW - 4).toFixed(1)} height=${hgt.toFixed(1)} rx="3" fill=${b.color ?? palette[bi % palette.length]}><title>${`${g.label} · ${b.label}: ${b.value === null ? "—" : b.value.toFixed(3)}`}</title></rect>`,
      );
    });
    parts.push(
      H`<text x=${(L + gi * gw + gw / 2).toFixed(1)} y=${Hh - 16} font-size="11" fill="var(--muted)" text-anchor="middle">${g.label}</text>`,
    );
  });
  for (let i = 0; i <= 4; i++) {
    const y = Hh - 34 - ((Hh - 34 - T) * i) / 4;
    const v = (yMax * i) / 4;
    parts.push(
      H`<line x1=${L} y1=${y.toFixed(1)} x2=${W - 12} y2=${y.toFixed(1)} stroke="var(--rule)" stroke-width="1"/>`,
    );
    parts.push(
      H`<text x=${L - 6} y=${(y + 4).toFixed(1)} font-size="11" fill="var(--muted)" text-anchor="end">${yf(v)}</text>`,
    );
  }
  return H`<figure class="fig">
    ${props.title ? H`<div style=${{ fontWeight: 600, marginBottom: "0.3rem" }}>${props.title}</div>` : null}
    <svg viewBox=${`0 0 ${W} ${Hh}`} width="100%" role="img" aria-label=${props.title ?? "chart"}>
      <rect x="0" y="0" width=${W} height=${Hh} fill="transparent"/>
      ${parts}
    </svg>
    ${props.caption ? H`<figcaption>${props.caption}</figcaption>` : null}
  </figure>`;
}

/** Simple scatter for complexity-vs-performance. */
export function Scatter(props: {
  title?: string;
  caption?: string;
  width?: number;
  height?: number;
  points: Array<{ x: number | string; y: number | null; label: string }>;
  yMax?: number;
  xLabel?: string;
  yLabel?: string;
}) {
  const W = props.width ?? 560;
  const Hh = props.height ?? 240;
  const L = 46;
  const B = 36;
  const yMax = props.yMax ?? 1;
  const xs = props.points.map((p) => (typeof p.x === "number" ? p.x : Number(p.x)));
  const xMin = Math.min(...xs, 8);
  const xMax = Math.max(...xs, 14);
  const sx = (x: number) => L + ((x - xMin) / Math.max(1, xMax - xMin)) * (W - L - 14);
  const sy = (y: number) => Hh - B - y * (Hh - B - 18) * (1 / yMax);
  const dots = props.points.map(
    (p, i) =>
      H`<circle cx=${sx(typeof p.x === "number" ? p.x : Number(p.x)).toFixed(1)} cy=${sy(p.y ?? 0).toFixed(1)} r="5" fill="var(--accent)" fill-opacity="0.85" key=${i}><title>${`${p.label}: size ${String(p.x)} → ${p.y === null ? "—" : (p.y * 100).toFixed(1)}%`}</title></circle>`,
  );
  const grid: preact.ComponentChild[] = [];
  for (let i = 0; i <= 4; i++) {
    const y = Hh - B - ((Hh - B - 18) * i) / 4;
    grid.push(
      H`<line x1=${L} y1=${y.toFixed(1)} x2=${W - 14} y2=${y.toFixed(1)} stroke="var(--rule)"/>`,
    );
    grid.push(
      H`<text x=${L - 6} y=${(y + 4).toFixed(1)} font-size="11" fill="var(--muted)" text-anchor="end">${Math.round((yMax * i * 100) / 4)}%</text>`,
    );
  }
  for (let x = xMin; x <= xMax; x++) {
    grid.push(
      H`<text x=${sx(x).toFixed(1)} y=${Hh - 16} font-size="11" fill="var(--muted)" text-anchor="middle">${x}</text>`,
    );
  }
  return H`<figure class="fig">
    ${props.title ? H`<div style=${{ fontWeight: 600, marginBottom: "0.3rem" }}>${props.title}</div>` : null}
    <svg viewBox=${`0 0 ${W} ${Hh}`} width="100%" role="img">${grid}${dots}</svg>
    ${props.caption ? H`<figcaption>${props.caption}</figcaption>` : null}
  </figure>`;
}
