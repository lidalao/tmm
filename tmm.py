"""Poll a plaintext Token URL and reconcile only the Compose tm service. Stdlib only."""
import fcntl
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

LOG = logging.getLogger('tmm')
SETTINGS = ('TM_IMAGE', 'TM_USER', 'TM_DEVICE_NAME')


class Failure(Exception):
    """Only fixed, safe messages may reach logs; never log HTTP/Docker output."""


def valid_token(value):
    if (not isinstance(value, str) or not value or len(value.encode()) > 4096
            or any(not c.isascii() or c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value)
            or value.startswith(('<', '{', '['))):
        raise Failure('Token 必须是单行非空文本，已忽略异常内容')
    return value


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise Failure('拒绝跳转到非 HTTPS 地址')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_token(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise Failure('TM_TOKEN_URL 必须是 HTTPS 地址')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'tmm/1.0', 'Cache-Control': 'no-cache'})
        with urllib.request.build_opener(HTTPSOnly()).open(req, timeout=30) as response:
            raw = response.read(4097)
        if len(raw) > 4096:
            raise Failure('Token 文件过大，已忽略')
        return valid_token(raw.decode('utf-8-sig').strip())
    except Failure:
        raise
    except Exception:
        raise Failure('抓取 Token 失败，保留现有配置') from None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.token-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


class Manager:
    def __init__(self, env=None, state_path=Path('/data/applied.json'), run=None, sleep=time.sleep):
        self.env = dict(os.environ if env is None else env)
        self.path = Path(state_path)
        self.run = run or subprocess.run
        self.sleep = sleep
        self.name = self.env.get('TM_CONTAINER_NAME', 'tm')
        self.project = self.env.get('COMPOSE_PROJECT_NAME', 'tm-stack')
        self.grace = int(self.env.get('TM_START_GRACE', '10'))
        self.settings = {key: self.env.get(key, default) for key, default in
                         zip(SETTINGS, ('traffmonetizer/cli_v2:latest', '0', 'tm'))}

    def command(self, argv, env=None, timeout=180):
        try:
            result = self.run(argv, env=self.env if env is None else env, capture_output=True,
                              text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Failure('Docker 不可用或操作超时') from None
        return result

    def inspect(self):
        result = self.command(['docker', 'container', 'inspect', self.name])
        if result.returncode:
            # Daemon failures must not be mistaken for a missing container.
            available = self.command(['docker', 'container', 'ls', '-a', '--format', '{{.Names}}'])
            if available.returncode:
                raise Failure('无法连接 Docker')
            if self.name in available.stdout.splitlines():
                raise Failure('无法读取 tm 容器状态')
            return None
        container = json.loads(result.stdout)[0]
        labels = container['Config'].get('Labels') or {}
        if (labels.get('com.docker.compose.project') != self.project
                or labels.get('com.docker.compose.service') != 'tm'):
            raise Failure('同名 tm 不属于本 Compose 项目；请先停用旧 Agent 并备份旧容器')
        return container

    def load(self):
        if not self.path.exists():
            return None
        try:
            state = json.loads(self.path.read_text())
            valid_token(state['token'])
            if set(state['settings']) != set(SETTINGS) or not all(
                    isinstance(value, str) for value in state['settings'].values()):
                raise ValueError()
            return state
        except Exception:
            raise Failure('本地 Token 状态文件损坏，请先检查 /data/applied.json') from None

    def apply(self, state, force=False):
        # Explicit service targeting starts tm's profile, never tmm or other services.
        env = dict(self.env, **state['settings'], TM_TOKEN=state['token'])
        command = ['docker', 'compose', '--ansi', 'never', '--project-name', self.project,
                   '--project-directory', '/project', '--env-file', '/dev/null',
                   '-f', '/project/compose.yaml', 'up', '-d', '--no-deps', '--no-build',
                   '--pull', 'missing', '--wait', '--wait-timeout', '30']
        if force:
            command.append('--force-recreate')
        result = self.command(command + ['tm'], env=env)
        if result.returncode:
            raise Failure('tm 创建或启动失败')
        self.sleep(self.grace)
        container = self.inspect()
        expected = ['start', 'accept', '--token', state['token'], '--device-name', state['settings']['TM_DEVICE_NAME']]
        if (not container or not container['State']['Running']
                or container['State'].get('Restarting') or container['Config'].get('Cmd') != expected):
            raise Failure('tm 启动检查失败')

    def restore(self):
        saved = self.load()
        if saved:
            self.inspect()  # Reject collisions before Compose can touch containers.
            self.apply(saved)
            LOG.info('已按保存的 Token 恢复 tm，开始检查远端')

    def check(self, fetch=fetch_token):
        token = fetch(self.env['TM_TOKEN_URL'])
        saved = self.load()
        container = self.inspect()
        target = {'token': token, 'settings': self.settings}
        expected = ['start', 'accept', '--token', token, '--device-name', self.settings['TM_DEVICE_NAME']]
        if (saved == target and container and container['State']['Running']
                and not container['State'].get('Restarting') and container['Config'].get('Cmd') == expected):
            LOG.info('Token 未变化，tm 运行正常，无需重建')
            return
        LOG.info('首次启动或配置发生变化，正在创建 tm' if saved != target else '正在恢复 tm')
        try:
            self.apply(target, force=saved != target)
            save_state(self.path, target)
        except Exception:
            if saved:
                try:
                    self.apply(saved, force=True)
                    LOG.warning('更新未完成，已使用保存的旧配置恢复 tm')
                except Exception:
                    LOG.error('恢复旧配置失败，请检查 Docker 和 tm 日志')
            raise Failure('新配置未保存，稍后重试') from None
        LOG.info('tm 已启动，新 Token 已保存（未验证 TM 账号认证）')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    manager = Manager()
    interval = int(os.environ.get('TM_CHECK_INTERVAL', '86400'))
    retry = int(os.environ.get('TM_RETRY_INTERVAL', '300'))
    if interval < 10 or retry < 10 or not 0 <= manager.grace <= 300:
        raise Failure('检查与重试间隔不能小于 10 秒，启动观察时间须为 0～300 秒')
    if not os.environ.get('TM_TOKEN_URL'):
        raise Failure('请填写 TM_TOKEN_URL')
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    with (manager.path.parent / 'tmm.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('已有 tmm 实例正在管理该状态目录') from None
        try:
            manager.restore()
        except Failure as error:
            LOG.warning('%s', error)
        LOG.info('tmm 已启动：每 %s 秒检查一次，失败后 %s 秒重试', interval, retry)
        while not stopped.is_set():
            delay = interval
            try:
                manager.check()
            except Failure as error:
                LOG.error('%s', error)
                delay = retry
            except Exception:
                LOG.error('检查失败，保留本地状态，稍后重试')
                delay = retry
            stopped.wait(delay)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        logging.error('%s', error if isinstance(error, Failure) else 'tmm 配置或状态异常，请检查配置')
        raise SystemExit(1)
