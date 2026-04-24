# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-04-23

### Added

- **Bootstrap Hook v4.1**: Core self-improvement mechanism
  - Auto-detect error signals at agent bootstrap
  - Scan recent memory files and streaming logs
  - Capture context window (±20 lines) around errors
  - Canonical dedup with 24h cooldown
  - Atomic write with lock file protection

- **Error Detection**: Pattern-based error recognition
  - User corrections ("不对", "其实", "错了", "并没有")
  - Exec errors (timeout, exit code, signals)
  - Import errors (ModuleNotFoundError, cannot import)
  - JSON errors (parse, encoding, truncation)
  - System errors (bootstrap warnings, critical failures)

- **Learning Promotion**: Priority-based knowledge extraction
  - Low: skip
  - Medium: write to LEARNINGS.md
  - High/Critical: write to LEARNINGS.md + MEMORY.md

- **Archive Policy**: Automatic knowledge base rotation
  - Archive when > 120 entries or 256KB
  - Keep latest 80 entries in LEARNINGS.md
  - Move old entries to archive/LEARNINGS-YYYYMM.md

- **Documentation**
  - README.md with architecture overview
  - QUICKSTART.md with setup and configuration
  - CONTRIBUTING.md with development guidelines
  - API documentation in code comments

- **Development Tools**
  - TypeScript configuration (strict mode)
  - ESLint configuration
  - Prettier configuration
  - GitHub Actions CI/CD workflow
  - Issue and PR templates

### Configuration

- `MAX_MEMORY_FILES = 3`
- `MAX_LOG_LINES = 200`
- `CONTEXT_RADIUS = 20`
- `MAX_NEW_ENTRIES_PER_RUN = 20`
- `DEDUP_COOLDOWN_MS = 24h`
- `LOCK_STALE_MS = 30s`
- `LOCK_WAIT_MS = 8s`

### Environment Variables

- `OPENCLAW_WORKSPACE`: Workspace root (auto-detected)
- `OPENCLAW_LOGS_DIR`: Logs directory (defaults to system temp)

---

## Roadmap

### v1.1.0 (Planned)

- [ ] Semantic dedup using embeddings
- [ ] Dashboard for error review/approval
- [ ] Slack/Feishu/Email notifications
- [ ] Multi-agent shared memory bus
- [ ] SQLite idempotency index

### v2.0.0 (Future)

- [ ] Web UI for learning management
- [ ] Advanced analytics and metrics
- [ ] Custom error pattern configuration
- [ ] Integration with external knowledge bases
- [ ] Machine learning-based priority scoring

---

## Migration Guide

### From v0.x to v1.0.0

1. Update hook location: `hooks/self-improvement.ts`
2. Update ERRORS.md header to `# ERRORS`
3. Verify environment variables are set
4. Re-enable hook: `openclaw hooks enable self-improvement`

---

## Known Issues

None at this time.

---

## Support

- **Documentation**: [README.md](./README.md) | [QUICKSTART.md](./QUICKSTART.md)
- **Issues**: https://github.com/yourusername/self-learning-genius-agent/issues
- **Discussions**: https://github.com/yourusername/self-learning-genius-agent/discussions
