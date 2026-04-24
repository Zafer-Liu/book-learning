/**
 * Self-Improvement Hook v1.0 (context-window + streaming-log optimized)
 *
 */

import type { HookHandler } from 'openclaw/hooks';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

// Resolve paths relative to workspace root or use environment variables
const WORKSPACE_ROOT = process.env.OPENCLAW_WORKSPACE || path.resolve(__dirname, '..');
const LEARNINGS_DIR = path.join(WORKSPACE_ROOT, '.learnings');
const MEMORY_DIR = path.join(WORKSPACE_ROOT, 'memory');
const LOGS_DIR = process.env.OPENCLAW_LOGS_DIR || path.join(os.tmpdir(), 'openclaw');
const ERRORS_FILE = path.join(LEARNINGS_DIR, 'ERRORS.md');
const LOCK_FILE = path.join(LEARNINGS_DIR, 'ERRORS.md.lock');

const MAX_MEMORY_FILES = 3;
const MAX_LOG_LINES = 200; // v4.1: only read latest 200 log lines
const MAX_NEW_ENTRIES_PER_RUN = 20;
const LOCK_STALE_MS = 30_000;
const LOCK_WAIT_MS = 8_000;
const LOCK_RETRY_INTERVAL_MS = 120;

// Capture context around hit line
const CONTEXT_RADIUS = 20; // 前后各20行

// Cooldown: same canonical error key within this time won't be logged again
const DEDUP_COOLDOWN_MS = 24 * 60 * 60 * 1000; // 24h

type Priority = 'low' | 'medium' | 'high' | 'critical';
type ErrorType = 'correction' | 'error' | 'system' | 'knowledge_gap';

type DetectedError = {
  type: ErrorType;
  priority: Priority;
  summary: string;
  details: string;
  key?: string; // canonical dedup key
};

const ERROR_PATTERNS: Array<{ pattern: RegExp; type: ErrorType }> = [
  { pattern: /不对|其实|错了|并没有|不是这样的|有问题|错误|报错|no,?\s*that's|actually|not right/i, type: 'correction' },
  { pattern: /exec.*error|error.*exec|keyerror|typerror|referenceerror|syntaxerror|timeout|connection.*fail|und_err_connect_timeout/i, type: 'error' },
  { pattern: /command failed|exit code|signal sigkill|sigkill|non-zero|errno|eaddrinuse|econnrefused|enotfound|eacces/i, type: 'error' },
  { pattern: /truncat|bootstrap.*warning|bootstrap.*removed/i, type: 'system' },
  { pattern: /cannot import|modulenotfound|no module named|import failed|import.*error/i, type: 'error' },
  { pattern: /json.*error|parse.*error|format.*error|unexpected.*token|encoding.*error/i, type: 'error' },
  { pattern: /ERROR|FAIL|Exception|CRITICAL/i, type: 'error' },
];

const REMINDER_CONTENT = `## Self-Improvement Reminder (Active)

Errors from recent memory queued in \`.learnings/ERRORS.md\`.
Continue to actively log new errors during this session:

**Log to \`.learnings/ERRORS.md\` when:**
- Tool call fails (exec error, import error, timeout, non-zero exit)
- Command returns unexpected output
- JSON parse fails, encoding error, truncation
- User corrects you ("不对", "其实", "错了", "并没有")

**Format:**
\`\`\`markdown
## [ERR-YYYYMMDD-HHMMSS-XXX] category
**Logged**: YYYY-MM-DDTHH:MM:SS.sssZ
**Priority**: low|medium|high|critical
**Status**: pending
**Area**: config|exec|system|chart-generate|github|llm|backtest

### Summary
One-line description

### Details
Error message, context, what failed

### Metadata
- Source: correction|error|knowledge_gap
- Tags: [relevant-tags]
---
\`\`\`
`.trim();

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function ensureDir(dir: string): void {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function normalizeSummary(s: string): string {
  return s.replace(/\s+/g, ' ').trim().toLowerCase().slice(0, 180);
}

/**
 * Canonicalize summary for robust dedup:
 * - lowercase
 * - collapse spaces
 * - normalize numbers in " +15 more truncated file(s)" -> "+N more truncated file(s)"
 * - normalize timestamps
 */
function canonicalizeSummary(s: string): string {
  let x = s.toLowerCase().replace(/\s+/g, ' ').trim();

  // normalize date/time patterns
  x = x.replace(/\b\d{4}-\d{2}-\d{2}\b/g, 'YYYY-MM-DD');
  x = x.replace(/\b\d{2}:\d{2}:\d{2}\b/g, 'HH:MM:SS');

  // normalize "+15 more truncated file(s)." variants
  x = x.replace(/\+\d+\s+more\s+truncated\s+file\(s\)\.?/g, '+N more truncated file(s).');

  // normalize windows paths in summary (optional coarse)
  x = x.replace(/[a-z]:\\[^ ]+/gi, '<path>');

  return x.slice(0, 220);
}

function inferPriority(type: ErrorType, line: string): Priority {
  const l = line.toLowerCase();
  if (type === 'system') return 'high'; // from critical -> high to reduce noise severity inflation
  if (/critical|fatal|panic|block|blocked|阻塞|严重/.test(l)) return 'critical';
  if (/sigkill|timeout|non-zero|exit code|cannot import|modulenotfound|und_err_connect_timeout/.test(l)) return 'high';
  if (/warn|warning|轻微/.test(l)) return 'low';
  return 'medium';
}

function isNoisySystemLine(line: string): boolean {
  const l = line.toLowerCase();
  return (
    /\[bootstrap truncation warning\]/.test(l) ||
    /some workspace bootstrap files were truncated before injection/.test(l) ||
    /\+\d+\s+more\s+truncated\s+file\(s\)/.test(l)
  );
}

function getContextWindow(lines: string[], hitIndex: number, radius = CONTEXT_RADIUS): string {
  const start = Math.max(0, hitIndex - radius);
  const end = Math.min(lines.length - 1, hitIndex + radius);

  const chunk: string[] = [];
  for (let i = start; i <= end; i++) {
    const prefix = i === hitIndex ? '>>' : '  ';
    chunk.push(`${prefix} [${i + 1}] ${lines[i] ?? ''}`);
  }
  return chunk.join('\n');
}

/**
 * Read last N lines efficiently from a potentially large streaming log file.
 * Binary reverse scan to avoid loading whole file into memory.
 */
function readLastLines(filePath: string, maxLines: number): string[] {
  if (maxLines <= 0) return [];
  let fd: number | null = null;

  try {
    const stat = fs.statSync(filePath);
    if (stat.size <= 0) return [];

    fd = fs.openSync(filePath, 'r');

    const chunkSize = 64 * 1024; // 64KB
    let position = stat.size;
    let buffer = '';
    let newlineCount = 0;

    while (position > 0 && newlineCount <= maxLines + 1) {
      const readSize = Math.min(chunkSize, position);
      position -= readSize;

      const buf = Buffer.allocUnsafe(readSize);
      fs.readSync(fd, buf, 0, readSize, position);

      const text = buf.toString('utf8');
      buffer = text + buffer;

      // count '\n'
      for (let i = 0; i < text.length; i++) {
        if (text.charCodeAt(i) === 10) newlineCount++;
      }
    }

    const lines = buffer.split(/\r?\n/);
    if (lines.length > maxLines) return lines.slice(lines.length - maxLines);
    return lines;
  } catch {
    return [];
  } finally {
    if (fd !== null) {
      try {
        fs.closeSync(fd);
      } catch {}
    }
  }
}

function formatErrorEntry(timestamp: string, idx: number, entry: DetectedError): string {
  const d = new Date(timestamp);
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mi = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  const id = `ERR-${yyyy}${mm}${dd}-${hh}${mi}${ss}-${String(idx).padStart(3, '0')}`;

  return `## [${id}] ${entry.type}

**Logged**: ${timestamp}
**Priority**: ${entry.priority}
**Status**: pending
**Area**: config | exec | system | chart-generate | github | llm | backtest

### Summary
${entry.summary}

### Details
${entry.details}

### Metadata
- Source: detected_at_bootstrap
- Tags: [auto-detected]
---
`;
}

function getLatestLogFile(): string | null {
  if (!fs.existsSync(LOGS_DIR)) return null;
  try {
    const files = fs.readdirSync(LOGS_DIR).filter((f) => f.endsWith('.log'));
    if (files.length === 0) return null;
    files.sort();
    return path.join(LOGS_DIR, files[files.length - 1]);
  } catch {
    return null;
  }
}

function scanLogFile(filepath: string): DetectedError[] {
  const recentLines = readLastLines(filepath, MAX_LOG_LINES);
  if (!recentLines.length) return [];

  const out: DetectedError[] = [];

  for (let i = 0; i < recentLines.length; i++) {
    const raw = recentLines[i];
    const line = raw.trim();
    if (!line) continue;

    for (const { pattern, type } of ERROR_PATTERNS) {
      if (!pattern.test(line)) continue;

      const summaryCore = line.slice(0, 180);
      const summary = `[${type.toUpperCase()}] ${summaryCore}`;
      const contextBlock = getContextWindow(recentLines, i, CONTEXT_RADIUS);

      out.push({
        type,
        priority: inferPriority(type, line),
        summary,
        details:
          `Detected in: ${path.basename(filepath)}\n` +
          `Hit line (in last ${MAX_LOG_LINES} lines view): ${i + 1}\n` +
          `Context window (±${CONTEXT_RADIUS} lines):\n` +
          '```text\n' + contextBlock + '\n```',
        key: canonicalizeSummary(summary),
      });
      break;
    }
  }

  return out;
}

function getRecentMemoryFiles(): string[] {
  if (!fs.existsSync(MEMORY_DIR)) return [];
  let files: string[] = [];
  try {
    files = fs.readdirSync(MEMORY_DIR).filter((f) => f.endsWith('.md') && !f.includes('instreet'));
  } catch {
    return [];
  }

  const withMtime = files
    .map((f) => path.join(MEMORY_DIR, f))
    .map((fp) => {
      try {
        return { fp, mtime: fs.statSync(fp).mtimeMs };
      } catch {
        return null;
      }
    })
    .filter((x): x is { fp: string; mtime: number } => x !== null)
    .sort((a, b) => b.mtime - a.mtime);

  return withMtime.slice(0, MAX_MEMORY_FILES).map((x) => x.fp);
}

function scanMemoryFile(filepath: string): DetectedError[] {
  let content = '';
  try {
    content = fs.readFileSync(filepath, 'utf-8');
  } catch {
    return [];
  }

  const out: DetectedError[] = [];
  const lines = content.split('\n');

  // keep only one noisy bootstrap system line per file/run
  let noisySystemCaptured = false;

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) continue;

    for (const { pattern, type } of ERROR_PATTERNS) {
      if (!pattern.test(line)) continue;

      if (type === 'system' && isNoisySystemLine(line)) {
        if (noisySystemCaptured) break;
        noisySystemCaptured = true;
      }

      const summaryCore = line.slice(0, 180);
      const summary = `[${type.toUpperCase()}] ${summaryCore}`;
      const contextBlock = getContextWindow(lines, i, CONTEXT_RADIUS);

      out.push({
        type,
        priority: inferPriority(type, line),
        summary,
        details:
          `Detected in: ${path.basename(filepath)}\n` +
          `Hit line: ${i + 1}\n` +
          `Context window (±${CONTEXT_RADIUS} lines):\n` +
          '```text\n' + contextBlock + '\n```',
        key: canonicalizeSummary(summary),
      });
      break;
    }
  }

  return out;
}

type ExistingRecord = { key: string; loggedAt?: number };

function loadExistingRecords(content: string): ExistingRecord[] {
  const records: ExistingRecord[] = [];

  // block-level parse: Logged + Summary
  const blockRegex =
    /## \[ERR-[^\]]+\][^\n]*\n(?:.|\n)*?\*\*Logged\*\*:\s*([^\n]+)\n(?:.|\n)*?### Summary\s*\n([^\n]+)\n/g;

  let m: RegExpExecArray | null;
  while ((m = blockRegex.exec(content)) !== null) {
    const loggedRaw = m[1]?.trim() || '';
    const summary = m[2]?.trim() || '';
    if (!summary) continue;

    const t = Date.parse(loggedRaw);
    records.push({
      key: canonicalizeSummary(summary),
      loggedAt: Number.isFinite(t) ? t : undefined,
    });
  }

  return records;
}

function withinCooldown(existing: ExistingRecord[], key: string, nowMs: number): boolean {
  for (const r of existing) {
    if (r.key !== key) continue;
    if (!r.loggedAt) return true; // conservative dedup for malformed old records
    if (nowMs - r.loggedAt < DEDUP_COOLDOWN_MS) return true;
  }
  return false;
}

async function acquireLock(lockPath: string): Promise<() => void> {
  const start = Date.now();
  while (true) {
    try {
      const fd = fs.openSync(lockPath, 'wx');
      fs.writeFileSync(fd, `${process.pid}@${os.hostname()} ${new Date().toISOString()}\n`, 'utf-8');
      fs.closeSync(fd);
      return () => {
        try {
          if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
        } catch {}
      };
    } catch (err: any) {
      if (err?.code !== 'EEXIST') throw err;
      try {
        const st = fs.statSync(lockPath);
        if (Date.now() - st.mtimeMs > LOCK_STALE_MS) {
          fs.unlinkSync(lockPath);
          continue;
        }
      } catch {}
      if (Date.now() - start > LOCK_WAIT_MS) throw new Error(`Lock timeout for ${lockPath}`);
      await sleep(LOCK_RETRY_INTERVAL_MS);
    }
  }
}

function atomicWriteFile(targetPath: string, content: string): void {
  const dir = path.dirname(targetPath);
  const tmp = path.join(dir, `.${path.basename(targetPath)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(tmp, content, 'utf-8');
  fs.renameSync(tmp, targetPath);
}

async function appendToErrorsAtomic(entries: DetectedError[]): Promise<number> {
  if (!entries.length) return 0;
  ensureDir(LEARNINGS_DIR);

  const release = await acquireLock(LOCK_FILE);
  try {
    const existingContent = fs.existsSync(ERRORS_FILE) ? fs.readFileSync(ERRORS_FILE, 'utf-8') : '';
    const existingRecords = loadExistingRecords(existingContent);

    const nowMs = Date.now();
    const uniqueNew: DetectedError[] = [];
    const seenThisBatch = new Set<string>();

    for (const e of entries) {
      const key = e.key || canonicalizeSummary(e.summary);
      if (seenThisBatch.has(key)) continue;
      if (withinCooldown(existingRecords, key, nowMs)) continue;

      seenThisBatch.add(key);
      uniqueNew.push(e);

      if (uniqueNew.length >= MAX_NEW_ENTRIES_PER_RUN) break;
    }

    if (!uniqueNew.length) return 0;

    const hasHeader = existingContent.startsWith('# ERRORS.md');
    const header = hasHeader ? '' : '# ERRORS.md - Tool & Command Errors\n\n';
    const nowIso = new Date().toISOString();

    const newBlocks = uniqueNew.map((e, i) => formatErrorEntry(nowIso, i + 1, e)).join('\n');
    const merged = header + newBlocks + (existingContent ? '\n' + existingContent : '');
    atomicWriteFile(ERRORS_FILE, merged);
    return uniqueNew.length;
  } finally {
    release();
  }
}

const handler: HookHandler = async (event) => {
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  console.log('[self-improvement][INFO] ▶ HOOK 执行 - 事件:', event.type, '/', event.action);

  if (event.type !== 'agent' || event.action !== 'bootstrap') {
    console.log('[self-improvement][INFO] ⏭ 非 bootstrap 事件，跳过');
    console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
    return;
  }

  const sessionKey = event.sessionKey || '';
  if (sessionKey.includes(':subagent:')) {
    console.log('[self-improvement][INFO] ⏭ 子 agent 会话，跳过');
    console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
    return;
  }

  if (!event.context || typeof event.context !== 'object') {
    console.log('[self-improvement][WARN] ⚠ 缺少 context，跳过');
    console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
    return;
  }

  console.log('[self-improvement][INFO] 📁 sessionKey:', sessionKey);
  console.log('[self-improvement][INFO] 📋 bootstrapFiles:', event.context.bootstrapFiles?.length ?? 0);

  const recentFiles = getRecentMemoryFiles();
  const scanned: DetectedError[] = [];
  for (const file of recentFiles) {
    console.log('[self-improvement][INFO] 🔍 扫描 memory:', file);
    scanned.push(...scanMemoryFile(file));
  }

  // 扫描最新日志文件（仅最后200行）
  const latestLog = getLatestLogFile();
  if (latestLog) {
    console.log('[self-improvement][INFO] 🔍 扫描日志(最后', MAX_LOG_LINES, '行):', latestLog);
    const logErrors = scanLogFile(latestLog);
    console.log('[self-improvement][INFO] 📋 日志扫描命中:', logErrors.length);
    scanned.push(...logErrors);
  } else {
    console.log('[self-improvement][INFO] ⚠️ 未找到日志文件');
  }

  console.log('[self-improvement][INFO] 🔎 扫描命中:', scanned.length);

  // same-run dedup by canonical key
  const inRun = new Map<string, DetectedError>();
  for (const e of scanned) {
    const key = e.key || canonicalizeSummary(e.summary);
    if (!inRun.has(key)) inRun.set(key, e);
  }

  let candidates = Array.from(inRun.values());
  if (candidates.length > MAX_NEW_ENTRIES_PER_RUN) {
    candidates = candidates.slice(0, MAX_NEW_ENTRIES_PER_RUN);
  }
  console.log('[self-improvement][INFO] 🔎 同次去重后候选:', candidates.length);

  if (candidates.length > 0) {
    try {
      const written = await appendToErrorsAtomic(candidates);
      console.log('[self-improvement][INFO] ✅ 已写入 ERRORS.md:', written, '条');
    } catch (e) {
      console.error('[self-improvement][ERROR] ❌ 写入 ERRORS.md 失败:', (e as Error).message);
    }
  }

  if (Array.isArray(event.context.bootstrapFiles)) {
    event.context.bootstrapFiles.push({
      path: 'SELF_IMPROVEMENT_REMINDER.md',
      content: REMINDER_CONTENT,
      virtual: true,
    });
    console.log('[self-improvement][INFO] ✅ 已注入 Self-Improvement Reminder');
  }

  console.log('[self-improvement][INFO] 📊 最终 bootstrapFiles 数量:', event.context.bootstrapFiles?.length ?? 0);
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
};

export default handler;