import React, { useMemo, useRef, useState } from "react";

export interface TrendPoint {
  label: string;
  value: number;
}

interface TrendChartProps {
  title: string;
  unit: string;
  points: TrendPoint[];
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
  format = (v) => v.toFixed(1),
  emptyLabel,
}) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const geom = useMemo(() => {
    const max = niceMax(Math.max(0, ...points.map((p) => p.value)));
    const iw = W - PAD.left - PAD.right;
    const ih = H - PAD.top - PAD.bottom;
    const x = (i: number) =>
      PAD.left + (points.length <= 1 ? iw / 2 : (i / (points.length - 1)) * iw);
    const y = (v: number) => PAD.top + ih - (v / max) * ih;
    return { max, x, y, ih };
  }, [points]);

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
        </div>
      )}
    </figure>
  );
};
