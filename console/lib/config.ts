// The console's one setting, read on the server at request time so that one image serves every
// environment. ADMIN_API_ORIGIN is the admin API's exact origin: a scheme, a host and an optional
// port, nothing else. https is required except for a loopback host. Anything else is refused,
// and the console then serves nothing but a 503 (proxy.ts).

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

export function parseAdminApiOrigin(value: string | undefined): string | null {
  if (value === undefined || value === "") {
    return null;
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.origin !== value) {
    return null;
  }
  if (url.protocol === "https:") {
    return url.origin;
  }
  if (url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname)) {
    return url.origin;
  }
  return null;
}

export function adminApiOrigin(): string | null {
  return parseAdminApiOrigin(process.env.ADMIN_API_ORIGIN);
}
