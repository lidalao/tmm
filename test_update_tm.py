"""Run the updater with an isolated Docker fixture, never touching real containers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

FAKE = '''#!/usr/bin/env python3
import os,sys,json
from pathlib import Path
a=sys.argv[1:]; mode=os.environ['UPDATE_CASE']
if a[0]=='pull' and mode=='pull_fail': sys.exit(1)
if a[:2]==['compose','ps']:
    if mode!='missing_manager': print('legacy-manager-id')
elif a[0]=='exec':
    assert a[1]=='legacy-manager-id', a
    print('custom-tm' if a[-1]=='TM_CONTAINER_NAME' else 'tmm-tm:local')
elif a[0]=='inspect':
    if a[2]=='{{.State.Running}}': print('false' if mode=='stopped_manager' else 'true')
    elif a[2]=='{{.Image}}': print('sha256:new' if mode=='unchanged' else 'sha256:old')
    else:
        assert a[-1]=='custom-tm', a
        p=Path(os.environ['UPDATE_COUNT']); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
        image='sha256:old' if mode=='wrong_image' else 'sha256:new'
        state='exited' if mode=='exited' else 'running'
        health='unhealthy' if mode=='unhealthy' else 'none'
        restart=n if mode=='restarts' else 0
        if mode=='inspect_fail': sys.exit(1)
        # A real inspect object omits State.Health when no health check exists.
        status={'Status':state, 'StartedAt':'started'}
        if mode=='unhealthy': status['Health']={'Status':health}
        obj={'Id':'container', 'Image':image, 'State':status, 'RestartCount':restart}
        if a[2]=='{{json .}}': print(json.dumps(obj))
        elif '.State.Health' in a[2] and 'Health' not in status:
            sys.exit(1)
        else: print(f'container|{image}|{state}|{restart}|started|{health}')
elif a[:2]==['image','inspect']:
    print('amd64' if a[3]=='{{.Architecture}}' else 'sha256:new')
'''


class UpdateTests(unittest.TestCase):
    def execute(self, case):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            docker=root/'docker'; docker.write_text(FAKE); docker.chmod(0o755)
            script=(Path(__file__).parent/'update-tm.sh').read_text().replace('cd /root/tmm', 'cd "'+temp+'"')
            env=dict(os.environ, PATH=temp+os.pathsep+os.environ['PATH'], UPDATE_CASE=case,
                     UPDATE_COUNT=str(root/'counter'), TM_UPDATE_TIMEOUT='3', TM_UPDATE_STABLE_SECONDS='1')
            return subprocess.run(['bash'], input=script, text=True, errors="replace", capture_output=True, env=env, timeout=10)

    def test_success_and_unchanged_show_actual_image_and_state(self):
        for case, message in [('success','成功，已使用'),('unchanged','已是最新镜像')]:
            with self.subTest(case=case):
                result=self.execute(case)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn(message,result.stdout)
                self.assertIn('当前镜像 ID：sha256:new',result.stdout)
                self.assertIn('容器状态：running',result.stdout)

    def test_missing_or_stopped_manager_has_actionable_message(self):
        for case, message in [('missing_manager', '未找到当前 Compose 项目的 TMM'),
                              ('stopped_manager', 'TMM 服务未运行')]:
            with self.subTest(case=case):
                result = self.execute(case)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
                self.assertNotIn('更新结果：成功', result.stdout)

    def test_wrong_image_exit_health_or_restart_loop_never_report_success(self):
        for case in ('wrong_image','exited','unhealthy','restarts','pull_fail','inspect_fail'):
            with self.subTest(case=case):
                result=self.execute(case)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('更新结果：失败',result.stdout)
                self.assertNotIn('更新结果：成功',result.stdout)


if __name__=='__main__':
    unittest.main()
