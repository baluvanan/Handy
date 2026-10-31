import React, { useMemo, useRef, useState } from "react";

export interface TrendPoint {
  label: string;
  value: number;
}

/** A point where the series is not directly comparable to what came before
 * (a metric definition or the verbatim source changed). */
export interface TrendBreak {
  label: string;
  detail: string;
}

interface TrendChartProps {
  title: string;
  unit: string;
  points: TrendPoint[];
  /** Same labels as `points`: measured value plus an estimate. Drawn dashed
   * so estimates never look like measurements. */
  estimate?: TrendPoint[];
  estimateLabel?: string;
  breaks?: TrendBreak[];
  breakLabel?: string;
  format?: (v: number) => string;
  emptyLabel: string;
}

const W = 320;
const H = 140;
const PAD = { top: 12, right: 12, bottom: 22, left: 36 };

const niceMax = (v: number) => {
  if (v <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / exp;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * exp;
};

/** One metric over time: a 2px line, crosshair + tooltip on hover. */
export const TrendChart: React.FC<TrendChartProps> = ({
  title,
  unit,
  points,
  estimate,
  estimateLabel,
  breaks = [],
  breakLabel,
  format = (v) => v.toFixed(1),
  emptyLabel,
}) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const geom = useMemo(() => {
    const max = niceMax(
      Math.max(
        0,
        ...points.map((p) => p.value),
        ...(estimate ?? []).map((p) => p.value),
      ),
    );
    const iw = W - PAD.left - PAD.right;
    const ih = H - PAD.top - PAD.bottom;
    const x = (i: number) =>
      PAD.left + (points.length <= 1 ? iw / 2 : (i / (points.length - 1)) * iw);
    const y = (v: number) => PAD.top + ih - (v / max) * ih;
    return { max, x, y, ih };
  }, [points, estimate]);

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg || points.length === 0) return;
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < points.length; i++) {
      if (Math.abs(geom.x(i) - px) < Math.abs(geom.x(best) - px)) best = i;
    }
    setHover(best);
  };

  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${geom.x(i)},${geom.y(p.value)}`)
    .join(" ");
  const estimateByLabel = new Map(
    (estimate ?? []).map((p) => [p.label, p.value]),
  );
  const hasEstimate = (estimate ?? []).some(
    (p, i) => Math.abs(p.value - (points[i]?.value ?? p.value)) > 1e-9,
  );
  const estimatePath = hasEstimate
    ? points
        .map((p, i) => {
          const v = estimateByLabel.get(p.label) ?? p.value;
          return `${i === 0 ? "M" : "L"}${geom.x(i)},${geom.y(v)}`;
        })
        .join(" ")
    : "";
  const breakIndex = breaks
    .map((b) => ({ ...b, i: points.findIndex((p) => p.label === b.label) }))
    .filter((b) => b.i >= 0);
  const hoveredBreaks =
    hover !== null ? breakIndex.filter((b) => b.i === hover) : [];
  const ticks = [0, geom.max / 2, geom.max];
  const last = points[points.length - 1];
  const hovered = hover !== null ? points[hover] : null;

  return (
    <figure className="viz-root relative bg-background border border-line rounded-lg p-3 m-0">
      <figcaption className="flex items-baseline justify-between gap-2 mb-1">
        <span className="text-sm font-medium">{title}</span>
        {last && (
          <span className="text-xs text-ink-3">
            {format(last.value)} {unit}
          </span>
        )}
      </figcaption>
      {points.length === 0 ? (
        <p className="text-xs text-ink-3 py-8 text-center">{emptyLabel}</p>
      ) : (
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="w-full h-auto touch-none"
          role="img"
          aria-label={title}
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={geom.y(t)}
                y2={geom.y(t)}
                stroke="var(--viz-grid)"
                strokeWidth={1}
              />
              <text
                x={PAD.left - 6}
                y={geom.y(t)}
                textAnchor="end"
                dominantBaseline="middle"
                fontSize={9}
                fill="var(--viz-text-muted)"
              >
                {format(t)}
              </text>
            </g>
          ))}
          <text
            x={PAD.left}
            y={H - 6}
            fontSize={9}
            fill="var(--viz-text-muted)"
          >
            {points[0].label}
          </text>
          {points.length > 1 && (
            <text
              x={W - PAD.right}
              y={H - 6}
              textAnchor="end"
              fontSize={9}
              fill="var(--viz-text-muted)"
            >
              {last.label}
            </text>
          )}
          {breakIndex.map((b) => (
            <g key={`break-${b.label}-${b.detail}`}>
              <line
                x1={geom.x(b.i)}
                x2={geom.x(b.i)}
                y1={PAD.top}
                y2={PAD.top + geom.ih}
                stroke="var(--viz-break)"
                strokeWidth={1.5}
                strokeDasharray="4 3"
              />
              <path
                d={`M${geom.x(b.i) - 4},${PAD.top - 6} l4,5 l4,-5 z`}
                fill="var(--viz-break)"
              />
            </g>
          ))}
          {hasEstimate && (
            <path
              d={estimatePath}
              fill="none"
              stroke="var(--viz-series-1)"
              strokeOpacity={0.6}
              strokeWidth={1.5}
              strokeDasharray="4 3"
            />
          )}
          <path
            d={path}
            fill="none"
            stroke="var(--viz-series-1)"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
          {points.length <= 31 &&
            points.map((p, i) => (
              <circle
                key={p.label}
                cx={geom.x(i)}
                cy={geom.y(p.value)}
                r={hover === i ? 4 : 2.5}
                fill="var(--viz-series-1)"
                stroke="var(--viz-surface)"
                strokeWidth={2}
              />
            ))}
          {hover !== null && (
            <line
              x1={geom.x(hover)}
              x2={geom.x(hover)}
              y1={PAD.top}
              y2={PAD.top + geom.ih}
              stroke="var(--viz-text-muted)"
              strokeWidth={1}
              strokeDasharray="2 2"
            />
          )}
        </svg>
      )}
      {hovered && hover !== null && (
        <div
          className="pointer-events-none absolute top-8 bg-background border border-line rounded-md px-2 py-1 text-xs shadow-sm"
          style={{
            left: `${Math.min(70, Math.max(5, (geom.x(hover) / W) * 100))}%`,
          }}
        >
          <strong>
            {format(hovered.value)} {unit}
          </strong>
          <span className="text-ink-3"> · {hovered.label}</span>
          {hasEstimate && estimateByLabel.has(hovered.label) && (
            <div className="text-ink-3">
              {estimateLabel}: {format(estimateByLabel.get(hovered.label) ?? 0)}{" "}
              {unit}
            </div>
          )}
          {hoveredBreaks.map((b) => (
            <div key={b.detail} className="text-warning">
              ⚠ {b.detail}
            </div>
          ))}
        </div>
      )}
      {(hasEstimate || breakIndex.length > 0) && (
        <div className="flex flex-wrap gap-3 mt-1 text-[10px] text-ink-3">
          {hasEstimate && (
            <span className="flex items-center gap-1">
              <svg width="16" height="4" aria-hidden="true">
                <line
                  x1="0"
                  x2="16"
                  y1="2"
                  y2="2"
                  stroke="var(--viz-series-1)"
                  strokeOpacity={0.6}
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                />
              </svg>
              {estimateLabel}
            </span>
          )}
          {breakIndex.length > 0 && (
            <span className="flex items-center gap-1">
              <svg width="10" height="8" aria-hidden="true">
                <path d="M1,1 l4,5 l4,-5 z" fill="var(--viz-break)" />
              </svg>
              {breakLabel}
            </span>
          )}
        </div>
      )}
    </figure>
  );
};
