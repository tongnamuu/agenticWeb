DEMO_BLOG_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>DevNotes 블로그 보드</title>
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 680px; margin: 36px auto; padding: 0 20px; background: #f8fafc; color: #0f172a; }
    .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 18px; }
    input, select, textarea { width: 100%; box-sizing: border-box; padding: 10px; margin-bottom: 10px; border: 1px solid #cbd5e1; border-radius: 8px; }
    button { padding: 10px 16px; border: none; border-radius: 8px; background: #2563eb; color: white; font-weight: 600; cursor: pointer; }
    button.del-btn { background: #ef4444; padding: 6px 12px; font-size: 12px; }
    ul { list-style: none; padding: 0; }
    li { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-bottom: 1px solid #e2e8f0; }
  </style>
</head>
<body>
  <h1>📝 DevNotes 기술 메모 보드</h1>
  <div class="card">
    <input id="note-title" type="text" placeholder="메모 제목을 입력하세요" />
    <select id="note-category">
      <option value="AI">AI</option>
      <option value="Web">Web</option>
      <option value="Cloud">Cloud</option>
    </select>
    <button id="publish-note-btn" onclick="publishNote()">메모 등록</button>
  </div>
  <div class="card">
    <h3>등록된 메모 목록</h3>
    <ul id="note-list">
      <li class="note-item"><span>[AI] Gemini 2.5 URL Skill 아키텍처 정리</span><button class="del-btn" onclick="this.parentElement.remove()">삭제</button></li>
    </ul>
  </div>
  <script>
    function publishNote() {
      const titleInput = document.getElementById('note-title');
      const catSelect = document.getElementById('note-category');
      const val = titleInput.value.trim();
      if (!val) return;
      const li = document.createElement('li');
      li.className = 'note-item';
      li.innerHTML = '<span>[' + catSelect.value + '] ' + val + '</span><button class="del-btn" onclick="this.parentElement.remove()">삭제</button>';
      document.getElementById('note-list').appendChild(li);
      titleInput.value = '';
    }
  </script>
</body>
</html>"""


DEMO_PROTECTED_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>사내 보안 업무 포털 (로그인 필요)</title>
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 20px; background: #0f172a; color: #f8fafc; }
    .box { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 24px; margin-bottom: 20px; }
    input { width: 100%; box-sizing: border-box; padding: 10px 12px; margin-bottom: 12px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: white; }
    button { padding: 10px 18px; border-radius: 8px; border: none; background: #6366f1; color: white; font-weight: 600; cursor: pointer; }
    li { display: flex; justify-content: space-between; align-items: center; padding: 10px; border-bottom: 1px solid #334155; }
  </style>
</head>
<body>
  <h1>🔒 사내 보안 승인 작업 시스템</h1>
  <div id="app-root"></div>
  <script>
    function renderApp() {
      const root = document.getElementById('app-root');
      const loggedInUser = sessionStorage.getItem('portal_user');
      if (!loggedInUser) {
        root.innerHTML = `
          <div class="box login-wall" id="loginGate" data-requires-auth="true">
            <h2>로그인이 필요한 서비스입니다</h2>
            <p style="color:#94a3b8;font-size:14px;">업무 티켓을 등록하거나 처리하려면 먼저 사내 계정으로 로그인해주세요.</p>
            <form onsubmit="doLogin(event)">
              <input id="login-username" name="username" type="text" placeholder="아이디 또는 이메일 (예: admin)" />
              <input id="login-password" name="password" type="password" placeholder="비밀번호 (아무 값이나 입력)" />
              <button id="login-submit-btn" type="submit">로그인</button>
            </form>
          </div>
        `;
      } else {
        const items = JSON.parse(sessionStorage.getItem('portal_tickets') || '["보안 감사 로그 점검"]');
        const listHtml = items.map((t, idx) =>
          `<li class="ticket-item"><span>${t}</span><button onclick="removeTicket(${idx})" style="background:#ef4444;padding:5px 10px;font-size:12px;">삭제</button></li>`
        ).join('');
        root.innerHTML = `
          <div class="box" data-authenticated="true">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
              <span>👤 접속 계정: <strong>${loggedInUser}</strong></span>
              <button onclick="doLogout()" style="background:#475569;padding:6px 12px;font-size:12px;">로그아웃</button>
            </div>
            <input id="ticket-input" type="text" placeholder="할 일을 입력하세요" />
            <button id="add-ticket-btn" onclick="addTicket()">추가</button>
          </div>
          <div class="box">
            <h3>내 보안 업무 티켓 목록</h3>
            <ul id="ticket-list" style="list-style:none;padding:0;">${listHtml}</ul>
          </div>
        `;
      }
    }
    function doLogin(e) {
      e.preventDefault();
      const u = document.getElementById('login-username').value.trim();
      const p = document.getElementById('login-password').value.trim();
      if (!u || !p) return;
      sessionStorage.setItem('portal_user', u);
      renderApp();
    }
    function doLogout() {
      sessionStorage.removeItem('portal_user');
      renderApp();
    }
    function addTicket() {
      const inp = document.getElementById('ticket-input');
      const v = inp.value.trim();
      if (!v) return;
      const items = JSON.parse(sessionStorage.getItem('portal_tickets') || '["보안 감사 로그 점검"]');
      items.push(v);
      sessionStorage.setItem('portal_tickets', JSON.stringify(items));
      renderApp();
    }
    function removeTicket(idx) {
      const items = JSON.parse(sessionStorage.getItem('portal_tickets') || '["보안 감사 로그 점검"]');
      items.splice(idx, 1);
      sessionStorage.setItem('portal_tickets', JSON.stringify(items));
      renderApp();
    }
    renderApp();
  </script>
</body>
</html>"""
