# The admin console: Next.js built once into its standalone server, run as a non-root user. The
# image holds no setting; ADMIN_API_ORIGIN is read when a request is served (proxy.ts), so the
# same image works in every environment.
FROM node:22.23.5-bookworm-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /console
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM node:22.23.5-bookworm-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb
RUN groupadd --system --gid 10001 console \
    && useradd --system --uid 10001 --gid console --no-create-home --shell /usr/sbin/nologin console
WORKDIR /console
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
# The standalone server and the files it serves; nothing else of the build.
COPY --from=build /console/.next/standalone ./
COPY --from=build /console/.next/static ./.next/static
# Images are not optimised (next.config.ts), so the image library Next.js traces in by default is
# never loaded: it is left out.
RUN rm -rf node_modules/sharp node_modules/@img
USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --start-interval=2s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/health', {signal: AbortSignal.timeout(3000)}).then((r) => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "server.js"]
