const $ = (selector) => document.querySelector(selector);
const state = {
  user: null, csrf: '', config: null, books: [], book: null, sections: [],
  conversations: [], conversation: null, messages: [], mode: 'qa', epoch: 0,
  pending: false, loading: false, registering: false, registrationOpen: false,
  testCodeRegistration: false, inviteRequired: false, controllers: new Set(), poll: null,
  statusText: '', typing: null, streamText: '',
};
const modeNames = { qa: '教材问答', explain: '章节讲解', outline: '要点梳理', quiz: '自测练习' };
const statusNames = { queued: '等待索引', indexing: '正在建立索引', ready: '可以学习', error: '索引失败' };
let toastTimer;
let evidenceSequence = 0;
let streamRenderTimer = null;

class StaleRequest extends Error {}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function devMode() {
  return localStorage.getItem('study.devMode') === '1';
}

function invalidate() {
  state.epoch += 1;
  state.controllers.forEach((controller) => controller.abort());
  state.controllers.clear();
  state.pending = false;
  state.loading = false;
  state.statusText = '';
  state.typing = null;
  state.streamText = '';
  clearTimeout(streamRenderTimer);
  streamRenderTimer = null;
  clearTimeout(state.poll);
  state.poll = null;
  return state.epoch;
}

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6000);
}

function showError(error) {
  if (error instanceof StaleRequest || error.name === 'AbortError') return;
  toast(error.message || '请求失败，请检查网络后重试。');
}

function action(node, event, callback) {
  node.addEventListener(event, (e) => {
    Promise.resolve().then(() => callback(e)).catch(showError);
  });
}

async function api(path, options = {}) {
  const epoch = state.epoch;
  const controller = new AbortController();
  state.controllers.add(controller);
  const headers = { ...options.headers };
  const method = options.method || 'GET';
  if (!['GET', 'HEAD'].includes(method)) headers['X-CSRF-Token'] = state.csrf;
  let body = options.body;
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  try {
    const response = await fetch(path, { ...options, method, body, headers, credentials: 'same-origin', signal: controller.signal });
    if (epoch !== state.epoch) throw new StaleRequest();
    let data;
    try { data = await response.json(); } catch { throw new Error('服务返回异常，请稍后重试。'); }
    if (epoch !== state.epoch) throw new StaleRequest();
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/api/auth/')) {
        showAuth();
        loadIdentity().catch(showError);
      }
      throw new Error(data.error || `请求失败（${response.status}）`);
    }
    return data;
  } catch (error) {
    if (epoch !== state.epoch) throw new StaleRequest();
    if (error instanceof TypeError) throw new Error('无法连接服务，请检查网络后重试。');
    throw error;
  } finally {
    state.controllers.delete(controller);
  }
}

// POST JSON and consume a Server-Sent-Events response frame by frame.
async function streamChat(path, payload, onEvent) {
  const epoch = state.epoch;
  const controller = new AbortController();
  state.controllers.add(controller);
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf },
      credentials: 'same-origin',
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (epoch !== state.epoch) throw new StaleRequest();
    if (!response.ok || !response.body) {
      let data = null;
      try { data = await response.json(); } catch { /* not JSON */ }
      if (response.status === 401) {
        showAuth();
        loadIdentity().catch(showError);
      }
      throw new Error((data && data.error) || `请求失败（${response.status}）`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const line = frame.split('\n').find((part) => part.startsWith('data: '));
        if (line) onEvent(JSON.parse(line.slice(6)));
      }
    }
  } catch (error) {
    if (epoch !== state.epoch) throw new StaleRequest();
    if (error instanceof TypeError) throw new Error('无法连接服务，请检查网络后重试。');
    throw error;
  } finally {
    state.controllers.delete(controller);
  }
}

function panel(name) {
  $('.desk').dataset.panel = name;
  document.querySelectorAll('[data-panel]').forEach((node) => {
    if (node.tagName === 'BUTTON') {
      if (node.dataset.panel === name) node.setAttribute('aria-current', 'page');
      else node.removeAttribute('aria-current');
    }
  });
}

function clearEvidence() {
  evidenceSequence += 1;
  const container = $('#evidence-content');
  container.replaceChildren();
  const empty = element('div', 'evidence-empty');
  empty.append(element('span', 'margin-line'), element('h3', '', '答案不是终点，\n原文才是起点。'),
    element('p', '', '点击回答中的引用标记，在这里核对章节与原文段落。'),
    element('small', '', 'Markdown 没有可靠页码，我们只标注真实的章节和分块位置。'));
  container.append(empty);
}

function setAuthMode() {
  const canRegister = state.registrationOpen || state.testCodeRegistration;
  if (!canRegister) state.registering = false;
  $('#auth-title').textContent = state.registering ? '创建你的书架' : '登录书架';
  $('#auth-description').textContent = state.registering ? '不同账户的教材与学习对话相互隔离。' : '继续上一次与教材的对话。';
  $('#auth-submit').textContent = state.registering ? '创建账户' : '登录';
  $('#auth-toggle').textContent = state.registering ? '已有账户？返回登录' : '还没有账户？创建书架';
  $('#auth-toggle').hidden = !canRegister;
  $('#invite-label').hidden = !(state.registering && state.inviteRequired);
  $('#invite-code').required = state.registering && state.inviteRequired;
  const codeEntry = state.registering && !state.registrationOpen && state.testCodeRegistration;
  $('#test-code-label').hidden = !codeEntry;
  $('#test-code').required = codeEntry;
  $('#password').autocomplete = state.registering ? 'new-password' : 'current-password';
  $('#auth-error').textContent = '';
}

function showAuth() {
  invalidate();
  state.user = null;
  state.csrf = '';
  state.config = null;
  state.books = [];
  state.book = null;
  state.sections = [];
  state.conversations = [];
  state.conversation = null;
  state.messages = [];
  $('#question').value = '';
  $('#password').value = '';
  $('#invite-code').value = '';
  $('#test-code').value = '';
  $('#upload-form').reset();
  if ($('#upload-dialog').open) $('#upload-dialog').close();
  $('#workspace').hidden = true;
  $('#auth-view').hidden = false;
  $('#account-name').textContent = '';
  $('#book-list').replaceChildren();
  $('#messages').replaceChildren();
  clearEvidence();
  setAuthMode();
}

async function loadIdentity() {
  const data = await api('/api/auth/me');
  state.csrf = data.csrf_token;
  state.registrationOpen = data.registration_open;
  state.testCodeRegistration = data.test_code_registration;
  state.inviteRequired = data.invite_required;
  setAuthMode();
  if (data.user) await enterWorkspace(data.user);
}

async function enterWorkspace(user) {
  invalidate();
  state.user = user;
  $('#auth-view').hidden = true;
  $('#workspace').hidden = false;
  $('#account-name').textContent = user.username;
  $('#password').value = '';
  $('#invite-code').value = '';
  $('#test-code').value = '';
  state.config = await api('/api/config');
  const warning = $('#config-warning');
  warning.hidden = state.config.llm_configured;
  warning.textContent = '问答模型尚未配置。你仍可上传教材；部署者需在 Railway 设置 STUDY_LLM_BASE_URL、STUDY_LLM_API_KEY 和 STUDY_LLM_MODEL。';
  renderBook();
  await refreshBooks();
}

function renderLibrary() {
  $('#book-count').textContent = state.books.length;
  const list = $('#book-list');
  list.replaceChildren();
  if (!state.books.length) {
    list.append(element('p', 'library-empty', '书架还空着。点击右上角“＋”，上传第一本 Markdown 教材。'));
    return;
  }
  state.books.forEach((book) => {
    const button = element('button', `book-item${state.book?.id === book.id ? ' active' : ''}`);
    button.setAttribute('aria-pressed', String(state.book?.id === book.id));
    const info = element('span', 'book-info');
    const name = element('span', 'book-name', book.title);
    if (book.builtin) name.append(element('span', 'book-badge', '内置'));
    info.append(name, element('span', `book-meta${book.status === 'error' ? ' error' : ''}`,
      book.status === 'ready' ? `${book.section_count} 个章节 · ${book.chunk_count} 段` : statusNames[book.status] || book.status));
    button.append(element('span', 'book-spine', book.title.slice(0, 1) || '书'), info);
    action(button, 'click', () => selectBook(book.id));
    list.append(button);
  });
}

function schedulePoll() {
  clearTimeout(state.poll);
  state.poll = null;
  if (!document.hidden && state.user && state.books.some((book) => ['queued', 'indexing'].includes(book.status))) {
    state.poll = setTimeout(() => refreshBooks().catch((error) => { showError(error); schedulePoll(); }), 3000);
  }
}

async function refreshBooks() {
  const { books } = await api('/api/books');
  state.books = books;
  renderLibrary();
  if (state.book) {
    const current = books.find((book) => book.id === state.book.id);
    if (!current) {
      invalidate();
      state.book = null;
      state.messages = [];
      state.conversation = null;
      clearEvidence();
      renderBook();
    } else if (current.status !== state.book.status) {
      await selectBook(current.id, false);
    }
  }
  schedulePoll();
}

function resetStudy() {
  state.conversation = null;
  state.conversations = [];
  state.messages = [];
  state.sections = [];
  $('#question').value = '';
  $('#section-select').replaceChildren(new Option('整本教材', ''));
  $('#conversation-select').replaceChildren(new Option('新的学习对话', ''));
  clearEvidence();
}

async function selectBook(bookId, showPanel = true) {
  const epoch = invalidate();
  resetStudy();
  state.book = state.books.find((book) => book.id === bookId) || null;
  state.loading = true;
  if (showPanel) panel('study');
  renderLibrary();
  renderBook();
  try {
    const detail = await api(`/api/books/${bookId}`);
    state.book = detail.book;
    state.sections = detail.sections;
    $('#section-select').replaceChildren(new Option('整本教材', ''));
    detail.sections.forEach((section) => $('#section-select').add(new Option(`${section.name} · ${section.chunk_count} 段`, section.name)));
    const data = await api(`/api/books/${bookId}/conversations`);
    state.conversations = data.conversations;
    renderConversations();
    if (data.conversations.length && state.book.status === 'ready') {
      const history = await api(`/api/books/${bookId}/conversations/${data.conversations[0].id}`);
      state.conversation = history.conversation;
      state.messages = history.messages;
      renderConversations();
    }
  } finally {
    if (epoch === state.epoch) {
      state.loading = false;
      renderBook();
      schedulePoll();
    }
  }
}

function renderConversations() {
  const select = $('#conversation-select');
  select.replaceChildren(new Option('新的学习对话', ''));
  state.conversations.forEach((conversation) => select.add(new Option(conversation.title, conversation.id)));
  select.value = state.conversation?.id || '';
}

async function selectConversation(conversationId) {
  const bookId = state.book?.id;
  if (!bookId) return;
  const epoch = invalidate();
  state.messages = [];
  state.conversation = null;
  state.loading = true;
  $('#question').value = '';
  clearEvidence();
  renderMessages();
  updateComposer();
  try {
    if (conversationId) {
      const data = await api(`/api/books/${bookId}/conversations/${conversationId}`);
      state.conversation = data.conversation;
      state.messages = data.messages;
    }
  } finally {
    if (epoch === state.epoch) {
      state.loading = false;
      renderConversations();
      renderMessages();
      updateComposer();
      schedulePoll();
    }
  }
}

async function deleteConversation() {
  const bookId = state.book?.id;
  const conversation = state.conversation;
  if (!bookId || !conversation || state.pending) return;
  if (!confirm(`删除对话「${conversation.title}」？对话与全部消息将无法恢复。`)) return;
  await api(`/api/books/${bookId}/conversations/${conversation.id}`, { method: 'DELETE' });
  state.conversations = state.conversations.filter((item) => item.id !== conversation.id);
  const remaining = state.conversations[0];
  toast(remaining ? '对话已删除，已切换到最近的对话。' : '对话已删除。');
  await selectConversation(remaining ? remaining.id : '');
}

function updateComposer() {
  const ready = state.book?.status === 'ready' && !state.loading;
  $('#question').disabled = !ready || state.pending;
  $('#send').disabled = !ready || state.pending || !state.config?.llm_configured;
  $('#cancel-send').hidden = !state.pending;
  $('#messages').setAttribute('aria-busy', String(state.pending || state.loading));
  $('#section-select').disabled = state.pending || state.loading;
  $('#scope-label').textContent = ready ? `仅检索：${$('#section-select').value || state.book.title}` : '先选择一本完成索引的教材';
  $('#new-conversation').disabled = !ready || state.pending;
  $('#delete-conversation').disabled = !ready || !state.conversation || state.pending;
  document.querySelectorAll('[data-mode]').forEach((button) => { button.disabled = state.pending; });
}

function renderBook() {
  const book = state.book;
  $('#current-title').textContent = book?.title || '从一本教材开始';
  $('#current-meta').textContent = book ? (book.status === 'ready'
    ? `${book.section_count} 个章节 · ${book.chunk_count} 段原文 · ${book.index_backend.startsWith('vector') ? '语义 + 关键词混合检索' : '关键词检索（未启用语义向量）'}`
    : `${statusNames[book.status] || book.status} · 大部头教材首次索引需要一些时间`) : '上传已完成 OCR 的 Markdown，或选择书架中的教材。';
  $('#book-actions').hidden = !book;
  $('#study-controls').hidden = !book || book.status !== 'ready';
  $('#book-error').hidden = !book?.error;
  $('#book-error').textContent = book?.error || '';
  $('#reindex').hidden = !book || Boolean(book.builtin);
  $('#reindex').disabled = !book || ['queued', 'indexing'].includes(book.status) || state.pending;
  renderMessages();
  updateComposer();
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll('[data-mode]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.mode === mode)));
  $('#send').firstChild.textContent = mode === 'quiz' ? '出题 ' : '提问 ';
}

function renderEmpty() {
  const container = element('div', 'study-empty');
  container.append(element('div', 'empty-book-line', 'READ / ASK / UNDERSTAND'));
  if (!state.book) {
    container.append(element('h2', '', '让每一次提问，\n都能在书里找到落点。'), element('p', '', '先从左侧选择一本教材，或上传一份包含正文的 Markdown。你的问题、引用与学习对话，只属于当前这本书。'));
    const button = element('button', 'suggestion', '上传第一本教材');
    button.append(element('span', '', '＋'));
    button.addEventListener('click', openUpload);
    container.append(button);
  } else if (state.loading || state.book.status !== 'ready') {
    container.append(element('h2', '', state.loading ? '正在打开这本书…' : state.book.status === 'error' ? '这本书还没准备好。' : '正在为教材建立索引。'),
      element('p', '', state.book.status === 'error' ? '查看上方失败原因。修正文件后重新上传，或点击“重新索引”重试。' : '索引完成后，就可以开始提问。你可以留在此页，也可以稍后再回来。'));
  } else {
    container.append(element('h2', '', '从一个不太确定的地方问起。'), element('p', '', '选择章节会缩小检索范围。没有指定问题的讲解与自测，只抽取部分原文，不代表覆盖整本教材。'));
    const suggestions = element('div', 'suggestions');
    const items = [
      ['解释一个概念', 'qa', '请解释：'], ['梳理当前范围的要点', 'outline', '梳理本章'],
      ['沿着原文逐步讲解', 'explain', '讲解本章'], ['用三个问题检查理解', 'quiz', '本章自测'],
    ];
    items.forEach(([label, mode, prompt]) => {
      const button = element('button', 'suggestion', label);
      button.append(element('span', '', `${modeNames[mode]} ↗`));
      button.addEventListener('click', () => { setMode(mode); $('#question').value = prompt; $('#question').focus(); });
      suggestions.append(button);
    });
    container.append(suggestions);
  }
  return container;
}

const CITATION_PATTERN = /\[(C\d+)\]/g;

function citationButton(label, references, message) {
  const reference = references.find((item) => item.label === label);
  if (!reference) return document.createTextNode('');
  const button = element('button', 'citation', `[${label}]`);
  button.title = `${reference.section} · 段落 ${reference.ordinal}`;
  const bookId = state.book.id;
  action(button, 'click', () => showEvidence(bookId, reference, message));
  return button;
}

// Split paragraph text into plain-text and inline citation-marker tokens.
function paragraphTokens(text) {
  const tokens = [];
  let last = 0;
  for (const match of text.matchAll(CITATION_PATTERN)) {
    if (match.index > last) tokens.push({ text: text.slice(last, match.index) });
    tokens.push({ label: match[1] });
    last = match.index + match[0].length;
  }
  if (last < text.length) tokens.push({ text: text.slice(last) });
  return tokens;
}

// Render one answer paragraph. budget=null means fully revealed (no typing);
// a number limits how many characters (markers included) are shown.
// Returns true when the paragraph rendered completely.
function renderAnswerParagraph(paragraph, item, references, message, budget) {
  const tokens = paragraphTokens(item.text);
  if (!tokens.some((token) => token.label)) {
    // Legacy answers keep citation buttons appended after the text.
    const piece = budget == null ? item.text : item.text.slice(0, Math.max(0, budget));
    if (piece) paragraph.append(document.createTextNode(piece));
    if (budget == null || budget >= item.text.length) {
      (item.citations || []).forEach((label) => paragraph.append(citationButton(label, references, message)));
      return true;
    }
    return false;
  }
  let consumed = 0;
  let complete = true;
  tokens.forEach((token) => {
    if (token.label === undefined) {
      const remaining = budget == null ? token.text.length : Math.max(0, budget - consumed);
      if (remaining > 0) paragraph.append(document.createTextNode(token.text.slice(0, remaining)));
      if (remaining < token.text.length) complete = false;
      consumed += token.text.length;
    } else {
      const cost = `[${token.label}]`.length;
      if (budget == null || consumed + cost <= budget) {
        paragraph.append(citationButton(token.label, references, message));
        consumed += cost;
      } else complete = false;
    }
  });
  return complete;
}

function typingItems(message) {
  if (!message.grounded) return [];
  return (message.paragraphs || []).length ? message.paragraphs : (message.quiz || []);
}

function typingText(message, item, index) {
  return (message.paragraphs || []).length ? item.text : `${index + 1}. ${item.question}`;
}

function startTyping(message) {
  const items = typingItems(message);
  const lengths = items.map((item, index) => typingText(message, item, index).length);
  const total = lengths.reduce((sum, length) => sum + length, 0);
  if (!total) { renderMessages(); return; }
  const typing = { message, reveal: lengths.map(() => 0) };
  state.typing = typing;
  renderMessages();
  // Time-based progression: whole answer reveals over ~2.2s. Using elapsed
  // time instead of a fixed per-tick step keeps the animation correct when
  // background tabs throttle setInterval to ~1Hz (ticks jump ahead instead
  // of stretching the animation out).
  const duration = 2200;
  const started = performance.now();
  const timer = setInterval(() => {
    if (state.typing !== typing) { clearInterval(timer); return; }
    const elapsed = performance.now() - started;
    const target = Math.min(total, Math.ceil((elapsed / duration) * total));
    let budget = target;
    for (let i = 0; i < lengths.length && budget > 0; i++) {
      const advance = Math.min(lengths[i] - typing.reveal[i], budget);
      if (advance > 0) { typing.reveal[i] += advance; budget -= advance; }
    }
    if (typing.reveal.every((shown, i) => shown >= lengths[i])) {
      clearInterval(timer);
      state.typing = null;
    }
    renderMessages();
  }, 16);
}

function renderAssistantBody(article, message, reveal) {
  const references = message.citations || [];
  const typing = Boolean(reveal);
  (message.paragraphs || []).forEach((item, index) => {
    const paragraph = element('p', 'answer-paragraph');
    const budget = typing ? (reveal[index] || 0) : null;
    const complete = renderAnswerParagraph(paragraph, item, references, message, budget);
    if (typing && !complete) paragraph.append(element('span', 'type-cursor', '▍'));
    article.append(paragraph);
  });
  (message.quiz || []).forEach((item, index) => {
    const full = `${index + 1}. ${item.question}`;
    const shown = typing ? full.slice(0, reveal[index] || 0) : full;
    const quiz = element('section', 'quiz-item');
    quiz.append(element('p', 'quiz-question', shown));
    if (typing && (reveal[index] || 0) < full.length) quiz.append(element('span', 'type-cursor', '▍'));
    if (!typing || (reveal[index] || 0) >= full.length) {
      const details = element('details');
      details.append(element('summary', '', '思考后，查看答案与解析'), element('p', 'quiz-answer', item.answer), element('p', 'quiz-answer', item.explanation));
      (item.citations || []).forEach((label) => details.append(citationButton(label, references, message)));
      quiz.append(details);
    }
    article.append(quiz);
  });
  if (!typing) {
    if (message.notice) article.append(element('p', 'answer-notice', message.notice));
    if (message.retrieval?.degraded) article.append(element('p', 'answer-notice', '本次使用关键词检索，语义向量不可用。'));
    if (devMode() && message.retrieval) {
      const meta = message.retrieval;
      const bits = [`通道 ${meta.backend}`, meta.degraded ? '语义向量未启用' : '语义向量已启用'];
      if (meta.hits != null) bits.push(`命中 ${meta.hits} 段`);
      if (meta.retrieve_ms != null) bits.push(`检索 ${meta.retrieve_ms}ms`);
      if (meta.generate_ms != null) bits.push(`生成 ${(meta.generate_ms / 1000).toFixed(1)}s`);
      article.append(element('p', 'answer-debug', bits.join(' · ')));
    }
  }
}

// Tester verdict row under every answer; tapping the active button clears it.
const DISLIKE_REASONS = ['没找到相关内容', '回答不准确', '引用与问题不符', '太简略'];

function feedbackRow(message) {
  const row = element('div', 'feedback-row');
  const current = message.feedback || 0;
  const good = element('button', 'feedback-button' + (current === 1 ? ' active good' : ''), '满意');
  const bad = element('button', 'feedback-button' + (current === -1 ? ' active bad' : ''), '不满意');
  good.addEventListener('click', () => sendFeedback(message, current === 1 ? 0 : 1));
  bad.addEventListener('click', () => {
    if (current === -1) { sendFeedback(message, 0); return; }
    if (row.querySelector('.feedback-reasons')) return;
    const picker = element('div', 'feedback-reasons');
    for (const reason of DISLIKE_REASONS) {
      const option = element('button', 'feedback-reason', reason);
      option.addEventListener('click', () => sendFeedback(message, -1, reason));
      picker.append(option);
    }
    const input = element('input', 'feedback-input');
    input.placeholder = '其他原因（可选）';
    input.maxLength = 200;
    const submit = element('button', 'feedback-reason', '提交');
    submit.addEventListener('click', () => sendFeedback(message, -1, input.value.trim()));
    const skip = element('button', 'feedback-reason muted', '跳过');
    skip.addEventListener('click', () => sendFeedback(message, -1, ''));
    picker.append(input, submit, skip);
    row.append(picker);
    input.focus();
  });
  row.append(element('span', 'feedback-label', '这条回答对你有帮助吗'), good, bad);
  return row;
}

async function sendFeedback(message, rating, reason) {
  try {
    const data = await api(`/api/books/${state.book.id}/conversations/${state.conversation.id}`
      + `/messages/${message.id}/feedback`, { method: 'POST', body: reason === undefined ? { rating } : { rating, reason } });
    message.feedback = data.feedback || 0;
    renderMessages();
  } catch (error) {
    toast(error.message || '评价提交失败，请重试。');
  }
}

function renderMessages() {
  const container = $('#messages');
  const follow = state.pending || container.scrollHeight - container.scrollTop - container.clientHeight < 120;
  container.replaceChildren();
  if (!state.messages.length && !state.pending && !state.typing) container.append(renderEmpty());
  state.messages.forEach((message) => {
    const article = element('article', `message ${message.role}`);
    const heading = element('div', 'message-head');
    heading.append(element('strong', '', message.role === 'user' ? '你' : '书内'), element('span', '', modeNames[message.mode] || '教材问答'));
    article.append(heading);
    if (message.role === 'user') article.append(element('div', 'message-body', message.content));
    else if (!message.grounded) article.append(element('div', 'no-evidence', message.content));
    else if (state.typing?.message === message) renderAssistantBody(article, message, state.typing.reveal);
    else renderAssistantBody(article, message, null);
    if (message.role !== 'user' && message.id && state.conversation?.id
        && state.typing?.message !== message) article.append(feedbackRow(message));
    container.append(article);
  });
  if (state.pending && state.streamText) {
    const live = element('article', 'message assistant');
    const heading = element('div', 'message-head');
    heading.append(element('strong', '', '书内'), element('span', '', modeNames[state.mode] || '教材问答'));
    live.append(heading);
    const body = element('div', 'stream-body');
    body.append(document.createTextNode(state.streamText), element('span', 'type-cursor', '▍'));
    live.append(body);
    container.append(live);
  }
  if (state.typing) container.append(element('p', 'waiting', '点击可立即显示全部内容'));
  if (state.pending) container.append(element('p', 'waiting', state.statusText || '正在检索本书，并核对回答引用…'));
  if (follow) container.scrollTop = container.scrollHeight;
}

// Merged [start, end) ranges of query-term occurrences, case-insensitive.
function highlightRanges(text, terms) {
  const ranges = [];
  const lower = text.toLowerCase();
  for (const term of terms || []) {
    const needle = String(term).toLowerCase();
    if (needle.length < 2) continue;
    let from = 0;
    for (;;) {
      const at = lower.indexOf(needle, from);
      if (at < 0) break;
      ranges.push([at, at + needle.length]);
      from = at + needle.length;
    }
  }
  ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([range[0], range[1]]);
  }
  return merged;
}

// Validated [start, end) sentence ranges coming from the match endpoint.
function sentenceRanges(text, sentences) {
  return (sentences || []).filter((range) => Array.isArray(range) && range.length === 2
    && Number.isInteger(range[0]) && Number.isInteger(range[1])
    && range[0] >= 0 && range[1] > range[0] && range[1] <= text.length);
}

// Reference text with keyword marks (exact term hits) and sentence marks
// (semantically closest to the question). Where both overlap, the keyword
// mark wins: it is the more specific highlight.
function referenceTextNode(text, terms, sentences) {
  const node = element('div', 'reference-text');
  const termRanges = highlightRanges(text, terms);
  const semanticRanges = sentenceRanges(text, sentences);
  const cuts = new Set([0, text.length]);
  termRanges.forEach(([start, end]) => { cuts.add(start); cuts.add(end); });
  semanticRanges.forEach(([start, end]) => { cuts.add(start); cuts.add(end); });
  const points = [...cuts].sort((a, b) => a - b);
  const covered = (ranges, at) => ranges.some(([start, end]) => at >= start && at < end);
  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i];
    const piece = text.slice(start, points[i + 1]);
    if (!piece) continue;
    if (covered(termRanges, start)) node.append(element('mark', 'reference-hit', piece));
    else if (covered(semanticRanges, start)) node.append(element('mark', 'reference-sentence', piece));
    else node.append(document.createTextNode(piece));
  }
  return node;
}

// The question that produced this assistant message (nearest preceding user turn).
function questionFor(message) {
  const index = state.messages.indexOf(message);
  for (let i = index - 1; i >= 0; i -= 1) {
    if (state.messages[i].role === 'user') return state.messages[i].content;
  }
  return '';
}

function evidenceFooter(termHits, sentenceHits) {
  const note = '段落编号是索引位置，不是原书页码；OCR 错误需回到源文件修正。';
  if (termHits && sentenceHits) return '米色词为检索关键词命中，淡蓝整句为与问题语义最相关的句子。' + note;
  if (sentenceHits) return '淡蓝整句为与问题语义最相关的句子。' + note;
  if (termHits) return '高亮处为本次提问的检索关键词命中位置。' + note;
  return '这里展示实际入库的文本分块。' + note;
}

function renderEvidence(chunk, reference, terms, sentences, prev, next) {
  const termHits = highlightRanges(chunk.text, terms).length > 0;
  const sentenceHits = sentenceRanges(chunk.text, sentences).length > 0;
  const nav = element('div', 'reference-nav');
  const prevButton = element('button', 'reference-nav-button' + (prev ? '' : ' disabled'),
    prev ? `← 上一段（${prev.ordinal}）` : '← 已是开头');
  prevButton.disabled = !prev;
  action(prevButton, 'click', () => showEvidence(state.book.id,
    { ...reference, chunk_id: prev.id }, reference.message));
  const nextButton = element('button', 'reference-nav-button' + (next ? '' : ' disabled'),
    next ? `下一段（${next.ordinal}）→` : '已是结尾 →');
  nextButton.disabled = !next;
  action(nextButton, 'click', () => showEvidence(state.book.id,
    { ...reference, chunk_id: next.id }, reference.message));
  nav.append(prevButton, nextButton);
  $('#evidence-content').replaceChildren(
    element('span', 'reference-label', `${reference.label} / 原文段落 ${chunk.ordinal}`),
    element('h3', 'reference-title', state.book.title),
    element('p', 'reference-section', chunk.section),
    referenceTextNode(chunk.text, terms, sentences),
    nav,
    element('p', 'reference-foot', evidenceFooter(termHits, sentenceHits)));
}

async function showEvidence(bookId, reference, message) {
  if (bookId !== state.book?.id) return;
  // Neighbour paging keeps the original message so highlights survive browsing.
  reference.message = message;
  const sequence = ++evidenceSequence;
  panel('evidence');
  $('#evidence-content').replaceChildren(element('p', 'muted', '正在读取原文…'));
  let chunk, prev, next;
  try {
    ({ chunk, prev, next } = await api(`/api/books/${bookId}/chunks/${reference.chunk_id}`));
  } catch (error) {
    if (sequence === evidenceSequence && bookId === state.book?.id) {
      $('#evidence-content').replaceChildren(element('p', 'form-error', error.message || '无法读取原文，请重试。'));
    }
    throw error;
  }
  if (sequence !== evidenceSequence || bookId !== state.book?.id) return;
  // Keyword marks render immediately; the semantic pass refines afterwards.
  const terms = message?.retrieval?.terms || [];
  renderEvidence(chunk, reference, terms, [], prev, next);
  const question = message ? questionFor(message) : '';
  if (!question) return;
  try {
    const data = await api(`/api/books/${bookId}/chunks/${reference.chunk_id}/match`,
      { method: 'POST', body: { question } });
    if (sequence === evidenceSequence && bookId === state.book?.id && data.ranges?.length) {
      renderEvidence(chunk, reference, terms, data.ranges, prev, next);
    }
  } catch { /* Semantic highlighting is best-effort; keyword marks stay. */ }
}

function openUpload() {
  $('#upload-error').textContent = '';
  $('#upload-dialog').showModal();
}

$('#auth-toggle').addEventListener('click', () => { state.registering = !state.registering; setAuthMode(); });
$('#auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#auth-submit').disabled = true;
  $('#auth-toggle').disabled = true;
  $('#auth-error').textContent = '';
  try {
    if (!state.csrf) {
      const identity = await api('/api/auth/me');
      state.csrf = identity.csrf_token;
    }
    const data = await api(`/api/auth/${state.registering ? 'register' : 'login'}`, { method: 'POST', body: {
      username: $('#username').value.trim(), password: $('#password').value, invite_code: $('#invite-code').value,
      test_code: $('#test-code').value.trim(),
    } });
    state.csrf = data.csrf_token;
    await enterWorkspace(data.user);
  } catch (error) {
    if (!(error instanceof StaleRequest)) $('#auth-error').textContent = error.message;
  } finally {
    $('#auth-submit').disabled = false;
    $('#auth-toggle').disabled = false;
  }
});
action($('#logout'), 'click', async () => {
  await api('/api/auth/logout', { method: 'POST', body: {} });
  showAuth();
  await loadIdentity();
});
document.querySelectorAll('.mobile-nav button').forEach((button) => button.addEventListener('click', () => panel(button.dataset.panel)));
document.querySelectorAll('[data-mode]').forEach((button) => button.addEventListener('click', () => setMode(button.dataset.mode)));
$('#upload-open').addEventListener('click', openUpload);
$('#upload-close').addEventListener('click', () => $('#upload-dialog').close());
$('#upload-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = $('#book-file').files[0];
  if (!file) return;
  if (!/\.(md|markdown|txt)$/i.test(file.name) || file.size > 20 * 1024 * 1024) {
    $('#upload-error').textContent = '请选择不超过 20 MB 的 Markdown 或 TXT 文件。';
    return;
  }
  $('#upload-submit').disabled = true;
  $('#upload-close').disabled = true;
  $('#upload-submit').textContent = '正在上传，请稍候…';
  $('#upload-error').textContent = '';
  const epoch = state.epoch;
  try {
    const data = await api('/api/books', { method: 'POST', body: new FormData($('#upload-form')) });
    $('#upload-dialog').close();
    $('#upload-form').reset();
    await refreshBooks();
    await selectBook(data.book.id);
    toast('教材已上传，正在后台建立索引。');
  } catch (error) {
    if (epoch === state.epoch && error.name !== 'AbortError') $('#upload-error').textContent = error.message;
  } finally {
    $('#upload-submit').disabled = false;
    $('#upload-close').disabled = false;
    $('#upload-submit').textContent = '上传并建立索引';
  }
});
$('#upload-dialog').addEventListener('cancel', (event) => { if ($('#upload-submit').disabled) event.preventDefault(); });
action($('#reindex'), 'click', async () => {
  if (!state.book || !confirm('重新索引会清除这本书的全部学习对话，以免旧引用指向新的分块。确定继续？')) return;
  const bookId = state.book.id;
  await api(`/api/books/${bookId}/reindex`, { method: 'POST', body: {} });
  await refreshBooks();
  await selectBook(bookId);
  toast('已重新加入索引队列。');
});
action($('#conversation-select'), 'change', () => selectConversation($('#conversation-select').value));
action($('#new-conversation'), 'click', () => selectConversation(''));
action($('#delete-conversation'), 'click', deleteConversation);
$('#section-select').addEventListener('change', () => {
  $('#question').value = '';
  updateComposer();
  clearEvidence();
});
$('#question').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('#send').disabled) $('#chat-form').requestSubmit();
  }
});
$('#chat-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = $('#question').value.trim();
  if (!question || !state.book || state.pending || state.loading || !state.config?.llm_configured) return;
  const bookId = state.book.id;
  const epoch = state.epoch;
  const mode = state.mode;
  const section = $('#section-select').value || null;
  state.pending = true;
  state.statusText = '';
  $('#question').value = '';
  const optimistic = { role: 'user', content: question, mode, created_at: null };
  state.messages.push(optimistic);
  updateComposer();
  renderMessages();
  try {
    if (!state.conversation) {
      const data = await api(`/api/books/${bookId}/conversations`, { method: 'POST', body: {} });
      state.conversation = data.conversation;
      state.conversations.unshift(data.conversation);
      renderConversations();
    }
    const cid = state.conversation.id;
    await streamChat(`/api/books/${bookId}/conversations/${cid}/messages`, { message: question, mode, section }, (evt) => {
      if (epoch !== state.epoch) return;
      if (evt.type === 'status') {
        state.statusText = evt.text;
        renderMessages();
      } else if (evt.type === 'delta') {
        state.streamText += evt.text;
        if (!streamRenderTimer) {
          streamRenderTimer = setTimeout(() => {
            streamRenderTimer = null;
            if (state.pending) renderMessages();
          }, 60);
        }
      } else if (evt.type === 'answer') {
        const streamed = state.streamText.length > 0;
        state.streamText = '';
        clearTimeout(streamRenderTimer);
        streamRenderTimer = null;
        const index = state.messages.indexOf(optimistic);
        if (index >= 0) state.messages.splice(index, 1, evt.user_message);
        state.messages.push(evt.message);
        state.statusText = '';
        state.pending = false;
        updateComposer();
        // The text was already streamed live; only replay the typewriter when
        // the server fell back to one-shot generation.
        if (streamed) renderMessages();
        else startTyping(evt.message);
      } else if (evt.type === 'error') {
        throw new Error(evt.error);
      }
    });
    const conversations = await api(`/api/books/${bookId}/conversations`);
    state.conversations = conversations.conversations;
    renderConversations();
  } catch (error) {
    const index = state.messages.indexOf(optimistic);
    if (index >= 0) state.messages.splice(index, 1);
    state.streamText = '';
    clearTimeout(streamRenderTimer);
    streamRenderTimer = null;
    if (epoch === state.epoch && !(error instanceof StaleRequest) && error.name !== 'AbortError') $('#question').value = question;
    showError(error);
  } finally {
    if (epoch === state.epoch) {
      state.pending = false;
      state.statusText = '';
      renderBook();
      $('#question').focus();
      schedulePoll();
    }
  }
});
$('#messages').addEventListener('click', (event) => {
  if (state.typing && !event.target.closest('.citation')) {
    state.typing = null;
    renderMessages();
  }
});
action($('#cancel-send'), 'click', async () => {
  const cid = state.conversation?.id;
  invalidate();
  toast('已停止等待；后台请求可能仍会完成。稍后重新打开此对话即可查看。');
  renderBook();
  if (cid) await selectConversation(cid);
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(state.poll); state.poll = null; }
  else if (state.user) refreshBooks().catch(showError);
});

const logEventNames = {
  chat: '问答', chat_error: '问答失败', index_ready: '索引完成', index_error: '索引失败',
  index_embed_fallback: '索引降级', request_error: '请求错误', login_failed: '登录失败',
};

async function loadLogs() {
  const box = $('#logs-list');
  box.replaceChildren(element('p', 'muted', '正在读取日志…'));
  const data = await api('/api/logs');
  box.replaceChildren();
  if (!data.logs.length) {
    box.append(element('p', 'muted', '暂无日志。提问或索引教材后，这里会出现记录。'));
    return;
  }
  data.logs.forEach((entry) => {
    const row = element('div', `log-row ${entry.level}`);
    const time = new Date(entry.created_at);
    row.append(element('span', 'log-time', Number.isNaN(time.getTime()) ? '' : time.toLocaleTimeString('zh-CN', { hour12: false })),
      element('span', `log-tag ${entry.level}`, logEventNames[entry.event] || entry.event),
      element('span', 'log-detail', entry.detail));
    box.append(row);
  });
}

$('#settings-open').addEventListener('click', () => {
  $('#dev-mode-toggle').checked = devMode();
  $('#settings-dialog').showModal();
  loadLogs().catch(showError);
});
$('#settings-close').addEventListener('click', () => $('#settings-dialog').close());
$('#logs-refresh').addEventListener('click', () => loadLogs().catch(showError));
$('#dev-mode-toggle').addEventListener('change', () => {
  localStorage.setItem('study.devMode', $('#dev-mode-toggle').checked ? '1' : '0');
  renderMessages();
});

const LAYOUT_KEY = 'study.deskLayout';
const desk = $('.desk');

function readLayout() {
  try { return JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}'); } catch { return {}; }
}

function saveLayout(patch) {
  localStorage.setItem(LAYOUT_KEY, JSON.stringify({ ...readLayout(), ...patch }));
}

function applyLayout() {
  const layout = readLayout();
  if (layout.evidence) desk.style.setProperty('--evidence-w', `${layout.evidence}px`);
  desk.classList.toggle('library-collapsed', Boolean(layout.collapsed));
}

$('#library-toggle').addEventListener('click', () => {
  desk.classList.add('library-collapsed');
  saveLayout({ collapsed: true });
});
$('#library-expand').addEventListener('click', () => {
  desk.classList.remove('library-collapsed');
  saveLayout({ collapsed: false });
});

$('#evidence-dragbar').addEventListener('pointerdown', (event) => {
  event.preventDefault();
  const bar = $('#evidence-dragbar');
  bar.setPointerCapture(event.pointerId);
  bar.classList.add('dragging');
  const rect = desk.getBoundingClientRect();
  const move = (e) => {
    const width = Math.round(Math.min(Math.max(rect.right - e.clientX, 220), Math.min(640, rect.width * 0.55)));
    desk.style.setProperty('--evidence-w', `${width}px`);
  };
  const finish = () => {
    bar.removeEventListener('pointermove', move);
    bar.removeEventListener('pointerup', finish);
    bar.removeEventListener('pointercancel', finish);
    bar.classList.remove('dragging');
    const value = parseInt(desk.style.getPropertyValue('--evidence-w'), 10);
    if (value) saveLayout({ evidence: value });
  };
  bar.addEventListener('pointermove', move);
  bar.addEventListener('pointerup', finish);
  bar.addEventListener('pointercancel', finish);
});

applyLayout();
loadIdentity().catch((error) => {
  $('#auth-error').textContent = error.message || '暂时无法连接服务，请刷新重试。';
});
