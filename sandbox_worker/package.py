"""构建或离线打包隔离执行器；不上传、不启动服务、不包含任何真实凭据。"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PINNED = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}")
SOURCES = ('worker.py', 'runner.py', 'Dockerfile', 'seccomp.json', 'requirements.txt',
           'worker.env.example', 'jianzi-worker.service', 'nginx.conf.example', 'acceptance.py', 'package.py')


def run(*args):
    return subprocess.run([os.getenv('SANDBOX_PODMAN', '/usr/bin/podman'), *args],
                          check=True, stdout=subprocess.PIPE, timeout=1800).stdout


def sha256(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    build = actions.add_parser('build', help='基础镜像必须已预载且固定 digest；只构建固定 runner')
    build.add_argument('--base-image', required=True)
    build.add_argument('--tag', required=True)
    export = actions.add_parser('export', help='导出已审批的固定镜像和控制面源文件')
    export.add_argument('--image', required=True)
    export.add_argument('--output', required=True)
    export.add_argument('--wheels-dir', help='预下载的 Worker Python 3.11 Linux 依赖 wheel 目录')
    args = parser.parse_args()
    info = json.loads(run('info', '--format=json'))
    if info['host']['security'].get('rootless') is not True:
        raise ValueError('构建与打包要求 rootless Podman')
    if args.action == 'build':
        if not PINNED.fullmatch(args.base_image) or not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*', args.tag):
            raise ValueError('基础镜像必须固定 digest，目标标签不可包含额外参数')
        run('image', 'inspect', args.base_image)
        run('build', '--network=none', '--pull=never', '--build-arg', 'PYTHON_BASE=' + args.base_image,
            '--tag', args.tag, '--file', str(ROOT / 'Dockerfile'), str(ROOT))
        print('固定 runner 已构建；管理员须审核产物并取得 repository@sha256 引用后 export，不自动推送仓库。')
        return
    if not PINNED.fullmatch(args.image):
        raise ValueError('仅导出 repository@sha256 格式的已审批镜像')
    image = json.loads(run('image', 'inspect', args.image))[0]
    if args.image not in (image.get('RepoDigests') or []):
        raise ValueError('本地镜像与审批 digest 不一致')
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError('目标包已存在，请使用新文件名，避免覆盖旧交付物')
    with tempfile.TemporaryDirectory(prefix='jianzi-worker-package-') as directory:
        stage = Path(directory)
        archive = stage / 'python-image.oci.tar'
        run('save', '--format=oci-archive', '--output', str(archive), args.image)
        entries = [(ROOT / name, 'jianzi-worker/' + name) for name in SOURCES]
        entries.append((archive, 'python-image.oci.tar'))
        if args.wheels_dir:
            wheels = sorted(Path(args.wheels_dir).glob('*.whl'))
            if not wheels or any(p.is_symlink() for p in wheels):
                raise ValueError('wheel 目录为空或包含符号链接')
            entries.extend((path, 'wheels/' + path.name) for path in wheels)
        manifest = {'image': args.image, 'architecture': image.get('Architecture'),
                    'seccomp_sha256': sha256(ROOT / 'seccomp.json'),
                    'files': {name: sha256(path) for path, name in entries}, 'isolation_verified': False}
        manifest_file = stage / 'manifest.json'
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        entries.append((manifest_file, 'manifest.json'))
        with tarfile.open(output, 'x:gz') as bundle:
            for path, name in entries:
                bundle.add(path, arcname=name, recursive=False)
    print(json.dumps({'bundle': str(output), 'sha256': sha256(output), 'image': args.image,
                      'isolation_verified': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
