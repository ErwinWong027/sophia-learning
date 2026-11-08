/**
 * Popup：两个功能按钮 + 设置。
 * 1. 提取题目到剪贴板：content script 提取 -> 格式化 -> 写入剪贴板
 * 2. 粘贴发给 AI：读剪贴板 -> background service worker 调 LLM -> 展示答案
 */

const DEFAULTS = {
  apiKey: '',
  baseUrl: 'https://api-gateway.openagents.org/v1',
  model: 'deepseek-v4-pro',
};

const $ = id => document.getElementById(id);
const btnExtract = $('btn-extract');
const btnAsk = $('btn-ask');
const statusEl = $('status');
const resultEl = $('result');

function showStatus(text, kind = 'info') {
  statusEl.textContent = text;
  statusEl.className = `status ${kind}`;
  statusEl.classList.remove('hidden');
}

function setBusy(busy) {
  btnExtract.disabled = busy;
  btnAsk.disabled = busy;
}

// 与 background.js 的 parseClipboardQuestion 对应的剪贴板格式
function formatQuestion(data) {
  const options = data.options
    .map((opt, i) => `${String.fromCharCode(97 + i)}.) ${opt}`)
    .join('\n');
  return `Question:\n${data.question}\n\nOptions:\n${options}\n`;
}

// 剪贴板写入兜底：navigator.clipboard 失败时用 textarea + execCommand
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch (_err) { /* fall through */ }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const ok = document.execCommand('copy');
  textarea.remove();
  if (!ok) throw new Error('剪贴板写入失败');
}

async function handleExtract() {
  resultEl.classList.add('hidden');
  setBusy(true);
  showStatus('正在提取题目…');
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https:\/\/app\.sophia\.org\//.test(tab.url || '')) {
      throw new Error('请先打开 app.sophia.org 的题目页面');
    }
    let resp;
    try {
      resp = await chrome.tabs.sendMessage(tab.id, { type: 'EXTRACT_QUESTION' });
    } catch (_err) {
      // content script 未注入（刚装插件后页面未刷新）
      throw new Error('页面脚本未就绪，请刷新该页面后重试');
    }
    if (!resp || !resp.ok) {
      throw new Error((resp && resp.error) || '提取失败');
    }
    await copyText(formatQuestion(resp));
    showStatus(`已复制题目到剪贴板（${resp.kind}，${resp.options.length} 个选项）`, 'ok');
  } catch (err) {
    showStatus(err.message || String(err), 'error');
  } finally {
    setBusy(false);
  }
}

async function handleAsk() {
  resultEl.classList.add('hidden');
  setBusy(true);
  showStatus('正在读取剪贴板并请求 AI…');
  try {
    let text = '';
    try {
      text = await navigator.clipboard.readText();
    } catch (_err) {
      throw new Error('无法读取剪贴板，请重试');
    }
    if (!text || !text.trim()) {
      throw new Error('剪贴板为空，请先提取或复制题目');
    }
    const resp = await chrome.runtime.sendMessage({ type: 'ASK_AI', text });
    if (!resp || !resp.ok) {
      throw new Error((resp && resp.error) || 'AI 请求失败');
    }
    // 持久化结果：关闭 popup 再打开仍可查看上次的 AI 结果
    resp.ts = new Date().toISOString();
    await chrome.storage.local.set({ lastResult: resp });
    renderResult(resp);
    showStatus('AI 已返回结果', 'ok');
  } catch (err) {
    showStatus(err.message || String(err), 'error');
  } finally {
    setBusy(false);
  }
}

function renderResult(resp) {
  if (resp.structured) {
    $('answer-line').textContent = `推荐答案：${resp.optionLetter.toUpperCase()}（选项 ${resp.optionIndex + 1}）`;
    $('answer-text').textContent = resp.optionText || '';
    $('answer-text').classList.remove('hidden');
  } else {
    $('answer-line').textContent = 'AI 回复';
    $('answer-text').classList.add('hidden');
  }
  $('rationale').textContent = resp.rationale || '';
  resultEl.classList.remove('hidden');
}

function resultToText(resp) {
  if (resp.structured) {
    return `答案：${resp.optionLetter.toUpperCase()}（选项 ${resp.optionIndex + 1}）\n${resp.optionText || ''}\n\n理由：\n${resp.rationale || ''}`;
  }
  return resp.rationale || '';
}

async function handleCopyResult() {
  const { lastResult } = await chrome.storage.local.get('lastResult');
  if (!lastResult) return;
  try {
    await copyText(resultToText(lastResult));
    showStatus('结果已复制到剪贴板', 'ok');
  } catch (err) {
    showStatus(err.message || String(err), 'error');
  }
}

// 打开 popup 时恢复上次的 AI 结果展示
async function restoreLastResult() {
  const { lastResult } = await chrome.storage.local.get('lastResult');
  if (lastResult && (lastResult.rationale || lastResult.structured)) {
    renderResult(lastResult);
  }
}

// 设置：打开时载入，修改即保存
async function initSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
  const settings = { ...DEFAULTS, ...stored };
  $('api-key').value = settings.apiKey || '';
  $('base-url').value = settings.baseUrl || '';
  $('model').value = settings.model || '';
  for (const [id, key] of [['api-key', 'apiKey'], ['base-url', 'baseUrl'], ['model', 'model']]) {
    $(id).addEventListener('change', () => {
      chrome.storage.local.set({ [key]: $(id).value.trim() });
    });
  }
}

btnExtract.addEventListener('click', handleExtract);
btnAsk.addEventListener('click', handleAsk);
$('btn-copy-result').addEventListener('click', handleCopyResult);
initSettings();
restoreLastResult();
