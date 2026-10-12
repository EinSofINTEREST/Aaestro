// 페이지 글루 — DOM 만 다룬다. REST 는 client.js 가, 선언 문서의 모양도 client.js 가 안다.
import { agentsOfGraph, ApiError, bumpPatch, createClient, emptyAgent, emptyGraph, formatPairs,
  materialPathProblem, moduleRef, parsePairs, pruneEmpty, revocationsFor, ruleState, suggestInputMap,
  unbuiltAgents } from "./client.js";

const $ = (selector) => document.querySelector(selector);
const el = (tag, props = {}, children = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) node.append(child);
  return node;
};

let api = createClient({ token: sessionStorage.getItem("aaestro.token") });
const status = (text, failed = false) => {
  const line = $("#status");
  line.textContent = text;
  line.className = failed ? "status-failed" : "muted";
};
const report = (err) => {
  status(err instanceof ApiError ? err.message : String(err), true);
  return null;
};

// --- 인증 / 탭 -------------------------------------------------------------------

$("#auth").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("#token").value.trim();
  sessionStorage.setItem("aaestro.token", token);
  api = createClient({ token: token || null });
  await connect();
});

async function connect() {
  try {
    await api.health();
    await api.graphs(); // 인증이 걸린 첫 호출 — 토큰이 틀리면 여기서 401
    $("#health").textContent = "연결됨";
    $("#health").className = "status-ready";
    await Promise.all([loadCatalog(), loadDeployments(), loadRuns()]);
  } catch (err) {
    $("#health").textContent = err instanceof ApiError && err.status === 401 ? "토큰 거부" : "연결 실패";
    $("#health").className = "status-failed";
    report(err);
  }
}

for (const button of document.querySelectorAll("#tabs button")) {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${button.dataset.tab}`));
  });
}

// --- 카탈로그 (#240) ------------------------------------------------------------

const catalog = { agents: [], graphs: [], groups: [], modules: {} };

async function loadCatalog() {
  const [agents, graphs, groups, skillsets, promptsets, memorysets] = await Promise.all([
    api.agents(), api.graphs(), api.groups(),
    api.modules("skillsets"), api.modules("promptsets"), api.modules("memorysets"),
  ]);
  catalog.agents = agents.items; catalog.graphs = graphs.items; catalog.groups = groups.items;
  catalog.modules = { skillsets: skillsets.items, promptsets: promptsets.items, memorysets: memorysets.items };

  fill("#catalog-agents", agents.items, (a) => `${a.name}@${a.version} — ${a.model.provider}/${a.model.name}`, (a) => api.agent(a.name));
  fill("#catalog-graphs", graphs.items, (g) => `${g.name}@${g.version} (${g.mode}, ${g.nodes} nodes)`, (g) => api.graph(g.name));
  fill("#catalog-groups", groups.items, (g) => g.name, (g) => Promise.resolve(g));
  const modules = Object.entries(catalog.modules).flatMap(([type, items]) => items.map((m) => ({ type, ...m })));
  fill("#catalog-modules", modules, (m) => `${m.type}/${m.name} @ ${m.versions.join(", ")}`, (m) => Promise.resolve(m));

  const problems = [...agents.problems, ...graphs.problems, ...groups.problems, ...skillsets.problems, ...promptsets.problems, ...memorysets.problems];
  $("#catalog-problems").replaceChildren(...problems.map((p) => el("li", { textContent: `${p.path}: ${p.code} ${p.message}` })));

  fillSelect("#access-agent", agents.items.map((a) => [a.name, a.name]));
  fillSelect("#deploy-graph", graphs.items.map((g) => [g.name, `${g.name}@${g.version}`]));
  checkDeployable();
  fillSelect("select[name=group]", [["", "(없음)"], ...groups.items.map((g) => [g.name, g.name])]);
  fillSelect("select[name=promptset]", versionsOf("promptsets"));
  fillSelect("select[name=skillsets]", versionsOf("skillsets"));
  fillSelect("select[name=memorysets]", versionsOf("memorysets"));
}

function versionsOf(type) {
  return (catalog.modules[type] || []).flatMap((m) => m.versions.map((v) => [moduleRef(type, m.name, v), moduleRef(type, m.name, v)]));
}

function fill(selector, items, label, detail) {
  $(selector).replaceChildren(
    ...items.map((item) => el("li", { textContent: label(item), onclick: async () => {
      try { $("#catalog-detail").textContent = JSON.stringify(await detail(item), null, 2); $("#catalog-detail").className = "detail"; }
      catch (err) { report(err); }
    } })),
  );
}

function fillSelect(selector, pairs, keep = true) {
  const select = $(selector);
  const current = keep ? select.value : null;
  select.replaceChildren(...pairs.map(([value, text]) => el("option", { value, textContent: text })));
  if (current && [...select.options].some((o) => o.value === current)) select.value = current;
}

// --- 그래프 편집기 (#242) --------------------------------------------------------

const graphForm = $("#graph-form");
function syncServiceFields(isService) {
  // hidden 만으로는 부족하다 — required 필드가 mission 모드에서도 폼 제출을 막는다.
  // disabled 는 constraint validation 과 값 읽기 양쪽에서 그 필드를 뺀다
  const fieldset = $("#graph-service");
  fieldset.hidden = !isService;
  fieldset.disabled = !isService;
}
graphForm.mode.addEventListener("change", () => syncServiceFields(graphForm.mode.value === "service"));

const rowFactories = {
  node: () => [
    el("input", { name: "node_id", placeholder: "planner" }),
    el("select", { name: "node_agent" }),
    el("input", { name: "node_input", placeholder: "query=state.query" }),
    el("input", { name: "node_output", placeholder: "plan=output.plan" }),
  ],
  edge: () => [el("input", { name: "edge_from", placeholder: "START" }), el("input", { name: "edge_to", placeholder: "END" }),
    el("input", { name: "edge_condition", placeholder: "state.needs_research" }), el("input", { name: "edge_max", type: "number", min: 1 })],
  connection: () => [el("input", { name: "conn_caller" }), el("input", { name: "conn_callee" })],
};
const tables = { node: "#graph-nodes", edge: "#graph-edges", connection: "#graph-connections" };

function addRow(kind, values = []) {
  const cells = rowFactories[kind]();
  cells.forEach((cell, i) => {
    if (cell.tagName === "SELECT") {
      cell.replaceChildren(...catalog.agents.map((a) => el("option", { value: `agents/${a.name}@${a.version}`, textContent: `${a.name}@${a.version}` })));
    }
    if (values[i] !== undefined && values[i] !== null) cell.value = values[i];
  });
  const remove = el("button", { type: "button", textContent: "✕", onclick: (e) => e.target.closest("tr").remove() });
  const row = el("tr", {}, [...cells.map((c) => el("td", {}, [c])), el("td", {}, [remove])]);
  if (kind === "node") {
    for (const cell of cells.slice(0, 2)) {
      cell.addEventListener("change", () => suggestNodeInput(row));
    }
  }
  $(`${tables[kind]} tbody`).append(row);
}
for (const button of document.querySelectorAll("[data-add]")) button.addEventListener("click", () => addRow(button.dataset.add));

// 노드가 물린 에이전트의 promptset 에서, 그 노드 id 템플릿의 필수 변수를 찾는다.
// 검증(#260)이 요구하는 값이므로 편집기가 먼저 알려 준다 — 규칙을 외우게 하지 않는다.
const promptsetCache = new Map();

async function requiredVariables(agentRef, nodeId) {
  const agentName = agentRef.split("/")[1]?.split("@")[0];
  if (!agentName || !nodeId) return [];
  try {
    const manifest = await api.agent(agentName);
    const ref = manifest.spec?.promptset?.ref;
    if (!ref) return [];
    if (!promptsetCache.has(ref)) {
      const [, rest] = ref.split("/");
      const [name, version] = rest.split("@");
      promptsetCache.set(ref, await api.module("promptsets", name, version));
    }
    const template = promptsetCache.get(ref)?.spec?.templates?.[nodeId];
    return Object.entries(template?.variables || {})
      .filter(([, spec]) => spec?.required)
      .map(([name]) => name);
  } catch {
    return []; // 못 알아내면 조용히 비워 둔다 — 검증이 잡는다
  }
}

async function suggestNodeInput(row) {
  const cell = row.querySelector("[name=node_input]");
  const idCell = row.querySelector("[name=node_id]");
  const agentCell = row.querySelector("[name=node_agent]");
  if (cell.value.trim()) return; // 사람이 적은 것을 덮지 않는다
  const id = idCell.value.trim();
  const agentRef = agentCell.value;
  const required = await requiredVariables(agentRef, id);
  // 조회하는 동안 사람이 타이핑했거나 행이 바뀌었을 수 있다 — 늦게 온 응답이
  // 그것을 덮으면 쓰던 값이 사라지거나 옛 노드의 변수가 채워진다
  if (!required.length) return;
  if (cell.value.trim() || idCell.value.trim() !== id || agentCell.value !== agentRef) return;
  if (!row.isConnected) return;
  cell.value = formatPairs(suggestInputMap(required));
  status(`${id}: 템플릿이 요구하는 ${required.join(", ")} 를 채웠습니다`);
}

function rows(kind) {
  return [...document.querySelectorAll(`${tables[kind]} tbody tr`)].map((tr) => [...tr.querySelectorAll("input, select")].map((i) => i.value.trim()));
}

// 인라인 필드가 있으면 그것이 state 다 — schema import ref 는 한 버전만 남는 옛 방식이다.
// 둘 다 보내면 서버가 거부해 운영자가 어느 쪽인지 고르게 한다
function stateDeclaration(f) {
  const fields = f.state_fields.value.trim();
  const schema = f.state_schema.value.trim();
  const state = {};
  if (fields) {
    try { state.fields = JSON.parse(fields); } catch (err) { throw new Error(`state 필드가 JSON 이 아니다: ${err.message}`); }
  }
  if (schema) state.schema = schema;
  return state;
}

function graphDocument() {
  const f = graphForm;
  const doc = emptyGraph(f.name.value.trim());
  doc.metadata.version = f.version.value.trim();
  doc.metadata.description = f.description.value.trim();
  doc.spec.mode = f.mode.value;
  doc.spec.goal = f.goal.value.trim();
  doc.spec.state = stateDeclaration(f);
  doc.spec.nodes = rows("node").map(([id, agent, input, output]) => {
    const node = pruneEmpty({ id, agent });
    const inputMap = parsePairs(input);
    const outputMap = parsePairs(output);
    if (Object.keys(inputMap).length) node.input_map = inputMap;
    if (Object.keys(outputMap).length) node.output_map = outputMap;
    return node;
  });
  doc.spec.edges = rows("edge").map(([from, to, condition, max]) => pruneEmpty({ from, to, condition, max_iterations: max ? Number(max) : "" }));
  doc.spec.connections = rows("connection").map(([caller, callee]) => ({ caller, callee }));
  if (f.mode.value === "service") {
    doc.spec.service = { idle: { min_delay_s: Number(f.idle_min.value), max_delay_s: Number(f.idle_max.value) }, max_failure_streak: Number(f.failure_streak.value) };
  }
  return pruneEmpty(doc);
}

function showGraph(doc) {
  const f = graphForm;
  f.name.value = doc.metadata.name; f.version.value = doc.metadata.version; f.description.value = doc.metadata.description || "";
  f.mode.value = doc.spec.mode; f.goal.value = doc.spec.goal || ""; f.state_schema.value = doc.spec.state?.schema || "";
  f.state_fields.value = doc.spec.state?.fields ? JSON.stringify(doc.spec.state.fields, null, 2) : "";
  syncServiceFields(doc.spec.mode === "service");
  if (doc.spec.service) { f.idle_min.value = doc.spec.service.idle.min_delay_s; f.idle_max.value = doc.spec.service.idle.max_delay_s; f.failure_streak.value = doc.spec.service.max_failure_streak ?? 5; }
  for (const kind of Object.keys(tables)) $(`${tables[kind]} tbody`).replaceChildren();
  (doc.spec.nodes || []).forEach((n) =>
    addRow("node", [n.id, n.agent, formatPairs(n.input_map), formatPairs(n.output_map)]));
  (doc.spec.edges || []).forEach((e) => addRow("edge", [e.from, e.to, e.condition || "", e.max_iterations || ""]));
  (doc.spec.connections || []).forEach((c) => addRow("connection", [c.caller, c.callee]));
}

function showFindings(selector, result) {
  const list = $(selector);
  if (result.ok) { list.replaceChildren(el("li", { className: "ok", textContent: "검증 통과" })); return true; }
  list.replaceChildren(...result.findings.map((f) => el("li", { textContent: `${f.check} [${f.code}] ${f.message}` })));
  return false;
}

async function validateGraph() {
  try { return showFindings("#graph-findings", await api.validate({ graphs: [graphDocument()] })); }
  catch (err) { showApiFindings("#graph-findings", err); return false; }
}
function showApiFindings(selector, err) {
  const errors = err instanceof ApiError && err.details.errors ? err.details.errors.map((e) => `${e.field}: ${e.problem}`) : [String(err.message || err)];
  $(selector).replaceChildren(...errors.map((text) => el("li", { textContent: text })));
}

$("#graph-validate").addEventListener("click", validateGraph);
$("#graph-load").addEventListener("click", async () => {
  const name = prompt("불러올 그래프 이름", catalog.graphs[0]?.name || "");
  if (!name) return;
  try { const doc = await api.graph(name); doc.metadata.version = bumpPatch(doc.metadata.version); showGraph(doc); status(`${name} 불러옴 — 저장하려면 버전이 올라가야 하므로 patch 를 올려 두었습니다`); }
  catch (err) { report(err); }
});
graphForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!(await validateGraph())) return; // 저장 전에 검증 실패가 화면에 보인다
  try {
    const doc = graphDocument();
    const saved = await api.saveGraph(doc.metadata.name, doc);
    status(`저장됨: ${saved.path}`);
    await loadCatalog();
  } catch (err) { showApiFindings("#graph-findings", err); report(err); }
});
$("#graph-delete").addEventListener("click", async () => {
  const name = graphForm.name.value.trim();
  if (!name || !confirm(`${name} 을 삭제할까요?`)) return;
  try { await api.deleteGraph(name); status(`삭제됨: ${name}`); await loadCatalog(); } catch (err) { report(err); }
});

// --- 에이전트 편집기 (#242) ------------------------------------------------------

const agentForm = $("#agent-form");
const selected = (select) => [...select.selectedOptions].map((o) => o.value);

function agentDocument() {
  const f = agentForm;
  const doc = emptyAgent(f.name.value.trim());
  doc.metadata.version = f.version.value.trim();
  doc.metadata.description = f.description.value.trim();
  doc.metadata.group = f.group.value;
  doc.spec.model = { provider: f.provider.value.trim(), name: f.model.value.trim() };
  doc.spec.promptset = { ref: f.promptset.value };
  doc.spec.skillsets = selected(f.skillsets).map((ref) => ({ ref }));
  const memory = selected(f.memorysets).map((ref) => ({ ref, as: ref.split("/")[1].split("@")[0] }));
  if (memory.length) doc.spec.memory = { spaces: memory };
  doc.spec.runtime = { image: f.image.value.trim(), env_allowlist: f.env_allowlist.value.split(",").map((s) => s.trim()).filter(Boolean) };
  doc.spec.entrypoint = f.entrypoint.value.trim();
  if (f.a2a.checked) doc.spec.a2a = { enabled: true };
  return pruneEmpty(doc);
}

function showAgent(doc) {
  const f = agentForm;
  f.name.value = doc.metadata.name; f.version.value = doc.metadata.version; f.description.value = doc.metadata.description || "";
  f.group.value = doc.metadata.group || "";
  f.provider.value = doc.spec.model.provider; f.model.value = doc.spec.model.name;
  f.promptset.value = doc.spec.promptset?.ref || "";
  const skill = new Set((doc.spec.skillsets || []).map((s) => s.ref));
  [...f.skillsets.options].forEach((o) => { o.selected = skill.has(o.value); });
  const mem = new Set((doc.spec.memory?.spaces || []).map((s) => s.ref));
  [...f.memorysets.options].forEach((o) => { o.selected = mem.has(o.value); });
  f.image.value = doc.spec.runtime?.image || "";
  f.entrypoint.value = doc.spec.entrypoint || "";
  f.env_allowlist.value = (doc.spec.runtime?.env_allowlist || []).join(", ");
  f.a2a.checked = Boolean(doc.spec.a2a?.enabled);
}

async function validateAgent() {
  try { return showFindings("#agent-findings", await api.validate({ agents: [agentDocument()] })); }
  catch (err) { showApiFindings("#agent-findings", err); return false; }
}
$("#agent-validate").addEventListener("click", validateAgent);
$("#agent-load").addEventListener("click", async () => {
  const name = prompt("불러올 에이전트 이름", catalog.agents[0]?.name || "");
  if (!name) return;
  try { const doc = await api.agent(name); doc.metadata.version = bumpPatch(doc.metadata.version); showAgent(doc); status(`${name} 불러옴 (patch 버전 올림)`); }
  catch (err) { report(err); }
});
agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!(await validateAgent())) return;
  try {
    const doc = agentDocument();
    const saved = await api.saveAgent(doc.metadata.name, doc);
    status(`저장됨: ${saved.path}`);
    await loadCatalog();
  } catch (err) { showApiFindings("#agent-findings", err); report(err); }
});
$("#agent-delete").addEventListener("click", async () => {
  const name = agentForm.name.value.trim();
  if (!name || !confirm(`${name} 을 삭제할까요?`)) return;
  try {
    // 손으로 둔 파일이 남으면 서버가 그 목록을 답한다 — 조용히 두면 같은 이름에서 되살아난다 (#258)
    const answer = await api.deleteAgent(name);
    const kept = answer && answer.retained ? ` — 남은 파일: ${answer.retained.join(", ")}` : "";
    status(`삭제됨: ${name}${kept}`);
    await loadCatalog();
  } catch (err) { report(err); }
});

// --- 빌드 재료와 이미지 (#267) ---------------------------------------------------
// 재료는 **저장된** 에이전트의 현재 버전에 묶인다. 저장은 불변이다: 같은 버전에 다른 내용을
// 넣으려면 버전을 올려 에이전트를 먼저 저장한다 (04 Build Materials).

const materialsForm = $("#materials-form");
const agentName = () => agentForm.name.value.trim();

function addMaterialRow(path = "", content = "") {
  const pathCell = el("input", { name: "material_path", placeholder: "src/agent.py", value: path });
  const contentCell = el("textarea", { name: "material_content", value: content });
  pathCell.addEventListener("input", showMaterialProblems);
  const remove = el("button", { type: "button", textContent: "✕", onclick: (e) => { e.target.closest("tr").remove(); showMaterialProblems(); } });
  $("#agent-materials tbody").append(el("tr", {}, [el("td", {}, [pathCell]), el("td", {}, [contentCell]), el("td", {}, [remove])]));
}
$("#material-add").addEventListener("click", () => addMaterialRow());

function materialRows() {
  return [...document.querySelectorAll("#agent-materials tbody tr")].map((tr) => [
    tr.querySelector("[name=material_path]").value,
    tr.querySelector("[name=material_content]").value,
  ]);
}

// 경로 규칙 위반을 저장 전에 보인다 — 판정은 서버가 다시 한다
function showMaterialProblems() {
  const seen = new Set();
  const problems = [];
  for (const [path] of materialRows()) {
    const problem = materialPathProblem(path);
    if (problem) problems.push(`${path || "(빈 경로)"}: ${problem}`);
    else if (seen.has(path)) problems.push(`${path}: 같은 경로가 두 번 있습니다`);
    seen.add(path);
  }
  $("#material-findings").replaceChildren(...problems.map((text) => el("li", { textContent: text })));
  return problems.length === 0;
}

function showImage(record) {
  const line = $("#image-status");
  const log = $("#image-log");
  log.hidden = true;
  // 명시적인 false 만 "재료 없음" 이다 — 빌드 제출 응답(202)에는 needs_build 가 없다
  if (record.needs_build === false) {
    line.textContent = "재료 없음 — base 이미지 + 선언으로 돕니다 (빌드 불필요)";
    line.className = "muted";
    return;
  }
  const state = record.status || "not built";
  line.className = `status-${record.status || "starting"}`;
  line.textContent = {
    "not built": `아직 굽지 않음 — ${record.image}`,
    building: `building — ${record.image}`,
    built: `built — ${record.image}`,
    failed: `failed — ${record.error || "원인 불명"}`,
  }[state] || `${state} — ${record.image}`;
  if (record.status === "failed") {
    // 원인은 로그 꼬리에 있다 — 화면에서 읽히지 않으면 운영자가 손으로 다시 굽는다
    log.textContent = record.log || "(로그 없음)";
    log.hidden = false;
  }
}

// 이미지 라우트가 없는 것(material_store 만 설정)은 문서화된 상태다 — 실패로 보고하면
// 방금 성공한 재료 저장이 실패처럼 보인다. 라우트 부재는 구조화 에러 코드가 없는 404 다
const buildSurfaceClosed = (err) => err instanceof ApiError && err.status === 404 && !err.code;

async function refreshImage(name) {
  try { const record = await api.image(name); showImage(record); return record; }
  catch (err) {
    if (buildSurfaceClosed(err)) {
      $("#image-status").textContent = "빌드 표면이 꺼져 있습니다 (build_store 미설정)";
      $("#image-status").className = "muted";
      $("#image-log").hidden = true;
      return null;
    }
    report(err);
    return null;
  }
}

// 재료는 **저장된** 매니페스트의 버전에 묶인다. 폼의 버전이 그와 다르면(불러오기가 patch 를
// 올렸거나 사람이 고쳤다) 서버는 옛 버전에 재료를 넣는다 — 새 버전의 빌드는 "재료 없음" 이
// 된다. 에이전트를 먼저 저장하게 한다
async function savedVersionOrRefuse(name) {
  const formVersion = agentForm.version.value.trim();
  let saved;
  try { saved = (await api.agent(name)).metadata.version; }
  catch (err) { report(err); return null; }
  if (formVersion && formVersion !== saved) {
    status(`${name}: 폼은 ${formVersion}, 저장된 선언은 ${saved} 입니다 — 에이전트를 먼저 저장하세요`, true);
    return null;
  }
  return saved;
}

$("#materials-load").addEventListener("click", async () => {
  const name = agentName();
  if (!name) return status("에이전트 이름을 먼저 적거나 불러오세요", true);
  if (!(await savedVersionOrRefuse(name))) return;
  try {
    const found = await api.materials(name);
    $("#agent-materials tbody").replaceChildren();
    Object.entries(found.files).forEach(([path, content]) => addMaterialRow(path, content));
    showMaterialProblems();
    status(`${name}@${found.version} 재료 ${Object.keys(found.files).length}개`);
    await refreshImage(name);
  } catch (err) { report(err); }
});

materialsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = agentName();
  if (!name) return status("에이전트 이름을 먼저 적거나 불러오세요", true);
  if (!showMaterialProblems()) return;
  const version = await savedVersionOrRefuse(name);
  if (!version) return;
  const rows = materialRows();
  try {
    if (rows.length === 0) {
      // 빈 집합을 PUT 하면 그 버전이 "재료 없음" 으로 굳어 나중에 재료를 넣지 못한다 — 비우기는 삭제다
      if (!confirm(`${name}@${version} 의 재료를 비울까요? 이 버전에 다른 재료는 다시 넣을 수 없습니다`)) return;
      await api.deleteMaterials(name);
      status(`재료 비움: ${name}@${version}`);
    } else {
      const saved = await api.saveMaterials(name, Object.fromEntries(rows));
      status(`재료 저장됨: ${name}@${saved.version} (${Object.keys(saved.files).length}개)`);
    }
    await refreshImage(name);
    checkDeployable(); // 재료가 생기면 이 에이전트를 쓰는 그래프는 굽기 전까지 배포할 수 없다
  } catch (err) { showApiFindings("#material-findings", err); report(err); }
});

$("#image-build").addEventListener("click", async () => {
  const name = agentName();
  if (!name) return status("에이전트 이름을 먼저 적거나 불러오세요", true);
  if (!(await savedVersionOrRefuse(name))) return;
  try {
    showImage(await api.buildImage(name));
    status(`빌드 제출: ${name}`);
    const tick = async () => {
      const record = await refreshImage(name);
      if (record && record.status === "building") setTimeout(tick, 2000);
      else if (record) { status(`${name} 빌드: ${record.status}`, record.status === "failed"); checkDeployable(); }
    };
    setTimeout(tick, 1000);
  } catch (err) { report(err); }
});

// --- 배포 (#243) ----------------------------------------------------------------

// 굽지 않은 에이전트를 쓰는 그래프는 **누르기 전에** 알린다 (#267). 서버의 게이트(#266)가
// 판정의 주인이고, 여기서는 같은 질문을 먼저 던질 뿐이다
let deployCheck = 0;
async function checkDeployable() {
  const select = $("#deploy-graph");
  const button = $("#deploy-form button[type=submit]");
  const graph = select.value;
  const check = ++deployCheck;
  const current = () => check === deployCheck && select.value === graph;
  $("#deploy-warnings").replaceChildren();
  if (!graph) { button.disabled = false; return; }
  // 답이 오기 전에 누르면 경고가 뜨기 전에 409 를 받는다 — 판정이 날 때까지 잠근다
  button.disabled = true;
  try {
    const names = agentsOfGraph(await api.graph(graph));
    // 빌드 표면이 꺼져 조회가 안 되면 경고하지 않는다 — 그때는 게이트도 없다
    const images = await Promise.all(names.map((name) => api.image(name).catch(() => null)));
    if (!current()) return; // 조회하는 동안 다른 그래프를 골랐거나 더 새 확인이 시작됐다
    const blocked = unbuiltAgents(images);
    $("#deploy-warnings").replaceChildren(...blocked.map((img) => el("li", {
      textContent: `${img.agent}@${img.version}: 이미지를 굽지 않았습니다 (${img.status || "아직 안 구움"}) — 에이전트 편집기에서 빌드하세요`,
    })));
    button.disabled = blocked.length > 0;
  } catch (err) {
    if (!current()) return; // 옛 그래프의 실패가 새 그래프의 잠금을 풀면 안 된다
    report(err);
    button.disabled = false; // 판정을 못 내리면 서버 게이트에 맡긴다
  }
}
$("#deploy-graph").addEventListener("change", checkDeployable);

async function loadDeployments() {
  const { items } = await api.deployments();
  $("#deployments tbody").replaceChildren(...items.map((d) => el("tr", {}, [
    el("td", { textContent: d.deployment_id }),
    el("td", { textContent: `${d.graph}@${d.version}` }),
    el("td", { textContent: d.status, className: `status-${d.status}` }),
    el("td", { textContent: d.agents.map((a) => `${a.name}#${a.replica} (${a.container_id})`).join(", ") || (d.error || "") }),
    el("td", {}, d.status === "ready" || d.status === "lost" ? [el("button", { textContent: "해체", className: "danger", onclick: async () => {
      try { await api.teardown(d.deployment_id); status(`해체됨: ${d.deployment_id}`); await Promise.all([loadDeployments(), loadRunTargets()]); } catch (err) { report(err); }
    } })] : []),
  ])));
  await loadRunTargets(items);
}
$("#deploy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const graph = $("#deploy-graph").value;
  status(`배포 중: ${graph} …`);
  try { const d = await api.deploy(graph); status(`배포됨: ${d.deployment_id} (${d.status})`); await loadDeployments(); }
  catch (err) { report(err); await loadDeployments(); }
});

async function loadRunTargets(items) {
  const ready = (items || (await api.deployments()).items).filter((d) => d.status === "ready");
  fillSelect("#run-deployment", ready.map((d) => [d.deployment_id, `${d.deployment_id} — ${d.graph}@${d.version}`]));
}

// --- run (#244) -----------------------------------------------------------------

async function loadRuns() {
  const items = await api.runs();
  $("#runs tbody").replaceChildren(...items.map((r) => el("tr", {}, [
    el("td", { textContent: r.run_id }),
    el("td", { textContent: r.graph }),
    el("td", { textContent: r.mode }),
    el("td", { textContent: r.status, className: `status-${r.status}` }),
    el("td", { textContent: String(r.iteration) }),
    el("td", {}, [
      el("button", { textContent: "보기", onclick: async () => { try { const full = await api.run(r.run_id); $("#run-detail").textContent = JSON.stringify(full, null, 2); $("#run-detail").className = "detail"; } catch (err) { report(err); } } }),
      ...(r.mode === "service" && r.status === "running" ? [el("button", { textContent: "drain", onclick: async () => { try { await api.drain(r.run_id); status(`drain 요청: ${r.run_id}`); await loadRuns(); } catch (err) { report(err); } } })] : []),
      ...(r.status === "halted" || r.status === "failed" ? [el("button", { textContent: "재개", onclick: async () => { try { await api.resume(r.run_id); status(`재개: ${r.run_id}`); await loadRuns(); } catch (err) { report(err); } } })] : []),
    ]),
  ])));
}
$("#run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  let input;
  try { input = JSON.parse($("#run-input").value || "{}"); } catch (err) { return report(new Error(`input 이 JSON 이 아닙니다: ${err.message}`)); }
  try {
    const submitted = await api.submit($("#run-deployment").value, input);
    status(`제출됨: ${submitted.run_id}`);
    await loadRuns();
    watch(submitted.run_id);
  } catch (err) { report(err); }
});
function watch(runId) {
  const tick = async () => {
    try {
      const current = await api.run(runId);
      await loadRuns();
      if (current.status === "running") setTimeout(tick, 2000);
      else { $("#run-detail").textContent = JSON.stringify(current, null, 2); $("#run-detail").className = "detail"; status(`${runId}: ${current.status}`); }
    } catch (err) { report(err); }
  };
  setTimeout(tick, 1000);
}

// --- 권한 (#283) -----------------------------------------------------------------

const when = (seconds) => (seconds === null || seconds === undefined ? "" : new Date(seconds * 1000).toLocaleString());
const accessOff = (err) => err instanceof ApiError && err.status === 404 && !err.code;

async function loadAccess() {
  const name = $("#access-agent").value;
  if (!name) return;
  let view;
  try {
    view = await api.accessAgent(name);
  } catch (err) {
    if (accessOff(err)) { $("#access-off").hidden = false; return; }
    return report(err);
  }
  $("#access-off").hidden = true;
  $("#access-declared tbody").replaceChildren(...view.declared.map((permission) => el("tr", {}, [
    el("td", { textContent: permission.kind }),
    el("td", { textContent: permission.target }),
    el("td", { textContent: permission.mode || "" }),
    el("td", {}, revocationsFor(permission).map(({ label, revocation }) => el("button", {
      textContent: label, className: "danger", onclick: () => revoke(name, revocation),
    }))),
  ])));
  const now = Date.now() / 1000;
  $("#access-rules tbody").replaceChildren(...view.rules.map((rule) => {
    const state = ruleState(rule, now);
    return el("tr", { id: `rule-${rule.rule_id}` }, [
      el("td", { textContent: rule.effect }),
      el("td", { textContent: rule.kind }),
      el("td", { textContent: rule.target }),
      el("td", { textContent: rule.mode || "" }),
      el("td", { textContent: rule.decided_by }),
      el("td", { textContent: rule.requested_by || "" }),
      el("td", { textContent: rule.reason }),
      el("td", { textContent: when(rule.expires_at) }),
      el("td", { textContent: state, className: `rule-${state}` }),
      el("td", {}, state === "active" ? [el("button", {
        textContent: rule.effect === "deny" ? "되돌리기" : "끝내기", onclick: () => lift(rule),
      })] : []),
    ]);
  }));
  $("#access-ceilings").textContent = view.ceilings.length
    ? JSON.stringify(view.ceilings, null, 2)
    : "상한이 없으면 확장도 없습니다";
  $("#access-ceilings").className = view.ceilings.length ? "detail" : "detail muted";
  $("#access-denials tbody").replaceChildren(...view.denials.map((denial) => el("tr", {}, [
    el("td", { textContent: when(denial.at) }),
    el("td", { textContent: denial.kind }),
    el("td", { textContent: denial.target }),
    el("td", { textContent: denial.mode || "" }),
    el("td", { textContent: denial.decided_by }),
  ])));
}

async function revoke(agent, revocation) {
  const reason = $("#access-reason").value.trim();
  if (!reason) return report(new Error("회수 사유를 적으세요 — 기록에 남습니다"));
  try {
    const rule = await api.revoke({ agent, reason, ...revocation });
    status(`회수됨: ${rule.target}${rule.mode ? ` (${rule.mode})` : ""} — ${rule.rule_id}`);
    await loadAccess();
  } catch (err) { report(err); }
}

async function lift(rule) {
  try {
    await api.liftRule(rule.rule_id);
    status(`되돌림: ${rule.target} — ${rule.rule_id}`);
    await loadAccess();
  } catch (err) { report(err); }
}

$("#access-form").addEventListener("submit", (event) => { event.preventDefault(); loadAccess(); });
$("#access-agent").addEventListener("change", loadAccess);
document.querySelector("#tabs button[data-tab=access]").addEventListener("click", loadAccess);

// --- 시작 ---------------------------------------------------------------------

$("#token").value = sessionStorage.getItem("aaestro.token") || "";
addRow("edge", ["START", ""]);
connect();
