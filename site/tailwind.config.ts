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
        // Muted slate-blue. Deliberately low-saturation — this is a tool, not
        // a product launch, and the numbers should carry the page, not the hue.
        accent: {
          DEFAULT: "#7d9bc1",
          dim: "#5f7fa6",
          deep: "#3d5878",
        },
        // Status colors are desaturated to match, but stay far enough apart in
        // hue and lightness to remain distinguishable, including for the most
        // common forms of color blindness.
        status: {
          ok: "#6d9e78",
          warn: "#c2a25e",
          bad: "#c07a72",
          info: "#7d9bc1",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontSize: {
        // Sized so a range like "4.47–5.05" fits on one line in a quarter-width
        // card — wrapping mid-figure reads as broken rather than emphatic.
        stat: ["clamp(1.75rem, 2.6vw, 2.5rem)", { lineHeight: "1.05", letterSpacing: "-0.02em" }],
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
