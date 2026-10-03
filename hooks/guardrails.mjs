#!/usr/bin/env node
/**
 * ENVIRONMENT layer: project-aware tool guardrails.
 *
 * Hook: PreToolUse(Bash). It ONLY adds friction — it never approves anything.
 *   deny  = irreversible, or rewrites shared history (data you cannot get back)
 *   ask   = destructive but recoverable, costs real money, or is outward-facing
 *   else  = passthrough: your normal permission flow (settings.json) decides
 *
 * A bug here must never block your work, so every failure path falls through to
 * passthrough.
 *
 * ── How it decides ────────────────────────────────────────────────────────────
 * Pattern-matching a raw command string is the wrong shape for this problem: a
 * regex over the whole string is defeated by a prefix, a quote, or a second
 * clause. So the command is taken apart first and judged structurally:
 *
 *   1. SPLIT into segments on unquoted `; | & newline ( )`, backticks, and `{ }` when they
 *      stand alone as group words. A quoted separator is not a separator, so a commit
 *      message containing `;` stays one segment; `${VAR}` and `{a,b}` stay one word.
 *      Heredoc bodies are removed first: they are stdin data, not commands.
 *   2. TOKENIZE each segment with quote awareness, so a quoted path resolves to
 *      the path it names, and a quoted string in a `-m` message stays an argument
 *      rather than being read as a command.
 *   3. CANONICALIZE: drop `VAR=val` prefixes and wrappers (`sudo`, `env`, `nice`,
 *      `timeout`, `xargs`, `exec`, …) with their own options, strip a leading
 *      backslash, reduce an absolute program path to its basename, and consume
 *      git's global options (`-C`, `-c`, `--git-dir`, …) so the subcommand is found
 *      wherever it sits. The string handed to `sh -c` / `bash -c` or to `eval` is
 *      split and judged as a command line of its own.
 *   4. COLLECT every rule that fires, across every segment, then emit the MAXIMUM
 *      severity. First-match-wins is not sound here: in a chain, the clause that
 *      matches first is not necessarily the clause that does the damage.
 *
 * Judging tokens in command position — rather than substrings anywhere in the
 * line — is also what keeps this quiet: a dangerous-looking string inside a
 * commit message, an `echo`, or a `grep` pattern is an argument, not a command.
 *
 * ── What this cannot see, by construction ─────────────────────────────────────
 * A PreToolUse hook reads the command *before* the shell runs it, so anything the
 * shell resolves at runtime is invisible here. These are NOT covered, and no
 * amount of pattern work would cover them:
 *
 *   · variable expansion            the value is not known until the shell runs
 *   · command substitution results  likewise
 *   · encoded payloads              the real command never appears in the string
 *   · shell aliases and functions   defined in the user's rc files
 *   · scripts invoked by name       ./deploy.sh, make clean, npm run reset
 *   · git aliases                   resolved from .gitconfig, not from this text
 *   · stdin redirection             `some-upload-tool < .env` — the redirection is
 *                                   stripped before the rules see it
 *   · output redirection            `: > file`, `cat /dev/null > file` truncate a
 *                                   file with no program the rules name
 *   · commands piped into a shell   `echo "..." | sh` — the text only becomes a
 *                                   command at runtime
 *   · heredoc bodies                data for the program's stdin; a heredoc fed to
 *                                   a shell (`bash <<EOF`) is the case above
 *   · other interpreters            `python -c`, `node -e`, `perl -e` run code in a
 *                                   language this hook does not parse
 *
 * And coverage is ENUMERATED, not exhaustive. The rules below name specific
 * programs: rm, shred, truncate, dd, find, git, and a listed set of readers and
 * network tools for dotenv files. Any other program that destroys data or moves
 * secrets is simply not in the list — mv, unlink, rsync --delete, cp /dev/null,
 * docker, every cloud CLI — and neither are git's branch-pointer rewrites
 * (branch -f, switch -C, update-ref). Adding one is a few lines; assuming the list
 * is complete is the mistake.
 *
 * Treat this as a seatbelt against a careless command, not as a sandbox against a
 * determined one. The sandbox is the permission system; this is friction on top.
 */
import { readFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

// ── output ───────────────────────────────────────────────────────────────────

const emit = (decision, reason) => {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: decision, // "deny" | "ask"
      permissionDecisionReason: reason,
    },
  }));
  process.exit(0);
};
const passthrough = () => process.exit(0); // no JSON on exit 0 → normal flow decides

// ── 1. split: quote-aware segmentation ───────────────────────────────────────

/**
 * A heredoc body is data fed to a program's stdin — a script for `python3 -`, the
 * contents of a file for `cat >` — not a command line. Drop the body before
 * splitting, or every line of it is judged as a command of its own. The line that
 * opens the heredoc, and anything after the closing delimiter, are still judged.
 */
function stripHeredocs(line) {
  const out = [];
  const pending = [];
  for (const l of line.split('\n')) {
    if (pending.length) {
      const { delim, tabs } = pending[0];
      if ((tabs ? l.replace(/^\t+/, '') : l) === delim) pending.shift();
      continue;
    }
    out.push(l);
    // `<<EOF`, `<<-EOF`, `<< 'EOF'`, `<<"EOF"` — but not the here-string `<<<`.
    for (const m of l.matchAll(/(?<!<)<<(-?)\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2/g))
      pending.push({ delim: m[3], tabs: m[1] === '-' });
  }
  return out.join('\n');
}

/** Split a command line on unquoted separators. A quoted separator is literal. */
function splitSegments(line) {
  const out = [];
  let cur = '', inS = false, inD = false, param = 0;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inS) { cur += c; if (c === "'") inS = false; continue; }
    if (inD) {
      cur += c;
      if (c === '\\' && i + 1 < line.length) { cur += line[++i]; }
      else if (c === '"') inD = false;
      continue;
    }
    if (c === "'") { inS = true; cur += c; continue; }
    if (c === '"') { inD = true; cur += c; continue; }
    if (c === '\\' && i + 1 < line.length) { cur += c + line[++i]; continue; }
    // `${VAR}` is a word, not a group: keep it whole, nesting included.
    if (c === '$' && line[i + 1] === '{') { param++; cur += '${'; i++; continue; }
    if (c === '}' && param) { param--; cur += c; continue; }
    // `{` and `}` are group words only when they stand alone (`{ cmd; }`);
    // `{a,b}` is brace expansion and stays part of its word.
    if (c === '{' && !/\s/.test(line[i + 1] ?? ' ')) { cur += c; continue; }
    if (c === '}' && !/(^|[\s;])$/.test(cur)) { cur += c; continue; }
    // A substitution or a group opens a nested command; treat the boundary as a
    // split so the nested body is judged as a command in its own right.
    if (c === ';' || c === '|' || c === '&' || c === '\n' ||
        c === '(' || c === ')' || c === '{' || c === '}' || c === '`') {
      out.push(cur); cur = ''; continue;
    }
    cur += c;
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

/** Tokenize one segment, resolving quotes and backslash escapes. */
function tokenize(seg) {
  const out = [];
  let cur = '', started = false, inS = false, inD = false;
  for (let i = 0; i < seg.length; i++) {
    const c = seg[i];
    if (inS) { if (c === "'") inS = false; else cur += c; continue; }
    if (inD) {
      if (c === '"') inD = false;
      else if (c === '\\' && i + 1 < seg.length) cur += seg[++i];
      else cur += c;
      continue;
    }
    if (c === "'") { inS = true; started = true; continue; }
    if (c === '"') { inD = true; started = true; continue; }
    if (c === '\\' && i + 1 < seg.length) { cur += seg[++i]; started = true; continue; }
    if (/\s/.test(c)) { if (started) { out.push(cur); cur = ''; started = false; } continue; }
    cur += c; started = true;
  }
  if (started) out.push(cur);
  return out;
}

// ── 3. canonicalize ──────────────────────────────────────────────────────────

/**
 * Wrappers that run another command; the real command is what follows. Each one
 * lists the options that consume a value, and how many plain positionals come
 * before the command (`timeout 10 cmd`). Options are per wrapper because the same
 * letter means different things: `sudo -s` takes no value, `timeout -s` does.
 */
const WRAPPERS = new Map(Object.entries({
  sudo:       { values: ['-u', '-g', '-h', '-p', '-C', '-U', '-r', '-t', '-D', '--user', '--group', '--host', '--prompt', '--chdir'] },
  doas:       { values: ['-u', '-C'] },
  env:        { values: ['-u', '-C', '--unset', '--chdir'] },
  '!':        {},
  // Shell grammar words that precede a command: `if rm ...`, `then rm ...`, `do rm ...`.
  if: {}, then: {}, elif: {}, else: {}, do: {}, while: {}, until: {}, coproc: {},
  busybox:    {},
  flock:      { values: ['-w', '--timeout', '-E', '--conflict-exit-code'], positionals: 1 },
  command:    {},
  builtin:    {},
  exec:       { values: ['-a'] },
  nohup:      {},
  time:       { values: ['-f', '-o', '--format', '--output'] },
  nice:       { values: ['-n', '--adjustment'] },
  ionice:     { values: ['-c', '-n', '-p', '--class', '--classdata'] },
  stdbuf:     { values: ['-i', '-o', '-e', '--input', '--output', '--error'] },
  setsid:     {},
  timeout:    { values: ['-s', '-k', '--signal', '--kill-after'], positionals: 1 },
  caffeinate: { values: ['-t', '-w'] },
  xargs:      { values: ['-I', '-n', '-P', '-L', '-d', '-s', '-E', '-a', '--max-args', '--max-procs',
                         '--max-lines', '--delimiter', '--max-chars', '--eof', '--arg-file', '--replace'] },
}));

/** Shells whose `-c` argument is itself a command line, judged like any other. */
const SHELLS = new Set(['sh', 'bash', 'zsh', 'dash', 'ksh', 'fish']);

/** How deep a `sh -c "sh -c ..."` chain is followed before giving up. */
const MAX_NESTING = 4;

/** Drop shell redirections — they are not operands. */
function dropRedirections(tokens) {
  const out = [];
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (/^[0-9]*(>>?|<<?|&>>?|>&)$/.test(t)) { i++; continue; }   // redirection + its target
    if (/^[0-9]*(>>?|<<?|&>>?|>&)\S/.test(t)) continue;           // target glued to the operator
    out.push(t);
  }
  return out;
}

/**
 * Reduce a segment to { program, args, gitDir }, with wrappers and paths removed.
 * `gitDir` is the value of a consumed `git -C <path>`, needed to resolve HEAD.
 */
function canonicalize(seg) {
  let tokens = dropRedirections(tokenize(seg));
  for (;;) {
    if (!tokens.length) return null;
    const t = tokens[0];
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(t)) { tokens = tokens.slice(1); continue; }
    const name = path.basename(t.replace(/^\\/, '')).toLowerCase();
    const wrapper = WRAPPERS.get(name);
    if (wrapper) {
      const values = new Set(wrapper.values || []);
      tokens = tokens.slice(1);
      // Skip that wrapper's own options (and their values) to reach the real command.
      while (tokens.length && tokens[0].startsWith('-')) {
        if (tokens[0] === '--') { tokens = tokens.slice(1); break; }
        const [tok] = tokens;
        const opt = tok.split('=')[0];
        // A short cluster takes a value when its LAST letter does: `sudo -iu root`.
        const cluster = !tok.startsWith('--') && tok.length > 2 && !tok.includes('=');
        const takesValue = !tok.includes('=') &&
          (values.has(opt) || (cluster && values.has(`-${tok.at(-1)}`)));
        // `env -S "cmd args"` splits its string into the command it runs.
        const splits = name === 'env' &&
          (opt === '--split-string' || (!tok.startsWith('--') && tok.slice(1).includes('S')));
        if (splits) {
          const inline = tok.includes('=') ? tok.slice(tok.indexOf('=') + 1) : null;
          const body = inline ?? tokens[1] ?? '';
          tokens = [...tokenize(body), ...tokens.slice(inline === null ? 2 : 1)];
          break;
        }
        tokens = tokens.slice(takesValue ? 2 : 1);
      }
      // Then any plain positionals it takes before the command, such as a duration.
      tokens = tokens.slice(wrapper.positionals || 0);
      continue;
    }
    break;
  }
  if (!tokens.length) return null;

  // Lower-cased: on a case-insensitive file system `RM` runs /bin/rm.
  const program = path.basename(tokens[0].replace(/^\\/, '')).toLowerCase();
  let args = tokens.slice(1);
  let gitDir = null;

  if (program === 'git') {
    // git's global options sit BEFORE the subcommand; consume them so the
    // subcommand is found no matter how many are stacked in front of it.
    const VALUE_OPTS = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace',
                                '--exec-path', '--config-env', '--attr-source']);
    for (;;) {
      while (args.length > 1 && /^\$(\{[^}]*\}|\w+)$/.test(args[0]) &&
             args.slice(1).some((a) => !a.startsWith('$'))) args = args.slice(1);
      if (!args.length || !args[0].startsWith('-')) break;
      const [opt] = args[0].split('=');
      if (VALUE_OPTS.has(opt)) {
        const inline = args[0].includes('=');
        if (opt === '-C' && !inline && args[1] !== undefined) gitDir = args[1];
        args = args.slice(inline ? 1 : 2);
        continue;
      }
      args = args.slice(1); // valueless global: -p, --no-pager, --bare, --literal-pathspecs…
    }
    while (args.length > 1 && /^\$(\{[^}]*\}|\w+)$/.test(args[0])) args = args.slice(1);
  }
  return { program, args, gitDir };
}

/** Split args into short-letter flags, long flags, and operands. */
function parseArgs(args) {
  const flags = new Set();
  const operands = [];
  let literal = false;
  for (const a of args) {
    if (literal) { operands.push(a); continue; }
    if (a === '--') { literal = true; continue; }
    if (a.startsWith('--') && a.length > 2) { flags.add(a.split('=')[0]); continue; }
    if (a.startsWith('-') && a.length > 1) { for (const ch of a.slice(1)) flags.add(ch); continue; }
    operands.push(a);
  }
  return { flags, operands };
}

// ── rule helpers ─────────────────────────────────────────────────────────────

/** Paths where a delete is catastrophic: root, home, or a top-level system dir. */
function isRootish(p) {
  const brace = String(p).match(/^(.*?)\{([^{}]*,[^{}]*)\}(.*)$/);
  if (brace) return brace[2].split(',').some((alt) => isRootish(brace[1] + alt + brace[3]));
  // Normalise first, so `/tmp/..`, `/./` and `~/Documents/..` are seen for what they
  // name. Home is written as `~` so that `~/x/..` collapses to `~`.
  const home = String(p).replace(/^(\$HOME|\$\{HOME\})(?=\/|$)/, '~');
  const s = (/[*?]/.test(home) ? home : path.posix.normalize(home)).replace(/\/+$/, '') || '/';
  if (['/', '/*', '~', '~/*', '*', '.', '..', './*', '../*',
       '$HOME', '${HOME}', '$HOME/*', '${HOME}/*'].includes(s)) return true;
  if (/^\/[^/]+$/.test(s)) return true;                       // a top-level system directory
  if (/^\/(Users|home)\/[^/]+$/.test(s)) return true;         // an entire user's home
  if (/^(~|\$HOME|\$\{HOME\})\/[^/]*$/.test(s)) return true;  // a directory directly in home
  return false;
}

/**
 * True for a real dotenv file; the committed *.example / *.template ones are fine.
 * The argument is unwrapped first, because upload tools do not name a file plainly:
 * a curl body is written `@file`, a form field `name=@file`.
 */
function isEnvFile(p) {
  const b = path.basename(String(p).split(/[@=]/).pop() || '');
  if (!/^\.env(\..+)?$/.test(b)) return false;
  return !/\.(example|sample|template|dist|tpl)$/.test(b);
}

const PROTECTED_BRANCH = /^(main|master|develop|trunk|release(?:[/-].*)?|prod(?:uction)?)$/i;

/** Project rules live in configuration so this file never needs editing per project. */
function loadProjectRules(cwd) {
  const candidates = [
    process.env.GUARDRAILS_CONFIG,
    path.join(cwd || '.', 'guardrails.config.json'),
    path.join(path.dirname(fileURLToPath(import.meta.url)), 'guardrails.config.json'),
  ].filter(Boolean);
  for (const file of candidates) {
    try {
      if (!existsSync(file)) continue;
      const parsed = JSON.parse(readFileSync(file, 'utf8'));
      if (Array.isArray(parsed.projects)) return parsed.projects;
      process.stderr.write(`guardrails: ${file} has no "projects" array — ignored\n`);
    } catch (e) {
      process.stderr.write(`guardrails: could not read ${file}: ${e.message}\n`);
    }
  }
  return [];
}

// ── main ─────────────────────────────────────────────────────────────────────

try {
  const input = JSON.parse(readFileSync(0, 'utf8') || '{}');
  const cmd = String(input?.tool_input?.command || '').trim();
  const cwd = String(input?.cwd || process.env.CLAUDE_PROJECT_DIR || '');
  if (!cmd) passthrough();

  /** Every rule that fires is recorded; the maximum severity is emitted at the end. */
  const findings = [];
  const flag = (decision, why) => findings.push({ decision, why });

  // A work queue rather than a plain loop: a shell's `-c` string and `eval`'s
  // arguments are command lines in their own right, and are queued to be judged.
  const queue = splitSegments(stripHeredocs(cmd)).map((seg) => ({ seg, depth: 0 }));
  /** Set once any clause reads a dotenv file; a network tool later in the line then sends it. */
  let readsEnv = false;
  const NETWORK = new Set(['curl', 'wget', 'nc', 'ncat', 'netcat', 'ssh', 'scp', 'rsync', 'sftp', 'ftp']);
  while (queue.length) {
    const { seg, depth } = queue.shift();
    const c = canonicalize(seg);
    if (!c) continue;
    const { program, args, gitDir } = c;
    const { flags, operands } = parseArgs(args);
    const hasFlag = (...names) => names.some((n) => flags.has(n));

    // ── nested command lines ─────────────────────────────────────────────────
    const nest = (line) => {
      if (depth + 1 > MAX_NESTING) {
        flag('ask', 'Shell commands nested this deep cannot be judged. Confirm what actually runs.');
        return;
      }
      for (const inner of splitSegments(line)) queue.push({ seg: inner, depth: depth + 1 });
    };
    if (SHELLS.has(program)) {
      // `-c` may sit alone or inside a cluster: `bash -lc "..."`, `sh -ec "..."`.
      const at = args.findIndex((a) => /^-[a-zA-Z]*c[a-zA-Z]*$/.test(a));
      const script = args[at + 1] === '--' ? args[at + 2] : args[at + 1];
      if (at !== -1 && script !== undefined) nest(script);
    }
    if (program === 'eval' && args.length) nest(args.join(' '));

    // ── deletes ──────────────────────────────────────────────────────────────
    if (program === 'rm') {
      const recursive = hasFlag('r', 'R', '--recursive');
      const force = hasFlag('f', '--force');
      const dangerous = operands.filter(isRootish);
      if (hasFlag('--no-preserve-root'))
        flag('deny', 'That flag exists only to delete the filesystem root. Refusing.');
      if (recursive && dangerous.length)
        flag('deny', `Recursive delete aimed at a root, home, or system path (${dangerous.join(' ')}). Not recoverable — run it yourself if you truly mean it.`);
      else if (dangerous.length && force)
        flag('deny', `Forced delete aimed at a root, home, or system path (${dangerous.join(' ')}). Not recoverable.`);
      else if (recursive && force)
        flag('deny', 'Recursive plus force is irreversible: no prompt, no trash. Delete specific paths deliberately, or run it yourself.');
      else if (recursive)
        flag('ask', 'Recursive delete of a directory tree. Confirm the exact path — this does not go to the trash.');
      else if (dangerous.length)
        flag('ask', 'Delete aimed at a root, home, or system path. Confirm the exact path.');
    }

    // ── other irreversible writes ────────────────────────────────────────────
    if (program === 'shred')
      flag('ask', 'shred overwrites the file in place before unlinking it. There is nothing to recover afterwards — confirm the exact path.');

    if (program === 'truncate')  // it has no mode that leaves the file as it was
      flag('ask', 'Truncating resizes the file in place; whatever is cut off is gone, with no undo. Confirm.');

    if (program === 'dd') {
      const out = args.find((a) => a.startsWith('of='));
      if (out && /^of=\/dev\//.test(out))
        flag('deny', `dd writing to ${out.slice(3)} overwrites a device directly. That destroys whatever is on it, with no undo and no prompt.`);
      else if (out)
        flag('ask', `dd overwrites ${out.slice(3)} in place. Confirm the destination is what you mean.`);
    }

    if (program === 'find') {
      const EXEC = new Set(['-exec', '-execdir', '-ok', '-okdir']);
      const quote = (t) => `'${t.replace(/'/g, `'\\''`)}'`;
      for (let k = 0; k < args.length; k++) {
        if (!EXEC.has(args[k])) continue;
        let end = k + 1;
        while (end < args.length && args[end] !== ';' && args[end] !== '+') end++;
        if (end > k + 1) nest(args.slice(k + 1, end).map(quote).join(' '));
      }
      const deletes = args.includes('-delete') ||
                      (args.some((a) => EXEC.has(a)) &&
                       args.some((a) => /^(rm|unlink|shred)$/i.test(path.basename(a))));
      if (deletes) {
        const from = operands.filter(isRootish);
        if (from.length)
          flag('deny', `A find-and-delete rooted at ${from.join(' ')} walks the whole tree below it. Not recoverable.`);
        else
          flag('ask', 'find with -delete or -exec rm removes every match, with no prompt and no trash. Run it without the delete first to see the list.');
      }
    }

    // ── secrets ──────────────────────────────────────────────────────────────
    // Every arg, not only the operands: an upload names its file inside a flag value.
    const envOperands = args.filter(isEnvFile);
    if (envOperands.length) {
      const READERS = new Set(['cat', 'bat', 'less', 'more', 'head', 'tail', 'nl', 'strings',
                               'xxd', 'od', 'cp', 'tee', 'rsync', 'scp', 'open']);
      const EXFIL = new Set(['curl', 'wget', 'nc', 'ncat', 'netcat', 'ssh']);
      // A `host:path` operand means the copy leaves this machine.
      const remote = (program === 'scp' || program === 'rsync') &&
                     operands.some((o) => /^[^/]*[^/\\]:/.test(o) && !isEnvFile(o));
      readsEnv = true;
      if (EXFIL.has(program) || remote)
        flag('deny', `${program} with a dotenv argument sends secrets off this machine. Refusing.`);
      else if (READERS.has(program))
        flag('ask', `This reads or copies ${envOperands.join(' ')} (secrets) into context. Confirm you need it.`);
    }

    if (readsEnv && NETWORK.has(program) && !args.some(isEnvFile))
      flag('deny', `A dotenv file is read earlier in this command line and ${program} can send it off this machine. Refusing.`);

    // ── git ──────────────────────────────────────────────────────────────────
    if (program !== 'git') continue;
    const sub = operands[0] === 'stage' ? 'add' : operands[0];
    if (!sub) continue;
    const rest = operands.slice(1);

    if (sub === 'filter-branch' || sub === 'filter-repo')
      flag('deny', `git ${sub} rewrites EVERY commit. Irreversible for anyone who already pulled. Run it yourself, on a fresh clone, deliberately.`);

    if (sub === 'reflog' && (rest[0] === 'delete' || rest[0] === 'expire'))
      flag('deny', 'Expiring the reflog destroys your last safety net for recovering lost commits. Run it yourself if you mean it.');

    if (sub === 'gc' && args.some((a) => /^--prune=(now|all)$/.test(a)))
      flag('deny', 'Pruning now permanently drops unreachable objects, including anything the reflog was still holding for you.');

    if (sub === 'push') {
      if (hasFlag('--mirror'))
        flag('deny', 'A mirror push makes every remote ref match this repository, protected branches included — it force-updates and deletes without asking. Run it yourself.');
      if (hasFlag('--prune'))
        flag('ask', 'Pruning deletes every remote branch that has no local counterpart. Confirm the list first with --dry-run.');

      const forced = hasFlag('f', '--force', '--force-with-lease', '--force-if-includes') ||
                     rest.some((t) => t.startsWith('+'));
      const deleting = hasFlag('d', '--delete') || rest.some((t) => t.startsWith(':'));
      if (forced || deleting) {
        // git push [remote] [refspec...] — every refspec after the remote is a target.
        let targets = rest.slice(1).map((t) => {
          let target = t.replace(/^\+/, '');
          if (target.includes('$')) return null;  // expanded by the shell: unknown here
          if (target.includes(':')) target = target.split(':').pop();
          if (target === '@') target = 'HEAD';
          return target.replace(/^(refs\/)?heads\//, '');
        });
        if (!targets.length || targets.some((t) => t !== null && /^HEAD$/i.test(t))) {
          let current = null;
          try {
            current = execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], {
              cwd: gitDir || cwd || process.cwd(),
              encoding: 'utf8', timeout: 2000, stdio: ['ignore', 'pipe', 'ignore'],
            }).trim();
          } catch {
            current = null;
          }
          targets = targets.filter((t) => t === null || !/^HEAD$/i.test(t));
          targets.push(current);
        }
        const verb = deleting && !forced ? 'Deleting the remote branch' : 'Force-push';
        if (targets.some((t) => !t))
          flag('deny', `${verb} with no explicit branch, and the current branch could not be resolved. Refusing rather than guessing — name the branch, or run it yourself.`);
        const protectedTargets = targets.filter((t) => t && PROTECTED_BRANCH.test(t));
        if (protectedTargets.length)
          flag('deny', `${verb} on "${protectedTargets.join('", "')}" rewrites or destroys shared history. Blocked — do it manually if you truly must.`);
        else if (targets.every(Boolean))
          flag('ask', `${verb} on "${targets.join('", "')}" rewrites history others may have pulled. Confirm this is what you intend.`);
      }
    }

    if (sub === 'commit' && hasFlag('--amend'))
      flag('ask', 'Amend rewrites the last commit. If it was already pushed, this rewrites shared history. Confirm it is unpushed.');

    if (sub === 'rebase' && !hasFlag('--abort', '--continue', '--skip', '--quit', '--edit-todo', '--show-current-patch'))
      flag('ask', 'Rebase rewrites commits. If any of them are pushed, this rewrites shared history. Confirm.');

    if (sub === 'reset' && hasFlag('--hard'))
      flag('ask', 'A hard reset discards all uncommitted work in the tree, with no undo. Confirm you have nothing to keep.');

    if ((sub === 'checkout' || sub === 'restore' || sub === 'switch') &&
        hasFlag('f', '--force', '--discard-changes'))
      flag('ask', 'Forced checkout or restore overwrites local modifications with no undo. Confirm.');

    if ((sub === 'checkout' || sub === 'restore') &&
        rest.some((t) => ['.', './', '*', ':/', ':(top)', ':/*'].includes(t)))
      flag('ask', 'Restoring the whole tree throws away every uncommitted change, with no undo. Confirm.');

    if (sub === 'clean' && hasFlag('f', 'd', 'x', 'X', '--force'))
      flag('ask', 'Cleaning deletes untracked files permanently — with -x, your gitignored local files too. Dry-run first with -n.');

    if (sub === 'branch' && (hasFlag('D') || (hasFlag('d', '--delete') && hasFlag('f', '--force'))))
      flag('ask', 'Force-deleting a branch drops unmerged commits. Confirm they are merged or expendable.');

    if (sub === 'stash' && (rest[0] === 'drop' || rest[0] === 'clear'))
      flag('ask', 'Dropping or clearing stashes discards work that is in no commit. Confirm.');

    if (sub === 'update-ref' && hasFlag('d'))
      flag('ask', 'Deleting a ref by hand can orphan commits. Confirm.');

    if (sub === 'worktree' && rest[0] === 'remove' && hasFlag('f', '--force'))
      flag('ask', 'Force-removing a worktree discards uncommitted changes inside it. Confirm.');

    if ((sub === 'add' || sub === 'commit') && args.some(isEnvFile))
      flag('deny', 'That stages or commits a dotenv file. Keep secrets out of git.');

    if (sub === 'add' && (hasFlag('A', '--all') ||
                          rest.some((t) => ['.', './', '*', ':/', ':(top)', ':/*'].includes(t))))
      flag('deny', 'Staging the whole tree sweeps in files you did not mean to commit. Stage by path.');
  }

  // ── project rules, from configuration ─────────────────────────────────────
  // A built-in deny is final: emit it before evaluating repository-supplied
  // regexes, which could otherwise run past the hook timeout (fail open).
  if (findings.some((f) => f.decision === 'deny'))
    emit('deny', [...new Set(findings.filter((f) => f.decision === 'deny').map((f) => f.why))].join('\n'));
  // Every project regex runs under a per-rule budget and all of them share a
  // total one, well inside the hook's 10 s timeout, so the built-in findings
  // above are always emitted. Rules left unevaluated turn the verdict into "ask".
  const RULE_MS = 250, TOTAL_MS = 2000;
  const started = Date.now();
  let outOfTime = false;
  const sandbox = vm.createContext({ cmd });
  projects: for (const project of loadProjectRules(cwd)) {
    if (typeof project?.match !== 'string' || !project.match || !cwd.includes(project.match)) continue;
    for (const rule of Array.isArray(project.rules) ? project.rules : []) {
      // A rule needs a real pattern before it can be compiled: a RegExp built from
      // undefined is the EMPTY pattern, which matches every command — so one
      // mistyped key would turn this hook into a total block on all work.
      if (typeof rule?.when !== 'string' || !rule.when.trim()) {
        process.stderr.write('guardrails: rule with no "when" pattern — skipped\n');
        continue;
      }
      let re;
      try { re = new RegExp(rule.when, 'i'); }
      catch { process.stderr.write(`guardrails: bad regex in rule "${rule.when}" — skipped\n`); continue; }
      const left = TOTAL_MS - (Date.now() - started);
      if (left <= 0) { outOfTime = true; break projects; }
      let hit;
      try { hit = new vm.Script(`new RegExp(${JSON.stringify(rule.when)}, 'i').test(cmd)`).runInContext(sandbox, { timeout: Math.min(RULE_MS, left) }); }
      catch { flag('ask', `Guardrail rule "${rule.when}" took too long to evaluate. Confirm this command.`); continue; }
      if (!hit) continue;
      flag(rule.decision === 'deny' ? 'deny' : 'ask', rule.why || `Matched guardrail rule: ${rule.when}`);
    }
  }
  if (outOfTime)
    flag('ask', 'The project guardrail rules ran out of time before all were checked. Confirm this command.');

  // ── emit the maximum severity, never the first match ──────────────────────
  const reasons = (level) => [...new Set(findings.filter((f) => f.decision === level).map((f) => f.why))];
  const denies = reasons('deny');
  if (denies.length) emit('deny', denies.join('\n'));
  const asks = reasons('ask');
  if (asks.length) emit('ask', asks.join('\n'));

  passthrough();
} catch {
  passthrough(); // never block on a guardrail bug
}
