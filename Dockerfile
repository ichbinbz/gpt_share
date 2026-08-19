FROM node:20-alpine AS FrontendBuilder

WORKDIR /app
RUN corepack enable && corepack prepare pnpm@8.15.9 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./frontend/

WORKDIR /app/frontend
RUN pnpm install --frozen-lockfile
COPY frontend ./
RUN pnpm build

FROM caddy:2.8-alpine AS CaddyBinary

FROM python:3.12-slim

COPY --from=CaddyBinary /usr/bin/caddy /usr/bin/caddy

WORKDIR /app
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY Caddyfile ./Caddyfile
COPY backend ./backend
COPY --from=FrontendBuilder /app/frontend/dist ./dist

EXPOSE 80

COPY startup.sh ./startup.sh
RUN chmod +x ./startup.sh && mkdir /data
CMD ["/app/startup.sh"]
