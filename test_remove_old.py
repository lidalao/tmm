"""Execute the destructive script only against rewritten temp paths and fake commands."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent

FAKE_COMMAND = r'''#!/usr/bin/env python3
import hashlib,json,os,sys
from pathlib import Path
p=Path(os.environ['TMM_FIXTURE_STATE'])
s=json.loads(p.read_text())
tool=Path(sys.argv[0]).name
a=sys.argv[1:]
code=0
out=''
s['ops'].append([tool]+a)
if tool=='id': out='0'
elif tool=='uname': out='Linux' if '-s' in a else 'x86_64'
elif tool=='curl':
    target=Path(a[a.index('-o')+1])
    url=next(x for x in a if x.startswith('https://'))
    asset='buildx-v0.26.1.linux-amd64' if '/buildx/' in url else 'docker-compose-linux-x86_64'
    binary=b'fixture plugin'
    if url.endswith('/checksums.txt'):
        digest='0'*64 if s.get('bad_checksum') else hashlib.sha256(binary).hexdigest()
        target.write_text(digest+'  '+asset+'\n')
    else: target.write_bytes(binary)
elif tool=='systemctl':
    if a[0]=='is-active': code=0 if s['active'] else 3
    elif a[0]=='stop':
        if s.get('stop_fail'): code=1
        else: s['active']=False
elif tool=='docker':
    if a[:2]==['compose','version']: out='v2.0.0' if s.get('outdated') else 'v2.39.2'
    elif a[:2]==['buildx','version']: out='v0.10.0' if s.get('outdated') else 'github.com/docker/buildx v0.26.1'
    elif a[:3]==['compose','build','tmm']:
        code=1 if s.get('build_fail') else 0
    elif a[0]=='compose' and '--format' in a:
        out=json.dumps({'name':'tm-stack','services':{
            'tm':{'image':'tmm-tm:local'},
            'tmm':{'environment':{'TM_TOKEN_URL':'https://raw.githubusercontent.com/lidalao/tmm/main/token.txt'}}}})
    elif a[:2]==['container','ls']:
        if '-q' in a: out='existing' if s.get('new_tmm') else ''
        else: out='\n'.join(s['containers'])
    elif a[:2]==['container','inspect']:
        out=json.dumps([s['containers'][a[2]]])
    elif a[:2]==['image','inspect']:
        ref='ubuntu:latest' if s.get('wrong_image') else 'traffmonetizer/cli_v2:latest'
        out=json.dumps([{'RepoTags':[ref]}])
    elif a[:2]==['container','rm']:
        s['containers'].pop(a[-1])
p.write_text(json.dumps(s))
print(out)
sys.exit(code)
'''


class RemoveOldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.paths = [self.root / p for p in ('opt/tm-manage', 'etc/tm-manage', 'var/lib/tm-manage')]
        for path in self.paths:
            path.mkdir(parents=True)
            (path / 'private').write_text('old private identity')
        self.unit = self.root / 'etc/systemd/system/tm-agent.service'
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text('old unit')
        script = (SOURCE / 'remove-old.sh').read_text()
        for old in ('/opt/tm-manage', '/etc/tm-manage', '/var/lib/tm-manage', '/etc/systemd/system'):
            script = script.replace(old, str(self.root / old.lstrip('/')))
        (self.bundle / 'remove-old.sh').write_text(script)
        shutil.copyfile(SOURCE / 'prepare-migration.py', self.bundle / 'prepare-migration.py')
        (self.bundle / 'tmm.py').write_text('''import json,os
class Failure(Exception): pass
def fetch_token(url):
    if json.load(open(os.environ['TMM_FIXTURE_STATE'])).get('fetch_fail'):
        raise RuntimeError('fetch failed')
    return 'fixture-token'
''')
        commands = self.root / 'commands'
        commands.mkdir()
        for name in ('docker', 'systemctl', 'id', 'uname', 'curl'):
            command = commands / name
            command.write_text(FAKE_COMMAND)
            command.chmod(0o755)
        self.state_path = self.root / 'state.json'
        old = {'Image':'sha256:'+'a'*64,
               'Config':{'Image':'traffmonetizer/cli_v2:latest','User':'1000:1001',
                         'Cmd':['start','accept','--token','old','--device-name','existing-device'], 'Labels':{}}}
        self.state = {'ops':[], 'active':True, 'containers':{
            'tm':old, 'tm-before-tmm':old, 'tm-rollback-2':old, 'unrelated-app':{}}}
        self.env = dict(os.environ, PATH=str(commands)+os.pathsep+os.environ['PATH'],
                        TMM_FIXTURE_STATE=str(self.state_path), DOCKER_CONFIG=str(self.root / 'docker-config'))

    def execute(self):
        self.state_path.write_text(json.dumps(self.state))
        result = subprocess.run(['bash', str(self.bundle / 'remove-old.sh')],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.state = json.loads(self.state_path.read_text())
        return result

    def test_removes_only_old_agent_and_tm_after_build_and_stop(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertFalse(self.state['active'])
        self.assertEqual(list(self.state['containers']), ['unrelated-app'])
        self.assertTrue(all(not path.exists() for path in self.paths))
        self.assertFalse(self.unit.exists())
        config = (self.bundle / '.env').read_text()
        self.assertIn('1000:1001', config)
        self.assertIn('existing-device', config)
        self.assertNotIn("'old'", config)
        ops = self.state['ops']
        self.assertLess(ops.index(['docker','compose','build','tmm']),
                        ops.index(['systemctl','stop','tm-agent.service']))
        self.assertLess(ops.index(['systemctl','stop','tm-agent.service']),
                        ops.index(['docker','container','rm','-f','tm']))

    def test_build_or_fetch_failure_keeps_old_service_containers_and_files(self):
        for flag in ('build_fail','fetch_fail'):
            with self.subTest(flag=flag):
                self.state[flag] = True
                result = self.execute()
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(self.state['active'])
                self.assertIn('tm', self.state['containers'])
                self.assertTrue(self.unit.exists())
                self.assertTrue(all(path.exists() for path in self.paths))
                self.state[flag] = False

    def test_stop_failure_aborts_without_removing_containers_or_files(self):
        self.state['stop_fail'] = True
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertIn('tm', self.state['containers'])
        self.assertTrue(self.unit.exists())
        self.assertTrue(all(path.exists() for path in self.paths))

    def test_refuses_wrong_image_or_existing_new_tmm(self):
        self.state['wrong_image'] = True
        for item in self.state['containers'].values():
            if item:
                item['Config']['Image'] = 'ubuntu:latest'
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertTrue(self.state['active'])
        self.state['wrong_image'] = False
        self.state['new_tmm'] = True
        for item in self.state['containers'].values():
            if item:
                item['Config']['Image'] = 'traffmonetizer/cli_v2:latest'
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertTrue(self.state['active'])
        self.assertIn('tm', self.state['containers'])

    def test_outdated_plugins_are_installed_only_after_checksum_verification(self):
        self.state['outdated'] = True
        self.assertEqual(self.execute().returncode, 0)
        plugins = self.root / 'docker-config/cli-plugins'
        self.assertTrue((plugins / 'docker-compose').exists())
        self.assertTrue((plugins / 'docker-buildx').exists())

    def test_bad_plugin_checksum_aborts_before_removing_old_services(self):
        self.state.update(outdated=True, bad_checksum=True)
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertTrue(self.state['active'])
        self.assertTrue(self.unit.exists())
        self.assertIn('tm', self.state['containers'])
        self.assertFalse((self.root / 'docker-config/cli-plugins/docker-compose').exists())


if __name__ == '__main__':
    unittest.main()
