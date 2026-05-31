import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { fmtPct } from "../lib/format";

export default function ConfigPanel({ config, onChange }) {
  const [kelly, setKelly]   = useState(0.25);
  const [edge, setEdge]     = useState(0.03);
  const [busy, setBusy]     = useState(false);
  const [saved, setSaved]   = useState(false);

  useEffect(() => {
    if (config) {
      setKelly(config.kelly_fraction ?? 0.25);
      setEdge(config.min_edge_threshold ?? 0.03);
    }
  }, [config]);

  const save = async () => {
    setBusy(true);
    setSaved(false);
    try {
      await api.updateConfig({
        kelly_fraction: Number(kelly),
        min_edge_threshold: Number(edge),
      });
      setSaved(true);
      onChange?.();
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setBusy(false);
    }
  };

  const stage = config?.active_stage;

  return (
    <div className="panel">
      <div className="panel-header">
        <span>▮ risk config</span>
        <span className="text-term-blue">{stage?.name?.split("—")[0] || "—"}</span>
      </div>
      <div className="panel-body space-y-4">
        {stage && (
          <div className="text-[11px] text-term-gray border border-border-subtle rounded p-2 bg-bg-subtle/30">
            <div className="text-term-blue font-semibold">{stage.name}</div>
            <div className="mt-1">{stage.description}</div>
            <div className="mt-1 grid grid-cols-3 gap-2 text-[10px]">
              <span>
                kelly <span className="text-white">{fmtPct(stage.kelly_fraction, 0)}</span>
              </span>
              <span>
                edge ≥ <span className="text-white">{fmtPct(stage.min_edge, 1)}</span>
              </span>
              <span>
                max pos <span className="text-white">{stage.max_positions}</span>
              </span>
            </div>
          </div>
        )}

        <label className="block">
          <div className="flex justify-between text-[10px] uppercase tracking-wider">
            <span className="text-term-gray">kelly fraction</span>
            <span className="text-white">{Number(kelly).toFixed(2)}</span>
          </div>
          <input
            type="range"
            min="0.05"
            max="1.00"
            step="0.05"
            value={kelly}
            onChange={(e) => setKelly(e.target.value)}
            className="w-full accent-term-green mt-1"
          />
        </label>

        <label className="block">
          <div className="flex justify-between text-[10px] uppercase tracking-wider">
            <span className="text-term-gray">min edge threshold</span>
            <span className="text-white">{fmtPct(edge, 1)}</span>
          </div>
          <input
            type="range"
            min="0.01"
            max="0.20"
            step="0.005"
            value={edge}
            onChange={(e) => setEdge(e.target.value)}
            className="w-full accent-term-green mt-1"
          />
        </label>

        <div className="grid grid-cols-2 gap-2 text-[10px] text-term-gray pt-2 border-t border-border-subtle">
          <div>
            max concurrent: <span className="text-white">{config?.max_concurrent_bets ?? "—"}</span>
          </div>
          <div>
            confidence ≥:{" "}
            <span className="text-white">{fmtPct(config?.min_confidence, 0)}</span>
          </div>
          <div>
            max pos size:{" "}
            <span className="text-white">{fmtPct(config?.max_position_fraction, 0)}</span>
          </div>
          <div>
            drawdown halt:{" "}
            <span className="text-white">{fmtPct(config?.drawdown_circuit_breaker, 0)}</span>
          </div>
        </div>

        <button className="btn-primary w-full disabled:opacity-40" disabled={busy} onClick={save}>
          {busy ? "saving…" : saved ? "✓ saved" : "save config"}
        </button>
      </div>
    </div>
  );
}
