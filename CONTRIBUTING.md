# Contributing to Self-Learning Genius Agent

Thank you for your interest in contributing! This document provides guidelines and instructions.

---

## Code of Conduct

- Be respectful and inclusive
- Focus on the code, not the person
- Help others learn and grow
- Report issues constructively

---

## Getting Started

### Prerequisites

- Node.js >= 18.0.0
- Git
- TypeScript knowledge (helpful but not required)

### Development Setup

```bash
# Clone your fork
git clone https://github.com/yourusername/self-learning-genius-agent.git
cd self-learning-genius-agent

# Install dependencies
npm install

# Build TypeScript
npm run build

# Run linter
npm run lint

# Format code
npm run format
```

---

## Development Workflow

### 1. Create a Feature Branch

```bash
git checkout -b feature/your-feature-name
```

Use descriptive names:
- `feature/semantic-dedup` — new feature
- `fix/lock-file-race-condition` — bug fix
- `docs/quickstart-guide` — documentation
- `perf/streaming-log-optimization` — performance

### 2. Make Changes

- Keep commits atomic and focused
- Write clear commit messages
- Update tests and documentation
- Follow TypeScript best practices

### 3. Test Your Changes

```bash
# Build
npm run build

# Lint
npm run lint

# Format
npm run format
```

### 4. Commit and Push

```bash
git add .
git commit -m "feat: add semantic dedup using embeddings"
git push origin feature/your-feature-name
```

### 5. Open a Pull Request

- Reference related issues: `Closes #123`
- Describe what changed and why
- Include before/after examples if applicable
- Request review from maintainers

---

## Coding Standards

### TypeScript

- Use strict mode (`strict: true` in tsconfig.json)
- Add type annotations for function parameters and returns
- Avoid `any` type; use generics or union types instead
- Use `const` by default, `let` when needed, avoid `var`

### Naming Conventions

- Functions: `camelCase` (e.g., `scanMemoryFile`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `MAX_LOG_LINES`)
- Types: `PascalCase` (e.g., `DetectedError`)
- Private functions: prefix with `_` (e.g., `_internalHelper`)

### Comments

- Use `//` for single-line comments
- Use `/** */` for JSDoc comments on public functions
- Explain *why*, not *what* (code shows what)

Example:
```typescript
/**
 * Canonicalize summary for robust dedup.
 * Normalizes timestamps, paths, and numbers to improve matching.
 */
function canonicalizeSummary(s: string): string {
  // ... implementation
}
```

### Error Handling

- Use try-catch for I/O operations
- Log errors with context
- Return null or empty array on failure (not throw)
- Include error type in logs: `[self-improvement][ERROR]`

---

## Documentation

### README Updates

- Keep README.md concise and focused
- Link to QUICKSTART.md for detailed setup
- Update architecture diagrams if changing core logic
- Include examples for new features

### Code Comments

- Document complex algorithms
- Explain non-obvious design decisions
- Keep comments up-to-date with code changes

### Commit Messages

Follow conventional commits:

```
feat: add semantic dedup using embeddings
fix: prevent race condition in lock file
docs: update QUICKSTART.md with env vars
perf: optimize streaming log read
refactor: extract error pattern matching
test: add unit tests for canonicalizeSummary
```

---

## Testing

### Manual Testing

1. Build the project
2. Enable the hook in a test OpenClaw workspace
3. Trigger bootstrap and verify ERRORS.md is updated
4. Check that learnings are promoted correctly

### Test Scenarios

- [ ] Hook fires on agent bootstrap
- [ ] Error patterns are detected correctly
- [ ] Dedup prevents duplicate entries
- [ ] Lock file prevents concurrent corruption
- [ ] Context window captures surrounding lines
- [ ] Archive rotates LEARNINGS.md when oversized

---

## Performance Considerations

- **Streaming log read**: Only read last 200 lines (not entire file)
- **Memory files**: Scan only 3 most recent files
- **Dedup**: Use canonical keys for fast lookup
- **Lock file**: Timeout after 30s to prevent deadlock
- **Archive**: Rotate when > 120 entries or 256KB

---

## Suggested Contributions

### High Priority

- [ ] **Semantic dedup**: Use embeddings to detect similar errors
- [ ] **Dashboard**: Web UI for error review and approval
- [ ] **Notifications**: Slack/Feishu/Email integration

### Medium Priority

- [ ] **Quality scoring**: Rank learnings by usefulness
- [ ] **Multi-agent memory**: Shared learning across agents
- [ ] **SQLite index**: Faster idempotency checks

### Low Priority

- [ ] **Parser robustness**: Handle more error formats
- [ ] **Archive strategy**: Configurable rotation policy
- [ ] **Metrics**: Track learning velocity and quality

---

## Reporting Issues

### Bug Reports

Include:
- OpenClaw version
- Node.js version
- Steps to reproduce
- Expected vs actual behavior
- Error logs (if applicable)

### Feature Requests

Include:
- Use case and motivation
- Proposed solution (if any)
- Alternative approaches considered
- Examples or mockups

---

## Review Process

1. **Automated checks**: Linter, TypeScript compiler, tests
2. **Code review**: Maintainers review for quality and fit
3. **Feedback**: Address comments and push updates
4. **Approval**: Maintainer approves and merges

---

## Release Process

Releases follow semantic versioning (MAJOR.MINOR.PATCH):

- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes

Maintainers handle releases and version bumps.

---

## Questions?

- **Documentation**: See [README.md](./README.md) and [QUICKSTART.md](./QUICKSTART.md)
- **Issues**: Open a GitHub issue with `[question]` tag
- **Discussions**: Use GitHub Discussions for broader topics

---

## License

By contributing, you agree that your contributions will be licensed under GPL-3.0.

---

Thank you for contributing! 🎉
