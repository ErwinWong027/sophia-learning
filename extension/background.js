/**
 * Service worker：把剪贴板里的题目发给 LLM，返回选择结果。
 * Prompt 构建、非法 JSON 重试、正则兜底解析、rationale 冲突纠正
 * 均移植自 solver/graph.py 的 _llm_answer / _parse_llm_payload。
 */

const DEFAULT_SETTINGS = {
  apiKey: '',
  baseUrl: 'https://api-gateway.openagents.org/v1',
  model: 'deepseek-v4-pro',
};

async function getSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULT_SETTINGS));
  return { ...DEFAULT_SETTINGS, ...stored };
}

// 去除选项前缀字母，如 "a.) xxx" -> "xxx"
function cleanOptionLabel(option) {
  return (option || '').trim().replace(/^\s*[a-dA-D]\s*[.)]\s*/, '');
}

// 把「提取到剪贴板」的文本解析回 {question, options}；解析失败返回 null
function parseClipboardQuestion(text) {
  const m = (text || '').match(/Question\s*[:：]\s*([\s\S]*?)\s*Options\s*[:：]\s*([\s\S]*)/i);
  if (!m) return null;
  const question = m[1].trim();
  const options = m[2]
    .split(/\n+/)
    .map(line => line.trim())
    .filter(Boolean)
    .map(line => cleanOptionLabel(line));
  if (!question || options.length < 2) return null;
  return { question, options };
}

function buildPrompt(question, options) {
  const numbered = options
    .map((opt, i) => `${String.fromCharCode(97 + i)}.) ${cleanOptionLabel(opt)}`)
    .join('\n');
  return (
    'You are answering a Sophia Learning multiple-choice quiz. '
    + 'Pick the single best answer. '
    + 'First solve the problem yourself, then eliminate distractors. '
    + 'If several options are technically true, choose the one stating the most direct root cause '
    + '(the most specific reason, not a restatement of a general criterion). '
    + 'Return ONLY a compact JSON object: {"option_index": <0-based int>, "option_text": "...", "rationale": "brief"}. '
    + 'The rationale must explain why the chosen option is better than the closest competitor. '
    + 'Do NOT repeat the question text; keep rationale under 60 words; escape quotes properly.\n\n'
    + `Question:\n${question}\n\n`
    + `Options:\n${numbered}\n`
  );
}

// 解析 LLM 返回的 JSON；解析失败时用正则尽力提取字段
function parseLlmPayload(content) {
  const start = content.indexOf('{');
  const end = content.lastIndexOf('}');
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(content.slice(start, end + 1));
    } catch (_err) { /* fall through */ }
  }
  // 正则后备：LLM 回显长题干时容易漏转义，直接抽字段
  const payload = {};
  const mIdx = content.match(/"option_index"\s*:\s*(\d+)/);
  if (mIdx) payload.option_index = parseInt(mIdx[1], 10);
  const mTxt = content.match(/"option_text"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  if (mTxt) payload.option_text = mTxt[1];
  const mRat = content.match(/"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  if (mRat) payload.rationale = mRat[1];
  return Object.keys(payload).length ? payload : null;
}

// 文本匹配选项：完整/子串匹配 -> 字母答案（"c" / "option c"）-> 首个选项兜底
function matchOption(raw, options) {
  const text = (raw || '').trim();
  if (!text) return 0;
  for (let i = 0; i < options.length; i += 1) {
    if (text === options[i] || options[i].includes(text) || text.includes(options[i])) return i;
  }
  const letter = text.match(/\b([a-dA-D])\b/);
  if (letter) {
    const index = letter[1].toLowerCase().charCodeAt(0) - 97;
    if (index >= 0 && index < options.length) return index;
  }
  return 0;
}

// rationale 明确支持另一个选项字母时返回该下标（模型推理对但 index 填错的情况）
function rationaleOptionConflict(rationale, index) {
  if (!rationale) return null;
  const letters = Array.from(rationale.toLowerCase().matchAll(/option\s*([a-d])\b/g)).map(m => m[1]);
  if (!letters.length) return null;
  const altIndex = letters[letters.length - 1].charCodeAt(0) - 97;
  return altIndex !== index ? altIndex : null;
}

async function callChatCompletions(settings, messages) {
  const url = `${settings.baseUrl.replace(/\/+$/, '')}/chat/completions`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${settings.apiKey}`,
    },
    body: JSON.stringify({
      model: settings.model,
      messages,
      temperature: 0,
      stream: false,
    }),
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`API 请求失败 ${resp.status}: ${body.slice(0, 200)}`);
  }
  const data = await resp.json();
  return ((data.choices && data.choices[0] && data.choices[0].message.content) || '').trim();
}

// 选择题：与 graph.py 相同，最多 2 次（首次 + 1 次针对非法 JSON 的重试）
async function answerQuestion(settings, question, options) {
  const baseMessages = [
    { role: 'system', content: 'Answer with compact JSON only. No markdown fences.' },
    { role: 'user', content: buildPrompt(question, options) },
  ];
  let payload = {};
  let content = '';
  let messages = baseMessages;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    content = await callChatCompletions(settings, messages);
    const parsed = parseLlmPayload(content);
    if (parsed) {
      payload = parsed;
      break;
    }
    // 非法 JSON：追加严格指令重试一次
    messages = baseMessages.concat([
      { role: 'assistant', content },
      { role: 'user', content: 'Your previous reply was not valid JSON. Reply again with ONLY the compact JSON object.' },
    ]);
  }
  const rawText = String(payload.option_text || payload.answer || content);
  let chosenIndex;
  if (Number.isInteger(payload.option_index) && payload.option_index >= 0 && payload.option_index < options.length) {
    chosenIndex = payload.option_index;
    // 一致性兜底：rationale 明确支持另一个选项时纠正
    const altIndex = rationaleOptionConflict(String(payload.rationale || ''), chosenIndex);
    if (altIndex !== null && altIndex >= 0 && altIndex < options.length) {
      chosenIndex = altIndex;
    }
  } else {
    chosenIndex = matchOption(rawText, options);
  }
  return {
    structured: true,
    optionIndex: chosenIndex,
    optionLetter: String.fromCharCode(97 + chosenIndex),
    optionText: options[chosenIndex],
    rationale: String(payload.rationale || content || ''),
  };
}

// 剪贴板内容不是标准题目格式时，直接作为普通提问发给模型
async function askFreeform(settings, text) {
  const messages = [
    { role: 'system', content: 'You are a helpful assistant. Answer concisely.' },
    { role: 'user', content: text.slice(0, 12000) },
  ];
  const content = await callChatCompletions(settings, messages);
  return { structured: false, rationale: content };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'ASK_AI') return false;
  (async () => {
    try {
      const settings = await getSettings();
      if (!settings.apiKey) {
        sendResponse({ ok: false, error: '请先在下方设置中填写 API Key' });
        return;
      }
      const parsed = parseClipboardQuestion(message.text || '');
      if (parsed) {
        sendResponse({ ok: true, ...(await answerQuestion(settings, parsed.question, parsed.options)) });
      } else {
        sendResponse({ ok: true, ...(await askFreeform(settings, message.text || '')) });
      }
    } catch (err) {
      sendResponse({ ok: false, error: `${err.name}: ${err.message}` });
    }
  })();
  return true; // 异步响应
});
