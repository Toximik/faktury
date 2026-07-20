/**
 * Tailwind config for the self-hosted (CDN-free) build.
 *
 * Regenerate the stylesheet after changing templates:
 *   .tools/tailwindcss -c tailwind.config.js \
 *     -i app/static/css/tailwind.input.css -o app/static/css/tailwind.css --minify
 *
 * The standalone CLI binary (.tools/tailwindcss) needs no Node.js. The compiled
 * app/static/css/tailwind.css IS committed and served directly by Nginx, so the
 * server never needs the CLI.
 */
module.exports = {
  content: ["./app/templates/**/*.html"],
  theme: { extend: {} },
  plugins: [],
};
