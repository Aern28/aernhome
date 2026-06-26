/** Precompiled Tailwind (replaces the runtime cdn.tailwindcss.com Play CDN,
 *  which recompiled CSS in the browser on every page load).
 *  Rebuild after adding new utility classes:  npm run build:css
 *  Content globs must cover every place a class can appear — templates AND the
 *  JS that injects markup (dashboard.js service cards, nexus.js note/capture rows). */
module.exports = {
  darkMode: 'class',
  content: ['./templates/**/*.html', './static/js/**/*.js'],
  theme: {
    extend: {
      colors: {
        'dark-bg': '#0f172a',
        'dark-card': '#1e293b',
        'dark-border': '#334155',
      },
    },
  },
};
