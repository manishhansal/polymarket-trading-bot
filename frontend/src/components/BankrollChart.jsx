import { useMemo } from "react";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { fmtUSD, fmtTime } from "../lib/format";

const STAGE_LINES = [25, 100, 500, 1000];

export default function BankrollChart({ history, state }) {
  const data = useMemo(() => {
    if (!history || history.length === 0) return [];
    return history.map((p) => ({
      t: new Date(p.timestamp).getTime(),
      bankroll: p.bankroll,
      cash: p.cash,
    }));
  }, [history]);

  return (
    <div className="panel h-[360px] flex flex-col">
      <div className="panel-header">
        <span>▮ bankroll curve · $5 → $1,000</span>
        <span className="text-term-green">{fmtUSD(state?.bankroll, 2)}</span>
      </div>
      <div className="flex-1 panel-body">
        {data.length < 2 ? (
          <div className="flex h-full items-center justify-center text-term-gray text-xs">
            collecting data… first snapshot writes within 60s
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
              <defs>
                <linearGradient id="g-bankroll" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#3fb950" stopOpacity={0.5} />
                  <stop offset="100%" stopColor="#3fb950" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="2 4" stroke="#21262d" />
              <XAxis
                dataKey="t"
                tickFormatter={(t) => fmtTime(new Date(t).toISOString())}
                stroke="#8b949e"
                fontSize={10}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                stroke="#8b949e"
                fontSize={10}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v) => `$${v.toFixed(0)}`}
              />
              <Tooltip
                contentStyle={{
                  background: "#0d1117",
                  border: "1px solid #30363d",
                  borderRadius: 4,
                  fontFamily: "JetBrains Mono, monospace",
                  fontSize: 11,
                }}
                labelFormatter={(t) => new Date(t).toLocaleString()}
                formatter={(v, name) => [fmtUSD(v, 2), name]}
              />
              {STAGE_LINES.map((y) => (
                <ReferenceLine
                  key={y}
                  y={y}
                  stroke="#58a6ff"
                  strokeDasharray="3 3"
                  strokeOpacity={0.35}
                  label={{
                    value: `$${y}`,
                    fill: "#58a6ff",
                    fontSize: 9,
                    position: "right",
                  }}
                />
              ))}
              <Area
                type="monotone"
                dataKey="bankroll"
                stroke="#3fb950"
                strokeWidth={2}
                fill="url(#g-bankroll)"
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
