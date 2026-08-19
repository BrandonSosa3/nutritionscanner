# Two stages: build with the full toolchain, serve the static output from an
# image with no Node in it at all. The result is a few megabytes of nginx and
# compiled assets — nothing that can execute a package script in production.

FROM node:22-alpine AS builder
WORKDIR /app

# Copied first so a dependency install is cached until the lockfile changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# `npm run build` type-checks before bundling, so a type error fails the image
# build rather than shipping.
RUN npm run build

# ── Development ───────────────────────────────────────────────────────────
FROM node:22-alpine AS dev
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ── Production ────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
