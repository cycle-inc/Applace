import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Where this app's declared APIs are reached while developing. Applace sets
// these two when it starts the dev server; without them there is simply no
// proxy, and `npm run dev` by hand still works. Neither value carries the
// `VITE_` prefix, so neither can be inlined into anything a browser downloads
// -- they are read here, in node, and go no further.
const gateway = process.env.APPLACE_GATEWAY
const gatewayKey = process.env.APPLACE_GATEWAY_KEY ?? ''

// The gate builds, then opens the result in a browser, and a console error out
// of a minified chunk is unreadable. Applace sets this while it builds, so the
// map exists exactly where it is read and nowhere else: a deploy builds without
// it, and nothing published carries this app's sources.
const forApplace = process.env.APPLACE_GATE === '1'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { sourcemap: forApplace },
  server: gateway
    ? {
        proxy: {
          '/api/gateway': {
            target: gateway,
            changeOrigin: true,
            headers: { 'x-applace-key': gatewayKey },
            rewrite: (path) => path.replace(/^\/api\/gateway/, ''),
          },
        },
      }
    : undefined,
})
