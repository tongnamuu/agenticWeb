// Keep existing JSON API calls authenticated without changing each feature.
(() => {
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const request = new Request(input, init);
    if (new URL(request.url).origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(request.method)) {
      request.headers.set('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
    }
    const response = await originalFetch(request);
    if (response.status === 401 && new URL(request.url).origin === location.origin) {
      location.assign('/auth/login');
      throw new Error('로그인이 만료되었습니다.');
    }
    return response;
  };
})();
