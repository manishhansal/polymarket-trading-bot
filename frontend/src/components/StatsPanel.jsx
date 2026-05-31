import clsx from "clsx";
import { fmtUSD, fmtPct } from "../lib/format";

function Stat({ label, value, hint, accent }) {
  return (
    <div className="rounded border border-border-subtle p-3 bg-bg-subtle/40">
      <div className="stat-label">{label}</div>
      <div className={clsx("stat-value mt-1", accent)}>{value}</div>
      {hint && <div className="text-[10px] text-term-gray mt-0.5">{hint}</div>}
    </div>
  );
}

export default function StatsPanel({ stats, state }) {
  const winRate = stats?.win_rate || 0;
  const roi = stats?.roi || 0;
  const sharpe = stats?.sharpe || 0;

  return (
    <div className="panel h-[360px] flex flex-col">
      <div className="panel-header">
        <span>▮ performance</span>
        <span>{stats?.total_trades || 0} trades</span>
      </div>
      <div className="panel-body grid grid-cols-2 gap-2 overflow-auto">
        <Stat
          label="win rate"
          value={fmtPct(winRate, 1)}
          accent={winRate >= 0.55 ? "text-term-green" : winRate > 0 ? "text-term-amber" : "text-term-gray"}
          hint={`${stats?.wins || 0}W / ${stats?.losses || 0}L`}
        />
        <Stat
          label="roi"
          value={fmtPct(roi, 1)}
          accent={roi > 0 ? "text-term-green" : roi < 0 ? "text-term-red" : "text-term-gray"}
        />
        <Stat
          label="avg edge"
          value={fmtPct(stats?.avg_edge, 2)}
          accent="text-term-blue"
        />
        <Stat
          label="sharpe"
          value={(sharpe || 0).toFixed(2)}
          accent={sharpe >= 1 ? "text-term-green" : "text-term-gray"}
        />
        <Stat
          label="realized pnl"
          value={fmtUSD(state?.realized_pnl, 2)}
          accent={
            (state?.realized_pnl || 0) > 0
              ? "text-term-green"
              : (state?.realized_pnl || 0) < 0
              ? "text-term-red"
              : "text-term-gray"
          }
        />
        <Stat
          label="unrealized"
          value={fmtUSD(state?.unrealized_pnl, 2)}
          accent={
            (state?.unrealized_pnl || 0) > 0
              ? "text-term-green"
              : (state?.unrealized_pnl || 0) < 0
              ? "text-term-red"
              : "text-term-gray"
          }
        />
        <Stat
          label="biggest win"
          value={fmtUSD(stats?.biggest_win, 2)}
          accent="text-term-green"
        />
        <Stat
          label="biggest loss"
          value={fmtUSD(stats?.biggest_loss, 2)}
          accent="text-term-red"
        />
      </div>
    </div>
  );
}
