/**
 * Content script：在 Sophia 页面内提取当前题目（题干 + 选项）。
 * 提取逻辑移植自 web_agent/agent.py 的 extract_sophia_question
 * 与 scripts/login_then_select.py 的 extract_question（challenge / milestone 两种 DOM）。
 * 只读操作：不点击、不选择、不提交任何答案。
 */

// 克隆节点后把所有 img 替换为其 alt 文本（WirisFormula 公式图、图表等），再取纯文本
function extractWithAlt(el) {
  if (!el) return '';
  const clone = el.cloneNode(true);
  clone.querySelectorAll('img[alt]').forEach(img => {
    const alt = img.alt.trim();
    if (alt) img.replaceWith(document.createTextNode(alt));
    else img.remove();
  });
  return clone.innerText.replace(/\s+/g, ' ').trim();
}

// Challenge 页面 DOM（.question-slide）
function extractChallenge() {
  if (!document.querySelector('.question-slide .challenge-v2-question__text')) return null;
  const slide = document.querySelector('.question-slide');
  // 题干：整个 question-slide 去掉选项/按钮/弹窗区，这样图片块（如 .quiz-zoom-image）也能被捕获
  let question = '';
  if (slide) {
    const slideClone = slide.cloneNode(true);
    slideClone.querySelectorAll(
      '.challenge-v2-answer__input, .challenge-v2-answer, button, nav, input, textarea, select, form, '
      + '[role=dialog], [role=tooltip], [class*=modal], [class*=dialog], [class*=popover], [class*=tooltip], '
      + '[class*=report], [class*=feedback], [class*=footer], [class*=controls], [class*=progress]'
    ).forEach(el => el.remove());
    question = extractWithAlt(slideClone);
  }
  if (!question) {
    question = extractWithAlt(document.querySelector('.question-slide .challenge-v2-question__text'));
  }
  // 防御：截断可能残留的反馈弹窗文案
  for (const marker of ['Report an issue', 'Notice something confusing']) {
    const i = question.indexOf(marker);
    if (i > 0) question = question.slice(0, i).trim();
  }
  const labels = document.querySelectorAll('.question-slide .challenge-v2-answer__input');
  const options = Array.from(labels).map(label => {
    const textDiv = label.querySelector('.challenge-v2-answer__text');
    if (!textDiv) return extractWithAlt(label);
    // 去掉字母标签 span.letter
    const clone = textDiv.cloneNode(true);
    clone.querySelectorAll('span.letter').forEach(el => el.remove());
    clone.querySelectorAll('img[alt]').forEach(img => {
      const alt = img.alt.trim();
      if (alt) img.replaceWith(document.createTextNode(alt));
      else img.remove();
    });
    const text = clone.innerText.replace(/\s+/g, ' ').trim();
    return text || extractWithAlt(label);
  });
  if (!question || !options.length) return null;
  return { question, options, kind: 'challenge' };
}

// Practice Milestone / Milestone 页面 DOM（.assessment-question-block / .milestone-take__question-area）
function extractMilestone() {
  const block = document.querySelector('.assessment-question-block.multiple_choice')
    || document.querySelector('.milestone-take__question-area');
  if (!block) return null;
  // 题干：整个题目块去掉选项区，图片统一转 alt 文本
  const clone = block.cloneNode(true);
  clone.querySelectorAll(
    'ul.answer-fields, .multiple-choice-answer-fields, label.milestone-answer__input, input, button, a'
  ).forEach(n => n.remove());
  const heading = (clone.querySelector('h3') && clone.querySelector('h3').textContent || '').trim();
  let body = extractWithAlt(clone);
  body = body.replace(/^Question\s*\d+/i, '').replace(/Mark this question/i, '').trim();
  // 选项：同样走 alt 提取，避免选项内公式图片丢失
  let options = Array.from(block.querySelectorAll('label.milestone-answer__input'))
    .map(label => extractWithAlt(label))
    .map(o => (o || '').trim())
    .filter(Boolean);
  if (!options.length) {
    // 后备：DOM 变体时直接取纯文本
    options = Array.from(document.querySelectorAll('label.milestone-answer__input'))
      .map(label => (label.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
  }
  if (!body && !heading) return null;
  if (!options.length) return null;
  return { question: body || heading, options, kind: 'milestone' };
}

function extractQuestion() {
  return extractChallenge() || extractMilestone();
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === 'EXTRACT_QUESTION') {
    try {
      const result = extractQuestion();
      if (result) {
        sendResponse({ ok: true, ...result, url: location.href });
      } else {
        sendResponse({ ok: false, error: '未在当前页面找到题目（需在 Challenge / Milestone 题目页）' });
      }
    } catch (err) {
      sendResponse({ ok: false, error: `提取失败: ${err.message}` });
    }
    return false; // 同步响应
  }
  return false;
});
