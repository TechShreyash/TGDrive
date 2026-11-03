import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Checks local evidence integrity and coverage, not visual quality or whether a
// person/agent actually inspected the pixels. No network, installs, or writes.
export function verifyVisualEvidence(receiptPath) {
    const errors = [];
    let checkedFiles = 0;
    const fail = message => errors.push(message);
    const nonempty = value => typeof value === 'string' && value.trim().length > 0;
    const positive = value => Number.isInteger(value) && value > 0;
    const base = dirname(resolve(receiptPath));
    function artifact(value, label, media = false) {
        if (!value || !nonempty(value.path) || /^[a-z][a-z0-9+.-]*:\/\//i.test(value.path)
            || !/^[a-f0-9]{64}$/.test(value.sha256 || '')) {
            fail(`${label}: supply a local path and its actual SHA-256.`);
            return;
        }
        try {
            const bytes = readFileSync(resolve(base, value.path));
            checkedFiles++;
            if (!bytes.length || createHash('sha256').update(bytes).digest('hex') !== value.sha256) {
                fail(`${label}: file is empty or changed; recapture/review and update the receipt.`);
            }
            if (media) {
                const image = bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
                    || bytes.subarray(0, 3).equals(Buffer.from([255, 216, 255]))
                    || (bytes.toString('ascii', 0, 4) === 'RIFF' && bytes.toString('ascii', 8, 12) === 'WEBP');
                const video = bytes.toString('ascii', 4, 8) === 'ftyp'
                    || bytes.subarray(0, 4).equals(Buffer.from([0x1a, 0x45, 0xdf, 0xa3]));
                if (!(value.kind === 'image' ? image : value.kind === 'video' && video)) {
                    fail(`${label}: expected a PNG/JPEG/WebP image or MP4/MOV/WebM video matching kind.`);
                }
            }
        } catch {
            fail(`${label}: local file cannot be read.`);
        }
    }
    let receipt;
    try { receipt = JSON.parse(readFileSync(resolve(receiptPath), 'utf8')); }
    catch { return { ok: false, errors: ['Receipt must be a readable JSON file.'], checkedFiles }; }
    if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)) {
        return { ok: false, errors: ['Receipt must be a JSON object.'], checkedFiles };
    }
    if (receipt.schemaVersion !== 'uizze.visual-evidence.v1') fail('Unsupported receipt schemaVersion.');
    if (!nonempty(receipt.scope)) fail('Name the surface and change being verified.');
    if (!['web', 'ios', 'android'].includes(receipt.platform)) fail('Declare the implemented platform: web, ios, or android.');
    if (!Array.isArray(receipt.sources) || !receipt.sources.length) fail('Record the affected source files, including styles/assets that determine this render.');
    else receipt.sources.forEach((file, index) => artifact(file, `sources[${index}]`));
    if (!['target', 'existing-system'].includes(receipt.comparison)) fail('comparison must be target or existing-system.');
    if (receipt.comparison === 'target') {
        artifact(receipt.target, 'target', true);
        if (receipt.target?.kind !== 'image') fail('The comparison target must be an image.');
    }
    if (!Array.isArray(receipt.unresolved) || receipt.unresolved.length) fail('List unresolved issues; a nonempty list cannot pass.');
    const required = Array.isArray(receipt.requiredChecks) ? receipt.requiredChecks : [];
    const checks = Array.isArray(receipt.checks) ? receipt.checks : [];
    if (!required.length) fail('Define requiredChecks before claiming coverage.');
    if (new Set(required.map(check => check?.id)).size !== required.length) fail('Required check IDs must be unique.');
    if (new Set(checks.map(check => check?.id)).size !== checks.length) fail('Check results must have unique IDs.');
    for (const requirement of required) {
        if (!nonempty(requirement?.id) || !['visual', 'interaction', 'motion'].includes(requirement.kind)) {
            fail('Each required check needs an id and kind: visual, interaction, or motion.'); continue;
        }
        const check = checks.find(value => value?.id === requirement.id);
        if (!check) { fail(`${requirement.id}: missing check result.`); continue; }
        if (check.status !== 'pass') fail(`${requirement.id}: ${check.status || 'unknown'} is not a pass.`);
        if (!nonempty(check.observed) || !nonempty(check.state)) fail(`${requirement.id}: record the actual state and observed comparison/behavior.`);
        if (!['browser', 'simulator', 'device'].includes(check.environment?.kind)
            || !nonempty(check.environment?.name) || !positive(check.environment?.width) || !positive(check.environment?.height)) {
            fail(`${requirement.id}: record browser/device and actual viewport dimensions.`);
        }
        if (['ios', 'android'].includes(receipt.platform) && check.environment?.kind === 'browser') {
            fail(`${requirement.id}: browser evidence cannot verify a native interface; use simulator/device evidence or keep the check blocked.`);
        }
        const artifacts = Array.isArray(check.artifacts) ? check.artifacts : [];
        const requiredKind = requirement.kind === 'motion' ? 'video' : 'image';
        if (!artifacts.some(file => file?.kind === requiredKind)) fail(`${requirement.id}: missing ${requiredKind} evidence.`);
        artifacts.forEach((file, index) => artifact(file, `${requirement.id}.artifacts[${index}]`, true));
        if (receipt.comparison === 'target' && nonempty(receipt.target?.path)) {
            for (const file of artifacts) {
                if (!nonempty(file?.path)) continue;
                try {
                    if (realpathSync(resolve(base, file.path)) === realpathSync(resolve(base, receipt.target.path))) {
                        fail(`${requirement.id}: the target file is not a rendered-result capture; retain independent evidence.`);
                    }
                } catch { /* Missing files are reported by artifact(). */ }
            }
        }
        if (requirement.kind !== 'visual' && (!Array.isArray(check.steps) || !check.steps.length || !check.steps.every(nonempty))) {
            fail(`${requirement.id}: record the interaction steps actually exercised.`);
        }
    }
    for (const check of checks) {
        if (!required.some(requirement => requirement?.id === check?.id)) fail('Every result must correspond to a declared required check.');
    }
    return { ok: errors.length === 0, scope: receipt.scope, errors, checkedFiles,
        note: 'Validates local artifact integrity and declared coverage. It does not judge UI quality or prove visual inspection.' };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    const result = process.argv[2] ? verifyVisualEvidence(process.argv[2])
        : { ok: false, errors: ['Usage: node verify-visual-evidence.mjs /path/to/visual-evidence.json'] };
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    process.exitCode = result.ok ? 0 : 1;
}
