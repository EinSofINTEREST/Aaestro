// Aaestro control plane 클라이언트 — DOM 을 모른다. 페이지(app.js)와 테스트가 같이 쓴다.
// 규칙: 파일 경로도 컨테이너 런타임 호출도 없다. /v1/* REST 만 부른다 (#245).

export class ApiError extends Error {
  constructor(status, payload) {
    const error = payload && payload.error ? payload.error : null;
    super(error ? `${error.code}: ${error.message}` : `HTTP ${status}`);
    this.status = status;
    this.code = error ? error.code : null;
    this.details = error ? error.details || {} : {};
  }
}

export function createClient({ baseUrl = "", token = null, fetchImpl = globalThis.fetch } = {}) {
  const headers = () => {
    const h = { "content-type": "application/json" };
    if (token) h.authorization = `Bearer ${token}`;
    return h;
  };

  async function call(method, path, body) {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      method,
      headers: headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
  }

  return {
    health: () => call("GET", "/v1/health"),
    // 카탈로그 (#240)
    agents: () => call("GET", "/v1/agents"),
    agent: (name) => call("GET", `/v1/agents/${encodeURIComponent(name)}`),
    graphs: () => call("GET", "/v1/graphs"),
    graph: (name) => call("GET", `/v1/graphs/${encodeURIComponent(name)}`),
    groups: () => call("GET", "/v1/groups"),
    modules: (type) => call("GET", `/v1/modules/${encodeURIComponent(type)}`),
    module: (type, name, version) =>
      call("GET", `/v1/modules/${encodeURIComponent(type)}/${encodeURIComponent(name)}/${encodeURIComponent(version)}`),
    // 저작 (#242)
    validate: ({ graphs = [], agents = [] }) => call("POST", "/v1/validate", { graphs, agents }),
    saveGraph: (name, document) => call("PUT", `/v1/graphs/${encodeURIComponent(name)}`, document),
    deleteGraph: (name) => call("DELETE", `/v1/graphs/${encodeURIComponent(name)}`),
    saveAgent: (name, document) => call("PUT", `/v1/agents/${encodeURIComponent(name)}`, document),
    deleteAgent: (name) => call("DELETE", `/v1/agents/${encodeURIComponent(name)}`),
    // 빌드 재료와 이미지 (#264, #265) — 재료는 에이전트의 **현재** 버전에 묶인다
    materials: (name) => call("GET", `/v1/agents/${encodeURIComponent(name)}/materials`),
    saveMaterials: (name, files) =>
      call("PUT", `/v1/agents/${encodeURIComponent(name)}/materials`, { files }),
    deleteMaterials: (name) => call("DELETE", `/v1/agents/${encodeURIComponent(name)}/materials`),
    buildImage: (name) => call("POST", `/v1/agents/${encodeURIComponent(name)}/image`),
    image: (name) => call("GET", `/v1/agents/${encodeURIComponent(name)}/image`),
    // 배포 (#243)
    deployments: () => call("GET", "/v1/deployments"),
    deploy: (graph) => call("POST", "/v1/deployments", { graph }),
    teardown: (id) => call("DELETE", `/v1/deployments/${encodeURIComponent(id)}`),
    // run (#244)
    runs: () => call("GET", "/v1/runs"),
    run: (id) => call("GET", `/v1/runs/${encodeURIComponent(id)}`),
    submit: (deploymentId, input, runId) =>
      call("POST", "/v1/runs", { deployment_id: deploymentId, input, run_id: runId || null }),
    drain: (id) => call("POST", `/v1/runs/${encodeURIComponent(id)}/drain`),
    resume: (id) => call("POST", `/v1/runs/${encodeURIComponent(id)}/resume`),
    // 권한 (#283) — 운영자 경로. 레지스트리가 꺼져 있으면 라우트가 없다 (404)
    accessAgent: (name) => call("GET", `/v1/access/agents/${encodeURIComponent(name)}`),
    revoke: (revocation) => call("POST", "/v1/access/revocations", revocation),
    liftRule: (ruleId) => call("DELETE", `/v1/access/rules/${encodeURIComponent(ruleId)}`),
  };
}

// 기록 하나의 지금 상태 — 되돌렸거나(lifted) 만료됐거나(expired) 아직 유효하거나(active)
export function ruleState(rule, nowSeconds) {
  if (rule.lifted_at !== null && rule.lifted_at !== undefined) return "lifted";
  if (rule.expires_at !== null && rule.expires_at !== undefined && rule.expires_at <= nowSeconds) return "expired";
  return "active";
}

// 선언 권한 한 줄에서 운영자가 할 수 있는 회수 — 메모리 rw 는 쓰기만 막는 강등과 전체 회수 둘이다.
// `server/*` 는 "그 서버의 모든 도구" 를 뜻하는 표시일 뿐 판정 대상 이름이 아니므로 회수할 수 없다
export function revocationsFor(permission) {
  if (permission.kind === "mcp_tool" && permission.target.endsWith("/*")) return [];
  const base = { kind: permission.kind, target: permission.target };
  if (permission.kind === "memory" && permission.mode === "rw") {
    return [
      { label: "쓰기 회수", revocation: { ...base, mode: "rw" } },
      { label: "전체 회수", revocation: base },
    ];
  }
  return [{ label: "회수", revocation: base }];
}

// --- 편집기의 순수 함수: 폼 상태 ↔ 선언 문서 --------------------------------------

export function emptyGraph(name = "") {
  return {
    apiVersion: "aaestro/v1",
    kind: "Graph",
    metadata: { name, version: "0.1.0", description: "" },
    spec: {
      mode: "mission",
      goal: "",
      state: { schema: "aaestro.graphs.schemas:ResearchState" },
      nodes: [],
      edges: [{ from: "START", to: "" }],
      connections: [],
    },
  };
}

export function emptyAgent(name = "") {
  return {
    apiVersion: "aaestro/v1",
    kind: "Agent",
    metadata: { name, version: "0.1.0", description: "" },
    spec: {
      model: { provider: "anthropic", name: "claude-sonnet-5" },
      promptset: { ref: "" },
      skillsets: [],
      runtime: { env_allowlist: ["ANTHROPIC_API_KEY"] },
    },
  };
}

// 폼 값 하나를 선언에 넣는다 — 빈 문자열은 "없는 값" 이므로 키를 지운다 (원본 값이 되살아나지 않는다)
function put(target, key, value) {
  if (value === "" || value === null || value === undefined) delete target[key];
  else target[key] = value;
}

// 불러온 원본 선언 위에 **폼이 다루는 키만** 덮어쓴 새 문서를 만든다 (#377).
// 폼이 모르는 블록(model.fallbacks · max_tokens · mcp · runtime.resources …)을 새로 조립하면
// 저장할 때 조용히 사라진다. 원본은 변형하지 않는다.
// 원본은 폼의 이름이 원본과 같을 때만 쓴다 — 이름을 바꿔 저장하면 새 에이전트이고, 폼에 보이지
// 않는 선언(mcp · egress · volumes 는 곧 권한이다)이 다른 에이전트로 복제되면 안 된다.
// 폼이 고른 ref 집합에 맞춰 원본 항목을 거른다. 남은 ref 의 원본 항목은 (같은 ref 의 중복
// 별칭 포함) 원래 순서와 폼 밖 키 그대로 두고, 새 ref 만 뒤에 붙인다.
function mergeByRef(originals, chosen, make) {
  const current = originals || [];
  const wanted = new Set(chosen);
  const have = new Set(current.map((item) => item.ref));
  return [...current.filter((item) => wanted.has(item.ref)), ...chosen.filter((ref) => !have.has(ref)).map(make)];
}

export function patchAgent(original, form) {
  const base = original && original.metadata?.name === form.name ? original : null;
  const doc = structuredClone(base || emptyAgent());
  // GET 응답은 필드명(`api_version`)으로 나오지만 manifest 는 별칭(`apiVersion`)만 받는다
  const { apiVersion, kind } = emptyAgent();
  doc.apiVersion = apiVersion;
  doc.kind = kind;
  delete doc.api_version;
  doc.metadata ||= {};
  doc.spec ||= {};
  const { metadata, spec } = doc;
  put(metadata, "name", form.name);
  put(metadata, "version", form.version);
  put(metadata, "description", form.description);
  put(metadata, "group", form.group);

  spec.model = { ...spec.model };
  put(spec.model, "provider", form.provider);
  put(spec.model, "name", form.model);
  spec.promptset = { ...spec.promptset };
  put(spec.promptset, "ref", form.promptset);
  spec.skillsets = mergeByRef(spec.skillsets, form.skillsets, (ref) => ({ ref }));

  // 이미 선언된 space 는 별칭(as)·mode 를 그대로 둔다 — 폼은 ref 만 안다
  const spaces = mergeByRef(spec.memory?.spaces, form.memorysets,
    (ref) => ({ ref, as: ref.split("/")[1].split("@")[0] }));
  if (spaces.length) spec.memory = { ...spec.memory, spaces };
  else if (spec.memory) {
    delete spec.memory.spaces;
    if (!Object.keys(spec.memory).length) delete spec.memory;
  }

  spec.runtime = { ...spec.runtime };
  put(spec.runtime, "image", form.image);
  spec.runtime.env_allowlist = form.envAllowlist;
  put(spec, "entrypoint", form.entrypoint);

  if (form.a2a) spec.a2a = { ...spec.a2a, enabled: true };
  else if (spec.a2a) spec.a2a.enabled = false;
  return doc;
}

// select 옵션 목록에 카탈로그에 없는 선택 값을 덧붙인다. 옵션에 없는 값은 select 에 올라가지
// 못해 폼 값이 비어 읽히고, 저장이 그 선언을 조용히 지우기 때문이다 (#377).
export const UNLISTED_SUFFIX = " (카탈로그에 없음)";

export function withSelected(pairs, selectedValues) {
  const listed = new Set(pairs.map(([value]) => value));
  const extra = [...new Set(selectedValues)]
    .filter((value) => value && !listed.has(value))
    .map((value) => [value, `${value}${UNLISTED_SUFFIX}`]);
  return [...pairs, ...extra];
}

// 편집기 폼의 텍스트 필드는 빈 문자열을 남긴다 — 선언에서는 없는 값이어야 한다
export function pruneEmpty(value) {
  if (Array.isArray(value)) return value.map(pruneEmpty);
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (v === "" || v === null || v === undefined) continue;
      out[k] = pruneEmpty(v);
    }
    return out;
  }
  return value;
}

// 노드 표의 한 칸(`key=source, key2=source2`)과 선언의 매핑 사이를 옮긴다.
// 편집기가 이 값을 만들지 못하면 노드는 빈 입력을 받고, 템플릿의 필수 변수가
// 채워지지 않아 run 이 시작된 뒤에야 MOD_004 로 죽는다 (#260).
export function parsePairs(text) {
  const pairs = {};
  for (const chunk of String(text || "").split(",")) {
    const item = chunk.trim();
    if (!item) continue;
    const at = item.indexOf("=");
    if (at < 0) continue;
    const key = item.slice(0, at).trim();
    const value = item.slice(at + 1).trim();
    if (key && value) pairs[key] = value;
  }
  return pairs;
}

export function formatPairs(pairs) {
  return Object.entries(pairs || {})
    .map(([key, value]) => `${key}=${value}`)
    .join(", ");
}

// 노드 하나의 기본 input_map — 템플릿의 필수 변수를 같은 이름의 state 필드에서 끌어온다.
// 대부분의 배선이 이 모양이므로 편집기가 미리 채워 준다.
export function suggestInputMap(required) {
  return Object.fromEntries((required || []).map((name) => [name, `state.${name}`]));
}

// 재료 경로 하나가 빌드 컨텍스트 레이아웃(#263)을 따르는지 — 문제가 없으면 null.
// **서버의 `aaestro.materials.check_path` 와 같은 규칙**이다. 판정은 서버가 하고, 여기서는
// 저장을 누르기 전에 보이게만 한다. 두 규칙이 어긋나지 않는지는 브라우저 E2E 가 같은 경로
// 표로 양쪽을 대조해 확인한다.
export const MATERIAL_DOCKERFILE = "Dockerfile";
export const MATERIAL_SOURCE_ROOT = "src";

export function materialPathProblem(path) {
  const value = String(path ?? "");
  if (!value || value !== value.trim()) return "경로가 비었거나 앞뒤에 공백이 있습니다";
  if (value.startsWith("/") || value.includes(":") || value.includes("\\")) {
    return "상대 경로(posix)여야 합니다";
  }
  const parts = value.split("/");
  if (parts.some((part) => part === "." || part === "..")) return "빌드 컨텍스트를 벗어납니다";
  if (parts.some((part) => part === "")) return "정규화된 경로여야 합니다 (빈 구간·끝 슬래시 없음)";
  if (value === MATERIAL_DOCKERFILE) return null;
  if (parts[0] === MATERIAL_SOURCE_ROOT && parts.length > 1) return null;
  return `${MATERIAL_DOCKERFILE} 이거나 ${MATERIAL_SOURCE_ROOT}/ 아래여야 합니다`;
}

// 그래프가 쓰는 에이전트 이름 — 노드의 `agents/<name>@<version>` 에서 뽑는다 (중복 없이)
export function agentsOfGraph(graph) {
  const names = (graph?.spec?.nodes || [])
    .map((node) => String(node.agent || "").split("/")[1]?.split("@")[0])
    .filter(Boolean);
  return [...new Set(names)];
}

// 배포 전에 보여 줄 빌드 문제 — 재료가 있는데 그 버전이 `built` 가 아닌 에이전트.
// 서버의 배포 게이트(#266, #383)와 같은 판정이다: 상태가 `built` 가 아니거나 기록의 태그가
// 기대 태그와 다르면 미빌드. 빌드 표면이 꺼져 조회가 안 되면 경고하지 않고, 옛 서버처럼
// expected_image 가 없으면 상태만 본다
export function unbuiltAgents(images) {
  return images.filter(
    (image) =>
      image &&
      image.needs_build &&
      (image.status !== "built" ||
        (image.expected_image != null && image.image !== image.expected_image)),
  );
}

export function moduleRef(type, name, version) {
  return `${type}/${name}@${version}`;
}

export function bumpPatch(version) {
  const parts = String(version).split(".").map((p) => parseInt(p, 10));
  if (parts.length !== 3 || parts.some(Number.isNaN)) return version;
  parts[2] += 1;
  return parts.join(".");
}
