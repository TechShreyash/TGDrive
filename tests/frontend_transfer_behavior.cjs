/* Compatibility entry point: exercise the actual TypeScript upload controller. */
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const result = spawnSync(process.execPath, [path.resolve(__dirname, '../website/node_modules/vitest/vitest.mjs'), 'run'], {cwd:path.resolve(__dirname, '../website'), stdio:'inherit'});
process.exit(result.status ?? 1);
