"""Web server entrypoint: python app.py, or WSGI target app:app."""
import os
from agentic_web.web import create_app

app = create_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5055'))
    host = os.environ.get('HOST', '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')
    print(f'Gemini Web Agent: http://{host}:{port}')
    app.run(host=host, port=port, debug=False, threaded=True)
