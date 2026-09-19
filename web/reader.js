// Source-backed reader. All positions use JavaScript's UTF-16 string offsets.
export function createReader({ api, element, getBook, getEpoch, panel, toast }) {
  const $ = (selector) => document.querySelector(selector);
  let current = null;
  let editor = null;
  const colors = new Set(['yellow', 'blue', 'green']);
  const live = (reader) => current === reader && reader.epoch === getEpoch() && reader.bookId === getBook()?.id;
  const button = (text, callback, className = 'outline-button') => {
    const node = element('button', className, text);
    node.type = 'button';
    node.addEventListener('click', callback);
    return node;
  };

  const desk = $('.desk');
  const chat = $('#study-panel');
  const dragHandle = $('#reader-chat-drag');
  let focused = false;
  let minimized = false;
  let chatPosition = null;
  let drag = null;
  let previousPanel = 'study';
  let previousFocus = null;
  let nativeOwned = false;
  let nativeRequest = null;
  let nativeExiting = false;

  function endDrag() {
    if (drag && dragHandle.hasPointerCapture(drag.id)) dragHandle.releasePointerCapture(drag.id);
    drag = null;
    chat.classList.remove('reader-chat-dragging');
  }

  function positionChat(x, y) {
    const bounds = desk.getBoundingClientRect();
    const box = chat.getBoundingClientRect();
    chatPosition = {
      x: Math.max(12, Math.min(x, bounds.width - box.width - 12)),
      y: Math.max(72, Math.min(y, bounds.height - box.height - 12)),
    };
    chat.style.setProperty('--chat-x', `${chatPosition.x}px`);
    chat.style.setProperty('--chat-y', `${chatPosition.y}px`);
  }

  function resizeFocus() {
    if (!focused) return;
    const viewport = window.visualViewport;
    desk.style.setProperty('--reader-height', `${viewport?.height ?? window.innerHeight}px`);
    desk.style.setProperty('--reader-width', `${viewport?.width ?? window.innerWidth}px`);
    desk.style.setProperty('--reader-left', `${viewport?.offsetLeft ?? 0}px`);
    desk.style.setProperty('--reader-top', `${viewport?.offsetTop ?? 0}px`);
    if (chatPosition && !minimized) positionChat(chatPosition.x, chatPosition.y);
  }

  function setChatMinimized(value, moveFocus = false) {
    minimized = focused && value;
    endDrag();
    chat.hidden = minimized;
    $('#reader-chat-bar').hidden = !focused;
    $('#reader-chat-launcher').hidden = !minimized;
    $('#reader-chat-launcher').setAttribute('aria-expanded', String(focused && !minimized));
    if (!minimized) resizeFocus();
    if (moveFocus) {
      const target = minimized ? $('#reader-chat-launcher') : $('#question').disabled ? dragHandle : $('#question');
      target.focus({ preventScroll: true });
    }
  }

  function exitNativeFullscreen() {
    if (!nativeOwned || nativeExiting || document.fullscreenElement !== document.documentElement) return;
    nativeExiting = true;
    $('#reader-focus').disabled = true;
    $('#read-book').disabled = true;
    return Promise.resolve().then(() => document.exitFullscreen()).catch(() => {
      // The viewport layout still restores if the browser rejects an exit.
    }).finally(() => {
      nativeExiting = false;
      $('#reader-focus').disabled = false;
      $('#read-book').disabled = false;
      if (document.fullscreenElement !== document.documentElement) nativeOwned = false;
    });
  }

  function requestNativeFullscreen() {
    if (nativeRequest || nativeExiting || document.fullscreenElement || !document.fullscreenEnabled
      || !document.documentElement.requestFullscreen) return;
    try {
      nativeRequest = document.documentElement.requestFullscreen();
    } catch {
      return;
    }
    Promise.resolve(nativeRequest).then(() => {
      nativeOwned = document.fullscreenElement === document.documentElement;
      if (!focused) exitNativeFullscreen();
      else resizeFocus();
    }).catch(() => {
      if (focused) toast('浏览器未允许系统全屏，已切换为铺满窗口阅读。');
    }).finally(() => { nativeRequest = null; });
  }

  function setFocus(expanded, { restore = true } = {}) {
    if (focused === expanded || (expanded && (nativeExiting || !current || !live(current)))) return;
    if (expanded) {
      previousPanel = desk.dataset.panel;
      previousFocus = document.activeElement;
    }
    focused = expanded;
    desk.classList.toggle('reading-focused', expanded);
    $('#workspace').classList.toggle('reader-fullscreen', expanded);
    $('#reader-focus').setAttribute('aria-pressed', String(expanded));
    $('#reader-focus').textContent = expanded ? '退出全屏' : '展开阅读';
    setChatMinimized(false);
    if (expanded) {
      panel('evidence');
      resizeFocus();
      requestNativeFullscreen();
      current.scroll.focus({ preventScroll: true });
    } else {
      const exiting = exitNativeFullscreen();
      chatPosition = null;
      chat.style.removeProperty('--chat-x');
      chat.style.removeProperty('--chat-y');
      for (const name of ['height', 'width', 'left', 'top']) desk.style.removeProperty(`--reader-${name}`);
      if (restore) {
        panel(previousPanel || 'study');
        const reader = current;
        const target = previousFocus;
        const restoreFocus = () => {
          if (focused || !reader || !live(reader)) return;
          const visibleTarget = [target, $('#reader-focus'), $('#question')]
            .find((node) => node?.isConnected && !node.disabled && node.getClientRects().length);
          visibleTarget?.focus({ preventScroll: true });
        };
        if (exiting) exiting.then(restoreFocus);
        else restoreFocus();
      }
      previousFocus = null;
    }
  }

  function reset() {
    if (current?.frame) cancelAnimationFrame(current.frame);
    current = null;
    editor = null;
    if ($('#annotation-dialog').open) $('#annotation-dialog').close();
    $('#annotation-form').reset();
    $('#annotation-quote').textContent = '';
    $('#annotation-error').textContent = '';
    $('.evidence').classList.remove('reader-open');
    $('#reader-focus').hidden = true;
    setFocus(false, { restore: false });
  }

  function preserveForConversation() {
    if (!focused || !current || current.bookId !== getBook()?.id) return false;
    if (current.epoch === getEpoch()) return true;
    // Replace the reader identity so aborted callbacks cannot mutate its successor.
    const old = current;
    const scrollTop = old.scroll.scrollTop;
    if (old.frame) cancelAnimationFrame(old.frame);
    const reader = { ...old, epoch: getEpoch(), frame: null, loading: null, navigating: false,
      notesLoading: false, selection: null, highlightsPending: false };
    current = reader;
    mount(reader);
    reader.tocSelect.replaceChildren(...reader.toc.map((item) => new Option(item.title, item.start)));
    reader.stream.replaceChildren(...reader.blocks.map((block) => blockNode(reader, block)));
    reader.scroll.scrollTop = scrollTop;
    updateEdges(reader);
    updateProgress(reader);
    if (old.navigating || !reader.version) navigate(reader, old.target || {});
    loadNotes(reader);
    return true;
  }

  function keepPosition(reader, change) {
    const top = reader.scroll.getBoundingClientRect().top;
    const visible = [...reader.stream.children].find((node) => node.getBoundingClientRect().bottom > top);
    const oldTop = visible?.getBoundingClientRect().top;
    change();
    if (visible?.isConnected) reader.scroll.scrollTop += visible.getBoundingClientRect().top - oldTop;
  }

  function mount(reader) {
    $('.evidence').classList.add('reader-open');
    $('#reader-focus').hidden = false;
    const shell = element('div', 'reader-shell');
    const tools = element('div', 'reader-tools');
    tools.append(element('h3', 'reader-book-title', getBook().title));
    const directory = element('label', 'reader-directory', '目录');
    reader.tocSelect = element('select');
    reader.tocSelect.setAttribute('aria-label', '跳转到正文目录');
    reader.tocSelect.disabled = true;
    reader.tocSelect.append(new Option('正在读取目录…', ''));
    reader.tocSelect.addEventListener('change', () => {
      if (reader.tocSelect.value !== '') navigate(reader, { at: Number(reader.tocSelect.value), version: reader.version });
    });
    directory.append(reader.tocSelect);
    tools.append(directory);
    const actions = element('div', 'reader-actions');
    reader.notesButton = button('我的批注', () => {
      reader.notesOpen = !reader.notesOpen;
      reader.noteFilter = null;
      renderNotes(reader);
    });
    reader.notesButton.setAttribute('aria-controls', 'reader-notes');
    reader.annotateButton = button('选文批注', () => openEditor(reader, null, reader.selection));
    reader.annotateButton.disabled = true;
    reader.annotateButton.addEventListener('pointerdown', (event) => event.preventDefault());
    actions.append(reader.notesButton, reader.annotateButton,
      button('从头阅读', () => navigate(reader, {}), 'text-button'));
    tools.append(actions);
    reader.hint = element('p', 'reader-hint', '选中文字后，可添加高亮或笔记；仅自己可见。');
    tools.append(reader.hint);
    reader.error = element('div', 'reader-error');
    reader.error.hidden = true;
    reader.error.setAttribute('role', 'status');
    reader.drawer = element('section', 'reader-notes');
    reader.drawer.id = 'reader-notes';
    reader.drawer.setAttribute('aria-label', '当前教材的私人批注');
    reader.scroll = element('div', 'reader-scroll');
    reader.scroll.tabIndex = 0;
    reader.scroll.setAttribute('aria-label', '教材全文，可上下滚动，按 Shift 配合方向键选择文字');
    reader.before = button('加载前文', () => extend(reader, 'before'), 'reader-more');
    reader.after = button('加载后文', () => extend(reader, 'after'), 'reader-more');
    reader.stream = element('div', 'reader-stream');
    reader.scroll.append(reader.before, reader.stream, reader.after);
    reader.progress = element('p', 'reader-progress', '正在读取正文…');
    reader.scroll.addEventListener('scroll', () => {
      if (reader.frame) return;
      reader.frame = requestAnimationFrame(() => {
        reader.frame = null;
        if (!live(reader)) return;
        updateProgress(reader);
        if (reader.navigating || reader.loading || !reader.blocks.length || reader.selection || $('#annotation-dialog').open) return;
        const box = reader.scroll;
        if (box.scrollTop <= 180 && reader.blocks[0].index > 0 && !reader.errors.before) extend(reader, 'before');
        else if (box.scrollHeight - box.scrollTop - box.clientHeight < 360
          && reader.blocks.at(-1).index < reader.total - 1 && !reader.errors.after) extend(reader, 'after');
      });
    }, { passive: true });
    shell.append(tools, reader.error, reader.drawer, reader.scroll, reader.progress);
    $('#evidence-content').replaceChildren(shell);
    renderNotes(reader);
    updateEdges(reader);
  }

  function updateEdges(reader) {
    const first = reader.blocks[0];
    const last = reader.blocks.at(-1);
    for (const direction of ['before', 'after']) {
      const node = reader[direction];
      const finished = direction === 'before' ? first?.index === 0 : last?.index === reader.total - 1;
      node.disabled = !first || finished || reader.navigating || Boolean(reader.loading);
      node.textContent = !first ? '正文尚未载入' : finished ? (direction === 'before' ? '正文开始' : '已到全文末尾')
        : reader.loading === direction ? '正在加载…'
          : reader.errors[direction] ? `${reader.errors[direction]} 点击重试`
            : direction === 'before' ? '向上滚动，继续读前文' : '向下滚动，继续读后文';
      node.classList.toggle('reader-load-error', Boolean(reader.errors[direction]));
    }
    reader.tocSelect.disabled = !reader.version || reader.navigating;
    reader.scroll.setAttribute('aria-busy', String(reader.navigating || Boolean(reader.loading)));
  }

  function updateProgress(reader) {
    if (!reader.blocks.length) {
      reader.progress.textContent = '全文阅读不调用模型；OCR 错误请对照源文件。';
      return;
    }
    const top = reader.scroll.getBoundingClientRect().top;
    const node = [...reader.stream.children].find((item) => item.getBoundingClientRect().bottom > top);
    const block = reader.blocks.find((item) => item.index === Number(node?.dataset.index)) || reader.blocks[0];
    const atEnd = reader.blocks.at(-1).index === reader.total - 1
      && reader.scroll.scrollHeight - reader.scroll.scrollTop - reader.scroll.clientHeight < 4;
    reader.progress.textContent = `正文位置 ${atEnd ? 100 : Math.min(99, Math.floor(block.start / reader.length * 100))}% · OCR 原文，非原书页码`;
    const section = reader.toc.filter((item) => item.start <= block.start).at(-1);
    if (section) reader.tocSelect.value = String(section.start);
  }

  function textNode(reader, block) {
    const node = element('div', 'reader-text');
    node.dataset.start = block.start;
    node.dataset.index = block.index;
    const notes = reader.annotations.filter((note) => note.version === reader.version && note.start < block.end && note.end > block.start);
    const spans = notes.map((note) => ({ start: Math.max(0, note.start - block.start), end: Math.min(block.text.length, note.end - block.start), note }));
    const anchor = reader.anchor;
    const cited = anchor && anchor.start < block.end && anchor.end > block.start
      ? { start: Math.max(0, anchor.start - block.start), end: Math.min(block.text.length, anchor.end - block.start) } : null;
    const cuts = new Set([0, block.text.length]);
    spans.forEach(({ start, end }) => { cuts.add(start); cuts.add(end); });
    if (cited) { cuts.add(cited.start); cuts.add(cited.end); }
    const points = [...cuts].sort((a, b) => a - b);
    for (let i = 0; i < points.length - 1; i += 1) {
      const start = points[i];
      const piece = block.text.slice(start, points[i + 1]);
      const covering = spans.filter((span) => span.start <= start && span.end > start).map((span) => span.note);
      const isCited = cited && start >= cited.start && start < cited.end;
      if (covering.length) {
        const color = colors.has(covering[0].color) ? covering[0].color : 'yellow';
        const mark = element('mark', `reader-highlight highlight-${color}${isCited ? ' reader-citation' : ''}`, piece);
        mark.tabIndex = 0;
        mark.setAttribute('role', 'button');
        mark.setAttribute('aria-label', `查看${covering.length > 1 ? '重叠' : ''}批注：${covering[0].note || covering[0].quote.slice(0, 60)}`);
        mark.title = covering.length > 1 ? `${covering.length} 条重叠批注` : covering[0].note || '查看或编辑高亮';
        const view = () => {
          if (covering.length === 1) openEditor(reader, covering[0]);
          else {
            reader.notesOpen = true;
            reader.noteFilter = covering.map((note) => note.id);
            renderNotes(reader);
          }
        };
        mark.addEventListener('click', () => { if (window.getSelection()?.isCollapsed) view(); });
        mark.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); view(); }
        });
        node.append(mark);
      } else if (isCited) node.append(element('mark', 'reader-citation', piece));
      else node.append(document.createTextNode(piece));
    }
    return node;
  }

  function blockNode(reader, block) {
    const article = element('article', 'reader-block');
    article.dataset.index = block.index;
    const sectionStart = reader.toc.some((item) => item.index === block.index);
    if (sectionStart || block.index === reader.blocks[0]?.index) {
      const label = element('p', 'reader-section', block.section);
      label.setAttribute('aria-hidden', 'true');
      article.append(label);
    }
    article.append(textNode(reader, block));
    return article;
  }

  function refreshHighlights(reader) {
    keepPosition(reader, () => {
      for (const block of reader.blocks) {
        const node = reader.stream.querySelector(`[data-index="${block.index}"] .reader-text`);
        if (node) node.replaceWith(textNode(reader, block));
      }
    });
  }

  function jumpTo(reader, offset) {
    const block = reader.blocks.find((item) => item.start <= offset && offset < item.end);
    if (!block) return;
    const node = reader.stream.querySelector(`[data-index="${block.index}"] .reader-text`);
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let remaining = offset - block.start;
    let text = walker.nextNode();
    while (text && remaining >= text.length) { remaining -= text.length; text = walker.nextNode(); }
    let rect = node.getBoundingClientRect();
    if (text) {
      const range = document.createRange();
      range.setStart(text, remaining);
      range.setEnd(text, Math.min(text.length, remaining + 1));
      const candidate = range.getBoundingClientRect();
      if (candidate.height) rect = candidate;
    }
    reader.scroll.scrollTop += rect.top - reader.scroll.getBoundingClientRect().top - 24;
    reader.scroll.focus({ preventScroll: true });
    updateProgress(reader);
  }

  async function navigate(reader, params) {
    if (!live(reader)) return;
    const navigation = ++reader.navigation;
    reader.target = params;
    reader.navigating = true;
    reader.loading = null;
    reader.selection = null;
    reader.annotateButton.disabled = true;
    reader.hint.textContent = '正在定位正文…';
    reader.error.hidden = true;
    updateEdges(reader);
    try {
      const query = new URLSearchParams({ count: 12, ...params });
      const data = await api(`/api/books/${reader.bookId}/reader?${query}`);
      if (!live(reader) || navigation !== reader.navigation) return;
      if (data.offset_unit !== 'utf-16' || !data.blocks.length) throw new Error('正文为空或位置格式不受支持。');
      reader.version = data.version;
      reader.blocks = data.blocks;
      reader.total = data.total;
      reader.length = data.length;
      reader.toc = data.toc;
      reader.anchor = data.anchor;
      reader.errors = {};
      reader.tocSelect.replaceChildren(...data.toc.map((item) => new Option(item.title, item.start)));
      reader.stream.replaceChildren(...data.blocks.map((block) => blockNode(reader, block)));
      reader.scroll.scrollTop = 0;
      reader.hint.textContent = data.anchor ? '蓝色底纹为本次引用；可上下连续阅读，选文添加私人批注。' : '选中文字后，可添加高亮或笔记；仅自己可见。';
      renderNotes(reader);
      const target = data.anchor?.start ?? params.at ?? 0;
      requestAnimationFrame(() => { if (live(reader) && navigation === reader.navigation) jumpTo(reader, target); });
    } catch (error) {
      if (!live(reader) || navigation !== reader.navigation) return;
      reader.error.replaceChildren(element('p', '', error.message || '正文读取失败。'),
        button('重试定位', () => navigate(reader, params)), button('重新打开正文', () => navigate(reader, {})));
      reader.error.hidden = false;
      reader.hint.textContent = '读取失败不会删除批注；可重试或重新打开当前正文。';
    } finally {
      if (live(reader) && navigation === reader.navigation) {
        reader.navigating = false;
        updateEdges(reader);
        updateProgress(reader);
      }
    }
  }

  async function extend(reader, direction) {
    if (!live(reader) || reader.loading || reader.navigating || !reader.blocks.length) return;
    const first = reader.blocks[0].index;
    const last = reader.blocks.at(-1).index;
    if (direction === 'before' ? first === 0 : last >= reader.total - 1) return;
    const navigation = reader.navigation;
    reader.loading = direction;
    delete reader.errors[direction];
    updateEdges(reader);
    try {
      const start = direction === 'before' ? Math.max(0, first - 8) : last + 1;
      const count = direction === 'before' ? first - start : 8;
      const query = new URLSearchParams({ start, count, version: reader.version });
      const data = await api(`/api/books/${reader.bookId}/reader?${query}`);
      if (!live(reader) || navigation !== reader.navigation) return;
      const fresh = data.blocks;
      if (data.version !== reader.version || !fresh.length
        || (direction === 'before' ? fresh.at(-1).end !== reader.blocks[0].start : fresh[0].start !== reader.blocks.at(-1).end)) {
        throw new Error('正文窗口不连续，请重新打开正文。');
      }
      keepPosition(reader, () => {
        reader.blocks = direction === 'before' ? fresh.concat(reader.blocks) : reader.blocks.concat(fresh);
        const fragment = document.createDocumentFragment();
        fresh.forEach((block) => fragment.append(blockNode(reader, block)));
        if (direction === 'before') reader.stream.prepend(fragment);
        else reader.stream.append(fragment);
      });
    } catch (error) {
      if (live(reader) && navigation === reader.navigation) reader.errors[direction] = error.message || '加载失败。';
    } finally {
      if (live(reader) && navigation === reader.navigation) {
        reader.loading = null;
        updateEdges(reader);
        updateProgress(reader);
      }
    }
  }

  function renderNotes(reader) {
    if (!live(reader)) return;
    reader.notesButton.textContent = `我的批注${reader.notesLoaded ? ` · ${reader.annotations.length}` : ''}`;
    reader.notesButton.setAttribute('aria-expanded', String(reader.notesOpen));
    reader.drawer.hidden = !reader.notesOpen;
    reader.drawer.replaceChildren();
    if (!reader.notesOpen) return;
    const heading = element('div', 'reader-notes-heading');
    heading.append(element('strong', '', reader.noteFilter ? '此处的重叠批注' : '仅自己可见'),
      button('刷新', () => loadNotes(reader), 'text-button'),
      button('收起', () => { reader.notesOpen = false; renderNotes(reader); }, 'text-button'));
    reader.drawer.append(heading);
    if (reader.noteFilter) reader.drawer.append(button('查看全部批注', () => { reader.noteFilter = null; renderNotes(reader); }, 'text-button'));
    if (reader.notesLoading) reader.drawer.append(element('p', 'reader-hint', '正在读取批注…'));
    if (reader.notesError) reader.drawer.append(element('p', 'form-error', `${reader.notesError} 可点击刷新重试。`));
    const notes = reader.annotations.filter((note) => !reader.noteFilter || reader.noteFilter.includes(note.id));
    if (!notes.length && reader.notesLoaded) reader.drawer.append(element('p', 'reader-hint', '还没有批注。在正文中选中文字，再点击“选文批注”。'));
    const list = element('div', 'reader-note-list');
    notes.forEach((note) => {
      const old = reader.version && note.version !== reader.version;
      const row = element('article', `reader-note note-${colors.has(note.color) ? note.color : 'yellow'}`);
      const location = button(note.quote.length > 160 ? `${note.quote.slice(0, 160)}…` : note.quote, async () => {
        reader.notesOpen = false;
        renderNotes(reader);
        await navigate(reader, { at: note.start, version: note.version });
      }, 'reader-note-quote');
      location.disabled = !reader.version || Boolean(old);
      location.title = old ? '正文已变化，不能定位到当前版本' : '回到正文';
      row.append(location);
      if (note.note) row.append(element('p', 'reader-note-body', note.note));
      const foot = element('div', 'reader-note-foot');
      foot.append(element('span', '', old ? '旧版本 · 保留原选文' : !reader.version ? '尚未核对正文版本' : '当前版本'),
        button('编辑', () => openEditor(reader, note), 'text-button'));
      row.append(foot);
      list.append(row);
    });
    reader.drawer.append(list);
  }

  async function loadNotes(reader) {
    if (!live(reader) || reader.notesLoading) return;
    reader.notesLoading = true;
    reader.notesError = '';
    renderNotes(reader);
    try {
      const data = await api(`/api/books/${reader.bookId}/annotations`);
      if (!live(reader)) return;
      reader.annotations = data.annotations;
      reader.notesLoaded = true;
      // Do not destroy an active selection when a delayed annotation fetch arrives.
      if (!reader.selection && !$('#annotation-dialog').open) refreshHighlights(reader);
      else reader.highlightsPending = true;
    } catch (error) {
      if (live(reader)) reader.notesError = error.message || '批注读取失败。';
    } finally {
      if (live(reader)) {
        reader.notesLoading = false;
        renderNotes(reader);
      }
    }
  }

  function endpoint(node, offset, reader) {
    const parent = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    const text = parent?.closest('.reader-text');
    if (!text || !reader.stream.contains(text)) return null;
    const range = document.createRange();
    range.selectNodeContents(text);
    range.setEnd(node, offset);
    return Number(text.dataset.start) + range.toString().length;
  }

  function captureSelection() {
    const reader = current;
    if (!reader || !live(reader) || $('#annotation-dialog').open || reader.navigating) return;
    reader.selection = null;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && selection.rangeCount === 1) {
      const range = selection.getRangeAt(0);
      const start = endpoint(range.startContainer, range.startOffset, reader);
      const end = endpoint(range.endContainer, range.endOffset, reader);
      if (start !== null && end !== null && end > start && end - start <= 4000) {
        const quote = reader.blocks.filter((block) => block.start < end && block.end > start)
          .map((block) => block.text.slice(Math.max(0, start - block.start), Math.min(block.text.length, end - block.start))).join('');
        if (quote.length === end - start && quote.trim()) reader.selection = { version: reader.version, start, end, quote };
      }
      if (start !== null && end !== null && end - start > 4000) reader.hint.textContent = '一次最多选择 4000 个字符，请缩小选文范围。';
    }
    reader.annotateButton.disabled = !reader.selection;
    if (reader.selection) reader.hint.textContent = `已选 ${reader.selection.end - reader.selection.start} 个字符 · 点击“选文批注”保存高亮或笔记。`;
    else if (!selection || selection.isCollapsed) {
      reader.hint.textContent = reader.anchor ? '蓝色底纹为本次引用；选文可添加私人批注。' : '选中文字后，可添加高亮或笔记；仅自己可见。';
      if (reader.highlightsPending) {
        reader.highlightsPending = false;
        refreshHighlights(reader);
      }
    }
  }

  function openEditor(reader, annotation, selection = null) {
    if (!live(reader) || (!annotation && (!selection || reader.navigating || selection.version !== reader.version))) return;
    editor = { reader, annotation, selection, busy: false };
    $('#annotation-form').reset();
    $('#annotation-title').textContent = annotation ? '编辑批注' : '添加批注';
    $('#annotation-quote').textContent = (annotation || selection).quote;
    $('#annotation-note').value = annotation?.note || '';
    const color = colors.has(annotation?.color) ? annotation.color : 'yellow';
    $(`input[name="annotation-color"][value="${color}"]`).checked = true;
    $('#annotation-version').textContent = !reader.version ? '尚未读取正文；已有批注仍可编辑或删除。'
      : annotation && annotation.version !== reader.version ? '旧版本批注：保留原选文，不会自动移动到当前正文。' : '当前正文 · 私人批注';
    $('#annotation-delete').hidden = !annotation;
    $('#annotation-error').textContent = '';
    setEditorBusy(false);
    $('#annotation-dialog').showModal();
    $('#annotation-note').focus();
  }

  function setEditorBusy(busy) {
    for (const id of ['annotation-save', 'annotation-delete', 'annotation-close', 'annotation-note']) $(`#${id}`).disabled = busy;
    document.querySelectorAll('input[name="annotation-color"]').forEach((node) => { node.disabled = busy; });
  }

  async function saveAnnotation(remove = false) {
    const context = editor;
    if (!context || context.busy || !live(context.reader)) return;
    const { reader, annotation, selection } = context;
    if (remove && (!annotation || !confirm('删除这条批注及其高亮？此操作无法恢复。'))) return;
    context.busy = true;
    setEditorBusy(true);
    $('#annotation-error').textContent = '';
    const note = $('#annotation-note').value;
    const color = $('input[name="annotation-color"]:checked').value;
    try {
      const path = `/api/books/${reader.bookId}/annotations${annotation ? `/${annotation.id}` : ''}`;
      const data = await api(path, { method: remove ? 'DELETE' : annotation ? 'PATCH' : 'POST',
        ...(remove ? {} : { body: annotation ? { note, color } : { ...selection, note, color } }) });
      if (!live(reader) || editor !== context) return;
      reader.annotations = reader.annotations.filter((item) => item.id !== annotation?.id);
      if (!remove) reader.annotations.push(data.annotation);
      reader.annotations.sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
      reader.selection = null;
      reader.annotateButton.disabled = true;
      reader.noteFilter = null;
      window.getSelection()?.removeAllRanges();
      $('#annotation-dialog').close();
      reader.highlightsPending = false;
      refreshHighlights(reader);
      renderNotes(reader);
      toast(remove ? '批注已删除。' : '批注已保存，仅自己可见。');
    } catch (error) {
      if (editor === context && live(reader)) $('#annotation-error').textContent = error.message || '批注保存失败，请重试。';
    } finally {
      if (editor === context) { context.busy = false; setEditorBusy(false); }
    }
  }

  async function open(bookId, { anchor, notes = false, expanded = false } = {}) {
    if (bookId !== getBook()?.id) return;
    if (current && live(current)) {
      const reader = current;
      if (expanded) setFocus(true);
      panel('evidence');
      if (notes) {
        reader.notesOpen = true;
        reader.noteFilter = null;
        renderNotes(reader);
      }
      if (anchor) await navigate(reader, { anchor });
      if (!reader.notesLoaded && !reader.notesLoading && live(reader)) await loadNotes(reader);
      return;
    }
    reset();
    const reader = { bookId, epoch: getEpoch(), navigation: 0, version: null, blocks: [], annotations: [],
      toc: [], anchor: null, total: 0, length: 0, notesOpen: notes, notesLoaded: false, notesLoading: false,
      errors: {}, loading: null, navigating: false, selection: null };
    current = reader;
    mount(reader);
    if (expanded) setFocus(true);
    panel('evidence');
    await navigate(reader, anchor ? { anchor } : {});
    if (live(reader)) await loadNotes(reader);
  }

  $('#reader-focus').addEventListener('click', () => setFocus(!focused));
  $('#reader-chat-minimize').addEventListener('click', () => setChatMinimized(true, true));
  $('#reader-chat-launcher').addEventListener('click', () => setChatMinimized(false, true));
  dragHandle.addEventListener('pointerdown', (event) => {
    if (!focused || minimized || event.button !== 0 || !event.isPrimary) return;
    const box = chat.getBoundingClientRect();
    drag = { id: event.pointerId, x: event.clientX - box.left, y: event.clientY - box.top };
    dragHandle.setPointerCapture(event.pointerId);
    chat.classList.add('reader-chat-dragging');
    dragHandle.focus({ preventScroll: true });
    event.preventDefault();
  });
  dragHandle.addEventListener('pointermove', (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    const bounds = desk.getBoundingClientRect();
    positionChat(event.clientX - bounds.left - drag.x, event.clientY - bounds.top - drag.y);
  });
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) dragHandle.addEventListener(name, endDrag);
  dragHandle.addEventListener('keydown', (event) => {
    const offsets = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (!focused || !offsets[event.key]) return;
    event.preventDefault();
    const box = chat.getBoundingClientRect();
    const bounds = desk.getBoundingClientRect();
    const [x, y] = offsets[event.key];
    const step = event.shiftKey ? 40 : 12;
    positionChat(box.left - bounds.left + x * step, box.top - bounds.top + y * step);
  });
  document.addEventListener('fullscreenchange', () => {
    if (document.fullscreenElement === document.documentElement) {
      if (nativeRequest) nativeOwned = true;
      if (!focused) exitNativeFullscreen();
    } else if (nativeOwned) {
      nativeOwned = false;
      if (focused && !nativeExiting) setFocus(false);
    }
    resizeFocus();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !focused || event.defaultPrevented || document.querySelector('dialog[open]')) return;
    event.preventDefault();
    setFocus(false);
  });
  window.addEventListener('resize', resizeFocus);
  window.visualViewport?.addEventListener('resize', resizeFocus);
  window.visualViewport?.addEventListener('scroll', resizeFocus);
  $('#annotation-form').addEventListener('submit', (event) => { event.preventDefault(); saveAnnotation(); });
  $('#annotation-delete').addEventListener('click', () => saveAnnotation(true));
  $('#annotation-close').addEventListener('click', () => $('#annotation-dialog').close());
  $('#annotation-dialog').addEventListener('cancel', (event) => { if (editor?.busy) event.preventDefault(); });
  $('#annotation-dialog').addEventListener('close', () => {
    editor = null;
    captureSelection();
    if (current?.highlightsPending && live(current)) {
      current.highlightsPending = false;
      refreshHighlights(current);
    }
  });
  document.addEventListener('selectionchange', captureSelection);
  return { open, reset, preserveForConversation, isFocused: () => focused };
}
