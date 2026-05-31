import { useState } from "react";
import clsx from "clsx";
import { api } from "../lib/api";
import { fmtUSD } from "../lib/format";

const MODE_INFO = {
  paper: {
    label: "PAPER",
    color: "text-term-amber",
    description:
      "Always simulated. Real Polymarket data, fake fills, zero risk. Bot never touches your wallet.",
  },
  live: {
    label: "LIVE",
    color: "text-term-red",
    description:
      "Always real USDC on Polygon. Trades execute on the Polymarket CLOB at the slippage guard.",
  },
  auto: {
    label: "AUTO",
    color: "text-term-purple",
    description:
      "Switches automatically. Bot trades real money when your wallet has enough USDC, otherwise paper-trades into a separate ledger. The recommended mode.",
  },
};

export default function ModeToggle({ state, config, onChange }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [info, setInfo] = useState(null);
  const [pendingMode, setPendingMode] = useState(null);

  const configured = state?.configured_mode || "paper";
  const effective = state?.mode || "paper";
  const credsPresent = config?.live_credentials_present;
  const wallet = state?.wallet;
  const threshold = state?.auto_threshold ?? 5;

  const requestMode = async (newMode, confirm = false) => {
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      const res = await api.toggleMode(newMode, confirm);
      setInfo(
        res.restart_required
          ? `Switched to ${newMode.toUpperCase()} — restart the backend for it to take effect.`
          : `Switched to ${newMode.toUpperCase()}`
      );
      setPendingMode(null);
      onChange?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const onClickMode = (newMode) => {
    if (newMode === configured) return;
    // "live" needs an explicit confirmation modal; "auto" and "paper" don't.
    if (newMode === "live") {
      setPendingMode("live");
    } else {
      requestMode(newMode, false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <span>▮ mode</span>
        <span className={MODE_INFO[configured]?.color}>
          ● {MODE_INFO[configured]?.label}
        </span>
      </div>
      <div className="panel-body space-y-3">
        <div className="text-xs text-term-gray leading-relaxed">
          {MODE_INFO[configured]?.description}
        </div>

        {configured === "auto" && (
          <div className="text-[11px] border border-border-subtle rounded p-2 bg-bg-subtle/40 space-y-1">
            <div className="flex justify-between">
              <span className="text-term-gray uppercase tracking-wider text-[10px]">
                current effective mode
              </span>
              <span className={MODE_INFO[effective]?.color}>{effective.toUpperCase()}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-term-gray uppercase tracking-wider text-[10px]">
                wallet pUSD
              </span>
              <span className="text-white">
                {wallet?.available ? fmtUSD(wallet.pusd ?? wallet.usdc, 2) : "—"}
              </span>
            </div>
            {wallet?.available && (wallet.usdce ?? 0) > 0.01 && (
              <div className="flex justify-between">
                <span className="text-term-amber uppercase tracking-wider text-[10px]">
                  USDC.e (needs wrap)
                </span>
                <span className="text-term-amber">{fmtUSD(wallet.usdce, 2)}</span>
              </div>
            )}
            <div className="flex justify-between">
              <span className="text-term-gray uppercase tracking-wider text-[10px]">
                live-trade threshold
              </span>
              <span className="text-white">{fmtUSD(threshold, 2)}</span>
            </div>
            {!wallet?.available && (
              <div className="text-term-amber pt-1">
                ⚠ wallet not configured — bot is paper-trading. Set PRIVATE_KEY in .env to enable
                live mode.
              </div>
            )}
            {wallet?.available && wallet.needs_wrap && (
              <div className="text-term-amber pt-1">
                ⚠ USDC.e detected. Polymarket settles in pUSD (post-Apr-2026).
                Run <span className="font-mono">python scripts/wrap_usdc.py</span> to wrap.
              </div>
            )}
          </div>
        )}

        <div className="grid grid-cols-3 gap-2">
          {["paper", "auto", "live"].map((m) => {
            const active = configured === m;
            const disabled =
              busy ||
              active ||
              (m === "live" && !credsPresent) ||
              (m === "auto" && !credsPresent);
            return (
              <button
                key={m}
                disabled={disabled}
                onClick={() => onClickMode(m)}
                className={clsx(
                  "btn",
                  active
                    ? "bg-bg-subtle text-white border-border cursor-default"
                    : "btn-ghost",
                  "disabled:opacity-30 disabled:cursor-not-allowed"
                )}
              >
                {MODE_INFO[m].label}
              </button>
            );
          })}
        </div>

        {!credsPresent && (
          <div className="text-[11px] text-term-amber border border-term-amber/30 bg-term-amber/5 rounded p-2">
            ⚠ Live + auto modes need POLYMARKET_API_KEY, POLYMARKET_SECRET,
            POLYMARKET_PASSPHRASE, and PRIVATE_KEY in <code>.env</code>.
          </div>
        )}

        {pendingMode === "live" && (
          <div className="border border-term-red/40 bg-term-red/5 rounded p-3 space-y-2">
            <div className="text-xs text-term-red font-semibold uppercase tracking-wider">
              ⚠ confirm always-live trading
            </div>
            <div className="text-[11px] text-term-gray">
              Every trade will use real USDC, even if the wallet balance is low. Consider AUTO
              mode instead — it only trades real money once the balance reaches{" "}
              {fmtUSD(threshold, 2)}.
            </div>
            <div className="flex gap-2">
              <button
                className="btn-danger flex-1"
                disabled={busy}
                onClick={() => requestMode("live", true)}
              >
                yes, always live
              </button>
              <button className="btn-ghost flex-1" onClick={() => setPendingMode(null)}>
                cancel
              </button>
            </div>
          </div>
        )}

        {error && <div className="text-[11px] text-term-red">⚠ {error}</div>}
        {info && <div className="text-[11px] text-term-green">✓ {info}</div>}
      </div>
    </div>
  );
}
