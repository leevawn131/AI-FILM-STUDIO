/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        studio: {
          950: '#090a0f',
          900: '#0f111a',
          850: '#151824',
          800: '#1d2133',
          700: '#2b3149',
          600: '#3e4668',
        },
        cinema: {
          gold: '#e5a93b',
          amber: '#f59e0b',
          crimson: '#e11d48',
          cyan: '#06b6d4',
          emerald: '#10b981',
          purple: '#8b5cf6'
        }
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
        display: ['system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
      }
    },
  },
  plugins: [],
}
