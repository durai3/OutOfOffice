"use client";

import * as React from "react";
import { formatDate } from "@/lib/utils";
import type { WeeklyHealthPoint } from "@/lib/types";

const WIDTH = 640;
const HEIGHT = 160;
const PAD_X = 12;
const PAD_TOP = 16;
const PAD_BOTTOM = 24;

/**
 * A single-series line chart for the weekly health score trend.
 *
 * One series needs no legend box (the title above it names the series); a
 * direct label on the last point substitutes for one. The accordion below
 * is the required table-view fallback for anyone who cannot read the SVG.
 */
export function HealthTrendChart({ points }: { points: WeeklyHealthPoint[] }) {
  const [hover, setHover] = React.useState<number | null>(null);
  const [showTable, setShowTable] = React.useState(false);
  const svgRef = React.useRef<SVGSVGElement>(null);

  if (points.length === 0) {
    return <p className="py-8 text-center text-xs text-muted-foreground">Not enough concluded meetings yet to chart a trend.</p>;
  }

  const plotWidth = WIDTH - PAD_X * 2;
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const step = points.length > 1 ? plotWidth / (points.length - 1) : 0;
  const xAt = (i: number) => PAD_X + step * i;
  const yAt = (score: number) => PAD_TOP + plotHeight * (1 - Math.max(0, Math.min(1, score)));

  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"} ${xAt(i).toFixed(1)} ${yAt(p.health_score).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * WIDTH;
    let nearest = 0;
    let best = Infinity;
    points.forEach((_, i) => {
      const d = Math.abs(xAt(i) - x);
      if (d < best) {
        best = d;
        nearest = i;
      }
    });
    setHover(nearest);
  }

  return (
    <div>
      <div className="relative">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="w-full"
          role="img"
          aria-label={`Weekly meeting health score, most recently ${Math.round(last.health_score * 100)}%`}
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        >
          <line x1={PAD_X} y1={yAt(0)} x2={WIDTH - PAD_X} y2={yAt(0)} stroke="var(--border)" strokeWidth={1} />
          <line x1={PAD_X} y1={yAt(1)} x2={WIDTH - PAD_X} y2={yAt(1)} stroke="var(--border)" strokeWidth={1} strokeDasharray="2 3" />
          <path d={linePath} fill="none" stroke="var(--info)" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
          {points.map((p, i) => (
            <circle
              key={p.week_start}
              cx={xAt(i)}
              cy={yAt(p.health_score)}
              r={hover === i ? 5 : 4}
              fill="var(--card)"
              stroke="var(--info)"
              strokeWidth={2}
            />
          ))}
          {hover !== null ? (
            <line x1={xAt(hover)} y1={PAD_TOP} x2={xAt(hover)} y2={HEIGHT - PAD_BOTTOM} stroke="var(--border)" strokeWidth={1} />
          ) : null}
          <text x={xAt(points.length - 1)} y={yAt(last.health_score) - 10} textAnchor="end" className="fill-foreground text-[11px] font-semibold tabular">
            {Math.round(last.health_score * 100)}%
          </text>
          {points.map((p, i) => (
            <text key={p.week_start} x={xAt(i)} y={HEIGHT - 6} textAnchor="middle" className="fill-muted-foreground text-[9px]">
              {formatDate(p.week_start).replace(/,.*/, "")}
            </text>
          ))}
        </svg>
        {hover !== null ? (
          <div
            className="pointer-events-none absolute top-0 z-10 -translate-x-1/2 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs shadow-lg"
            style={{ left: `${(xAt(hover) / WIDTH) * 100}%` }}
          >
            <p className="font-medium">{formatDate(points[hover].week_start)}</p>
            <p className="tabular text-muted-foreground">
              {Math.round(points[hover].health_score * 100)}% · {points[hover].meetings} meeting{points[hover].meetings === 1 ? "" : "s"}
            </p>
          </div>
        ) : null}
      </div>
      <button type="button" className="mt-1 text-[11px] text-muted-foreground underline hover:text-foreground" onClick={() => setShowTable((v) => !v)}>
        {showTable ? "Hide" : "Show"} as a table
      </button>
      {showTable ? (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-muted-foreground">
              <tr className="border-b border-border">
                <th className="py-1 pr-3 font-medium">Week of</th>
                <th className="py-1 pr-3 font-medium">Meetings</th>
                <th className="py-1 font-medium">Health score</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {points.map((p) => (
                <tr key={p.week_start}>
                  <td className="py-1 pr-3">{formatDate(p.week_start)}</td>
                  <td className="py-1 pr-3 tabular">{p.meetings}</td>
                  <td className="py-1 tabular">{Math.round(p.health_score * 100)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
