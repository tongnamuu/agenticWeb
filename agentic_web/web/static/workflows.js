(() => {
  const state = {sites: [], skills: [], steps: [], saved: [], id: null, busy: false};
  const el = (tag, text, cls = '') => { const n = document.createElement(tag); n.textContent = text; n.className = cls; return n; };
  const button = (text, action) => { const n = el('button', text, 'wf-button'); n.type = 'button'; n.onclick = action; return n; };
  const notice = text => document.getElementById('workflow-message').textContent = text;
  function setBusy(busy) {
    state.busy = busy;
    document.getElementById('workflow-save').disabled = busy;
    document.getElementById('workflow-run').disabled = busy;
    document.getElementById('workflow-new').disabled = busy;
    document.querySelectorAll('[data-workflow-run]').forEach(button => button.disabled = busy);
  }
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
    if (!state.skills.length) box.append(el('p', '등록된 사이트가 없습니다. 사이트 분석 탭에서 URL을 분석하면 이곳에 스킬이 나타납니다.', 'wf-muted'));
    for (const siteGroup of state.sites) {
      const group = el('article', '', 'wf-site');
      group.append(el('h3', siteGroup.name || '등록된 사이트'), el('p', siteGroup.origin, 'wf-url'));
      for (const site of siteGroup.packages) {
      group.append(el('p', site.normalized_url, 'wf-url'));
      for (const tool of site.tools || []) {
        const row = el('div', '', 'wf-tool');
        const info = el('div', ''); info.append(el('strong', tool.display_name || tool.name), el('p', tool.description || '', 'wf-muted'));
        row.append(info, button('+ 단계 추가', () => {
          state.steps.push({url: site.normalized_url, tool_name: tool.name, arguments: {}, tool});
          renderSteps(); notice('단계에 추가했습니다. 입력값과 순서를 지정하세요.');
        })); group.append(row);
      }
      }
      box.append(group);
    }
  }
  function renderSteps() {
    document.getElementById('workflow-step-count').textContent = `${state.steps.length}단계`;
    const box = document.getElementById('workflow-steps'); box.replaceChildren();
    if (!state.steps.length) box.append(el('p', '왼쪽 사이트에서 사용할 스킬을 추가하세요. 같은 스킬을 여러 번 사용할 수 있습니다.', 'wf-muted'));
    state.steps.forEach((step, index) => {
      const card = el('article', '', 'wf-site');
      card.append(el('strong', `${index + 1}. ${step.tool.display_name || step.tool_name}`), el('p', step.url, 'wf-url'));
      for (const [key, definition] of Object.entries(props(step.tool))) {
        const label = el('label', `${key}${definition.required ? ' *' : ''} · ${definition.description || definition.type || 'string'}`, 'wf-label');
        const input = document.createElement(definition.type === 'string' ? 'textarea' : 'input'); input.className = 'wf-input';
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
    const [catalogResult, savedResult] = await Promise.allSettled([api('/api/skills'), api('/api/workflows')]);
    if (catalogResult.status === 'fulfilled') {
      const catalog = catalogResult.value;
      state.skills = catalog.skills || [];
      const groups = new Map();
      for (const pkg of state.skills) {
        const origin = new URL(pkg.normalized_url).origin;
        const metadata = (catalog.sites || []).find(site => site.origin === origin);
        if (!groups.has(origin)) groups.set(origin, {origin, name:metadata?.name || pkg.skill_name || pkg.site_title, packages:[]});
        groups.get(origin).packages.push(pkg);
      }
      state.sites = [...groups.values()];
      renderCatalog();
      document.getElementById('workflow-catalog-status').textContent = `${state.sites.length}개 사이트 · ${state.skills.reduce((n, pkg) => n + (pkg.tools || []).length, 0)}개 스킬`;
    } else {
      document.getElementById('workflow-catalog-status').textContent = '스킬을 불러오지 못했습니다: ' + catalogResult.reason.message;
    }
    if (savedResult.status === 'fulfilled') {
      state.saved = savedResult.value.workflows || [];
      renderSaved();
    } else {
      document.getElementById('workflow-saved').replaceChildren(el('p', '저장 목록을 불러오지 못했습니다: ' + savedResult.reason.message, 'wf-muted'));
    }
  }
  function renderSaved() {
    const box = document.getElementById('workflow-saved'); box.replaceChildren();
    if (!state.saved.length) box.append(el('p', '저장된 워크플로우가 없습니다.', 'wf-muted'));
    for (const workflow of state.saved) {
      const row = el('div', '', 'wf-tool');
      const details = el('div', '');
      details.append(el('strong', `${workflow.name} · ${workflow.steps.length}단계`),
        el('p', workflow.steps.map((step, index) => `${index + 1}. ${step.tool_name}`).join(' → '), 'wf-muted'));
      row.append(details);
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
      }));
      const execute = button('실행 ▶', () => run(workflow.id));
      execute.classList.add('wf-primary'); execute.dataset.workflowRun = workflow.id;
      execute.disabled = state.busy; row.append(execute);
      const remove = button('삭제', async () => {
        if (state.busy || !window.confirm(`“${workflow.name}” 워크플로우를 삭제할까요? 사이트와 스킬은 유지됩니다.`)) return;
        setBusy(true);
        try {
          await api(`/api/workflows/${workflow.id}/delete`, {});
          state.saved = state.saved.filter(item => item.id !== workflow.id);
          if (state.id === workflow.id) {
            state.id = null; state.steps = [];
            document.getElementById('workflow-name').value = ''; renderSteps();
          }
          renderSaved(); notice('워크플로우를 삭제했습니다.');
        } catch (error) { notice(error.message); }
        finally { setBusy(false); }
      });
      remove.dataset.workflowRun = workflow.id; remove.disabled = state.busy;
      remove.style.color = '#fca5a5'; row.append(remove); box.append(row);
    }
  }
  function renderRun(job) {
    const data = job.result || job;
    const labels = {RUNNING:'실행 중', SUCCESS:'성공', FAILED:'실패', NEEDS_USER_INPUT:'사용자 입력 대기'};
    const verdict = document.getElementById('workflow-verdict');
    const elapsed = job.created_at ? ` · ${Math.max(0, Math.floor(Date.now()/1000-job.created_at))}초 경과` : '';
    verdict.textContent = `${job.status === 'RUNNING' ? '진행 상태' : '최종 판정'}: ${labels[job.status] || '결과 확인 필요'}${elapsed}`;
    verdict.style.color = {SUCCESS:'#4ade80', FAILED:'#f87171', NEEDS_USER_INPUT:'#fbbf24'}[job.status] || '#67e8f9';
    notice(job.message || data.message || '');
    const output = document.getElementById('workflow-result'); output.replaceChildren();
    const results = data.results || [];
    (data.workflow?.steps || []).forEach((step, index) => {
      const latest = results.filter(result => result.step === index + 1).at(-1);
      let label = index < data.next_step ? '성공' : '대기';
      if (index === data.next_step) {
        label = job.status === 'FAILED' ? '실패' : job.status === 'NEEDS_USER_INPUT' ? '사용자 입력 대기' :
          data.phase === 'VERIFYING' ? '결과 검증 중' : '실행 중';
      }
      if (latest?.verification.success) label = '성공';
      const card = el('article', '', 'wf-site');
      card.append(el('strong', `${index + 1}. ${step.tool_name} — ${label}`), el('p', step.url, 'wf-url'));
      results.filter(result => result.step === index + 1).forEach(result => {
        card.append(el('p', `${result.attempt}차 검증: ${result.verification.success ? '성공' : '미확인'} · ${result.verification.reason || ''}`, 'wf-muted'));
      });
      output.append(card);
    });
  }
  async function run(id, runId) {
    if (state.busy) return;
    setBusy(true); notice('워크플로우를 순서대로 실행·검증하고 있습니다…');
    const output = document.getElementById('workflow-result'); output.replaceChildren();
    try {
      let job = (await api(`/api/workflows/${id}/run`, runId ? {run_id: runId} : {})).job;
      while (job.status === 'RUNNING') {
        renderRun(job);
        await new Promise(resolve => setTimeout(resolve, 1000));
        job = (await api('/api/workflow-jobs/' + job.id)).job;
      }
      renderRun(job);
      const data = job.result || job;
      if (data.status === 'NEEDS_USER_INPUT') {
        output.append(button('로그인 브라우저로 이동 · 자동 재개', async () => {
          if (state.busy) return;
          setBusy(true);
          try {
            const remote = await api('/api/login-browser', {});
            const frame = document.createElement('iframe');
            frame.src = remote.view_url; frame.title = '실행기 브라우저 로그인';
            frame.style.cssText = 'width:100%;height:650px;border:1px solid #475569';
            output.append(frame);
            notice('실행기 브라우저에서 로그인하세요. 완료되면 자동 재개합니다.');
            const url = data.workflow.steps[data.next_step].url;
            while (true) {
              await new Promise(resolve => setTimeout(resolve, 2000));
              if ((await api('/api/login-status', {url})).authenticated) break;
            }
            setBusy(false);
            await run(id, data.run_id);
          } catch (error) {notice(error.message);}
          finally {setBusy(false);}
        }));
        output.append(button('입력 완료 · 현재 단계 재개', () => run(id, data.run_id)));
      }
    } catch (error) {
      notice(error.message);
      document.getElementById('workflow-verdict').textContent = '결과 확인 불가 · 서버 응답을 받지 못했습니다. 중복 실행하지 말고 상태를 확인하세요.';
    }
    finally { setBusy(false); }
  }
  document.getElementById('workflow-refresh').onclick = refresh;
  document.getElementById('workflow-new').onclick = () => {state.id = null; state.steps = []; document.getElementById('workflow-name').value = ''; renderSteps(); notice('새 워크플로우');};
  async function saveCurrent(execute = false) {
    if (state.busy) return;
    setBusy(true);
    let savedId = null;
    try {
      const data = await api('/api/workflows', {id: state.id, name: document.getElementById('workflow-name').value.trim(), steps: serialize()});
      state.id = data.workflow.id; savedId = state.id;
      state.saved = [data.workflow, ...state.saved.filter(item => item.id !== savedId)];
      renderSaved();
      notice('저장했습니다. 바로 실행하려면 저장하고 실행 버튼을 누르세요.');
    } catch (error) { notice(error.message); }
    finally { setBusy(false); }
    if (execute && savedId) await run(savedId);
  }
  document.getElementById('workflow-save').onclick = () => saveCurrent();
  document.getElementById('workflow-run').onclick = () => saveCurrent(true);
  window.addEventListener('skills-updated', refresh);
  window.addEventListener('focus', () => { if (!state.busy) refresh(); });
  renderSteps(); refresh();
})();
