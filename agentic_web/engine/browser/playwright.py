import re
from pathlib import Path
from playwright.sync_api import sync_playwright
from agentic_web.engine.skills.registry import URLMatcher


class Browser:
    def __init__(self, profile='.browser-profile', headless=False):
        self.runtime = sync_playwright().start()
        try:
            options = {'headless': headless}
            if not Path(self.runtime.chromium.executable_path).exists():
                options['channel'] = 'chrome'
            self.context = self.runtime.chromium.launch_persistent_context(profile, **options)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(8000)
        except Exception:
            self.runtime.stop()
            raise

    @property
    def current_url(self):
        return self.page.url

    def open(self, url):
        self.page.goto(url, wait_until='domcontentloaded', timeout=30000)

    def snapshot(self):
        return self.page.evaluate('''() => ({
          url: location.href, title: document.title,
          text: document.body.innerText.slice(0, 24000),
          password_visible: [...document.querySelectorAll('input[type=password]')]
              .some(e => e.getClientRects().length > 0),
          controls: [...document.querySelectorAll('input,textarea,button,select,a,[role],form')]
            .filter(e => e.getClientRects().length > 0).slice(0, 250).map(e => ({
              tag:e.tagName, id:e.id, name:e.getAttribute('name'), type:e.getAttribute('type'),
              role:e.getAttribute('role'), aria:e.getAttribute('aria-label'),
              placeholder:e.getAttribute('placeholder'), text:e.innerText?.slice(0,200),
              testid:e.getAttribute('data-testid'), href:e.getAttribute('href')
            }))
        })''')

    def execute_tool(self, tool, arguments):
        required = {p['name'] for p in tool.get('parameters', [])}
        if set(arguments) != required:
            raise ValueError(f'Expected arguments: {sorted(required)}')
        def render(value):
            def replace(match):
                key = match.group(1)
                if key not in arguments:
                    raise ValueError(f'Missing parameter {key}')
                return arguments[key]
            return re.sub(r'\{\{([a-zA-Z0-9_]+)\}\}', replace, value)
        completed = []
        for step in tool['steps']:
            # Recheck at every step, including after navigation caused by a click.
            if not URLMatcher.is_exact_url_match(self.current_url, tool['bound_normalized_url']):
                raise ValueError('URL_MISMATCH_BLOCKED')
            locator = self.page.locator(render(step['selector'])).first
            action, value = step['action'], render(step.get('value', ''))
            if action == 'click': locator.click()
            elif action == 'fill': locator.fill(value)
            elif action == 'press': locator.press(value)
            elif action == 'select': locator.select_option(value)
            elif action == 'check': locator.check()
            elif action == 'uncheck': locator.uncheck()
            elif action == 'wait': locator.wait_for(state='visible')
            else: raise ValueError(f'Unsupported action: {action}')
            completed.append(action)
        return {'action_result': completed, 'verification': 'Pending independent root verification'}

    def close(self):
        try:
            self.context.close()
        finally:
            self.runtime.stop()
