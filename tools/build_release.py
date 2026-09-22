"""Allowlist release builder: no private config, evidence, caches or old metadata."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import shutil
import subprocess
import sys
import tomllib
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def main():
    version=tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8-sig'))['project']['version']
    stage=ROOT/'.release-build'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')/'SHADE'
    stage.mkdir(parents=True)
    files=[ROOT/name for name in ('README.md','RELEASE_NOTES.md','LICENSE','pyproject.toml','.gitignore','config.example.toml','config.local.example.toml','Open SHADE.vbs')]
    for directory in ('src/shade_node','tests','docs','assets','.github','tools'):
        files.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'})
    # Read private identity solely to validate that it never enters the release.
    private=tomllib.loads((ROOT/'config.toml').read_text(encoding='utf-8-sig')) if (ROOT/'config.toml').exists() else {}
    secrets=[private.get('operator',{}).get('callsign','')]
    import re
    for config in ('config.toml','config.local.toml'):
        if (ROOT/config).exists(): secrets += re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]+',(ROOT/config).read_text())
    secrets=[s.encode().lower() for s in secrets if s and s not in {'SET-ME','TEST'} and not s.endswith('.invalid')]
    for source in files:
        data=source.read_bytes()
        if any(secret in data.lower() for secret in secrets): raise RuntimeError('Private identity found in public file: '+str(source.relative_to(ROOT)))
        if b'houn'+b'dog' in data.lower(): raise RuntimeError('Obsolete project metadata: '+str(source.relative_to(ROOT)))
        target=stage/source.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    output=ROOT/'release';output.mkdir(exist_ok=True)
    subprocess.run([sys.executable,'-m','pip','wheel',str(stage),'--no-deps','--wheel-dir',str(output)],check=True)
    wheel=output/f'shade_node-{version}-py3-none-any.whl'
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith('config.toml') or name.endswith('.db') or any(s in archive.read(name).lower() for s in secrets):
                raise RuntimeError('Private content in wheel')
    bundle=output/f'SHADE-{version}-beta.zip'
    with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED) as archive:
        for source in files:
            archive.write(source,'SHADE/'+source.relative_to(ROOT).as_posix())
        archive.write(wheel,'SHADE/release/'+wheel.name)
    hashes=[]
    for artifact in (wheel,bundle): hashes.append(hashlib.sha256(artifact.read_bytes()).hexdigest()+'  '+artifact.name)
    (output/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n')
    print('\n'.join(str(p) for p in (wheel,bundle)))

if __name__=='__main__': main()
