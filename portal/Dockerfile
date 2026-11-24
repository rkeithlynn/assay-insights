# The portal's static build (linux/amd64). Run by scripts/build_portal.py, which exports the
# final stage's files and uploads them; nothing from this image is ever deployed as a
# container. Build context: the repository root (portal/Dockerfile.dockerignore limits it to
# portal/ and assets/). The base image is pinned by digest.

# 24.22.1-trixie-slim (Node 24 LTS)
FROM node:24.22.1-trixie-slim@sha256:89c5244bc22a9bc3287d89ebc7e49b9cb5bb84cd40e6175ac3421d5a9f68b95a AS build
ENV NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false NPM_CONFIG_UPDATE_NOTIFIER=false
WORKDIR /repo/portal
COPY portal/package.json portal/package-lock.json ./
# Exactly the locked versions, and no package's install scripts run.
RUN npm ci --ignore-scripts
COPY assets /repo/assets
COPY portal ./
RUN npm run typecheck && npm test && npm run build

FROM scratch
COPY --from=build /repo/portal/dist /
