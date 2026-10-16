"""Run deployment with a fake gcloud; no network or cloud resources are used."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class DeployTests(unittest.TestCase):
    def test_local_callback_is_not_sent_to_cloud_run(self):
        source = Path(__file__).resolve().parents[1] / 'deploy.sh'
        subprocess.run(['bash', '-n', str(source)], check=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'deploy.sh'
            # The production Homebrew PATH is irrelevant to this isolated fixture.
            script.write_text(source.read_text().replace('export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/google-cloud-sdk/bin:$PATH"', '# fixture PATH'))
            (root / '.env').write_text('GOOGLE_CLIENT_ID=fake\nGOOGLE_CLIENT_SECRET=fake\nFLASK_SECRET_KEY=fake\nGOOGLE_REDIRECT_URI=http://127.0.0.1:5055/auth/google/callback\n')
            gcloud = root / 'gcloud'
            gcloud.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DEPLOY_TEST_LOG"
case "$*" in
  "auth list"*) echo test@example.com ;;
  "projects describe"*) echo 800515339243 ;;
  "run services describe"*) echo https://agentic-web-800515339243.asia-northeast3.run.app ;;
esac
''')
            gcloud.chmod(0o700)
            env = {**os.environ, 'PATH': str(root)+':'+os.environ['PATH'], 'DEPLOY_TEST_LOG':str(root/'calls')}
            env.pop('GOOGLE_DEPLOY_REDIRECT_URI', None)
            env['SERVICE_NAME'] = 'agentic-web'
            env['REGION'] = 'asia-northeast3'
            result = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root/'calls').read_text()
            self.assertIn('GOOGLE_REDIRECT_URI=https://agentic-web-800515339243.asia-northeast3.run.app/auth/google/callback', calls)
            self.assertNotIn('placeholder.run.app', calls)
            self.assertNotIn('127.0.0.1', calls)
