"""专用 Linux 主机的真实隔离验收；启动实际容器，不自动开启生产入口。"""
import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

from worker import Worker, command, create_args, profile, digest, LABEL


PROBE = '''def main(data):
    import os, socket
    from pathlib import Path
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    def denied(path, mode):
        try:
            with open(path, mode) as f:
                if mode == 'w': f.write('probe')
                else: f.read(1)
            return False
        except OSError:
            return True
    try:
        sock = socket.socket()
        sock.settimeout(1)
        sock.connect(('1.1.1.1', 443))
        network_denied = False
        sock.close()
    except OSError:
        network_denied = True
    cgroup = Path('/sys/fs/cgroup')
    return {'uid': os.getuid(), 'caps': status['CapEff'].strip(),
            'nnp': status['NoNewPrivs'].strip(), 'seccomp': status['Seccomp'].strip(),
            'host_denied': denied(data['sentinel'], 'r'),
            'business_denied': not Path('/app/backend/data').exists() and not Path('/app/data').exists(),
            'readonly': denied('/opt/forbidden-probe', 'w'), 'network_denied': network_denied,
            'cpu': (cgroup/'cpu.max').read_text().strip(),
            'memory': (cgroup/'memory.max').read_text().strip(),
            'pids': (cgroup/'pids.max').read_text().strip(),
            'tmp_bytes': os.statvfs('/tmp').f_blocks * os.statvfs('/tmp').f_frsize,
            'value': data['n'] * 2}
'''
PIDS = '''def main(data):
    import subprocess
    children, limited = [], False
    try:
        for _ in range(64):
            try: children.append(subprocess.Popen(['/usr/local/bin/python3', '-I', '-c', 'import time; time.sleep(20)']))
            except OSError:
                limited = True
                break
        return {'limited': limited, 'spawned': len(children)}
    finally:
        for child in children: child.kill()
        for child in children: child.wait()
'''
TMP = '''def main(data):
    limited, count = False, 0
    try:
        for i in range(64):
            with open('/tmp/probe-' + str(i), 'wb') as f: f.write(b'x' * 1048576)
            count += 1
    except OSError:
        limited = True
    return {'limited': limited, 'count': count}
'''


async def verify(args):
    if sys.platform != 'linux' or os.geteuid() == 0 or not args.acknowledge_dedicated_host:
        raise RuntimeError('仅限专用 Linux 非 root 账户，并须确认业务数据未部署在本机')
    directory = Path(os.environ['SANDBOX_WORKER_STATE']).resolve()
    worker = Worker(directory, os.environ['SANDBOX_PYTHON_IMAGE'], secrets.token_hex(32), accepted=True)
    results = []
    sentinel = directory / ('acceptance-sentinel-' + uuid.uuid4().hex)
    await worker.startup()
    try:
        if not worker.ready:
            raise RuntimeError('Worker 运行时探测未通过；不得开启脚本入口')
        sentinel.write_text('仅用于验收的非敏感哨兵')
        owner = hashlib.sha256(('acceptance-' + uuid.uuid4().hex).encode()).hexdigest()

        def submit(code, data=None):
            task_id = uuid.uuid4().hex
            worker.submit(owner, task_id, {'code': code, 'code_hash': hashlib.sha256(code.encode()).hexdigest(),
                'version': hashlib.sha256(code.encode()).hexdigest(), 'data': data or {},
                'profile_hash': digest(profile(worker.image)), 'expires_at': time.time() + 90})
            return task_id

        async def run(code, data=None):
            task_id = submit(code, data)
            await asyncio.wait_for(asyncio.shield(worker.tasks[task_id]), 110)
            await asyncio.sleep(0)
            result = worker.lookup(owner, task_id)
            code, _, _ = await command('container', 'exists', 'jzpy-' + task_id)
            if code != 1:
                raise RuntimeError('任务结束但容器未清理')
            return result

        def passed(name, condition):
            results.append({'check': name, 'passed': bool(condition)})
            if not condition:
                raise RuntimeError('隔离验收失败：' + name)

        result = await run(PROBE, {'sentinel': str(sentinel), 'n': 21})
        passed('真实计算输入输出', result['status'] == 'completed' and result.get('output', {}).get('value') == 42)
        out = result['output']
        passed('非 root／无 capabilities／禁止提权／seccomp', out['uid'] == 65532 and int(out['caps'], 16) == 0 and out['nnp'] == '1' and out['seccomp'] == '2')
        passed('宿主及业务目录不可读／只读根／断网', all(out[k] for k in ('host_denied', 'business_denied', 'readonly', 'network_denied')))
        cpu = out['cpu'].split()
        passed('CPU／内存／PID／临时空间配额', cpu[0] != 'max' and int(cpu[0]) == int(cpu[1]) and out['memory'] == '268435456' and out['pids'] == '32' and out['tmp_bytes'] <= 33554432)
        out = (await run(PIDS)).get('output', {})
        passed('真实 PID 超限拒绝', out.get('limited') and 1 <= out.get('spawned', 100) < 32)
        out = (await run(TMP)).get('output', {})
        passed('真实临时空间超限拒绝', out.get('limited') and 1 <= out.get('count', 100) <= 32)
        result = await run("def main(data):\n    x = bytearray(536870912)\n    return {'size': len(x)}")
        passed('真实内存超限终止', result['status'] == 'failed')
        result = await run("def main(data):\n    print('x' * 300000)\n    return {}")
        passed('真实输出超限终止', result['status'] == 'failed')
        result = await run("def main(data):\n    while True: pass")
        passed('真实运行超时及清理', result['status'] == 'timeout')
        task_id = submit("def main(data):\n    import time\n    time.sleep(60)\n    return {}")
        for _ in range(100):
            code, out, _ = await command('inspect', '--format={{.State.Running}}', 'jzpy-' + task_id)
            if code == 0 and out.strip() == b'true':
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError('取消验收未观测到真实运行容器')
        result = await worker.cancel(owner, task_id)
        code, _, _ = await command('container', 'exists', 'jzpy-' + task_id)
        passed('真实取消后容器消失', result['status'] == 'cancelled' and code == 1)
        try:
            worker.lookup('0' * 64, task_id)
        except Exception as error:
            passed('任务归属隔离', getattr(error, 'status_code', None) == 404)
        else:
            passed('任务归属隔离', False)
        await asyncio.sleep(0)
        orphan = 'jzpy-' + uuid.uuid4().hex
        code, _, _ = await command(*create_args(worker.image, orphan))
        passed('创建受控遗留容器样本', code == 0)
        await worker.shutdown()
        worker = Worker(directory, worker.image, secrets.token_hex(32), accepted=True)
        await worker.startup()
        code, _, _ = await command('container', 'exists', orphan)
        passed('重启扫描回收遗留容器', worker.ready and code == 1)
        print(json.dumps({'passed': True, 'checks': results, 'profile': profile(worker.image),
                          'profile_hash': digest(profile(worker.image)), 'verified_at': time.time(),
                          'production_enabled': False}, ensure_ascii=False, indent=2))
    finally:
        await worker.shutdown()
        # 哨兵不含秘密，保留供管理员核验；验收不清除其他状态或自动修改生产配置。


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acknowledge-dedicated-host', action='store_true')
    try:
        asyncio.run(verify(parser.parse_args()))
    except Exception as error:
        print(json.dumps({'passed': False, 'error': str(error), 'production_enabled': False}, ensure_ascii=False))
        raise SystemExit(1)
