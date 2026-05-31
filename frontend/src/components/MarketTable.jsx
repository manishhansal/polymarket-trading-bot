import clsx from "clsx";
import { fmtUSD, fmtPct } from "../lib/format";

export default function MarketTable({ positions }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <span>▮ active positions</span>
        <span>{positions.length} open</span>
      </div>
      <div className="overflow-auto max-h-[320px]">
        {positions.length === 0 ? (
          <div className="p-6 text-center text-term-gray text-xs">
            no open positions — scanner is hunting…
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-term-gray sticky top-0 bg-bg-panel">
              <tr>
                <th className="text-left px-4 py-2">market</th>
                <th className="text-center">side</th>
                <th className="text-right">shares</th>
                <th className="text-right">avg</th>
                <th className="text-right">now</th>
                <th className="text-right">cost</th>
                <th className="text-right pr-4">pnl</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {positions.map((p) => {
                const pnl = p.unrealized_pnl_usd;
                const pnlPct = p.cost_basis_usd ? pnl / p.cost_basis_usd : 0;
                return (
                  <tr key={p.id} className="border-t border-border-subtle hover:bg-bg-hover">
                    <td className="px-4 py-2 max-w-[280px] truncate text-white">
                      #{p.market_id.slice(0, 6)}
                    </td>
                    <td className="text-center">
                      <span
                        className={clsx(
                          "pill",
                          p.side === "YES"
                            ? "bg-term-green/15 text-term-green"
                            : "bg-term-red/15 text-term-red"
                        )}
                      >
                        {p.side}
                      </span>
                    </td>
                    <td className="text-right">{p.size_shares.toFixed(2)}</td>
                    <td className="text-right">${p.avg_price.toFixed(4)}</td>
                    <td className="text-right">${p.current_price.toFixed(4)}</td>
                    <td className="text-right">{fmtUSD(p.cost_basis_usd, 2)}</td>
                    <td
                      className={clsx(
                        "text-right pr-4 font-semibold",
                        pnl > 0 ? "text-term-green" : pnl < 0 ? "text-term-red" : "text-term-gray"
                      )}
                    >
                      {fmtUSD(pnl, 2)}{" "}
                      <span className="text-[10px] opacity-70">{fmtPct(pnlPct, 1)}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
