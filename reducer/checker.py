import subprocess


class SolidityPropertyChecker():

    def __init__(self, file_path: str, test_script: str):
        self.file_path = file_path
        self.test_script = test_script

    def run_test_script(self, file_path: str, cwd: str = None) -> int:
        print(self.test_script)
        print(file_path)
        command = ["bash", self.test_script, file_path or self.file_path]
        try:
            result = subprocess.run(command, capture_output=True,
                                    text=False, cwd=cwd)

            return result.returncode
        except subprocess.CalledProcessError:
            return -1


class CPropertyChecker():

    def __init__(self, file_path: str, test_script: str):
        self.file_path = file_path
        self.test_script = test_script

    def run_test_script(self, file_path: str, cwd: str = None) -> int:
        print(self.test_script)
        print(file_path)
        command = ["bash", self.test_script, file_path or self.file_path]
        try:
            while True:
                result = subprocess.run(command, capture_output=True,
                                        text=False, cwd=cwd)
                if ("exit 3" not in result.stdout.decode("utf-8") or
                    "exit 4 " not in result.stdout.decode("utf-8")
                ):
                    break
            return result.returncode
        except subprocess.CalledProcessError:
            return -1


class JavaPropertyChecker():

    def __init__(self, file_path: str, test_script: str):
        self.file_path = file_path
        self.test_script = test_script

    def run_test_script(self, file_path: str, cwd: str = None) -> int:

        command = ["bash", self.test_script, file_path or self.file_path]
        try:
            result = subprocess.run(command, capture_output=True,
                                    text=False, cwd=cwd)
            return result.returncode
        except subprocess.CalledProcessError:
            return -1


PROPERTY_CHECKERS = {
    "solidity": SolidityPropertyChecker,
    "c": CPropertyChecker,
    "java": JavaPropertyChecker,
}
