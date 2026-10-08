"""Patch an AVAILABLE checkout of the main service, preserving a backup.

python3 services/tanja/integration/install_menu.py --repo /path/to/checkout
No network, deployment or git operations. Refuses iCloud placeholders.
"""
import argparse
import ast
import os
from pathlib import Path
import re
import shutil


def install(root):
    root=Path(root)
    target=root/'agent.py'
    if getattr(target.stat(),'st_flags',0) & 0x40000000:
        raise ValueError('agent.py is an iCloud placeholder. Download the current repository first.')
    original=target.read_text()
    if 'tanja_menu.install(dashboard)' in original:
        return 'Already installed'
    matches=list(re.finditer(r'^dashboard\.register\(app\)[^\n]*$', original, re.M))
    if len(matches)!=1:
        raise ValueError('Expected one top-level dashboard.register(app); inspect source before editing')
    m=matches[0]
    updated=original[:m.end()]+'\nimport tanja_menu\ntanja_menu.install(dashboard)'+original[m.end():]
    ast.parse(updated)
    backup=root/'agent.py.before-tanja'
    if backup.exists():
        raise ValueError('Backup already exists; inspect it before reinstalling')
    shutil.copy2(target,backup)
    helper=root/'tanja_menu.py'
    source=Path(__file__).with_name('tanja_menu.py')
    if helper.exists() and helper.read_bytes()!=source.read_bytes():
        raise ValueError('A different tanja_menu.py exists; refusing overwrite')
    shutil.copy2(source,helper)
    temporary=target.with_suffix('.py.tanja-new')
    temporary.write_text(updated)
    os.replace(temporary,target)
    return 'Installed. Review agent.py and tanja_menu.py, then deploy the main service.'


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--repo',required=True)
    print(install(p.parse_args().repo))
