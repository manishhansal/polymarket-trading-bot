export const fmtUSD = (n, dp = 2) =>
  n == null
    ? "—"
    : `$${Number(n).toLocaleString(undefined, {
        minimumFractionDigits: dp,
        maximumFractionDigits: dp,
      })}`;

export const fmtPct = (n, dp = 1) =>
  n == null ? "—" : `${(Number(n) * 100).toFixed(dp)}%`;

export const fmtSigned = (n, dp = 2) => {
  if (n == null) return "—";
  const v = Number(n);
  const s = v.toFixed(dp);
  return v > 0 ? `+${s}` : s;
};

export const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour12: false });
};

export const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
};
