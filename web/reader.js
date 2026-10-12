// Source-backed reader. All positions use JavaScript's UTF-16 string offsets.
export function createReader({ api, element, findBook, getEpoch, getAuth, panel, toast, onAsk, onQuiz, onProgress }) {
  const $ = (selector) => document.querySelector(selector);
  let current = null;
  let editor = null;
  const colors = new Set(['yellow', 'blue', 'green']);
  // The reader owns its book identity; shelf switches reset it via
  // clearEvidence, so liveness only tracks the epoch of async callbacks.
  const live = (reader) => current === reader && reader.epoch === getEpoch();
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
  const typeSteps = [15, 17, 19, 21];
  let typeStep = 1;
  try {
    const saved = Number(localStorage.getItem('study.reader.fontSize'));
    if (typeSteps.includes(saved)) typeStep = typeSteps.indexOf(saved);
  } catch { /* Reading remains available when storage is blocked. */ }

  function updateType(reader) {
    if (reader.sourceFormat === 'pdf' && reader.pdf) {
      reader.shell.style.removeProperty('--reader-font-size');
      reader.typeValue.textContent = `${Math.round((reader.pdf.zoom || 1) * 100)}%`;
      reader.typeSmaller.disabled = zoomSteps[0] === (reader.pdf.zoom || 1);
      reader.typeLarger.disabled = zoomSteps.at(-1) === (reader.pdf.zoom || 1);
      return;
    }
    reader.shell.style.setProperty('--reader-font-size', `${typeSteps[typeStep]}px`);
    reader.typeValue.textContent = `${typeSteps[typeStep]}px`;
    reader.typeSmaller.disabled = typeStep === 0;
    reader.typeLarger.disabled = typeStep === typeSteps.length - 1;
  }

  function changeType(reader, delta) {
    if (reader.sourceFormat === 'pdf' && reader.pdf) {
      const current = reader.pdf.zoom || 1;
      const index = Math.max(0, Math.min(zoomSteps.length - 1, zoomSteps.indexOf(current) + delta));
      reader.pdf.zoom = zoomSteps[index];
      try { localStorage.setItem('study.reader.pdfZoom', String(reader.pdf.zoom)); }
      catch { /* The setting still works for this visit. */ }
      updateType(reader);
      relayoutPdf(reader);
      return;
    }
    typeStep = Math.max(0, Math.min(typeSteps.length - 1, typeStep + delta));
    updateType(reader);
    try { localStorage.setItem('study.reader.fontSize', String(typeSteps[typeStep])); }
    catch { /* The setting still works for this visit. */ }
  }

  async function startOver(reader) {
    reader.restoring = true;
    const data = await navigate(reader, { at: 0 });
    reader.restoring = false;
    if (data && live(reader)) savePosition(reader, 0, false, true);
  }

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
    // 展开阅读 is the in-page expanded layout only; the browser/system
    // fullscreen is a separate, explicit toggle (#reader-native).
    $('#reader-focus').textContent = expanded ? '收起阅读' : '展开阅读';
    // Reading starts with an unobstructed page; the tutor opens on demand.
    setChatMinimized(expanded);
    if (expanded) {
      panel('evidence');
      resizeFocus();
      current.scroll.focus({ preventScroll: true });
    } else {
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
          const visibleTarget = [target, $('#reader-focus'), $('#reader-native'), $('#question')]
            .find((node) => node?.isConnected && !node.disabled && node.getClientRects().length);
          visibleTarget?.focus({ preventScroll: true });
        };
        restoreFocus();
      }
      previousFocus = null;
    }
  }

  function releasePdf(reader) {
    reader?.pdf?.observer?.disconnect();
    reader?.pdf?.resizeObserver?.disconnect();
    clearTimeout(reader?.pdf?.relayoutTimer);
    if (reader?.pdf?.doc) reader.pdf.doc.destroy().catch(() => {});
  }

  function reset() {
    if (current && !current.restoring) rememberPosition(current, true);
    if (current?.frame) cancelAnimationFrame(current.frame);
    if (current?.positionTimer) clearTimeout(current.positionTimer);
    if (current?.syncTimer) clearTimeout(current.syncTimer);
    releasePdf(current);
    current = null;
    editor = null;
    hidePopover();
    if ($('#annotation-dialog').open) $('#annotation-dialog').close();
    $('#annotation-form').reset();
    $('#annotation-quote').textContent = '';
    $('#annotation-error').textContent = '';
    $('.evidence').classList.remove('reader-open');
    $('#reader-focus').hidden = true;
    $('#reader-native').hidden = true;
    setFocus(false, { restore: false });
  }

  function preserveForConversation() {
    if (!focused || !current) return false;
    if (current.epoch === getEpoch()) return true;
    // Replace the reader identity so aborted callbacks cannot mutate its successor.
    const old = current;
    const scrollTop = old.scroll.scrollTop;
    if (old.frame) cancelAnimationFrame(old.frame);
    if (old.positionTimer) clearTimeout(old.positionTimer);
    if (old.syncTimer) clearTimeout(old.syncTimer);
    const reader = { ...old, epoch: getEpoch(), frame: null, positionTimer: null, syncTimer: null,
      loading: null, navigating: false, anchorNeedle: null,
      notesLoading: false, selection: null, highlightsPending: false };
    current = reader;
    mount(reader);
    if (reader.sourceFormat === 'pdf') {
      // The PDF view owns canvases that cannot survive a re-mount; a fresh
      // navigate() rebuilds page placeholders from the cached document.
      navigate(reader, old.target || {});
    } else {
      reader.tocSelect.replaceChildren(...reader.toc.map((item) => new Option(item.title, item.start)));
      reader.stream.replaceChildren(...reader.blocks.map((block) => blockNode(reader, block)));
      setScrollTop(reader, scrollTop);
    }
    updateEdges(reader);
    updateProgress(reader);
    if (reader.sourceFormat !== 'pdf' && !reader.restoring) rememberPosition(reader);
    if (reader.sourceFormat !== 'pdf' && (old.navigating || !reader.version)) navigate(reader, old.target || {});
    loadNotes(reader);
    return true;
  }

  function keepPosition(reader, change) {
    const top = reader.scroll.getBoundingClientRect().top;
    const visible = [...reader.stream.children].find((node) => node.getBoundingClientRect().bottom > top);
    const oldTop = visible?.getBoundingClientRect().top;
    change();
    if (visible?.isConnected) setScrollTop(reader, reader.scroll.scrollTop + visible.getBoundingClientRect().top - oldTop);
  }

  function setScrollTop(reader, top) {
    // Programmatic navigation must finish before notes refresh or scroll
    // tracking runs; CSS smooth scrolling can otherwise cancel the jump.
    const previous = reader.scroll.style.scrollBehavior;
    reader.scroll.style.scrollBehavior = 'auto';
    reader.scroll.scrollTop = top;
    reader.scroll.style.scrollBehavior = previous;
  }

  function mount(reader) {
    $('.evidence').classList.add('reader-open');
    $('#reader-focus').hidden = false;
    $('#reader-native').hidden = false;
    const shell = element('div', 'reader-shell');
    reader.shell = shell;
    const tools = element('div', 'reader-tools');
    tools.append(element('h3', 'reader-book-title', reader.title));
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
    // Ask the tutor about the current passage; disabled alongside annotate.
    reader.askButton = button('问 AI 此段', () => {
      if (!reader.selection) return;
      onAsk?.({ quote: reader.selection.quote, section: passageSection(reader) });
    });
    reader.askButton.disabled = true;
    reader.askButton.addEventListener('pointerdown', (event) => event.preventDefault());
    actions.append(reader.notesButton, reader.annotateButton, reader.askButton,
                   button('从头阅读', () => startOver(reader), 'text-button'));
    const typeControls = element('div', 'reader-type-controls');
    typeControls.setAttribute('role', 'group');
    typeControls.setAttribute('aria-label', '正文字号');
    reader.typeSmaller = button('A−', () => changeType(reader, -1), 'text-button');
    reader.typeSmaller.setAttribute('aria-label', '缩小正文字号');
    reader.typeValue = element('span', 'reader-type-value');
    reader.typeLarger = button('A+', () => changeType(reader, 1), 'text-button');
    reader.typeLarger.setAttribute('aria-label', '放大正文字号');
    typeControls.append(reader.typeSmaller, reader.typeValue, reader.typeLarger);
    actions.append(typeControls);
    tools.append(actions);
    updateType(reader);
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
    // Track pointer state on the whole document: the drag may leave the
    // scroll box, and touch selection also fires pointerup when lifted.
    // Never hide on presses INSIDE the popover: destroying it on pointerdown
    // detaches the buttons before their click events fire, making
    // 高亮批注 / 问 AI dead on click.
    const onPointerDown = (event) => {
      pointerDown = true;
      if (!popover?.contains(event.target)) hidePopover();
    };
    const onPointerUp = () => {
      pointerDown = false;
      // Arm the toolbar; captureSelection (which follows on selectionchange)
      // shows it when the selection is intact.
      popoverArmed = true;
      if (current && live(current) && current.selection) showPopover(current, current.selection);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('pointerup', onPointerUp, true);
    document.addEventListener('pointercancel', () => { pointerDown = false; });
    reader.progress = element('div', 'reader-progress');
    reader.progressText = element('span', '', '正在读取正文…');
    reader.syncStatus = element('span', 'reader-sync-status');
    reader.syncStatus.setAttribute('aria-live', 'polite');
    reader.progress.append(reader.progressText, reader.syncStatus, button('自测本节', () => {
      if (focused) setChatMinimized(false);
      onQuiz?.({ section: passageSection(reader) });
    }, 'text-button'));
    reader.scroll.addEventListener('scroll', () => {
      hidePopover();
      if (reader.frame) return;
      reader.frame = requestAnimationFrame(() => {
        reader.frame = null;
        if (!live(reader)) return;
        updateProgress(reader);
        const stamp = Date.now();
        if (!reader.restoring) {
          const elapsed = stamp - (reader.positionSavedAt || 0);
          if (elapsed >= 3000) {
            if (reader.positionTimer) clearTimeout(reader.positionTimer);
            reader.positionTimer = null;
            reader.positionSavedAt = stamp;
            rememberPosition(reader);
          } else if (!reader.positionTimer) {
            reader.positionTimer = setTimeout(() => {
              reader.positionTimer = null;
              if (!live(reader) || reader.restoring) return;
              reader.positionSavedAt = Date.now();
              rememberPosition(reader);
            }, 3000 - elapsed);
          }
        }
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
    if (reader.sourceFormat === 'pdf') {
      reader.before.disabled = reader.after.disabled = true;
      reader.before.textContent = '原书第 1 页';
      reader.after.textContent = `共 ${reader.pageMap?.length || 0} 页`;
      reader.tocSelect.disabled = !reader.version || reader.navigating;
      reader.scroll.setAttribute('aria-busy', String(reader.navigating || Boolean(reader.loading)));
      return;
    }
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

  function visibleOffset(reader, article, block) {
    const content = article?.querySelector('.reader-text');
    if (!content) return block.start;
    const rect = content.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) return block.start;
    const top = reader.scroll.getBoundingClientRect().top;
    const x = Math.min(rect.right - 2, rect.left + 12);
    const y = Math.max(rect.top + 1, Math.min(top + 24, rect.bottom - 1));
    const caret = document.caretPositionFromPoint?.(x, y);
    const range = caret ? null : document.caretRangeFromPoint?.(x, y);
    const target = caret?.offsetNode || range?.startContainer;
    const offset = caret?.offset ?? range?.startOffset;
    if (!target || target.nodeType !== Node.TEXT_NODE || !content.contains(target)) return block.start;
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    let local = 0;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node === target) { local += offset; break; }
      local += node.length;
    }
    local = Math.max(0, Math.min(local, block.text.length - 1));
    const code = block.text.charCodeAt(local);
    if (local && code >= 0xDC00 && code <= 0xDFFF) local -= 1;
    return block.start + local;
  }

  function updateProgress(reader) {
    if (reader.sourceFormat === 'pdf') {
      if (!reader.pageMap?.length) {
        reader.progressText.textContent = '正在载入原书页面…';
        return;
      }
      const entry = pdfTopPage(reader);
      const atEnd = entry === reader.pageMap.at(-1)
        && reader.scroll.scrollHeight - reader.scroll.scrollTop - reader.scroll.clientHeight < 4;
      // Self-heal: if the visible page somehow stayed a placeholder (an
      // observer miss, a swallowed error), kick a render — throttled so a
      // genuinely broken page cannot spin.
      const visible = reader.scroll.querySelector(`[data-pdf-page="${entry.pdf_page}"]`);
      if (visible && !visible.dataset.rendered && Date.now() - (visible.__kick || 0) > 1500) {
        visible.__kick = Date.now();
        renderPdfPage(reader, visible);
      }
      const index = reader.pageMap.indexOf(entry) + 1;
      reader.progressText.textContent = `原书第 ${index} / ${reader.pageMap.length} 页 · 原书版式`;
      reader.tocSelect.value = String(entry.start);
      return;
    }
    if (!reader.blocks.length) {
      reader.progressText.textContent = '全文阅读不调用模型；OCR 错误请对照源文件。';
      return;
    }
    const top = reader.scroll.getBoundingClientRect().top;
    const node = [...reader.stream.children].find((item) => item.getBoundingClientRect().bottom > top);
    const block = reader.blocks.find((item) => item.index === Number(node?.dataset.index)) || reader.blocks[0];
    const atEnd = reader.blocks.at(-1).index === reader.total - 1
      && reader.scroll.scrollHeight - reader.scroll.scrollTop - reader.scroll.clientHeight < 4;
    const at = atEnd ? reader.length : visibleOffset(reader, node, block);
    reader.progressText.textContent = `正文位置 ${atEnd ? 100 : Math.min(99, Math.floor(at / reader.length * 100))}% · OCR 原文，非原书页码`;
    const section = reader.toc.filter((item) => item.start <= block.start).at(-1);
    if (section) reader.tocSelect.value = String(section.start);
  }

  function passageSection(reader) {
    // Chapter title covering the current viewport, for ask-context.
    if (reader.sourceFormat === 'pdf') return pdfTopPage(reader)?.label || '';
    if (!reader.toc?.length || !reader.blocks.length) return '';
    const top = reader.scroll.getBoundingClientRect().top;
    const node = [...reader.stream.children].find((item) => item.getBoundingClientRect().bottom > top);
    const block = reader.blocks.find((item) => item.index === Number(node?.dataset.index)) || reader.blocks[0];
    const section = reader.toc.filter((item) => item.start <= block.start).at(-1);
    return section?.title || '';
  }

  function positionKey(reader) {
    return `study.read.${reader.userId}.${reader.bookId}`;
  }

  function savePosition(reader, start, finished = false, force = false) {
    if (!reader.userId || !reader.version || !reader.length) return;
    const savedAt = new Date().toISOString();
    const progress = { version: reader.version, start, length: reader.length, finished,
      percent: finished ? 100 : Math.min(99, Math.floor(start * 100 / reader.length)), updated_at: savedAt };
    let localSaved = false;
    try {
      localStorage.setItem(positionKey(reader), JSON.stringify({ ...progress, savedAt }));
      localSaved = true;
    } catch { /* Reading still works if storage is unavailable. */ }
    onProgress?.({ bookId: reader.bookId, userId: reader.userId, progress });
    const version = reader.version;
    const signature = `${version}:${start}:${Number(finished)}`;
    reader.latestPositionSignature = signature;
    if (signature === reader.lastRemotePosition) {
      if (live(reader)) reader.syncStatus.textContent = '已同步';
      return;
    }
    const stamp = Date.now();
    if (!force && stamp - (reader.remoteSavedAt || 0) < 12000) {
      if (live(reader)) reader.syncStatus.textContent = '待同步';
      if (!reader.syncTimer) reader.syncTimer = setTimeout(() => {
        reader.syncTimer = null;
        if (live(reader) && !reader.restoring) rememberPosition(reader, true);
      }, 12000 - (stamp - reader.remoteSavedAt));
      return;
    }
    if (reader.syncTimer) clearTimeout(reader.syncTimer);
    reader.syncTimer = null;
    reader.remoteSavedAt = stamp;
    if (live(reader)) reader.syncStatus.textContent = '同步中…';
    const save = async () => {
      const response = await fetch(`/api/books/${reader.bookId}/reading-progress`, {
        method: 'PUT', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': reader.csrf },
        body: JSON.stringify({ version, start, finished }),
      });
      if (!response.ok) throw new Error('阅读进度保存失败');
      reader.lastRemotePosition = signature;
      if (live(reader) && reader.version === version) {
        reader.syncStatus.textContent = reader.latestPositionSignature === signature ? '已同步' : '待同步';
      }
    };
    reader.saveQueue = (reader.saveQueue || Promise.resolve()).catch(() => {}).then(save);
    reader.saveQueue.catch(() => {
      if (live(reader) && reader.latestPositionSignature === signature) {
        reader.syncStatus.textContent = localSaved ? '仅本机保存' : '保存失败';
      }
    });
  }

  function rememberPosition(reader, force = false) {
    if (!reader.version) return;
    if (reader.sourceFormat === 'pdf') {
      if (!reader.pageMap?.length) return;
      const entry = pdfTopPage(reader);
      const atEnd = entry === reader.pageMap.at(-1)
        && reader.scroll.scrollHeight - reader.scroll.scrollTop - reader.scroll.clientHeight < 4;
      savePosition(reader, atEnd ? reader.length : entry.start, atEnd, force);
      return;
    }
    if (!reader.blocks.length) return;
    const top = reader.scroll.getBoundingClientRect().top;
    const node = [...reader.stream.children].find((item) => item.getBoundingClientRect().bottom > top);
    const block = reader.blocks.find((item) => item.index === Number(node?.dataset.index));
    if (!block) return;
    const finished = reader.blocks.at(-1).index === reader.total - 1
      && reader.scroll.scrollHeight - reader.scroll.scrollTop - reader.scroll.clientHeight < 4;
    savePosition(reader, finished ? reader.blocks.at(-1).start : visibleOffset(reader, node, block), finished, force);
  }

  function recallPosition(reader) {
    try {
      const scoped = localStorage.getItem(positionKey(reader));
      const legacy = reader.legacyLocalAllowed ? localStorage.getItem(`study.read.${reader.bookId}`) : null;
      const saved = JSON.parse(scoped || legacy || 'null');
      if (saved && Number.isSafeInteger(saved.start) && saved.start >= 0
        && typeof saved.version === 'string') return saved;
    } catch { /* Ignore malformed entries. */ }
    return null;
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
    if (reader.sourceFormat === 'pdf') {
      refreshPdfHighlights(reader);
      return;
    }
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
    setScrollTop(reader, reader.scroll.scrollTop + rect.top - reader.scroll.getBoundingClientRect().top - 24);
    reader.scroll.focus({ preventScroll: true });
    updateProgress(reader);
  }

  function citationNeedle(reader, anchor) {
    const block = reader.blocks.find((item) => item.start <= anchor.start && anchor.end <= item.end);
    return block ? block.text.slice(anchor.start - block.start, anchor.end - block.start) : null;
  }

  // --- PDF mode: original pages via self-hosted PDF.js ---------------------
  // The server keeps one canonical text (pypdf) for chunks, citations and
  // annotations; this view only *displays* the original pages. Client/server
  // text can differ by whitespace, so every highlight match is folded
  // (whitespace stripped) — the server's locate() applies the same fold.
  let pdfjsPromise = null;

  function loadPdfjs() {
    if (!pdfjsPromise) {
      pdfjsPromise = import('/assets/vendor/pdfjs/pdf.min.mjs').then((pdfjs) => {
        pdfjs.GlobalWorkerOptions.workerSrc = '/assets/vendor/pdfjs/pdf.worker.min.mjs';
        return pdfjs;
      });
    }
    return pdfjsPromise;
  }

  // Strip whitespace plus the invisible artifacts OCR text layers sprinkle
  // in: soft hyphens, zero-width spaces/joiners, BOM and replacement chars.
  const FOLD_INVISIBLE = /[\s\u00ad\u200b\u200c\u200d\ufeff\ufffd]+/g;

  function foldText(text) {
    return text.replace(FOLD_INVISIBLE, '');
  }

  // Folded index over a rendered text layer: one entry per kept character,
  // pointing at its DOM text node + offset so a folded match becomes a Range.
  // Hot path: parallel plain arrays + charCode whitespace test, no per-char
  // objects or regex calls — dense pages fold in a fraction of the time.
  function foldLayer(container) {
    let folded = '';
    const nodes = [];
    const offsets = [];
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const value = node.nodeValue;
      for (let i = 0; i < value.length; i += 1) {
        const code = value.charCodeAt(i);
        // Whitespace plus the invisible artifacts OCR layers emit (soft
        // hyphen, zero-width chars, BOM, replacement char).
        if (code === 32 || (code >= 9 && code <= 13) || code === 0xa0 || code === 0x3000
          || code === 0xad || (code >= 0x200b && code <= 0x200d) || code === 0xfeff || code === 0xfffd) continue;
        nodes.push(node);
        offsets.push(i);
        folded += value[i];
      }
    }
    return { folded, nodes, offsets };
  }

  function overlayMarks(container, overlay, ranges) {
    // Rebuild the highlight overlay from a list of {range, className}.
    overlay.replaceChildren();
    const base = overlay.getBoundingClientRect();
    for (const { range, className, note } of ranges) {
      for (const rect of range.getClientRects()) {
        if (!rect.width && !rect.height) continue;
        const mark = element('div', `reader-pdf-mark ${className}`);
        mark.style.left = `${rect.left - base.left}px`;
        mark.style.top = `${rect.top - base.top}px`;
        mark.style.width = `${rect.width}px`;
        mark.style.height = `${rect.height}px`;
        if (note) {
          mark.title = note.note || note.quote.slice(0, 80);
          mark.setAttribute('role', 'button');
          mark.setAttribute('tabindex', '0');
          const view = () => { if (window.getSelection()?.isCollapsed) openEditor(current, note); };
          mark.addEventListener('click', view);
          mark.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); view(); }
          });
        }
        overlay.append(mark);
      }
    }
  }

  function pageHighlightRanges(reader, entry, layer) {
    const ranges = [];
    const span = (start, end) => entry.start <= end && entry.end >= start;
    const toRange = (at, length) => {
      const fromNode = layer.nodes[at];
      const toNode = layer.nodes[at + length - 1];
      if (!fromNode || !toNode || !fromNode.isConnected || !toNode.isConnected) return null;
      const range = document.createRange();
      range.setStart(fromNode, layer.offsets[at]);
      range.setEnd(toNode, layer.offsets[at + length - 1] + 1);
      return range;
    };
    // Whitespace folding covers most PDF.js-vs-server differences; OCR text
    // layers can also differ in case. Retry case-insensitively, and surface
    // a failed match instead of dropping it silently.
    const looseNeedles = new Map();
    const looseNeedle = (text) => {
      const key = foldText(text);
      if (!looseNeedles.has(key)) looseNeedles.set(key, key.toLowerCase());
      return looseNeedles.get(key);
    };
    const foldedLayer = () => {
      if (!layer.loose) layer.loose = layer.folded.toLowerCase();
      return layer.loose;
    };
    for (const note of reader.annotations) {
      if (note.version !== reader.version || !span(note.start, note.end)) continue;
      const needle = foldText(note.quote);
      let at = layer.folded.indexOf(needle);
      if (at < 0) at = foldedLayer().indexOf(looseNeedle(note.quote));
      if (at < 0) {
        console.warn('批注未能定位到本页文字层', entry.label, note.quote.slice(0, 40));
        continue;
      }
      const range = toRange(at, needle.length);
      if (range) ranges.push({ range, className: `reader-highlight highlight-${colors.has(note.color) ? note.color : 'yellow'}`, note });
    }
    if (reader.anchor && reader.anchorNeedle && span(reader.anchor.start, reader.anchor.end)) {
      const needle = foldText(reader.anchorNeedle);
      let at = layer.folded.indexOf(needle);
      if (at < 0) at = foldedLayer().indexOf(looseNeedle(reader.anchorNeedle));
      const range = at >= 0 ? toRange(at, needle.length) : null;
      if (range) ranges.push({ range, className: 'reader-citation' });
    }
    return ranges;
  }

  function pageHasHighlights(reader, entry) {
    const overlap = (start, end) => entry.start <= end && entry.end >= start;
    return reader.annotations.some((note) => note.version === reader.version && overlap(note.start, note.end))
      || Boolean(reader.anchor && reader.anchorNeedle && overlap(reader.anchor.start, reader.anchor.end));
  }

  function applyPdfOverlays(reader, node, entry, textLayer, overlay) {
    const ranges = pageHighlightRanges(reader, entry, foldLayer(textLayer));
    overlayMarks(node, overlay, ranges);
    // When the text layer is too noisy to place a highlight, still surface
    // that annotations exist on this page instead of dropping them silently.
    const overlap = (start, end) => entry.start <= end && entry.end >= start;
    const expected = reader.annotations.filter((note) => note.version === reader.version
      && overlap(note.start, note.end)).length;
    node.querySelector('.reader-pdf-pageflag')?.remove();
    if (expected && !ranges.some((item) => item.note)) {
      const flag = element('button', 'reader-pdf-pageflag', `本页 ${expected} 条批注 · 点击查看`);
      flag.type = 'button';
      flag.addEventListener('click', () => {
        if (!live(reader)) return;
        reader.notesOpen = true;
        reader.noteFilter = null;
        renderNotes(reader);
      });
      node.append(flag);
    }
  }

  // Page width: fit the pane but cap it so a maximized/fullscreen window
  // does not blow pages up to wall size; the A−/A+ controls scale around it.
  const PDF_MAX_WIDTH = 860;
  const zoomSteps = [0.7, 0.85, 1, 1.2, 1.45];

  function pdfPageWidth(reader) {
    const fit = Math.max(240, reader.scroll.clientWidth - 28);
    return Math.min(fit, PDF_MAX_WIDTH) * (reader.pdf?.zoom || 1);
  }

  function relayoutPdf(reader) {
    const pdf = reader.pdf;
    if (!pdf?.doc || !pdf.base) return;
    // Page heights change with the width; keep the reader on the same page.
    const anchor = pdfTopPage(reader);
    const width = pdfPageWidth(reader);
    pdf.scale = width / pdf.base.width;
    for (const node of reader.scroll.querySelectorAll('.reader-pdf-page')) {
      node.style.width = `${width}px`;
      if (node.dataset.rendered === 'done' || node.dataset.rendered === 'pending') {
        // A render still in flight shares the canvas with any new render and
        // PDF.js rejects that ("multiple render() operations"), leaving a
        // blank canvas behind — cancel it before resetting the page.
        node.__task?.cancel?.();
        node.__task = null;
        node.__renderId = (node.__renderId || 0) + 1;
        node.dataset.rendered = '';
        node.style.aspectRatio = `${pdf.base.width} / ${pdf.base.height}`;
        node.style.height = '';
        node.replaceChildren(element('span', 'reader-pdf-loading'));
        // Re-observe: an unchanged, still-intersecting box never re-fires the
        // observer, so the reset page would stay a placeholder forever.
        pdf.observer?.unobserve(node);
        pdf.observer?.observe(node);
      }
    }
    if (anchor) {
      const node = reader.scroll.querySelector(`[data-pdf-page="${anchor.pdf_page}"]`);
      if (node) setScrollTop(reader, reader.scroll.scrollTop + node.getBoundingClientRect().top
        - reader.scroll.getBoundingClientRect().top - 16);
    }
  }

  function scheduleRelayout(reader) {
    clearTimeout(reader.pdf?.relayoutTimer);
    reader.pdf.relayoutTimer = setTimeout(() => {
      // Widths near zero come from display:none transitions, not real panes;
      // everything else (the normal ~300px evidence pane included) is honest.
      if (live(reader) && reader.scroll.clientWidth >= 120) relayoutPdf(reader);
    }, 250);
  }

  async function mountPdf(reader) {
    if (!reader.pdf) {
      let zoom = 1;
      try {
        const saved = Number(localStorage.getItem('study.reader.pdfZoom'));
        if (zoomSteps.includes(saved)) zoom = saved;
      } catch { /* Reading remains available when storage is blocked. */ }
      reader.pdf = { doc: null, loadedVersion: null, observer: null, zoom };
    }
    const pdf = reader.pdf;
    reader.shell.classList.add('reader-pdf');
    if (!pdf.doc || pdf.loadedVersion !== reader.version) {
      reader.hint.textContent = '正在载入原书页面…';
      const [pdfjs, response] = await Promise.all([
        loadPdfjs(),
        fetch(`/api/books/${reader.bookId}/source?inline=1`, { credentials: 'same-origin' }),
      ]);
      if (!response.ok) throw new Error('原书文件读取失败。');
      const buffer = await response.arrayBuffer();
      if (pdf.doc) pdf.doc.destroy().catch(() => {});
      pdf.doc = await pdfjs.getDocument({
        data: buffer,
        cMapUrl: '/assets/vendor/pdfjs/cmaps/', cMapPacked: true,
        standardFontDataUrl: '/assets/vendor/pdfjs/standard_fonts/',
      }).promise;
      pdf.pdfjs = pdfjs;
      pdf.loadedVersion = reader.version;
    }
    if (pdf.observer) pdf.observer.disconnect();
    pdf.observer = new IntersectionObserver((records) => {
      for (const record of records) {
        if (record.isIntersecting) {
          renderPdfPage(reader, record.target);
        } else if (record.target.dataset.rendered === 'done') {
          // Evict far-away pages back to placeholders: rendered canvases are
          // megabytes each, and a long book otherwise accumulates them all.
          record.target.__task?.cancel?.();
          record.target.__task = null;
          record.target.__renderId = (record.target.__renderId || 0) + 1;
          record.target.dataset.rendered = '';
          record.target.style.aspectRatio = `${pdf.base.width} / ${pdf.base.height}`;
          record.target.style.height = '';
          record.target.replaceChildren(element('span', 'reader-pdf-loading'));
        }
      }
    }, { root: reader.scroll, rootMargin: '600px 0px' });
    // Fullscreen enter/exit and pane resizes re-fit the pages.
    pdf.resizeObserver?.disconnect();
    pdf.resizeObserver = new ResizeObserver(() => scheduleRelayout(reader));
    pdf.resizeObserver.observe(reader.scroll);
    // The observer can miss the settled size after a fullscreen transition
    // (transitional widths are skipped above), so re-measure explicitly.
    if (!pdf.layoutListeners) {
      pdf.layoutListeners = true;
      const resettle = () => { if (current?.pdf && live(current)) setTimeout(() => scheduleRelayout(current), 400); };
      window.addEventListener('resize', resettle);
      document.addEventListener('fullscreenchange', resettle);
    }
    // Uniform placeholder aspect from the first page; actual renders fix up
    // per-page heights, so mixed page sizes only wobble while loading.
    const first = await pdf.doc.getPage(1);
    const base = first.getViewport({ scale: 1 });
    pdf.base = { width: base.width, height: base.height };
    const width = pdfPageWidth(reader);
    pdf.scale = width / base.width;
    const fragment = document.createDocumentFragment();
    for (const entry of reader.pageMap) {
      const page = element('div', 'reader-pdf-page');
      page.dataset.pdfPage = String(entry.pdf_page);
      page.style.aspectRatio = `${base.width} / ${base.height}`;
      page.style.width = `${width}px`;
      page.append(element('span', 'reader-pdf-loading', entry.label));
      fragment.append(page);
    }
    reader.stream.replaceChildren(fragment);
    reader.scroll.querySelectorAll('.reader-pdf-page').forEach((node) => pdf.observer.observe(node));
    updateType(reader);
  }

  async function renderPdfPage(reader, node) {
    const pdf = reader.pdf;
    if (!pdf?.doc || !live(reader) || node.dataset.rendered) return;
    const entry = reader.pageMap.find((item) => item.pdf_page === Number(node.dataset.pdfPage));
    if (!entry) return;
    node.dataset.rendered = 'pending';
    // A relayout/eviction can reset the page mid-render (placeholder swapped
    // back in). Each attempt carries a token; waking up after an await with
    // a stale token must NOT mark the reset page as rendered — that left
    // permanently-white "done" pages.
    const attempt = (node.__renderId = (node.__renderId || 0) + 1);
    const ours = () => node.__renderId === attempt && node.dataset.rendered === 'pending' && node.isConnected;
    try {
      const page = await pdf.doc.getPage(entry.pdf_page + 1);
      if (!ours()) return;
      const viewport = page.getViewport({ scale: pdf.scale });
      const canvas = element('canvas', 'reader-pdf-canvas');
      // Render sharp, but cap the backing store: full devicePixelRatio on
      // 4K monitors quadruples raster work for little visible gain, and huge
      // canvases are the main source of scroll jank on long books.
      let ratio = Math.min(window.devicePixelRatio || 1, 1.5);
      if (viewport.width * viewport.height * ratio * ratio > 4_200_000) ratio = 1;
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      const render = page.render({ canvasContext: canvas.getContext('2d', { alpha: false }), viewport, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null });
      node.__task = render;
      const textLayer = element('div', 'reader-pdf-text');
      const overlay = element('div', 'reader-pdf-overlay');
      node.replaceChildren(canvas, textLayer, overlay);
      node.style.aspectRatio = '';
      node.style.height = `${viewport.height}px`;
      await render.promise;
      if (!ours()) { render.cancel?.(); return; }
      const layer = new pdf.pdfjs.TextLayer({
        textContentSource: page.streamTextContent(),
        container: textLayer, viewport,
      });
      await layer.render();
      if (!ours()) return;
      node.dataset.rendered = 'done';
      node.__task = null;
      // Skip the folded-highlight computation entirely on pages with nothing
      // to highlight — the common case while flipping through a book.
      if (pageHasHighlights(reader, entry)) {
        applyPdfOverlays(reader, node, entry, textLayer, overlay);
      }
    } catch (error) {
      node.__task = null;
      // A cancelled or superseded render (page reset by relayout/eviction)
      // is not a failure; the page stays a placeholder awaiting its next turn.
      if (!ours()) return;
      node.dataset.rendered = '';
      node.append(element('span', 'reader-pdf-error', '本页渲染失败，滚动重试。'));
    }
  }

  function refreshPdfHighlights(reader) {
    for (const node of reader.scroll.querySelectorAll('.reader-pdf-page[data-rendered="done"]')) {
      const entry = reader.pageMap.find((item) => item.pdf_page === Number(node.dataset.pdfPage));
      const textLayer = node.querySelector('.reader-pdf-text');
      const overlay = node.querySelector('.reader-pdf-overlay');
      if (entry && textLayer && overlay) applyPdfOverlays(reader, node, entry, textLayer, overlay);
    }
  }

  function pdfTopPage(reader) {
    // Runs on every scroll frame: pages are in document order, so a binary
    // search needs ~log2(N) rect reads instead of touching all 300+ nodes.
    const nodes = reader.scroll.querySelectorAll('.reader-pdf-page');
    if (!nodes.length) return reader.pageMap[0];
    const top = reader.scroll.getBoundingClientRect().top + 24;
    let low = 0;
    let high = nodes.length - 1;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (nodes[middle].getBoundingClientRect().bottom > top) high = middle;
      else low = middle + 1;
    }
    const pdfPage = Number(nodes[low].dataset.pdfPage);
    return reader.pageMap.find((item) => item.pdf_page === pdfPage) || reader.pageMap[0];
  }

  function pdfJumpTo(reader, offset) {
    const entry = reader.pageMap.filter((item) => item.start <= offset).at(-1) || reader.pageMap[0];
    const node = reader.scroll.querySelector(`[data-pdf-page="${entry.pdf_page}"]`);
    if (node) {
      setScrollTop(reader, reader.scroll.scrollTop + node.getBoundingClientRect().top
        - reader.scroll.getBoundingClientRect().top - 16);
    }
    reader.scroll.focus({ preventScroll: true });
    updateProgress(reader);
  }

  function capturePdfSelection(reader, selection) {
    reader.selection = null;
    if (selection && !selection.isCollapsed && selection.rangeCount === 1) {
      const range = selection.getRangeAt(0);
      const page = (node) => node.nodeType === Node.ELEMENT_NODE ? node.closest?.('.reader-pdf-page')
        : node.parentElement?.closest('.reader-pdf-page');
      const startPage = page(range.startContainer);
      const endPage = page(range.endContainer);
      if (startPage && startPage === endPage && reader.stream.contains(startPage)) {
        const quote = range.toString();
        const entry = reader.pageMap.find((item) => item.pdf_page === Number(startPage.dataset.pdfPage));
        if (quote && quote.trim().length >= 2 && quote.length <= 4000 && entry) {
          reader.selection = { version: reader.version, pdf_page: entry.pdf_page, quote };
          if (popoverArmed && !pointerDown) showPopover(reader, reader.selection);
          reader.hint.textContent = `已在${entry.label}选中 ${quote.length} 个字符；批注由服务器精确定位。`;
        } else if (quote.length > 4000) {
          reader.hint.textContent = '一次最多选择 4000 个字符，请缩小选文范围。';
        }
      }
    }
    reader.annotateButton.disabled = !reader.selection;
    reader.askButton.disabled = !reader.selection;
    if (!reader.selection && (!selection || selection.isCollapsed)) {
      reader.hint.textContent = reader.anchor ? '原书版式呈现；蓝色底纹为本次引用，选文可添加私人批注。'
        : '原书版式呈现；选中文字后可添加高亮或笔记，仅自己可见。';
      if (reader.highlightsPending) {
        reader.highlightsPending = false;
        refreshPdfHighlights(reader);
      }
    }
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
      reader.sourceFormat = data.source_format;
      reader.pageMap = data.pages || null;
      reader.errors = {};
      reader.tocSelect.replaceChildren(...data.toc.map((item) => new Option(item.title, item.start)));
      if (reader.sourceFormat === 'pdf') {
        // Citation quote for the PDF highlight: the anchor slice out of the
        // block window that the same response carried.
        reader.anchorNeedle = reader.anchor ? citationNeedle(reader, reader.anchor) : null;
        await mountPdf(reader);
      } else {
        reader.stream.replaceChildren(...data.blocks.map((block) => blockNode(reader, block)));
        setScrollTop(reader, 0);
      }
      reader.hint.textContent = reader.sourceFormat === 'pdf'
        ? '原书版式呈现；蓝色底纹为本次引用，选中文字可添加私人批注。'
        : data.anchor ? '蓝色底纹为本次引用；可上下连续阅读，选文添加私人批注。' : '选中文字后，可添加高亮或笔记；仅自己可见。';
      renderNotes(reader);
      const target = params.at ?? data.anchor?.start ?? 0;
      if (reader.sourceFormat === 'pdf') {
        if (!params.anchor && !('at' in params)) await pdfJumpTo(reader, 0);
        else requestAnimationFrame(() => { if (live(reader) && navigation === reader.navigation) pdfJumpTo(reader, target); });
      } else {
        setScrollTop(reader, 0);
        requestAnimationFrame(() => { if (live(reader) && navigation === reader.navigation) jumpTo(reader, target); });
      }
      return data;
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
    if (!live(reader) || reader.sourceFormat === 'pdf' || reader.loading || reader.navigating || !reader.blocks.length) return;
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
        button('追问', () => onAsk?.({ quote: note.quote, section: passageSection(reader) }), 'text-button'),
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

  // Floating selection toolbar: appears next to the mouse-up point and offers
  // annotate / ask without reaching for the top buttons (which stay as the
  // accessible fallback). It may only appear while the pointer is up: mid-drag
  // selectionchange events never surface it, so long drags stay uninterrupted.
  let popover = null;
  let pointerDown = false;
  let popoverArmed = false;

  function hidePopover() {
    popoverArmed = false;
    if (popover) popover.remove();
    popover = null;
  }

  function showPopover(reader, selection) {
    // Idempotent for the same selection: the global pointerup re-invokes this
    // on every click, and rebuilding the popover mid-click redirects the
    // click to the common ancestor (dead buttons). selectionchange also
    // re-creates the selection OBJECT for identical text, so compare by
    // content, not identity.
    const signature = `${selection.pdf_page ?? ''}:${selection.start ?? ''}:${selection.quote}`;
    if (popover && popover.__signature === signature) return;
    hidePopover();
    popover = element('div', 'reader-popover');
    popover.__signature = signature;
    popover.setAttribute('role', 'toolbar');
    popover.setAttribute('aria-label', '选文操作');
    const highlight = button('高亮批注', () => {
      hidePopover();
      openEditor(reader, null, selection);
    });
    highlight.type = 'button';
    const ask = button('问 AI', () => {
      hidePopover();
      onAsk?.({ quote: selection.quote, section: passageSection(reader) });
    });
    ask.type = 'button';
    // A press on a popover button must not let the browser collapse the text
    // selection — that fires selectionchange → hidePopover before the click
    // lands. Cancel the default on MOUSEDOWN (pointerdown's preventDefault
    // would also suppress the click event itself).
    for (const node of [highlight, ask]) {
      node.addEventListener('mousedown', (event) => event.preventDefault());
    }
    popover.append(highlight, ask);
    document.body.append(popover);
    // Place it above the selection end, flipping below when near the top.
    const box = popover.getBoundingClientRect();
    const range = window.getSelection().getRangeAt(0);
    const anchor = range.getBoundingClientRect();
    // Collapsed rects (empty ranges) fall back to the focus point's parent.
    const spot = anchor.width || anchor.height ? anchor
      : (range.startContainer.parentElement?.getBoundingClientRect() || anchor);
    const left = Math.min(Math.max(8, spot.left + spot.width / 2 - box.width / 2),
                          window.innerWidth - box.width - 8);
    const above = spot.top > box.height + 12;
    popover.style.left = `${Math.round(left)}px`;
    popover.style.top = `${Math.round(above ? spot.top - box.height - 8 : spot.bottom + 8)}px`;
    popover.classList.toggle('below', !above);
    highlight.focus({ preventScroll: true });
  }

  function captureSelection() {
    const reader = current;
    if (!reader || !live(reader) || $('#annotation-dialog').open || reader.navigating) {
      hidePopover();
      return;
    }
    if (reader.sourceFormat === 'pdf') {
      reader.selection = null;
      const selection = window.getSelection();
      if (pointerDown) hidePopover();
      capturePdfSelection(reader, selection);
      return;
    }
    reader.selection = null;
    const selection = window.getSelection();
    // A fresh drag clears the popover immediately; the toolbar is re-armed on
    // pointerup (below), so mid-drag events can never surface it early.
    if (pointerDown) hidePopover();
    // Touch handles (iOS/Android drag knobs) mutate an existing selection
    // without pointer events: a popover pointing at a stale selection closes.
    if (popover && (!selection || selection.isCollapsed || selection.rangeCount !== 1)) hidePopover();
    if (selection && !selection.isCollapsed && selection.rangeCount === 1) {
      const range = selection.getRangeAt(0);
      const start = endpoint(range.startContainer, range.startOffset, reader);
      const end = endpoint(range.endContainer, range.endOffset, reader);
      if (start !== null && end !== null && end > start && end - start <= 4000) {
        const quote = reader.blocks.filter((block) => block.start < end && block.end > start)
          .map((block) => block.text.slice(Math.max(0, start - block.start), Math.min(block.text.length, end - block.start))).join('');
        if (quote.length === end - start && quote.trim()) {
          reader.selection = { version: reader.version, start, end, quote };
          // Surface the toolbar only when the pointer is already up (keyboard
          // selection or a just-finished drag whose pointerup came first).
          if (popoverArmed && !pointerDown) showPopover(reader, reader.selection);
        }
      }
      if (start !== null && end !== null && end - start > 4000) reader.hint.textContent = '一次最多选择 4000 个字符，请缩小选文范围。';
    }
    reader.annotateButton.disabled = !reader.selection;
    reader.askButton.disabled = !reader.selection;
    if (reader.selection) reader.hint.textContent = `已选 ${reader.selection.end - reader.selection.start} 个字符 · 可用选区旁的按钮高亮或提问。`;
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
    disarmDelete();
    $('#annotation-error').textContent = '';
    setEditorBusy(false);
    $('#annotation-dialog').showModal();
    $('#annotation-note').focus();
  }

  function setEditorBusy(busy) {
    for (const id of ['annotation-save', 'annotation-delete', 'annotation-close', 'annotation-note']) $(`#${id}`).disabled = busy;
    document.querySelectorAll('input[name="annotation-color"]').forEach((node) => { node.disabled = busy; });
  }

  // Two-step in-dialog delete confirmation: the first click arms the button
  // (red, "确认删除"), the second within the window deletes. No browser
  // confirm() dialogs.
  let deleteArmed = false;
  let deleteArmTimer = null;

  function disarmDelete() {
    deleteArmed = false;
    clearTimeout(deleteArmTimer);
    const button = $('#annotation-delete');
    button.classList.remove('armed');
    button.textContent = '删除批注';
  }

  async function saveAnnotation(remove = false) {
    const context = editor;
    if (!context || context.busy || !live(context.reader)) return;
    const { reader, annotation, selection } = context;
    if (remove && !annotation) return;
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
    // Citations carry their own book: a group answer can open a member paper
    // here while the study panel keeps showing the group conversation.
    if (current && live(current) && current.bookId === bookId) {
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
    const auth = getAuth();
    const reader = { bookId, title: findBook(bookId)?.title || bookId, epoch: getEpoch(), navigation: 0, version: null, blocks: [], annotations: [],
      toc: [], anchor: null, total: 0, length: 0, notesOpen: notes, notesLoaded: false, notesLoading: false,
      errors: {}, loading: null, navigating: false, selection: null, restoring: true,
      userId: auth.userId, csrf: auth.csrf, legacyLocalAllowed: !findBook(bookId)?.builtin };
    current = reader;
    mount(reader);
    if (expanded) setFocus(true);
    panel('evidence');
    const local = anchor ? null : recallPosition(reader);
    const data = await navigate(reader, anchor ? { anchor } : {});
    if (data && !anchor && live(reader)) {
      const remote = data.progress;
      const valid = (item) => item?.version === data.version && Number.isSafeInteger(item.start)
        && item.start >= 0 && item.start < data.length && (!item.length || item.length === data.length);
      const remoteValid = valid(remote);
      const localValid = valid(local);
      const localNewer = localValid && (!remoteValid
        || Date.parse(local.savedAt || '') > Date.parse(remote.updated_at || ''));
      const saved = localNewer ? local : remoteValid ? remote : null;
      if (saved?.start > 0) await navigate(reader, { at: saved.start, version: data.version });
      if (saved?.finished && live(reader)) requestAnimationFrame(() => {
        if (live(reader)) { setScrollTop(reader, reader.scroll.scrollHeight); updateProgress(reader); }
      });
      if (live(reader) && localNewer) savePosition(reader, saved.start, Boolean(saved.finished), true);
      else if (live(reader) && remote && !remoteValid) savePosition(reader, 0, false, true);
      else if (remoteValid) {
        reader.lastRemotePosition = `${remote.version}:${remote.start}:${Number(remote.finished)}`;
        reader.syncStatus.textContent = '已同步';
      }
    }
    reader.restoring = false;
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
      // Entering system fullscreen always implies the expanded layout.
      if (!focused && current && live(current)) setFocus(true);
    } else if (nativeOwned) {
      nativeOwned = false;
      // Leaving system fullscreen returns to the expanded in-page layout,
      // not all the way back to the study panel.
    }
    $('#reader-native').textContent = document.fullscreenElement ? '退出全屏' : '全屏';
    $('#reader-native').setAttribute('aria-pressed', String(Boolean(document.fullscreenElement)));
    resizeFocus();
  });
  $('#reader-native').addEventListener('click', () => {
    if (document.fullscreenElement) exitNativeFullscreen();
    else {
      if (!focused && current && live(current)) setFocus(true);
      requestNativeFullscreen();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !focused || event.defaultPrevented || document.querySelector('dialog[open]')) return;
    // In system fullscreen the browser's own Esc exit is the expected
    // response; the expanded layout stays.
    if (document.fullscreenElement) return;
    event.preventDefault();
    setFocus(false);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && current && !current.restoring) rememberPosition(current, true);
  });
  window.addEventListener('pagehide', () => { if (current && !current.restoring) rememberPosition(current, true); });
  window.addEventListener('resize', resizeFocus);
  window.visualViewport?.addEventListener('resize', resizeFocus);
  window.visualViewport?.addEventListener('scroll', resizeFocus);
  $('#annotation-form').addEventListener('submit', (event) => { event.preventDefault(); saveAnnotation(); });
  $('#annotation-delete').addEventListener('click', () => {
    if (deleteArmed) {
      disarmDelete();
      saveAnnotation(true);
      return;
    }
    deleteArmed = true;
    const button = $('#annotation-delete');
    button.classList.add('armed');
    button.textContent = '确认删除';
    clearTimeout(deleteArmTimer);
    deleteArmTimer = setTimeout(disarmDelete, 2600);
  });
  $('#annotation-close').addEventListener('click', () => $('#annotation-dialog').close());
  $('#annotation-dialog').addEventListener('cancel', (event) => { if (editor?.busy) event.preventDefault(); });
  $('#annotation-dialog').addEventListener('close', () => {
    editor = null;
    disarmDelete();
    captureSelection();
    if (current?.highlightsPending && live(current)) {
      current.highlightsPending = false;
      refreshHighlights(current);
    }
  });
  document.addEventListener('selectionchange', captureSelection);
  // A click outside the popover (e.g. collapsing the selection) closes it;
  // buttons inside run first because hidePopover fires on the next tick.
  document.addEventListener('pointerdown', (event) => {
    if (popover && !popover.contains(event.target)) hidePopover();
  });
  return { open, reset, preserveForConversation, isFocused: () => focused };
}
