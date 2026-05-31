import clsx from "clsx";
import { fmtUSD, fmtPct } from "../lib/format";

function ModePill({ configured, effective }) {
  if (configured === "auto") {
    return (
      <span className="pill bg-term-purple/15 text-term-purple border border-term-purple/40">
        <span
          className={clsx(
            "inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle animate-pulse-dot",
            effective === "live" ? "bg-term-red" : "bg-term-amber"
          )}
        />
        AUTO · {effective === "live" ? "LIVE" : "PAPER"}
      </span>
    );
  }
  const live = effective === "live";
  return (
    <span
      className={clsx(
        "pill border",
        live
          ? "bg-term-red/15 text-term-red border-term-red/40"
          : "bg-term-amber/15 text-term-amber border-term-amber/40"
      )}
    >
      <span
        className={clsx(
          "inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle animate-pulse-dot",
          live ? "bg-term-red" : "bg-term-amber"
        )}
      />
      {live ? "LIVE" : "PAPER"}
    </span>
  );
}

function WalletBadge({ state }) {
  const wallet = state?.wallet;
  if (!wallet) return null;

  if (!wallet.available) {
    return (
      <span
        className="pill bg-term-gray/15 text-term-gray border border-term-gray/40"
        title={wallet.error || "Set PRIVATE_KEY in .env to enable live mode"}
      >
        wallet: not configured
      </span>
    );
  }

  const threshold = state.auto_threshold ?? 5;
  const pusd = wallet.pusd ?? wallet.usdc ?? 0; // back-compat with older payloads
  const usdce = wallet.usdce ?? 0;
  const aboveThreshold = pusd >= threshold;
  const tooltip = [
    wallet.address || "",
    `pUSD:   ${pusd.toFixed(4)}`,
    `USDC.e: ${usdce.toFixed(4)}${wallet.needs_wrap ? "  <- needs wrap" : ""}`,
    `MATIC:  ${wallet.matic?.toFixed(4) || "—"}`,
  ].join("\n");

  return (
    <div className="flex items-center gap-1.5">
      <span
        className={clsx(
          "pill border font-mono",
          aboveThreshold
            ? "bg-term-green/10 text-term-green border-term-green/40"
            : "bg-term-amber/10 text-term-amber border-term-amber/40"
        )}
        title={tooltip}
      >
        wallet: {fmtUSD(pusd, 2)} pUSD
      </span>
      {wallet.needs_wrap && (
        <span
          className="pill bg-term-amber/15 text-term-amber border border-term-amber/40"
          title="USDC.e detected. Run: python scripts/wrap_usdc.py"
        >
          ⚠ {fmtUSD(usdce, 2)} USDC.e needs wrap
        </span>
      )}
    </div>
  );
}

export default function Header({ connected, isLive, paused, breaker, state, onScan, onPause }) {
  const configured = state?.configured_mode || (isLive ? "live" : "paper");
  const effective = state?.mode || (isLive ? "live" : "paper");
  const isAuto = configured === "auto";

  return (
    <header className="flex flex-wrap items-center justify-between gap-4">
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-2xl font-bold tracking-tight text-term-green">$</span>
          <h1 className="text-xl font-bold tracking-tight text-white">
            polybot<span className="text-term-gray">_terminal</span>
          </h1>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <ModePill configured={configured} effective={effective} />
          <WalletBadge state={state} />

          {paused && (
            <span className="pill bg-term-gray/15 text-term-gray border border-term-gray/40">
              paused
            </span>
          )}
          {breaker && (
            <span className="pill bg-term-red/15 text-term-red border border-term-red/40">
              circuit breaker
            </span>
          )}
          <span
            className={clsx(
              "pill border",
              connected
                ? "bg-term-green/10 text-term-green border-term-green/40"
                : "bg-term-red/10 text-term-red border-term-red/40"
            )}
          >
            {connected ? "ws ●" : "ws ○"}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-6">
        <div className="text-right">
          <div className="stat-label">{isAuto ? `${effective} bankroll` : "bankroll"}</div>
          <div className="text-2xl text-term-green font-bold tracking-tight">
            {fmtUSD(state?.bankroll, 2)}
          </div>
          {isAuto && (
            <div className="text-[10px] text-term-gray mt-0.5">
              paper {fmtUSD(state?.paper_bankroll, 2)} · live {fmtUSD(state?.live_bankroll, 2)}
            </div>
          )}
        </div>
        <div className="text-right">
          <div className="stat-label">drawdown</div>
          <div
            className={clsx(
              "text-lg font-bold",
              (state?.drawdown_pct || 0) > 0.15 ? "text-term-red" : "text-term-gray"
            )}
          >
            {fmtPct(state?.drawdown_pct)}
          </div>
        </div>
        <div className="text-right">
          <div className="stat-label">stage</div>
          <div className="text-sm text-term-blue">{state?.stage_name?.split("—")[0] || "—"}</div>
        </div>

        <div className="flex gap-2">
          <button className="btn-ghost" onClick={onScan} title="Force an immediate scan">
            scan ↻
          </button>
          <button
            className={paused ? "btn-primary" : "btn-danger"}
            onClick={onPause}
            title="Pause/resume all trading"
          >
            {paused ? "resume" : "pause"}
          </button>
        </div>
      </div>
    </header>
  );
}
