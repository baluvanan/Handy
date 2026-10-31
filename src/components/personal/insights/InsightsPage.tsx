import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  commands,
  events,
  type Aggregate,
  type CoachingNote,
  type Discontinuity,
  type InsightsReport,
  type PeriodAggregate,
} from "@/bindings";
import { Button } from "../../ui/Button";
import { TrendChart, type TrendBreak, type TrendPoint } from "./TrendChart";
import { BarList } from "./BarList";
import { VerbatimStatusPanel } from "../verbatim/VerbatimStatusPanel";
import "../i18n";
import "./insights.css";

type Range = 7 | 30 | 90 | null;
type Granularity = "day" | "week";

const RANGES: Range[] = [7, 30, 90, null];

const fmt0 = (v: number) => Math.round(v).toString();
const fmt1 = (v: number) => (Math.round(v * 10) / 10).toString();
const fmt2 = (v: number) => (Math.round(v * 100) / 100).toString();

/** Hedges/false starts per 100 words: comparable across speaking speeds. */
const per100 = (count: number, words: number) =>
  words > 0 ? (count / words) * 100 : 0;

const StatTile: React.FC<{
  label: string;
  value: string;
  unit: string;
  note?: string;
}> = ({ label, value, unit, note }) => (
  <div className="bg-background border border-line rounded-lg p-3">
    <div className="text-xs text-ink-3">{label}</div>
    <div className="text-2xl font-semibold tabular-nums">
      {value}
      <span className="text-xs font-normal text-ink-3 ms-1">{unit}</span>
    </div>
    {note && <div className="text-xs text-ink-3 tabular-nums">{note}</div>}
  </div>
);

export const InsightsPage: React.FC = () => {
  const { t } = useTranslation();
  const [range, setRange] = useState<Range>(30);
  const [granularity, setGranularity] = useState<Granularity>("day");
  const [report, setReport] = useState<InsightsReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [coaching, setCoaching] = useState<string | null>(null);
  const [coachingBusy, setCoachingBusy] = useState(false);
  const [showTable, setShowTable] = useState(false);
  const [notes, setNotes] = useState<CoachingNote[]>([]);

  const load = useCallback(async () => {
    const result = await commands.getSpeechInsights(range);
    if (result.status === "ok") {
      setReport(result.data);
      setError(null);
    } else {
      setError(result.error);
    }
  }, [range]);

  useEffect(() => {
    void load();
    void commands.getCoachingNotes().then((r) => {
      if (r.status === "ok") setNotes(r.data);
    });
    // A finished verbatim run or a new weekly note refreshes the page.
    const unlisten = events.verbatimServiceChanged.listen((e) => {
      if (e.payload.reason === "run_finished") void load();
      if (e.payload.reason === "coaching_note")
        void commands.getCoachingNotes().then((r) => {
          if (r.status === "ok") setNotes(r.data);
        });
    });
    return () => {
      void unlisten.then((u) => u());
    };
  }, [load]);

  const periods: PeriodAggregate[] = useMemo(
    () => (granularity === "day" ? report?.days : report?.weeks) ?? [],
    [report, granularity],
  );

  const series = useCallback(
    (pick: (m: Aggregate) => number): TrendPoint[] =>
      periods
        .filter((p) => p.metrics.dictations > 0)
        .map((p) => ({ label: p.period, value: pick(p.metrics) })),
    [periods],
  );

  const discontinuities: Discontinuity[] = useMemo(
    () =>
      (granularity === "day"
        ? report?.day_discontinuities
        : report?.week_discontinuities) ?? [],
    [report, granularity],
  );

  /** Markers for one chart: definition changes of its metrics, plus source
   * changes (verbatim start, model change), which affect every metric. */
  const breaksFor = useCallback(
    (...keys: string[]): TrendBreak[] =>
      discontinuities
        .filter(
          (d) =>
            d.metrics.length === 0 || d.metrics.some((m) => keys.includes(m)),
        )
        .map((d) => ({
          label: d.period,
          detail:
            d.kind === "definitions"
              ? t("personal.insights.breaks.definitions", { detail: d.detail })
              : t("personal.insights.breaks.source", { detail: d.detail }),
        })),
    [discontinuities, t],
  );

  const frozen = periods.some((p) => p.frozen);
  const recompute = async () => {
    const r = await commands.recomputeSpeechMetrics();
    if (r.status === "error") setError(r.error);
    await load();
  };

  const requestCoaching = async () => {
    setCoachingBusy(true);
    const result = await commands.generateCoachingNote();
    setCoachingBusy(false);
    setCoaching(result.status === "ok" ? result.data : `⚠ ${result.error}`);
  };

  const totals = report?.totals;
  const empty = t("personal.insights.empty");

  return (
    <div className="max-w-3xl w-full mx-auto space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 px-1">
        <div>
          <h1 className="text-lg font-semibold">
            {t("personal.insights.title")}
          </h1>
          <p className="text-xs text-ink-3">
            {t("personal.insights.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <div className="flex rounded-md border border-line overflow-hidden">
            {RANGES.map((r) => (
              <button
                key={String(r)}
                type="button"
                className={`px-2 py-1 ${range === r ? "bg-logo-primary/20 font-medium" : "hover:bg-wash"}`}
                onClick={() => setRange(r)}
              >
                {r === null
                  ? t("personal.insights.range.all")
                  : t("personal.insights.range.days", { count: r })}
              </button>
            ))}
          </div>
          <div className="flex rounded-md border border-line overflow-hidden">
            {(["day", "week"] as Granularity[]).map((g) => (
              <button
                key={g}
                type="button"
                className={`px-2 py-1 ${granularity === g ? "bg-logo-primary/20 font-medium" : "hover:bg-wash"}`}
                onClick={() => setGranularity(g)}
              >
                {t(`personal.insights.granularity.${g}`)}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && <p className="text-sm text-error px-1">{error}</p>}

      <VerbatimStatusPanel />

      {totals && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <StatTile
            label={t("personal.insights.metrics.wpm")}
            value={fmt0(totals.words_per_minute)}
            unit={t("personal.insights.units.wpm")}
          />
          <StatTile
            label={t("personal.insights.metrics.fillers")}
            value={fmt1(totals.fillers.per_minute)}
            unit={t("personal.insights.units.perMin")}
            note={
              totals.estimated_fillers.count > 0
                ? t("personal.insights.units.withEstimateNote", {
                    estimate: fmt1(totals.fillers_with_estimate.per_minute),
                  })
                : undefined
            }
          />
          <StatTile
            label={t("personal.insights.metrics.medianPause")}
            value={fmt1(totals.pauses.median_ms / 1000)}
            unit={t("personal.insights.units.seconds")}
          />
          <StatTile
            label={t("personal.insights.metrics.dictations")}
            value={String(totals.dictations)}
            unit={t("personal.insights.units.words", { count: totals.words })}
          />
        </div>
      )}

      {totals && totals.dictations > 0 && (
        <p className="text-xs text-ink-3 px-1">
          {t("personal.insights.basis", {
            verbatim: totals.basis.verbatim,
            total: totals.dictations,
          })}
          {totals.basis.asr_only > 0 &&
            ` ${t("personal.insights.basisEstimate", {
              samples: report?.calibration_samples ?? 0,
            })}`}
        </p>
      )}
      {frozen && (
        <div className="flex items-center justify-between gap-2 px-1 text-xs text-ink-3">
          <span>{t("personal.insights.frozen")}</span>
          <Button size="sm" variant="ghost" onClick={recompute}>
            {t("personal.insights.recompute")}
          </Button>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <TrendChart
          title={t("personal.insights.metrics.wpm")}
          unit={t("personal.insights.units.wpm")}
          points={series((m) => m.words_per_minute)}
          breaks={breaksFor("words_per_minute")}
          breakLabel={t("personal.insights.breaks.legend")}
          format={fmt0}
          emptyLabel={empty}
        />
        <TrendChart
          title={t("personal.insights.metrics.fillers")}
          unit={t("personal.insights.units.perMin")}
          points={series((m) => m.fillers.per_minute)}
          estimate={series((m) => m.fillers_with_estimate.per_minute)}
          estimateLabel={t("personal.insights.withEstimate")}
          breaks={breaksFor("fillers", "filler_estimate")}
          breakLabel={t("personal.insights.breaks.legend")}
          format={fmt1}
          emptyLabel={empty}
        />
        <TrendChart
          title={t("personal.insights.metrics.hedges")}
          unit={t("personal.insights.units.per100")}
          points={series((m) => per100(m.hedges.count, m.words))}
          breaks={breaksFor("hedges")}
          breakLabel={t("personal.insights.breaks.legend")}
          format={fmt1}
          emptyLabel={empty}
        />
        <TrendChart
          title={t("personal.insights.metrics.restarts")}
          unit={t("personal.insights.units.per100")}
          points={series((m) =>
            per100(m.false_starts.count + m.repeated_words.count, m.words),
          )}
          breaks={breaksFor("false_starts", "repeated_words")}
          breakLabel={t("personal.insights.breaks.legend")}
          format={fmt1}
          emptyLabel={empty}
        />
        <TrendChart
          title={t("personal.insights.metrics.pauses")}
          unit={t("personal.insights.units.seconds")}
          points={series((m) => m.pauses.median_ms / 1000)}
          breaks={breaksFor("pauses")}
          breakLabel={t("personal.insights.breaks.legend")}
          format={fmt1}
          emptyLabel={empty}
        />
        <TrendChart
          title={t("personal.insights.metrics.variety")}
          unit={t("personal.insights.units.mattr")}
          points={series((m) => m.mattr)}
          breaks={breaksFor("vocabulary")}
          breakLabel={t("personal.insights.breaks.legend")}
          format={fmt2}
          emptyLabel={empty}
        />
      </div>

      {totals && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <BarList
            title={t("personal.insights.breakdown.fillers")}
            emptyLabel={empty}
            items={Object.entries(totals.fillers_by_word)
              .filter(([, r]) => r)
              .sort(([, a], [, b]) => (b?.count ?? 0) - (a?.count ?? 0))
              .slice(0, 8)
              .map(([word, r]) => ({
                label: word,
                value: r?.count ?? 0,
                detail: `${r?.count ?? 0} · ${fmt1(r?.per_minute ?? 0)}/min`,
              }))}
          />
          <BarList
            title={t("personal.insights.breakdown.hedges")}
            emptyLabel={empty}
            items={Object.entries(totals.hedges_by_phrase)
              .filter(([, r]) => r)
              .sort(([, a], [, b]) => (b?.count ?? 0) - (a?.count ?? 0))
              .slice(0, 8)
              .map(([phrase, r]) => ({
                label: phrase,
                value: r?.count ?? 0,
              }))}
          />
          <BarList
            title={t("personal.insights.breakdown.pauses")}
            emptyLabel={empty}
            items={["<1s", "1–2s", "2–5s", "≥5s"].map((label, i) => ({
              label,
              value: totals.pauses.buckets[i] ?? 0,
            }))}
          />
        </div>
      )}

      <div className="bg-background border border-line rounded-lg p-3 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="text-sm font-medium">
              {t("personal.insights.coaching.title")}
            </div>
            <div className="text-xs text-ink-3">
              {t("personal.insights.coaching.description")}
            </div>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onClick={requestCoaching}
            disabled={coachingBusy}
          >
            {coachingBusy
              ? t("personal.insights.coaching.busy")
              : t("personal.insights.coaching.button")}
          </Button>
        </div>
        {coaching && (
          <p className="text-sm whitespace-pre-wrap select-text">{coaching}</p>
        )}
        {notes.length > 0 && (
          <div className="border-t border-line pt-2 space-y-2">
            <div className="text-xs text-ink-3">
              {t("personal.insights.coaching.weekly")}
            </div>
            {notes.slice(0, 4).map((n) => (
              <div key={n.week}>
                <div className="text-xs font-medium">{n.week}</div>
                <p className="text-sm whitespace-pre-wrap select-text">
                  {n.text}
                </p>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="px-1">
        <button
          type="button"
          className="text-xs text-ink-3 underline"
          onClick={() => setShowTable((v) => !v)}
        >
          {showTable
            ? t("personal.insights.table.hide")
            : t("personal.insights.table.show")}
        </button>
        {showTable && (
          <table className="w-full mt-2 text-xs tabular-nums">
            <thead className="text-ink-3 text-start">
              <tr>
                <th className="text-start font-normal">
                  {t("personal.insights.table.period")}
                </th>
                <th className="text-end font-normal">
                  {t("personal.insights.metrics.dictations")}
                </th>
                <th className="text-end font-normal">
                  {t("personal.insights.units.wpm")}
                </th>
                <th className="text-end font-normal">
                  {t("personal.insights.table.fillersPerMin")}
                </th>
                <th className="text-end font-normal">
                  {t("personal.insights.table.hedgesPer100")}
                </th>
                <th className="text-end font-normal">
                  {t("personal.insights.table.medianPause")}
                </th>
                <th className="text-end font-normal">MATTR</th>
              </tr>
            </thead>
            <tbody>
              {periods.map((p) => (
                <tr key={p.period} className="border-t border-line">
                  <td>{p.period}</td>
                  <td className="text-end">{p.metrics.dictations}</td>
                  <td className="text-end">
                    {fmt0(p.metrics.words_per_minute)}
                  </td>
                  <td className="text-end">
                    {fmt1(p.metrics.fillers.per_minute)}
                    {p.metrics.estimated_fillers.count > 0 &&
                      ` (${fmt1(p.metrics.fillers_with_estimate.per_minute)}*)`}
                  </td>
                  <td className="text-end">
                    {fmt1(per100(p.metrics.hedges.count, p.metrics.words))}
                  </td>
                  <td className="text-end">
                    {fmt1(p.metrics.pauses.median_ms / 1000)}
                  </td>
                  <td className="text-end">{fmt2(p.metrics.mattr)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};
