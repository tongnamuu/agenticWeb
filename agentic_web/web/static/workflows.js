(() => {
  const state = {skills: [], steps: [], saved: [], id: null, busy: false};
  const el = (tag, text, cls = '') => { const n = document.createElement(tag); n.textContent = text; n.className = cls; return n; };
  const button = (text, action) => { const n = el('button', text, 'wf-button'); n.type = 'button'; n.onclick = action; return n; };
  const notice = text => document.getElementById('workflow-message').textContent = text;
  async function api(url, data) {
    const response = await fetch(url, data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '요청에 실패했습니다.');
    return body;
  }
  function props(tool) {
    const schema = tool.parameters || {};
    return Array.isArray(schema) ? Object.fromEntries(schema.map(p => [p.name, {type: 'string', description: p.description, required: true}])) :
      Object.fromEntries(Object.entries(schema.properties || {}).map(([key, p]) => [key, {...p, required: (schema.required || []).includes(key)}]));
  }
  function renderCatalog() {
    const box = document.getElementById('workflow-catalog'); box.replaceChildren();
    if (!state.skills.length) box.append(el('p', '등록된 사이트가 없습니다. 위에서 URL을 분석하면 이곳에 스킬이 나타납니다.', 'wf-muted'));
    for (const site of state.skills) {
      const group = el('article', '', 'wf-site');
      group.append(el('h3', site.site_title || site.skill_name || '등록된 사이트'), el('p', site.normalized_url, 'wf-url'));
      for (const tool of site.tools || []) {
        const row = el('div', '', 'wf-tool');
        const info = el('div', ''); info.append(el('strong', tool.display_name || tool.name), el('p', tool.description || '', 'wf-muted'));
        row.append(info, button('+ 단계 추가', () => {
          state.steps.push({url: site.normalized_url, tool_name: tool.name, arguments: {}, tool});
          renderSteps(); notice('단계에 추가했습니다. 입력값과 순서를 지정하세요.');
        })); group.append(row);
      }
      box.append(group);
    }
  }
  function renderSteps() {
    const box = document.getElementById('workflow-steps'); box.replaceChildren();
    if (!state.steps.length) box.append(el('p', '왼쪽 사이트에서 사용할 스킬을 추가하세요. 같은 스킬을 여러 번 사용할 수 있습니다.', 'wf-muted'));
    state.steps.forEach((step, index) => {
      const card = el('article', '', 'wf-site');
      card.append(el('strong', `${index + 1}. ${step.tool.display_name || step.tool_name}`), el('p', step.url, 'wf-url'));
      for (const [key, definition] of Object.entries(props(step.tool))) {
        const label = el('label', `${key}${definition.required ? ' *' : ''} · ${definition.description || definition.type || 'string'}`, 'wf-label');
        const input = document.createElement('input'); input.className = 'wf-input';
        input.value = key in step.arguments ? (typeof step.arguments[key] === 'object' ? JSON.stringify(step.arguments[key]) : String(step.arguments[key])) : '';
        input.placeholder = definition.type === 'boolean' ? 'true / false' : definition.type === 'object' || definition.type === 'array' ? 'JSON 입력' : '입력값';
        input.oninput = () => {
          if (!input.value && !definition.required) delete step.arguments[key];
          else step.arguments[key] = input.value;
        };
        label.append(input); card.append(label);
      }
      const actions = el('div', '', 'wf-actions');
      const up = button('위로', () => { [state.steps[index - 1], state.steps[index]] = [state.steps[index], state.steps[index - 1]]; renderSteps(); }); up.disabled = index === 0;
      const down = button('아래로', () => { [state.steps[index + 1], state.steps[index]] = [state.steps[index], state.steps[index + 1]]; renderSteps(); }); down.disabled = index === state.steps.length - 1;
      actions.append(up, down, button('단계 제거', () => { state.steps.splice(index, 1); renderSteps(); })); card.append(actions); box.append(card);
    });
  }
  function serialize() {
    return state.steps.map(step => {
      const arguments_ = {};
      for (const [key, value] of Object.entries(step.arguments)) {
        const kind = props(step.tool)[key]?.type;
        if (kind === 'integer' || kind === 'number') {
          if (String(value).trim() === '' || !Number.isFinite(Number(value))) throw new Error(`${key}: 숫자를 입력하세요.`);
          arguments_[key] = Number(value);
        } else if (kind === 'boolean') {
          if (!['true', 'false'].includes(String(value))) throw new Error(`${key}: true 또는 false를 입력하세요.`);
          arguments_[key] = String(value) === 'true';
        } else arguments_[key] = ['array', 'object'].includes(kind) && typeof value === 'string' ? JSON.parse(value) : value;
      }
      return {url: step.url, tool_name: step.tool_name, arguments: arguments_};
    });
  }
  async function refresh() {
    try {
      const [catalog, workflows] = await Promise.all([api('/api/skills'), api('/api/workflows')]);
      state.skills = catalog.skills || []; state.saved = workflows.workflows || [];
      renderCatalog(); renderSaved();
    } catch (error) { notice(error.message); }
  }
  function renderSaved() {
    const box = document.getElementById('workflow-saved'); box.replaceChildren();
    if (!state.saved.length) box.append(el('p', '저장된 워크플로우가 없습니다.', 'wf-muted'));
    for (const workflow of state.saved) {
      const row = el('div', '', 'wf-tool'); row.append(el('span', `${workflow.name} · ${workflow.steps.length}단계`));
      row.append(button('불러오기', () => {
        try {
          const steps = workflow.steps.map(step => {
            const tool = state.skills.find(s => s.normalized_url === step.url)?.tools.find(t => t.name === step.tool_name);
            if (!tool) throw new Error('삭제된 스킬이 포함되어 있습니다.');
            return {...step, arguments: {...step.arguments}, tool};
          });
          state.steps = steps; state.id = workflow.id;
          document.getElementById('workflow-name').value = workflow.name; renderSteps(); notice('불러왔습니다. 수정 후 저장할 수 있습니다.');
        } catch (error) { notice(error.message); }
      }), button('실행', () => run(workflow.id))); box.append(row);
    }
  }
  async function run(id, runId) {
    if (state.busy) return;
    state.busy = true; notice('워크플로우를 순서대로 실행·검증하고 있습니다…');
    const output = document.getElementById('workflow-result'); output.replaceChildren();
    try {
      const data = await api(`/api/workflows/${id}/run`, runId ? {run_id: runId} : {});
      notice(data.message || data.error || data.status);
      for (const result of data.results || []) {
        output.append(el('p', `${result.step}단계 · ${result.tool_name} · ${result.verification.success ? '성공' : '확인 필요'} · ${result.verification.reason || ''}`, 'wf-muted'));
      }
      if (data.status === 'NEEDS_USER_INPUT') output.append(button('로그인·입력 완료 후 재개', () => run(id, data.run_id)));
    } catch (error) { notice(error.message); }
    finally { state.busy = false; }
  }
  document.getElementById('workflow-refresh').onclick = refresh;
  document.getElementById('workflow-new').onclick = () => {state.id = null; state.steps = []; document.getElementById('workflow-name').value = ''; renderSteps(); notice('새 워크플로우');};
  document.getElementById('workflow-save').onclick = async () => {
    try {
      const data = await api('/api/workflows', {id: state.id, name: document.getElementById('workflow-name').value.trim(), steps: serialize()});
      state.id = data.workflow.id; await refresh(); notice('워크플로우를 저장했습니다. 아래 저장 목록에서 실행하세요.');
    } catch (error) { notice(error.message); }
  };
  window.addEventListener('skills-updated', refresh);
  renderSteps(); refresh();
})();
