import clsx from "clsx";
import { fmtUSD, fmtPct, fmtDate } from "../lib/format";

const STATUS_COLOR = {
  filled:         "bg-term-green/15 text-term-green",
  open:           "bg-term-blue/15 text-term-blue",
  pending:        "bg-term-amber/15 text-term-amber",
  cancelled:      "bg-term-gray/15 text-term-gray",
  rejected:       "bg-term-red/15 text-term-red",
  resolved_win:   "bg-term-green/20 text-term-green",
  resolved_loss:  "bg-term-red/15 text-term-red",
  stopped_out:    "bg-term-red/20 text-term-red",
};

export default function TradeLog({ trades }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <span>▮ trade log</span>
        <span>{trades.length} entries</span>
      </div>
      <div className="overflow-auto max-h-[420px]">
        {trades.length === 0 ? (
          <div className="p-6 text-center text-term-gray text-xs">
            no trades yet — sit tight, the scanner runs every 60s
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-term-gray sticky top-0 bg-bg-panel">
              <tr>
                <th className="text-left px-4 py-2">time</th>
                <th className="text-left">mode</th>
                <th className="text-left">market</th>
                <th className="text-center">side</th>
                <th className="text-right">size</th>
                <th className="text-right">edge</th>
                <th className="text-center">status</th>
                <th className="text-right pr-4">pnl</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {trades.map((t) => (
                <tr key={t.id} className="border-t border-border-subtle hover:bg-bg-hover">
                  <td className="px-4 py-2 text-term-gray">{fmtDate(t.created_at)}</td>
                  <td>
                    <span
                      className={clsx(
                        "pill",
                        t.mode === "live"
                          ? "bg-term-red/15 text-term-red"
                          : "bg-term-amber/15 text-term-amber"
                      )}
                    >
                      {t.mode}
                    </span>
                  </td>
                  <td className="max-w-[280px] truncate text-white" title={t.market_question}>
                    {t.market_question}
                  </td>
                  <td className="text-center">
                    <span
                      className={clsx(
                        "pill",
                        t.side === "YES"
                          ? "bg-term-green/15 text-term-green"
                          : "bg-term-red/15 text-term-red"
                      )}
                    >
                      {t.side}
                    </span>
                  </td>
                  <td className="text-right">{fmtUSD(t.size_usd, 2)}</td>
                  <td className="text-right text-term-blue">{fmtPct(t.edge, 2)}</td>
                  <td className="text-center">
                    <span className={clsx("pill", STATUS_COLOR[t.status] || "bg-term-gray/15 text-term-gray")}>
                      {t.status.replace("_", " ")}
                    </span>
                  </td>
                  <td
                    className={clsx(
                      "text-right pr-4 font-semibold",
                      t.pnl_usd > 0 ? "text-term-green" : t.pnl_usd < 0 ? "text-term-red" : "text-term-gray"
                    )}
                  >
                    {t.pnl_usd ? fmtUSD(t.pnl_usd, 2) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
