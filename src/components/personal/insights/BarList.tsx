import React from "react";

export interface BarItem {
  label: string;
  value: number;
  detail?: string;
}

/** Horizontal bars (one series), value labels in text ink. */
export const BarList: React.FC<{
  title: string;
  items: BarItem[];
  emptyLabel: string;
}> = ({ title, items, emptyLabel }) => {
  const max = Math.max(0, ...items.map((i) => i.value));
  return (
    <figure className="viz-root bg-background border border-line rounded-lg p-3 m-0">
      <figcaption className="text-sm font-medium mb-2">{title}</figcaption>
      {items.length === 0 ? (
        <p className="text-xs text-ink-3 py-4 text-center">{emptyLabel}</p>
      ) : (
        <ul className="space-y-1.5">
          {items.map((item) => (
            <li
              key={item.label}
              className="grid grid-cols-[6rem_1fr_auto] items-center gap-2 text-xs"
              title={item.detail ?? `${item.label}: ${item.value}`}
            >
              <span className="truncate text-ink-2">{item.label}</span>
              <span className="h-3 rounded-r-[4px] bg-wash">
                <span
                  className="block h-3 rounded-r-[4px]"
                  style={{
                    width: `${max > 0 ? Math.max(2, (item.value / max) * 100) : 0}%`,
                    background: "var(--viz-series-1)",
                  }}
                />
              </span>
              <span className="tabular-nums">{item.detail ?? item.value}</span>
            </li>
          ))}
        </ul>
      )}
    </figure>
  );
};
