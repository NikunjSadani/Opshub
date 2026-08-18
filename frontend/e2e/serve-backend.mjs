// Boots the FastAPI backend for the E2E harness against a FRESH sqlite DB:
// migrate -> seed (users) -> e2e master-data bootstrap -> uvicorn, all with the
// dev-auth shim + the local-only stub PDF renderer enabled. Playwright's webServer
// manages this process's lifecycle (it is killed on teardown).
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { rmSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));
const backend = join(here, '..', '..', 'backend');
const py = join(backend, '.venv', 'Scripts', 'python.exe');

const env = {
  ...process.env,
  DATABASE_URL: 'sqlite:///./_e2e.db',
  ENV: 'local',
  DEV_AUTH: 'true',
  STUB_RENDER: 'true',
};

// Fresh DB each run so the harness is deterministic.
try { rmSync(join(backend, '_e2e.db'), { force: true }); } catch { /* first run */ }

function step(args) {
  const r = spawnSync(py, args, { cwd: backend, env, stdio: 'inherit' });
  if (r.status !== 0) {
    console.error(`[e2e backend] step failed: ${args.join(' ')}`);
    process.exit(r.status ?? 1);
  }
}

step(['-m', 'alembic', 'upgrade', 'head']);
step(['-m', 'app.seed']);
step(['-m', 'app.e2e_bootstrap']);

const srv = spawn(
  py,
  ['-m', 'uvicorn', 'app.main:app', '--port', '8000', '--log-level', 'warning'],
  { cwd: backend, env, stdio: 'inherit' },
);
srv.on('exit', (code) => process.exit(code ?? 0));
process.on('SIGTERM', () => srv.kill('SIGTERM'));
process.on('SIGINT', () => srv.kill('SIGINT'));
