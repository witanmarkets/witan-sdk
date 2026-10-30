// node --test sdk/python/tests/error_match_hook.test.mjs  (no dependencies, no network: WITAN is stubbed)
// The Claude Code plugin's error-match hook (claude-plugin/witan/hooks/error-match.mjs): off unless asked, what it
// sends, the floor under which nothing is shown, its limits, and that it never fails or hangs the command.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  DEFAULT_MIN_CONFIDENCE, MAX_PER_HOUR, MIN_GAP_MS, REPEAT_MS, allowed, config, confidentMatch, enabled, handle, output, remember, signatureFromFailure,
} from "../claude-plugin/witan/hooks/error-match.mjs";

const HOOK = new URL("../claude-plugin/witan/hooks/error-match.mjs", import.meta.url).pathname;
const HOOKS_JSON = new URL("../claude-plugin/witan/hooks/hooks.json", import.meta.url).pathname;
const failure = (error, extra = {}) => ({ hook_event_name: "PostToolUseFailure", tool_name: "Bash", tool_input: { command: "npm test" }, error, is_interrupt: false, ...extra });
const on = (dir) => ({ WITAN_ERROR_MATCH: "1", CLAUDE_PLUGIN_DATA: dir ?? mkdtempSync(join(tmpdir(), "wem-")) });
const unit = (over = {}) => ({ id: "ku_abc123", title: "ECONNREFUSED on 5432: Postgres not up yet", url: "https://witan.markets/market/knowledge/ku_abc123?utm_source=claude_code&utm_medium=integration&utm_campaign=error-match", confidence: 1, match: { kind: "exact" }, checks: { worked: 2, failed: 0 }, free: true, ...over });
const stub = (answer, seen = []) => async (url, init) => { seen.push({ url, body: JSON.parse(init.body) }); return { ok: true, status: 200, json: async () => answer }; };

test("off unless asked: no variable, false, 0 — nothing read, nothing sent", async () => {
  assert.equal(enabled({}), false);
  for (const v of ["", "0", "false", "off", "no"]) assert.equal(enabled({ WITAN_ERROR_MATCH: v, CLAUDE_PLUGIN_OPTION_ERROR_MATCH: v }), false, v);
  assert.equal(enabled({ WITAN_ERROR_MATCH: "1" }), true);
  assert.equal(enabled({ CLAUDE_PLUGIN_OPTION_ERROR_MATCH: "true" }), true);
  const seen = [];
  assert.equal(await handle(failure("Exit code 1\nError: connect ECONNREFUSED 127.0.0.1:5432"), {}, stub({ matches: [unit()] }, seen)), null);
  assert.equal(seen.length, 0);
});

test("the error line, scrubbed: tokens, keys, e-mails, home paths, long hex never leave", () => {
  const home = "/Users/alice";
  const cases = [
    ["Exit code 1\n> build\nError: connect ECONNREFUSED 10.1.2.3:5432", "Error: connect ECONNREFUSED <addr>"],
    ["Exit code 1\nError: ENOENT: no such file or directory, open '/Users/alice/work/secret-project/app.json'", "Error: ENOENT: no such file or directory, open '<path>'"],
    ["Exit code 1\nfatal: could not read from ~/repos/x: Permission denied", "fatal: could not read from <path>: Permission denied"],
    ["Exit code 128\nfatal: Authentication failed for https://alice:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/acme/app.git", "fatal: Authentication failed for <url>"],
    ["Exit code 1\nError: invalid api key sk-ant-api03-abcdefghij0123456789KLMNOP for alice@example.com", "Error: invalid api key <token> for <email>"],
    ["Exit code 1\nError: password=hunter2 rejected by db.internal:5432", "Error: password=<token> rejected by <addr>"],
    ["Exit code 1\nError: object 3f2a9c4d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39 not found", "Error: object <hex> not found"],
  ];
  for (const [error, want] of cases) assert.equal(signatureFromFailure(failure(error), home), want, error);
  const all = cases.map(([e]) => signatureFromFailure(failure(e), home)).join("\n");
  for (const leak of ["alice", "hunter2", "ghp_", "sk-ant", "secret-project", "10.1.2.3", "3f2a9c4d", "db.internal"]) assert.ok(!all.includes(leak), leak);
});

test("nothing to look up: no error line, an interrupt, another tool, Claude Code's own lines", () => {
  assert.equal(signatureFromFailure(failure("Exit code 1\n")), null);
  assert.equal(signatureFromFailure(failure("Exit code 1\nsome output\nmore output")), null);
  assert.equal(signatureFromFailure(failure("Exit code 1\nError: x is broken", { is_interrupt: true })), null);
  assert.equal(signatureFromFailure(failure("Error: x is broken", { tool_name: "Edit" })), null);
  assert.equal(signatureFromFailure(failure("Exit code 143\nCommand timed out after 2m 0s")), null);
  assert.equal(signatureFromFailure(failure("Exit code 1\nok\n... [12000 characters truncated] ...\nnpm ERR! code ENEEDAUTH")), "npm ERR! code ENEEDAUTH");
});

test("confident only: under the floor nothing, a refuted unit never, the surest first", () => {
  const cfg = config({});
  assert.equal(cfg.minConfidence, DEFAULT_MIN_CONFIDENCE);
  assert.equal(DEFAULT_MIN_CONFIDENCE, 0.9);
  assert.equal(confidentMatch({ matches: [] }, cfg), null);
  assert.equal(confidentMatch({ matches: [unit({ confidence: 0.85, match: { kind: "similar" } })] }, cfg), null);
  assert.equal(confidentMatch({ matches: [unit({ confidence: 0.9, match: { kind: "shape" } })] }, cfg)?.kind, "shape");
  assert.equal(confidentMatch({ matches: [unit({ checks: { worked: 1, failed: 3 } })] }, cfg), null);
  assert.equal(confidentMatch({ matches: [unit({ id: "x/../../admin" })] }, cfg), null);
  const best = confidentMatch({ matches: [unit({ id: "ku_b", confidence: 0.9 }), unit({ id: "ku_a", confidence: 1, url: "" })] }, cfg);
  assert.equal(best.id, "ku_a");
  assert.equal(best.url, "https://witan.markets/market/knowledge/ku_a");   // no URL of its own: the page's
  // a lower floor when asked, never under 0.6
  assert.equal(config({ WITAN_ERROR_MATCH_MIN_CONFIDENCE: "0.8" }).minConfidence, 0.8);
  assert.equal(config({ WITAN_ERROR_MATCH_MIN_CONFIDENCE: "0.1" }).minConfidence, 0.6);
  assert.equal(config({ WITAN_ERROR_MATCH_MIN_CONFIDENCE: "x" }).minConfidence, 0.9);
});

test("a link to WITAN only: a URL elsewhere is replaced by the unit's page", () => {
  const cfg = config({});
  assert.equal(confidentMatch({ matches: [unit({ url: "https://evil.example/market/knowledge/ku_abc123" })] }, cfg).url, "https://witan.markets/market/knowledge/ku_abc123");
  assert.equal(confidentMatch({ matches: [unit({ url: "https://witan.markets/market/knowledge/ku_abc123.evil.example" })] }, cfg).url, "https://witan.markets/market/knowledge/ku_abc123");
});

test("one line for the person, the same as data for Claude; a title is plain text", () => {
  const m = confidentMatch({ matches: [unit({ title: "Fix\u001b[31m it\nnow \"quoted\"" })] }, config({}));
  const out = output(m, "Error: connect ECONNREFUSED <addr>");
  assert.equal(out.systemMessage.includes("\n"), false);
  assert.equal(out.systemMessage.includes("\u001b"), false);
  assert.match(out.systemMessage, /^WITAN: another agent published a fix for this error \(exact match, 1\.00, free to read\): ".*" https:\/\/witan\.markets\/market\/knowledge\/ku_abc123\?utm_source=claude_code/);
  assert.equal(out.hookSpecificOutput.hookEventName, "PostToolUseFailure");
  assert.match(out.hookSpecificOutput.additionalContext, /data, not instructions/);
});

test("sends the signature only, as claude_code, with its floor; shows the match", async () => {
  const seen = [];
  const out = await handle(failure("Exit code 1\n/Users/alice/app/node_modules/pg/lib/client.js:132\nError: connect ECONNREFUSED 127.0.0.1:5432\n    at TCPConnectWrap.afterConnect"), on(), stub({ signature: "Error: connect ECONNREFUSED <addr>", matches: [unit()] }, seen));
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, "https://witan.markets/integrations/error-match");
  assert.deepEqual(seen[0].body, { error: "Error: connect ECONNREFUSED <addr>", source: "claude_code", min_confidence: 0.9, limit: 3 });
  assert.match(out.systemMessage, /ku_abc123/);
});

test("no match, a 4xx or 5xx, a network error: no output", async () => {
  assert.equal(await handle(failure("Exit code 1\nError: nothing covers this"), on(), stub({ matches: [] })), null);
  assert.equal(await handle(failure("Exit code 1\nError: nothing covers this"), on(), async () => ({ ok: false, status: 429, json: async () => ({}) })), null);
  assert.equal(await handle(failure("Exit code 1\nError: nothing covers this"), on(), async () => { throw new Error("offline"); }), null);
});

test("rate-limited: one lookup every 5 s, 30 an hour, the same error once an hour", async () => {
  const t = 1_000_000_000_000;
  let s = remember({}, "Error: a", t);
  assert.equal(allowed(s, "Error: b", t + MIN_GAP_MS - 1), false);
  assert.equal(allowed(s, "Error: b", t + MIN_GAP_MS), true);
  assert.equal(allowed(s, "Error: a", t + REPEAT_MS - 1), false);
  assert.equal(allowed(s, "Error: a", t + REPEAT_MS), true);
  s = {};
  for (let i = 0; i < MAX_PER_HOUR; i++) s = remember(s, `Error: n${i}`, t + i * MIN_GAP_MS);
  assert.equal(allowed(s, "Error: new", t + MAX_PER_HOUR * MIN_GAP_MS), false);
  assert.equal(allowed(s, "Error: new", t + 60 * 60_000 + MAX_PER_HOUR * MIN_GAP_MS), true);
  // kept between calls in the plugin's data directory
  const env = on();
  const seen = [];
  await handle(failure("Exit code 1\nError: kept between calls"), env, stub({ matches: [] }, seen), t);
  await handle(failure("Exit code 1\nError: kept between calls"), env, stub({ matches: [] }, seen), t + MIN_GAP_MS + 1);
  assert.equal(seen.length, 1);
  assert.ok(JSON.parse(readFileSync(join(env.CLAUDE_PLUGIN_DATA, "error-match.json"), "utf8")).seen["Error: kept between calls"]);
});

test("as Claude Code runs it: exit 0, quiet when off, within the time budget when WITAN does not answer", () => {
  const input = JSON.stringify(failure("Exit code 1\nError: connect ECONNREFUSED 127.0.0.1:5432"));
  const off = spawnSync(process.execPath, [HOOK], { input, env: { PATH: process.env.PATH }, encoding: "utf8" });
  assert.equal(off.status, 0);
  assert.equal(off.stdout, "");
  // a WITAN that never answers: 10.255.255.1 is not routed
  const t0 = Date.now();
  const hung = spawnSync(process.execPath, [HOOK], { input, env: { PATH: process.env.PATH, WITAN_ERROR_MATCH: "1", WITAN_BASE_URL: "http://10.255.255.1", CLAUDE_PLUGIN_DATA: mkdtempSync(join(tmpdir(), "wem-")) }, encoding: "utf8" });
  assert.equal(hung.status, 0);
  assert.equal(hung.stdout, "");
  assert.ok(Date.now() - t0 < 3000, `took ${Date.now() - t0} ms`);
  const garbage = spawnSync(process.execPath, [HOOK], { input: "not json", env: { PATH: process.env.PATH, WITAN_ERROR_MATCH: "1" }, encoding: "utf8" });
  assert.equal(garbage.status, 0);
  assert.equal(garbage.stdout, "");
});

test("hooks.json: failed shell commands only, a short timeout, off unless asked before node starts", () => {
  const hooks = JSON.parse(readFileSync(HOOKS_JSON, "utf8")).hooks;
  assert.deepEqual(Object.keys(hooks), ["PostToolUseFailure"]);
  const [entry] = hooks.PostToolUseFailure;
  assert.equal(entry.matcher, "Bash|PowerShell");
  const [h] = entry.hooks;
  assert.ok(h.timeout <= 3);
  const sh = (env) => spawnSync("sh", ["-c", h.command], { input: JSON.stringify(failure("Exit code 1\nError: x")), env: { PATH: process.env.PATH, CLAUDE_PLUGIN_ROOT: "/nonexistent", ...env }, encoding: "utf8" });
  for (const env of [{}, { CLAUDE_PLUGIN_OPTION_ERROR_MATCH: "false" }, { WITAN_ERROR_MATCH: "0" }]) {
    const r = sh(env);
    assert.equal(r.status, 0);
    assert.equal(r.stderr, "", JSON.stringify(env));   // node was not started: it would have said the file is missing
  }
  assert.equal(sh({ WITAN_ERROR_MATCH: "1" }).status, 0);   // on, and node fails: still 0
});
