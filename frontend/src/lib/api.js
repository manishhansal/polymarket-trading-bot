const BASE = import.meta.env.VITE_API_URL || "";

async function request(path, opts = {}) {
  const r = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const body = await r.json();
      detail = body.detail || JSON.stringify(body);
    } catch {/* ignore */}
    throw new Error(detail);
  }
  if (r.status === 204) return null;
  return r.json();
}

export const api = {
  state:           () => request("/api/state"),
  stats:           () => request("/api/stats"),
  bankrollHistory: (hours = 168) => request(`/api/bankroll/history?hours=${hours}`),
  trades:          (limit = 100) => request(`/api/trades?limit=${limit}`),
  positions:       () => request("/api/positions"),
  alerts:          (limit = 50) => request(`/api/alerts?limit=${limit}`),
  config:          () => request("/api/config"),
  updateConfig:    (body) => request("/api/config", { method: "POST", body: JSON.stringify(body) }),
  toggleMode:      (mode, confirm) =>
    request("/api/mode", { method: "POST", body: JSON.stringify({ mode, confirm }) }),
  pause:           (paused) =>
    request("/api/pause", { method: "POST", body: JSON.stringify({ paused }) }),
  forceScan:       () => request("/api/scan", { method: "POST" }),
  wallet:          (force = false) => request(`/api/wallet${force ? "?force=true" : ""}`),
};
