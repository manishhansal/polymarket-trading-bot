import { useEffect, useState, useCallback } from "react";
import clsx from "clsx";
import { api } from "./lib/api";
import { useWebSocket } from "./hooks/useWebSocket";
import BankrollChart from "./components/BankrollChart.jsx";
import MarketTable from "./components/MarketTable.jsx";
import TradeLog from "./components/TradeLog.jsx";
import ModeToggle from "./components/ModeToggle.jsx";
import StatsPanel from "./components/StatsPanel.jsx";
import AlertsFeed from "./components/AlertsFeed.jsx";
import ConfigPanel from "./components/ConfigPanel.jsx";
import Header from "./components/Header.jsx";

export default function App() {
  const [state, setState] = useState(null);
  const [stats, setStats] = useState(null);
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [config, setConfig] = useState(null);
  const [bankrollHistory, setBankrollHistory] = useState([]);

  const { connected, lastEvent } = useWebSocket("/ws");

  const refreshAll = useCallback(async () => {
    try {
      const [s, st, p, t, a, c, bh] = await Promise.all([
        api.state(),
        api.stats(),
        api.positions(),
        api.trades(50),
        api.alerts(30),
        api.config(),
        api.bankrollHistory(168),
      ]);
      setState(s);
      setStats(st);
      setPositions(p);
      setTrades(t);
      setAlerts(a);
      setConfig(c);
      setBankrollHistory(bh);
    } catch (e) {
      console.error("Refresh failed:", e);
    }
  }, []);

  useEffect(() => {
    refreshAll();
    const id = setInterval(refreshAll, 15000);
    return () => clearInterval(id);
  }, [refreshAll]);

  useEffect(() => {
    if (!lastEvent) return;
    if (lastEvent.event === "heartbeat" && lastEvent.state) {
      setState(lastEvent.state);
    }
    if (
      lastEvent.event === "trade_attempt" ||
      lastEvent.event === "stop_loss" ||
      lastEvent.event === "bankroll_snapshot"
    ) {
      refreshAll();
    }
  }, [lastEvent, refreshAll]);

  const isLive = state?.mode === "live";
  const paused = state?.trading_paused;
  const breaker = state?.circuit_breaker;

  return (
    <div className="min-h-screen p-4 md:p-6 max-w-[1600px] mx-auto">
      <Header
        connected={connected}
        isLive={isLive}
        paused={paused}
        breaker={breaker}
        state={state}
        onScan={() => api.forceScan().then(refreshAll)}
        onPause={() => api.pause(!paused).then(refreshAll)}
      />

      {/* TOP ROW — bankroll curve + stats */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
        <div className="lg:col-span-2">
          <BankrollChart history={bankrollHistory} state={state} />
        </div>
        <StatsPanel stats={stats} state={state} />
      </div>

      {/* MIDDLE — positions table + alerts feed */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
        <div className="lg:col-span-2">
          <MarketTable positions={positions} />
        </div>
        <AlertsFeed alerts={alerts} />
      </div>

      {/* BOTTOM — trades log + mode/config */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
        <div className="lg:col-span-2">
          <TradeLog trades={trades} />
        </div>
        <div className="space-y-4">
          <ModeToggle
            state={state}
            config={config}
            onChange={() => refreshAll()}
          />
          <ConfigPanel config={config} onChange={() => refreshAll()} />
        </div>
      </div>

      <footer className="mt-8 text-center text-[10px] uppercase tracking-widest text-term-gray/60">
        polybot · $5 → $1,000 ·{" "}
        <span className={clsx(connected ? "text-term-green" : "text-term-red")}>
          {connected ? "ws live" : "ws offline"}
        </span>
      </footer>
    </div>
  );
}
