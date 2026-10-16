(() => {
  const screen = document.getElementById('screen');
  const status = document.getElementById('status');
  const tabs = document.getElementById('tabs');
  let viewport = null, refreshing = false, queue = Promise.resolve(), tabSignature = '';
  async function request(url, data) {
    const response = await fetch(url, data === undefined ? {cache:'no-store'} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '연결 실패');
    return body;
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const frame = await request('/api/browser/frame');
      viewport = frame;
      if (frame.screenshot) screen.src = 'data:image/png;base64,' + frame.screenshot;
      document.getElementById('url').textContent = frame.url;
      const signature = JSON.stringify(frame.tabs);
      if (signature !== tabSignature) {
        tabSignature = signature;
        tabs.replaceChildren(...frame.tabs.map(tab => {
          const option = document.createElement('option'); option.value = tab.id;
          option.textContent = `${tab.title || '탭'} · ${tab.url}`;
          option.selected = tab.url === frame.url;
          return option;
        }));
      }
      status.textContent = '연결됨 · 화면을 클릭해 직접 로그인하세요.';
    } catch (error) { status.textContent = error.message; }
    finally { refreshing = false; }
  }
  function send(data) {
    queue = queue.then(() => request('/api/browser/control', data))
      .then(() => refresh()).catch(error => {status.textContent = error.message;});
    return queue;
  }
  function point(event) {
    const box = screen.getBoundingClientRect();
    return {x:(event.clientX-box.left)*viewport.width/box.width, y:(event.clientY-box.top)*viewport.height/box.height};
  }
  screen.onclick = event => {if (viewport) {screen.focus(); send({action:'click', ...point(event)});}};
  screen.addEventListener('wheel', event => {
    if (!viewport) return;
    event.preventDefault(); send({action:'scroll', ...point(event), delta:Math.max(-1500,Math.min(1500,event.deltaY))});
  }, {passive:false});
  screen.onkeydown = event => {
    if (event.isComposing) return;
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault(); send({action:'text',text:event.key});
    } else if (['Enter','Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key)) {
      event.preventDefault(); send({action:'key',key:event.key,modifiers:event.shiftKey ? 8 : 0});
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
      event.preventDefault(); send({action:'key',key:'a',modifiers:2});
    }
  };
  screen.addEventListener('paste', event => {
    const text = event.clipboardData.getData('text');
    if (text) {event.preventDefault(); send({action:'text',text});}
  });
  document.getElementById('text-form').onsubmit = event => {
    event.preventDefault(); const input = document.getElementById('text');
    const text = input.value; input.value = '';
    if (text) send({action:'text',text});
  };
  document.querySelectorAll('[data-key]').forEach(button => button.onclick = () => send({action:'key',key:button.dataset.key}));
  document.getElementById('select-all').onclick = () => send({action:'key',key:'a',modifiers:2});
  document.getElementById('refresh').onclick = refresh;
  tabs.onchange = () => send({action:'tab',id:tabs.value});
  setInterval(refresh, 1500); refresh();
})();
