import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Base surfaces — near-black, cool-shifted, like a good terminal theme.
        ink: {
          950: "#08090c",
          900: "#0b0d11",
          850: "#0f1216",
          800: "#14181e",
          700: "#1c222a",
          600: "#272f3a",
        },
        // Restrained single accent. Everything else is status color.
        accent: {
          DEFAULT: "#5eead4",
          dim: "#2dd4bf",
          deep: "#0f766e",
        },
        status: {
          ok: "#4ade80",
          warn: "#fbbf24",
          bad: "#f87171",
          info: "#60a5fa",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontSize: {
        stat: ["clamp(2.5rem, 6vw, 4.25rem)", { lineHeight: "0.95", letterSpacing: "-0.03em" }],
      },
      keyframes: {
        "row-in": {
          "0%": { opacity: "0", transform: "translateY(-8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-ring": {
          "0%": { boxShadow: "0 0 0 0 currentColor", opacity: "0.7" },
          "70%": { boxShadow: "0 0 0 6px transparent", opacity: "0" },
          "100%": { boxShadow: "0 0 0 0 transparent", opacity: "0" },
        },
      },
      animation: {
        "row-in": "row-in 260ms cubic-bezier(0.16, 1, 0.3, 1)",
        "pulse-ring": "pulse-ring 2s ease-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
