/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "#0d1117",
          panel: "#161b22",
          subtle: "#1f2937",
          hover: "#1c2128",
        },
        border: {
          DEFAULT: "#30363d",
          subtle: "#21262d",
        },
        term: {
          green: "#3fb950",
          dim: "#26a641",
          red: "#f85149",
          amber: "#d29922",
          blue: "#58a6ff",
          purple: "#bc8cff",
          gray: "#8b949e",
        },
      },
      fontFamily: {
        mono: [
          "JetBrains Mono",
          "Fira Code",
          "SF Mono",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      boxShadow: {
        glow: "0 0 12px rgba(63, 185, 80, 0.25)",
        "glow-red": "0 0 12px rgba(248, 81, 73, 0.3)",
      },
      keyframes: {
        pulseDot: {
          "0%, 100%": { opacity: 1 },
          "50%": { opacity: 0.4 },
        },
      },
      animation: {
        "pulse-dot": "pulseDot 1.6s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
