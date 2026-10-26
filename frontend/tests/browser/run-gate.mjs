/** Required browser matrix. A missing runtime or failed scenario fails the gate. */
import { spawn } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const directory = dirname(fileURLToPath(import.meta.url));
const scenarios = ['chat-layout', 'followup-outcome', 'task-status-focus', 'session-recovery', 'shell-console', 'analysis-lifecycle', 'open-data-chat', 'user-management', 'skill-creation', 'skill-resource-preview', 'personal-skill-delete'];
const matrix = ['chromium', 'webkit'].flatMap(engine => scenarios.map(scenario => [engine, scenario]));
let failures = 0;
for (const [engine, scenario] of matrix) {
  console.log(`Browser gate: ${engine} / ${scenario}`);
  const code = await new Promise((done, reject) => {
    const child = spawn(process.execPath, [resolve(directory, `${scenario}-smoke.mjs`)], {
      env: { ...process.env, DATALAB_BROWSER_ENGINE: engine }, stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('exit', (status, signal) => done(signal ? 1 : status ?? 1));
  });
  if (code !== 0) failures += 1;
}
console.log(`Browser gate: ${matrix.length - failures}/${matrix.length} required scenarios passed`);
if (failures) process.exitCode = 1;
