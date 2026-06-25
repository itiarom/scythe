import os
import random
import shutil
import string
import subprocess
import tempfile


class PropertyChecker:
    """Runs the property oracle for one language. Subclasses declare how the
    candidate is staged for the test script:

    EXT             file extension the script expects.
    USE_TEMPDIR     stage the candidate in a fresh temp dir (vs scythe's cwd).
    PASS_BASENAME   pass the script the basename (vs the absolute path).
    INITIAL_TEMPDIR run the one-time initial check in a fresh temp dir.
    """

    EXT = ""
    USE_TEMPDIR = False
    PASS_BASENAME = True
    INITIAL_TEMPDIR = False

    def __init__(self, file_path: str, test_script: str):
        self.file_path = file_path
        self.test_script = os.path.abspath(test_script)

    def run_test_script(self, file_path: str = None, cwd: str = None) -> int:
        command = ["bash", self.test_script, file_path or self.file_path]
        try:
            return subprocess.run(command, capture_output=True,
                                  text=False, cwd=cwd).returncode
        except subprocess.CalledProcessError:
            return -1

    def run_oracle(self, content: str) -> int:
        """Stage ``content`` as a candidate and return the test script's exit
        code (0 = the property holds)."""
        name = ''.join(random.sample(string.ascii_letters + string.digits, 5))
        fname = f"{name}.{self.EXT}"
        workdir = tempfile.mkdtemp(prefix="scythe_") if self.USE_TEMPDIR else None
        full = os.path.join(workdir, fname) if workdir is not None else fname
        try:
            with open(full, "w") as f:
                f.write(content)
            return self.run_test_script(
                fname if self.PASS_BASENAME else full, cwd=workdir)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            else:
                os.remove(full)

    def check_initial(self) -> int:
        """Verify the property holds on the original file before reducing."""
        workdir = tempfile.mkdtemp(prefix="scythe_") \
            if self.INITIAL_TEMPDIR else None
        try:
            return self.run_test_script(None, cwd=workdir)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)


class SolidityPropertyChecker(PropertyChecker):
    EXT = "sol"


class CPropertyChecker(PropertyChecker):
    # C resolves $(pwd)/$1 and bind-mounts $(pwd) into a per-call dir.
    EXT = "c"
    USE_TEMPDIR = False

    def run_test_script(self, file_path: str = None, cwd: str = None) -> int:
        command = ["bash", self.test_script, file_path or self.file_path]
        try:
            while True:
                result = subprocess.run(command, capture_output=True,
                                        text=False, cwd=cwd)
                out = result.stdout.decode("utf-8")
                if "exit 3" not in out or "exit 4 " not in out:
                    break
            return result.returncode
        except subprocess.CalledProcessError:
            return -1


class JavaPropertyChecker(PropertyChecker):
    # javac writes .class files next to the source, so isolate every run.
    EXT = "java"
    USE_TEMPDIR = True
    PASS_BASENAME = False
    INITIAL_TEMPDIR = True


PROPERTY_CHECKERS = {
    "solidity": SolidityPropertyChecker,
    "c": CPropertyChecker,
    "java": JavaPropertyChecker,
}
