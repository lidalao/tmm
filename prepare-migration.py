"""Fixed migration helpers for remove-old.sh. Never print Docker inspect or Tokens."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

from tmm import Failure, fetch_token


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Docker 操作失败，未继续删除')
    return result.stdout


def old_containers():
    names = docker('container', 'ls', '-a', '--format', '{{.Names}}').splitlines()
    selected = [name for name in names if name in ('tm', 'tm-before-tmm')
                or re.fullmatch(r'tm-rollback-[0-9]+', name)]
    result = []
    for name in selected:
        container = json.loads(docker('container', 'inspect', name))[0]
        config = container['Config']
        labels = config.get('Labels') or {}
        if labels.get('com.docker.compose.service'):
            raise ValueError('检测到已由 Compose 管理的 tm，请勿重复执行首次移除脚本')
        refs = [config.get('Image', ''), labels.get('io.tm-manage.image-ref', '')]
        image = json.loads(docker('image', 'inspect', container['Image']))[0]
        refs.extend(image.get('RepoTags') or [])
        refs.extend(image.get('RepoDigests') or [])
        if not any(re.fullmatch(r'traffmonetizer/cli_v2(?::[\w.-]+|@sha256:[a-f0-9]{64})?', ref)
                   for ref in refs):
            raise ValueError('同名容器并非已识别的 TM 镜像，未自动删除')
        result.append((name, container))
    return result


def configure():
    containers = old_containers()
    if Path('.env').exists():
        print('沿用当前 .env。')
        return
    current = next((item for name, item in containers if name == 'tm'), None)
    settings = {
        'TM_TOKEN_URL': 'https://raw.githubusercontent.com/lidalao/tmm/main/token.txt',
        'TM_CHECK_INTERVAL': '86400', 'TM_RETRY_INTERVAL': '300', 'TM_START_GRACE': '10',
        'TM_IMAGE': 'traffmonetizer/cli_v2:arm64v8' if os.uname().machine in ('aarch64', 'arm64')
                    else 'traffmonetizer/cli_v2:latest',
        'TM_USER': '0', 'TM_DEVICE_NAME': socket.gethostname(),
        'COMPOSE_PROJECT_NAME': 'tm-stack', 'TM_CONTAINER_NAME': 'tm',
    }
    if current:
        config = current['Config']
        settings['TM_USER'] = config.get('User') or '0'
        command = config.get('Cmd') or []
        if '--device-name' in command:
            settings['TM_DEVICE_NAME'] = command[command.index('--device-name') + 1]
        # Reuse the exact official image for this VPS architecture, not its old container.
        settings['TM_IMAGE'] = 'tmm-tm:local'
        docker('image', 'tag', current['Image'], settings['TM_IMAGE'])

    def quote(value):
        if not isinstance(value, str) or any(c in value for c in '\r\n\0'):
            raise ValueError('配置中包含无效字符')
        return "'" + value.replace("'", "\\'") + "'"

    with Path('.env').open('x') as stream:
        os.chmod('.env', 0o600)
        stream.write(''.join(f'{key}={quote(value)}\n' for key, value in settings.items()))
    print('已自动生成 .env，配置每天检查一次。')


def check(config_path):
    config = json.loads(Path(config_path).read_text())
    existing = docker('container', 'ls', '-a', '-q',
                      '--filter', 'label=com.docker.compose.project=' + config['name'],
                      '--filter', 'label=com.docker.compose.service=tmm').strip()
    if existing:
        raise ValueError('此项目已有 TMM 容器，请勿重复执行首次移除脚本')
    old_containers()
    fetch_token(config['services']['tmm']['environment']['TM_TOKEN_URL'])
    print('远端 Token 可访问且格式有效，未显示 Token 内容。')


def main():
    action = sys.argv[1]
    if action == 'configure':
        configure()
    elif action == 'check':
        check(sys.argv[2])
    elif action == 'remove-containers':
        for name, _ in old_containers():
            docker('container', 'rm', '-f', name)
            print('已删除旧容器：' + name)
    else:
        raise ValueError('未知迁移操作')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, (ValueError, Failure)) else '迁移检查失败，未继续操作。', file=sys.stderr)
        raise SystemExit(1)
