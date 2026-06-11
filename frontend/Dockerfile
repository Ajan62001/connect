# connect frontend — Next.js standalone output (runtime design §7).
# In production Caddy routes /api/* directly to the api container; this
# image serves ONLY pages/assets — the next.config.ts rewrite (a dev-flow
# proxy) is never in the request path.

# --- deps ---------------------------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# --- build --------------------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- runtime ------------------------------------------------------------------
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

# standalone server.js + traced node_modules subset; public/ and
# .next/static are not copied by the tracer — server.js serves them once
# placed at these exact paths (next docs: 01-next-config-js/output.md)
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

USER node
EXPOSE 3000
CMD ["node", "server.js"]
