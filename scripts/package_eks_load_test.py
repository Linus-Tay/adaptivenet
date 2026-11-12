"""Build a dependency-free operator-started load test bundle for CloudShell."""
import base64
import io
from pathlib import Path
import zipfile
from scripts.package_eks_idle_check import ROOT, main as package_idle


def main():
    package_idle()
    original=ROOT/"data/eks-controller-idle-check/controller-idle-check.pyz"
    buffer=io.BytesIO()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(buffer,"w",compression=zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            if name!="__main__.py":
                target.writestr(name,source.read(name))
        code=(ROOT/"scripts/eks_load_test.py").read_text()
        code=code.replace('str(Path(__file__).resolve().parent.parent / "services")',
                          'str(Path(__file__).resolve().parent / "services")')
        target.writestr("__main__.py",code)
    out=ROOT/"data/eks-controller-load-test"
    out.mkdir(parents=True,exist_ok=True)
    (out/"controller-load-test.pyz").write_bytes(buffer.getvalue())
    lines=["(", '  runner=$(mktemp /tmp/adaptivenet-load.XXXXXX.pyz) || exit 1',
           '  trap \'rm -f "$runner"\' EXIT',
           '  base64 -d > "$runner" <<\'CONTROLLER_BUNDLE\'',
           base64.b64encode(buffer.getvalue()).decode(), "CONTROLLER_BUNDLE",
           '  evidence="$HOME/adaptivenet-load-test-$(date +%s)"',
           '  echo "Evidence directory: $evidence"',
           '  python3 -S "$runner" --kubeconfig ~/adaptivenet-sg.kubeconfig --evidence-dir "$evidence"',")",""]
    (out/"run-sg.txt").write_text("\n".join(lines),encoding="utf-8")
    print(out)

if __name__=="__main__":
    main()
