import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#05070c",
          900: "#0a0e17",
          850: "#0f1523",
          800: "#141b2b",
          700: "#1d2739",
          600: "#2a3852",
          500: "#3d4d6b",
        },
        signal: {
          red: "#ff4d6a",
          amber: "#ffb020",
          green: "#2ee6a8",
          cyan: "#38bdf8",
          violet: "#a78bfa",
          orange: "#fb923c",
        },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
