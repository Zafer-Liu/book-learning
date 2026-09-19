# Quick Start Guide

## Course tutor / 课程学习助手

The new application uses Python 3.11+ and a build-free web UI. Install `requirements.txt` in your chosen virtual environment, copy `.env.example` to `.env`, configure the model, and run `python -m study`. Open `http://127.0.0.1:8080`, register an account, then upload UTF-8 Markdown or TXT. `npm start` is an alias for this Python entry point, not an installer.

For Railway, connect this repository to a new service, use the included Dockerfile, attach a Volume at `/data`, and configure `STUDY_SECRET_KEY`, `STUDY_DATA_DIR=/data`, `STUDY_COOKIE_SECURE=1`, and `STUDY_LLM_BASE_URL/API_KEY/MODEL`. Keep one replica/worker. Optional embedding settings enable semantic retrieval; otherwise retrieval is explicitly keyword-only. Set an invitation code before sharing a classroom deployment.

[完整中文部署说明、隐私边界与人工验收](README.zh-CN.md) · [Environment template](.env.example)

The following instructions apply only to the preserved OpenClaw hook. They are **not required** to start the course tutor.

## Legacy hook installation

### Prerequisites
- Node.js >= 18.0.0
- OpenClaw >= 1.0.0
- TypeScript 5.0+

### Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/self-learning-genius-agent.git
   cd self-learning-genius-agent
   ```

2. **Install dependencies**
   ```bash
   npm install
   ```

3. **Build TypeScript**
   ```bash
   npm run build
   ```

4. **Enable the hook in OpenClaw**
   ```bash
   openclaw hooks enable self-improvement
   ```

---

## Configuration

### Environment Variables

Set these in your OpenClaw environment or `.env` file:

```bash
# Workspace root (defaults to OPENCLAW_WORKSPACE)
OPENCLAW_WORKSPACE=/path/to/workspace

# Logs directory (defaults to system temp)
OPENCLAW_LOGS_DIR=/path/to/logs
```

### Hook Configuration

The hook is configured in `self-improvement/handler.ts`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `MAX_MEMORY_FILES` | 3 | Number of recent memory files to scan |
| `MAX_LOG_LINES` | 200 | Lines to read from latest log file |
| `MAX_NEW_ENTRIES_PER_RUN` | 20 | Max errors to log per bootstrap |
| `CONTEXT_RADIUS` | 20 | Lines of context around error (±N) |
| `DEDUP_COOLDOWN_MS` | 24h | Cooldown for duplicate errors |
| `LOCK_STALE_MS` | 30s | Lock file timeout |

---

## How It Works

### Bootstrap Flow

1. **Agent starts** → Hook fires on `agent:bootstrap`
2. **Scan phase**
   - Read 3 most recent memory files
   - Read last 200 lines of latest log file
   - Match against error patterns
3. **Dedup phase**
   - Same-run dedup by canonical key
   - 24h cooldown for repeated errors
4. **Write phase**
   - Atomic write to `.learnings/ERRORS.md`
   - Lock file prevents concurrent corruption
5. **Inject phase**
   - Add `SELF_IMPROVEMENT_REMINDER.md` to bootstrap context

### Error Detection Patterns

The hook detects:
- **User corrections**: "不对", "其实", "错了", "并没有"
- **Exec errors**: timeout, exit code, signal, connection failures
- **Import errors**: ModuleNotFoundError, cannot import
- **JSON errors**: parse error, encoding error, truncation
- **System errors**: bootstrap warnings, critical failures

### Learning Promotion

Errors in `ERRORS.md` are promoted based on priority:

| Priority | Action | Destination |
|----------|--------|-------------|
| `low` | Skip | — |
| `medium` | Write | `LEARNINGS.md` |
| `high` | Write | `LEARNINGS.md` + `MEMORY.md` |
| `critical` | Write | `LEARNINGS.md` + `MEMORY.md` |

---

## File Structure

```
.
├── README.md                          # Main documentation
├── README.md                    # Chinese documentation
├── QUICKSTART.md                      # This file
├── package.json                       # NPM configuration
├── tsconfig.json                      # TypeScript configuration
├── .gitignore                         # Git ignore rules
├── LICENSE                            # GPL-3.0 license
├── self-improvement/
│   ├── handler.ts                     # Main hook implementation
│   └── HOOK.md                        # Hook metadata
└── .learnings/
    ├── ERRORS.md                      # Pending error inbox
    ├── LEARNINGS.md                   # Promoted learnings
    └── archive/
        └── LEARNINGS-YYYYMM.md        # Archived learnings
```

---

## Usage Examples

### Manual Error Logging

Add to `.learnings/ERRORS.md`:

```markdown
## [ERR-20260423-220300-001] exec/timeout

**Logged**: 2026-04-23T22:03:00.000Z
**Priority**: high
**Status**: pending
**Area**: exec

### Summary
Command execution timeout after 30 seconds

### Details
Running `npm run build` exceeded timeout. Recommend increasing timeout or optimizing build.

### Metadata
- Source: correction
- Tags: [exec, timeout, performance]

---
```

### Viewing Learnings

```bash
# View pending errors
cat .learnings/ERRORS.md

# View promoted learnings
cat .learnings/LEARNINGS.md

# View archived learnings
ls .learnings/archive/
```

---

## Troubleshooting

### Hook not firing

1. Verify hook is enabled:
   ```bash
   openclaw hooks list
   ```

2. Check hook configuration in `openclaw.json`:
   ```bash
   cat ~/.openclaw/openclaw.json | grep -A 5 self-improvement
   ```

3. Check logs:
   ```bash
   tail -f ~/.openclaw/logs/openclaw.log
   ```

### ERRORS.md not updating

1. Verify `.learnings/` directory exists and is writable
2. Check for stale lock file:
   ```bash
   rm .learnings/ERRORS.md.lock
   ```

3. Verify memory files exist:
   ```bash
   ls -la memory/
   ```

### High false positives

Adjust error patterns in `handler.ts`:
- Reduce `MAX_LOG_LINES` to focus on recent errors
- Add more specific patterns to `ERROR_PATTERNS`
- Increase `DEDUP_COOLDOWN_MS` to reduce noise

---

## Performance Tips

- **Large log files**: Hook reads only last 200 lines (streaming optimization)
- **Memory files**: Scans only 3 most recent files
- **Concurrency**: Lock file prevents corruption under concurrent access
- **Archive**: Automatically archives `LEARNINGS.md` when > 120 entries or 256KB

---

## Contributing

Contributions welcome! Areas for improvement:

- [ ] Semantic dedup using embeddings
- [ ] Dashboard for error review/approval
- [ ] Slack/Feishu/Email notifications
- [ ] Multi-agent shared memory bus
- [ ] SQLite idempotency index

See [README.md](./README.md) for full contribution guidelines.

---

## License

GPL-3.0 — See [LICENSE](./LICENSE) for details.

---

## Support

- **Issues**: https://github.com/yourusername/self-learning-genius-agent/issues
- **Discussions**: https://github.com/yourusername/self-learning-genius-agent/discussions
- **OpenClaw Docs**: https://docs.openclaw.ai
