import json
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
class DeploymentContracts(unittest.TestCase):
    def test_development_and_production_controls_agree(self):
        dev=json.loads(subprocess.check_output(['docker','compose','-f',str(ROOT/'docker-compose.yml'),'config','--format','json'],text=True))
        prod=json.loads((ROOT/'deploy/shutdown/youtube-admin.compose.json').read_text())
        self.assertEqual(prod['name'],'youtube-admin')
        self.assertEqual(set(prod['services']),{'youtube-admin'})
        for service in [dev['services']['cron'],prod['services']['youtube-admin']]:
            self.assertTrue(service['init'])
            self.assertEqual(service['stop_grace_period'],'1m0s' if service is dev['services']['cron'] else '60s')
            self.assertEqual(service['cap_drop'],['ALL'])
            self.assertEqual(set(service['cap_add']),{'CHOWN','SETUID','SETGID'})
            self.assertEqual(service['security_opt'],['no-new-privileges:true'])
        for name in ['metrics','transcripts']:
            self.assertEqual(prod['volumes'][name],{'external':True,'name':'youtube-admin_'+name})
    def test_build_and_entrypoint_include_supervisor(self):
        self.assertIn('COPY cron-supervisor.py .',(ROOT/'Dockerfile').read_text())
        self.assertIn('exec python3 /app/cron-supervisor.py',(ROOT/'entrypoint-cron.sh').read_text())
        subprocess.run(['bash','-n',str(ROOT/'entrypoint-cron.sh')],check=True)
