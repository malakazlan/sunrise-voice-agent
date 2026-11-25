// `npm run dev:mock`: the console in development against the mock admin API (mock/server.ts),
// for work before the real API is running. The mock is a separate process the console reaches
// over HTTP, exactly as it reaches the real API, so nothing of it is in the console's build.

import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const MOCK_PORT = process.env.MOCK_API_PORT ?? "8001";
const CONSOLE_PORT = process.env.PORT ?? "3000";

const children = [
  spawn(process.execPath, ["mock/server.ts"], {
    stdio: "inherit",
    env: {
      ...process.env,
      MOCK_API_PORT: MOCK_PORT,
      MOCK_CONSOLE_ORIGIN: `http://localhost:${CONSOLE_PORT}`,
    },
  }),
  spawn(process.execPath, [require.resolve("next/dist/bin/next"), "dev", "--port", CONSOLE_PORT], {
    stdio: "inherit",
    env: { ...process.env, ADMIN_API_ORIGIN: `http://localhost:${MOCK_PORT}`, NEXT_TELEMETRY_DISABLED: "1" },
  }),
];

const stop = () => {
  for (const child of children) child.kill();
};
for (const child of children) {
  child.on("exit", (code) => {
    stop();
    process.exitCode = code ?? 0;
  });
}
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
