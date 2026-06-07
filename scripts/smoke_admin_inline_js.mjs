#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const BASE_URL = process.env.ADMIN_UI_BASE_URL || 'http://127.0.0.1:8000';
const PROJECT_ROOT = process.env.PROJECT_ROOT || process.cwd();

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function extractScripts(html) {
  const scripts = [];
  const pattern = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
  for (const match of html.matchAll(pattern)) scripts.push(match[1]);
  return scripts.join('\n');
}

async function fetchText(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`GET ${url} failed: HTTP ${response.status}`);
  return await response.text();
}

function readTemplateScript(name) {
  const html = fs.readFileSync(path.join(PROJECT_ROOT, 'app', 'templates', name), 'utf8');
  return extractScripts(html);
}

async function liveScriptOrTemplate(url, templateName) {
  try {
    return extractScripts(await fetchText(`${BASE_URL}${url}`));
  } catch (_error) {
    return readTemplateScript(templateName);
  }
}

function firstMatch(text, regex) {
  const match = text.match(regex);
  return match ? match[0] : null;
}

class MockButton {
  constructor({ dataset = {}, fields = {} } = {}) {
    this.dataset = dataset;
    this.fields = fields;
    this.disabled = false;
    this.listeners = {};
  }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  async dispatch(type) { await this.listeners[type]?.({ preventDefault() {}, currentTarget: this, target: this }); }
}

class MockForm extends MockButton {
  constructor({ dataset = {}, fields = {}, button = null } = {}) {
    super({ dataset, fields });
    this.button = button || new MockButton();
  }
  querySelector(selector) {
    if (selector === 'button') return this.button;
    return null;
  }
  async dispatch(type) {
    await this.listeners[type]?.({ preventDefault() {}, currentTarget: this, target: this });
  }
}

class MockFormData {
  constructor(form) {
    this.values = new Map(Object.entries(form?.fields || {}));
  }
  entries() { return this.values.entries(); }
  get(key) { return this.values.get(key); }
  append(key, value) { this.values.set(key, value); }
}

function responseJson(payload = { id: 1001 }) {
  return {
    ok: true,
    status: 200,
    async json() { return payload; },
    async text() { return JSON.stringify(payload); },
  };
}

function createHarness(selectorMap = {}, idMap = {}) {
  const calls = [];
  const alerts = [];
  let reloads = 0;
  const context = {
    console,
    Error,
    Object,
    String,
    Number,
    JSON,
    FormData: MockFormData,
    alert: (message) => alerts.push(String(message)),
    fetch: async (url, options = {}) => {
      const requestUrl = String(url);
      calls.push({ url: requestUrl, method: options.method || 'GET', body: options.body, headers: options.headers || {} });
      if (requestUrl === '/api/videos') return responseJson({ id: 501 });
      if (requestUrl.endsWith('/generate-search-queries')) return responseJson([{ id: 2001 }, { id: 2002 }]);
      if (requestUrl.endsWith('/search')) return responseJson([{ id: 3001 }, { id: 3002 }]);
      return responseJson({ id: 1001 });
    },
    document: {
      querySelectorAll(selector) { return selectorMap[selector] || []; },
      getElementById(id) { return idMap[id] || null; },
    },
    window: {
      confirm: () => true,
      prompt: () => 'manual license note',
      alert: (message) => alerts.push(String(message)),
      location: {
        set href(value) { this._href = value; },
        get href() { return this._href; },
        reload() { reloads += 1; },
      },
    },
  };
  context.globalThis = context;
  return { context, calls, alerts, get reloads() { return reloads; } };
}

async function runScript(script, selectorMap, idMap, triggers) {
  const harness = createHarness(selectorMap, idMap);
  vm.runInNewContext(script, harness.context, { timeout: 1000 });
  for (const trigger of triggers) await trigger();
  return harness;
}

async function smokeVideos(script) {
  const editForm = new MockForm({ dataset: { videoId: '12' }, fields: { title: 'T', slug: 's', status: 'draft', transcript: '' } });
  const deleteButton = new MockButton({ dataset: { videoId: '12' } });
  const createForm = new MockForm({ fields: { title: 'New', slug: 'new', transcript: '' } });
  const h = await runScript(
    script,
    { '.edit-video-form': [editForm], '.delete-video-button': [deleteButton] },
    { 'create-video-form': createForm },
    [() => editForm.dispatch('submit'), () => deleteButton.dispatch('click'), () => createForm.dispatch('submit')],
  );
  assert(h.calls.some(c => c.url === '/api/videos/12?actor=admin-ui' && c.method === 'PATCH'), 'video edit PATCH was not called');
  assert(h.calls.some(c => c.url === '/api/videos/12?actor=admin-ui' && c.method === 'DELETE'), 'video delete DELETE was not called');
  assert(h.calls.some(c => c.url === '/api/videos' && c.method === 'POST'), 'video create POST was not called');
  assert(h.context.window.location.href.includes('/videos/501/mistakes'), 'video create did not navigate to new mistake page');
  assert(h.reloads >= 2, 'video edit/delete success paths did not request reloads');
}

async function smokeMistakes(script) {
  const createForm = new MockForm({ dataset: { videoId: '7' }, fields: { order_index: '1', title: 'Mistake', short_title: '', time_start: '', time_end: '', explanation: '', wrong_visual_prompt: '', right_visual_prompt: '', negative_criteria: 'a\nb' } });
  const editForm = new MockForm({ dataset: { mistakeId: '21' }, fields: { order_index: '2', title: 'M', short_title: '', time_start: '', time_end: '', explanation: '', wrong_visual_prompt: '', right_visual_prompt: '', negative_criteria: 'x\ny' } });
  const deleteButton = new MockButton({ dataset: { mistakeId: '21' } });
  const searchButton = new MockButton({ dataset: { mistakeId: '21' } });
  const exportButton = new MockButton({ dataset: { videoId: '7' } });
  const h = await runScript(
    script,
    { '.edit-mistake-form': [editForm], '.delete-mistake-button': [deleteButton], '.search-candidates-button': [searchButton] },
    { 'create-mistake-form': createForm, 'export-video-button': exportButton },
    [() => createForm.dispatch('submit'), () => editForm.dispatch('submit'), () => deleteButton.dispatch('click'), () => searchButton.dispatch('click'), () => exportButton.dispatch('click')],
  );
  assert(h.calls.some(c => c.url === '/api/videos/7/mistakes' && c.method === 'POST'), 'mistake create POST was not called');
  assert(h.calls.some(c => c.url === '/api/mistakes/21?actor=admin-ui' && c.method === 'PATCH'), 'mistake edit PATCH was not called');
  assert(h.calls.some(c => c.url === '/api/mistakes/21?actor=admin-ui' && c.method === 'DELETE'), 'mistake delete DELETE was not called');
  assert(h.calls.some(c => c.url === '/api/mistakes/21/search' && c.method === 'POST'), 'mistake search POST was not called');
  const mistakeSearch = h.calls.find(c => c.url === '/api/mistakes/21/search');
  assert(mistakeSearch.headers['Content-Type'] === 'application/json', 'mistake search must send JSON');
  assert(JSON.parse(mistakeSearch.body).limit_per_query === 20, 'mistake search payload limit is wrong');
  assert(h.calls.some(c => c.url === '/api/videos/7/export' && c.method === 'POST'), 'video export POST was not called');
  const created = h.calls.find(c => c.url === '/api/videos/7/mistakes');
  assert(JSON.parse(created.body).order_index === 1, 'mistake create did not convert order_index to number');
  assert(JSON.parse(created.body).negative_criteria.length === 2, 'mistake create did not split negative_criteria');
  assert(h.reloads >= 3, 'mistake create/edit/delete success paths did not request reloads');
}

async function smokeCandidates(script) {
  const generateButton = new MockButton({ dataset: { mistakeId: '21' } });
  const searchButton = new MockButton({ dataset: { mistakeId: '21' } });
  const manualQueryForm = new MockForm({ dataset: { mistakeId: '21' }, fields: { side: 'wrong', source_provider: 'mock_search', query_text: 'manual kitchen query', results_count: '20' } });
  const editQueryButton = new MockButton({ dataset: { mistakeId: '21', queryId: '77', queryText: 'old query' } });
  const deleteQueryButton = new MockButton({ dataset: { mistakeId: '21', queryId: '77' } });
  const uploadForm = new MockForm({ dataset: { mistakeId: '21', side: 'wrong' }, fields: { file: 'fake-file', license_note: 'own', author_name: '', license_document_ref: '' } });
  const reviewButton = new MockButton({ dataset: { candidateId: '301' } });
  const selectForm = new MockForm({ dataset: { candidateId: '301' } });
  const rightsButton = new MockButton({ dataset: { candidateId: '301', sourceUrl: 'https://example.com/source', authorName: 'A' } });
  const finalRightsButton = new MockButton({ dataset: { assetId: '501', sourceUrl: 'https://example.com/final-source', authorName: 'Owner' } });
  const referenceButton = new MockButton({ dataset: { candidateId: '301' } });
  const referenceBriefButton = new MockButton({ dataset: { candidateId: '301' } });
  const rejectForm = new MockForm({ dataset: { candidateId: '301' }, fields: { reason: 'bad_quality' } });
  const blockButton = new MockButton({ dataset: { candidateId: '301', domain: 'example.com' } });
  const h = await runScript(
    script,
    {
      '.generate-search-queries-button': [generateButton],
      '.search-button': [searchButton],
      '.manual-search-query-form': [manualQueryForm],
      '.edit-search-query-button': [editQueryButton],
      '.delete-search-query-button': [deleteQueryButton],
      '.upload-final-form': [uploadForm],
      '.review-run-button': [reviewButton],
      '.select-final-form': [selectForm],
      '.rights-button': [rightsButton],
      '.final-rights-button': [finalRightsButton],
      '.reference-button': [referenceButton],
      '.reference-brief-button': [referenceBriefButton],
      '.reject-candidate-form': [rejectForm],
      '.block-button': [blockButton],
    },
    {},
    [
      () => generateButton.dispatch('click'),
      () => searchButton.dispatch('click'),
      () => manualQueryForm.dispatch('submit'),
      () => editQueryButton.dispatch('click'),
      () => deleteQueryButton.dispatch('click'),
      () => uploadForm.dispatch('submit'),
      () => reviewButton.dispatch('click'),
      () => selectForm.dispatch('submit'),
      () => rightsButton.dispatch('click'),
      () => finalRightsButton.dispatch('click'),
      () => referenceButton.dispatch('click'),
      () => referenceBriefButton.dispatch('click'),
      () => rejectForm.dispatch('submit'),
      () => blockButton.dispatch('click'),
    ],
  );
  const expected = [
    ['/api/mistakes/21/generate-search-queries', 'POST'],
    ['/api/mistakes/21/search', 'POST'],
    ['/api/mistakes/21/search-queries', 'POST'],
    ['/api/mistakes/21/search-queries/77', 'PATCH'],
    ['/api/mistakes/21/search-queries/77', 'DELETE'],
    ['/api/mistakes/21/upload-final-asset', 'POST'],
    ['/api/candidates/301/reviews/run', 'POST'],
    ['/api/candidates/301/select-final', 'POST'],
    ['/api/candidates/301/confirm-rights', 'POST'],
    ['/api/final-assets/501/confirm-rights', 'POST'],
    ['/api/candidates/301/use-as-reference', 'POST'],
    ['/api/candidates/301/reference-brief', 'POST'],
    ['/api/candidates/301/review', 'POST'],
    ['/api/candidates/301/block-domain', 'POST'],
  ];
  for (const [url, method] of expected) assert(h.calls.some(c => c.url === url && c.method === method), `${method} ${url} was not called`);
  for (const url of ['/api/mistakes/21/generate-search-queries', '/api/mistakes/21/search']) {
    const call = h.calls.find(c => c.url === url);
    assert(call.headers['Content-Type'] === 'application/json', `${url} must send JSON`);
    const payload = JSON.parse(call.body);
    assert(JSON.stringify(payload.sides) === JSON.stringify(['wrong', 'right']), `${url} sides payload is wrong`);
    assert(payload.limit_per_query === 20, `${url} limit payload is wrong`);
  }
  const manualQuery = h.calls.find(c => c.url === '/api/mistakes/21/search-queries');
  assert(manualQuery.headers['Content-Type'] === 'application/json', 'manual query must send JSON');
  assert(JSON.parse(manualQuery.body).query_text === 'manual kitchen query', 'manual query text payload is wrong');
  assert(JSON.parse(manualQuery.body).results_count === 20, 'manual query limit payload is wrong');
  const editQuery = h.calls.find(c => c.url === '/api/mistakes/21/search-queries/77' && c.method === 'PATCH');
  assert(editQuery.headers['Content-Type'] === 'application/json', 'edit query must send JSON');
  assert(JSON.parse(editQuery.body).query_text === 'manual license note', 'edit query prompt payload is wrong');
  const upload = h.calls.find(c => c.url === '/api/mistakes/21/upload-final-asset');
  assert(upload.body instanceof MockFormData, 'upload-final did not submit FormData');
  assert(upload.body.get('side') === 'wrong', 'upload-final did not append side');
  const reviewRun = h.calls.find(c => c.url === '/api/candidates/301/reviews/run');
  assert(JSON.stringify(JSON.parse(reviewRun.body)) === '{}', 'review run body must be empty JSON object');
  const rights = h.calls.find(c => c.url === '/api/candidates/301/confirm-rights');
  assert(JSON.parse(rights.body).rights_status === 'manual_licensed', 'rights confirmation payload is wrong');
  const finalRights = h.calls.find(c => c.url === '/api/final-assets/501/confirm-rights');
  assert(JSON.parse(finalRights.body).rights_status === 'manual_licensed', 'final asset rights confirmation payload is wrong');
  const reference = h.calls.find(c => c.url === '/api/candidates/301/use-as-reference');
  assert(JSON.parse(reference.body).mark_high_value === true, 'reference payload is wrong');
  assert(h.calls.some(c => c.url === '/api/candidates/301/reference-brief' && c.method === 'POST'), 'reference brief POST was not called');
  const reject = h.calls.find(c => c.url === '/api/candidates/301/review');
  assert(JSON.parse(reject.body).action === 'reject', 'reject payload is wrong');
  assert(h.reloads >= 6, 'candidate success paths did not request reloads');
}

async function smokeJobs(script) {
  const dryRun = new MockButton({ dataset: { dryRun: 'true' } });
  const deleteRun = new MockButton({ dataset: { dryRun: 'false' } });
  const h = await runScript(script, { '.cleanup-button': [dryRun, deleteRun] }, {}, [() => dryRun.dispatch('click'), () => deleteRun.dispatch('click')]);
  assert(h.calls.some(c => c.url === '/api/jobs/cleanup?dry_run=true' && c.method === 'POST'), 'cleanup dry-run POST was not called');
  assert(h.calls.some(c => c.url === '/api/jobs/cleanup?dry_run=false' && c.method === 'POST'), 'cleanup delete POST was not called');
  assert(h.reloads === 2, 'cleanup success paths did not request reloads');
}

async function main() {
  const videosHtml = await fetchText(`${BASE_URL}/ui/videos`);
  const videosScript = extractScripts(videosHtml);
  const mistakesUrl = firstMatch(videosHtml, /\/ui\/videos\/\d+\/mistakes/) || '/ui/videos/1/mistakes';
  const mistakesHtml = await fetchText(`${BASE_URL}${mistakesUrl}`);
  const mistakesScript = extractScripts(mistakesHtml);
  const candidatesUrl = firstMatch(mistakesHtml, /\/ui\/videos\/\d+\/mistakes\/\d+/);
  const candidatesScript = candidatesUrl
    ? extractScripts(await fetchText(`${BASE_URL}${candidatesUrl}`))
    : readTemplateScript('review_candidates.html');
  const jobsScript = await liveScriptOrTemplate('/ui/jobs', 'jobs.html');

  await smokeVideos(videosScript);
  await smokeMistakes(mistakesScript);
  await smokeCandidates(candidatesScript);
  await smokeJobs(jobsScript);
  console.log('admin inline JS smoke passed');
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
