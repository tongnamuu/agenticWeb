"""Interactive view of the user's existing CDP browser, including OAuth popups."""
import math
import requests
import websocket


def pages(browser):
    if browser.proc is None:
        browser.start()
    response = requests.get(f'http://127.0.0.1:{browser.port}/json/list', timeout=3)
    response.raise_for_status()
    return [page for page in response.json() if page.get('type') == 'page']


def select_page(browser, identifier):
    page = next((page for page in pages(browser) if page['id'] == identifier), None)
    if page is None:
        raise ValueError('브라우저 탭을 찾을 수 없습니다.')
    connection = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=15)
    if browser.ws:
        browser.ws.close()
    browser.ws = connection
    browser._send_cdp('Page.enable')
    browser._send_cdp('Runtime.enable')
    browser._send_cdp('Page.bringToFront')
    browser.current_url = page.get('url', '')


def frame(browser):
    available = pages(browser)
    if not browser.ws or not browser.ws.connected:
        if not available:
            raise ValueError('열린 탭이 없습니다.')
        select_page(browser, available[0]['id'])
    viewport = browser.evaluate_js('() => ({url:location.href,width:innerWidth,height:innerHeight})')
    browser.current_url = viewport['url']
    return {**viewport, 'screenshot': browser.capture_screenshot(),
            'tabs': [{'id': page['id'], 'title': page.get('title', ''), 'url': page.get('url', '')}
                     for page in pages(browser)]}


def control(browser, data):
    action = data.get('action')
    if action == 'tab':
        select_page(browser, data.get('id'))
        return
    if action == 'text':
        text = data.get('text')
        if not isinstance(text, str) or not 1 <= len(text) <= 10000:
            raise ValueError('입력 길이는 1~10000자여야 합니다.')
        browser._send_cdp('Input.insertText', {'text': text})
        return
    if action == 'key':
        key = data.get('key')
        keys = {'Enter': 13, 'Tab': 9, 'Backspace': 8, 'Delete': 46, 'Escape': 27,
                'ArrowLeft': 37, 'ArrowUp': 38, 'ArrowRight': 39, 'ArrowDown': 40,
                'Home': 36, 'End': 35, 'a': 65}
        if key not in keys:
            raise ValueError('지원하지 않는 키입니다.')
        modifiers = data.get('modifiers', 0)
        if type(modifiers) is not int or not 0 <= modifiers <= 15:
            raise ValueError('잘못된 키 조합입니다.')
        params = {'key': key, 'windowsVirtualKeyCode': keys[key], 'modifiers': modifiers}
        browser._send_cdp('Input.dispatchKeyEvent', {**params, 'type': 'rawKeyDown'})
        browser._send_cdp('Input.dispatchKeyEvent', {**params, 'type': 'keyUp'})
        return
    if action not in ('click', 'scroll'):
        raise ValueError('지원하지 않는 브라우저 동작입니다.')
    x, y = data.get('x'), data.get('y')
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10000 for value in (x, y)):
        raise ValueError('잘못된 좌표입니다.')
    if action == 'click':
        for kind in ('mousePressed', 'mouseReleased'):
            browser._send_cdp('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    else:
        delta = data.get('delta', 0)
        if type(delta) not in (int, float) or not math.isfinite(delta) or abs(delta) > 2000:
            raise ValueError('잘못된 스크롤 값입니다.')
        browser._send_cdp('Input.dispatchMouseEvent', {'type':'mouseWheel', 'x':x, 'y':y, 'deltaX':0, 'deltaY':delta})
