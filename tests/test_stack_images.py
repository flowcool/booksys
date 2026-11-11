"""Candidate builds preserve the downloader and all probe evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class StackImageTests(unittest.TestCase):
    def test_three_role_build_retains_stopped_probes_and_logs(self):
        spec = importlib.util.spec_from_file_location('stack_images', Path(__file__).resolve().parents[1]/'ops/stack_images.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        def command(*argv):
            calls.append(argv)
            if argv[:3] == ('git', 'status', '--porcelain'):
                return ''
            if argv[:2] == ('git', 'rev-parse'):
                return 'a'*40
            if argv[:3] == ('docker', 'image', 'inspect'):
                return 'sha256:'+'b'*64
            if argv[:2] == ('docker', 'create'):
                return 'c'*64
            if argv[:2] == ('docker', 'inspect'):
                return json.dumps({'Id': 'c'*64, 'State': {'Running': False, 'ExitCode': 0}})
            return ''
        def run(argv, **kwargs):
            calls.append(tuple(argv))
            if argv[:2] == ['docker', 'logs']:
                kwargs['stdout'].write(json.dumps({'application_sha256': module.code_digest(), 'python': '3.14.10', 'psycopg': '3.3.8'})+'\n')
            return subprocess.CompletedProcess(argv, 0)
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'command', side_effect=command), patch.object(module.subprocess, 'run', side_effect=run), patch.object(sys, 'argv', ['stack_images', '--evidence', str(Path(folder)/'proof')]):
            module.main()
            proof = Path(folder)/'proof'
            value = json.loads((proof/'images.json').read_text())
            self.assertEqual(set(value['images']), {'app', 'reader', 'launcher'})
            for role in value['images']:
                self.assertTrue((proof/(role+'-probe.log')).is_file())
                self.assertEqual(value['images'][role]['probe']['Id'], 'c'*64)
            self.assertFalse(any('--rm' in call or 'rm' in call for call in calls))
            self.assertEqual(len([call for call in calls if call[:2] == ('docker', 'build')]), 3)
            self.assertEqual(len([call for call in calls if call[:2] == ('docker', 'create')]), 3)
