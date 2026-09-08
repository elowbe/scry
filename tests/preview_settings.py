"""Local-only visual fixture for the actual player settings form (no stream)."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from gamestream.config import load_config, PROJECT_ROOT
from gamestream.settings import public_settings, settings_schema
from gamestream.quality import quality_options


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.strip('/') or 'index.html'
        if name not in ('index.html', 'styles.css', 'app.js'):
            self.send_error(404)
            return
        content = (PROJECT_ROOT / 'web' / name).read_text()
        mime = {'index.html': 'text/html', 'styles.css': 'text/css', 'app.js': 'text/javascript'}[name]
        if name == 'app.js':
            config = load_config()
            fixture = dict(settings=public_settings(config), settings_schema=settings_schema(),
                           quality_options=quality_options(config), dlss_target_fps=60)
            content = content.removesuffix('initialize();\n')
            content += '\nstate.config = ' + json.dumps(fixture) + ';\n'
            content += '''
state.settings = state.config.settings;
bindEvents(); buildAdvancedSettings(); renderLiveSettings();
$("#player").classList.remove("hidden");
$("#playerBackdrop").classList.add("ready");
$("#streamSettings").classList.remove("hidden");
$("#capturePrompt").classList.add("hidden");
'''
        data = content.encode()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == '__main__':
    HTTPServer(('127.0.0.1', 8766), Handler).serve_forever()
